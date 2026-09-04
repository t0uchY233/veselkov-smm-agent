"""Deterministic YouTube Data API adapter built around an injected transport.

The adapter deliberately contains no OAuth client and never writes a token to a
request log. A Windows composition root supplies an authenticated transport.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from smm_agent.adapters.publishing.http import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    json_body,
    provider_error_from_exception,
    provider_error_from_response,
    with_query,
)
from smm_agent.contracts.publication import (
    PreparedPublication,
    PublicationRequest,
    PublicationSnapshot,
    PublicationState,
    RecoveryErrorCode,
)
from smm_agent.domain.publication.ports import ProviderOperationError

YOUTUBE_API_BASE: Final = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD_BASE: Final = "https://www.googleapis.com/upload/youtube/v3"
_PAYLOAD_TAG_PREFIX: Final = "smm-agent-payload-"
_REQUEST_TAG_PREFIX: Final = "smm-agent-request-"
_TARGET_TAG_PREFIX: Final = "smm-agent-target-"


def youtube_request(
    *,
    release_id: str,
    schedule_key: str,
    payload_sha256: str,
    payload: dict[str, object],
) -> PublicationRequest:
    """Build the stable publication identity consumed by ``YouTubePublisher``."""

    return PublicationRequest(
        release_id=release_id,
        platform="youtube",
        idempotency_key=f"publication:{release_id}:youtube:{schedule_key}",
        payload_sha256=payload_sha256,
        payload=payload,
    )


class YouTubePublisher:
    """YouTube private-upload and native ``publishAt`` state machine.

    Internal tags carry the immutable payload/request identities. YouTube
    returns tags to an authenticated caller, so a fresh process can validate a
    remote receipt rather than trusting an arbitrary video ID. They never enter
    the video title or description.
    """

    platform = "youtube"

    def __init__(
        self,
        *,
        transport: HttpTransport,
        channel_id: str,
        api_base: str = YOUTUBE_API_BASE,
        upload_base: str = YOUTUBE_UPLOAD_BASE,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> None:
        if not channel_id:
            raise ValueError("Для YouTube adapter нужен channel_id.")
        if chunk_size <= 0:
            raise ValueError("Размер YouTube upload chunk должен быть положительным.")
        self._transport = transport
        self._channel_id = channel_id
        self._api_base = api_base.rstrip("/")
        self._upload_base = upload_base.rstrip("/")
        self._chunk_size = chunk_size
        self._cancelled: set[str] = set()

    def prepare(self, request: PublicationRequest) -> PreparedPublication:
        self._validate_request(request)
        video_path, video_size, video_media_type, metadata, cover_path = self._prepare_input(
            request
        )
        title = self._string(metadata, "title", provider="YouTube")
        description = self._string(metadata, "description", provider="YouTube")
        tags = self._strings(metadata.get("tags", []), provider="YouTube")
        metadata_body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": self._metadata_tags(tags, request),
                "categoryId": str(metadata.get("category_id", "22")),
            },
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
        }
        body = json_body(metadata_body)
        initialize = self._send(
            HttpRequest(
                method="POST",
                url=with_query(
                    f"{self._upload_base}/videos",
                    {"part": "snippet,status", "uploadType": "resumable"},
                ),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(body)),
                    "X-Goog-Request-Id": request.idempotency_key,
                    "X-Upload-Content-Length": str(video_size),
                    "X-Upload-Content-Type": video_media_type,
                },
                body=body,
                idempotency_key=request.idempotency_key,
            ),
            provider="YouTube",
            accepted_statuses={200},
        )
        session_url = self._header(initialize.headers, "location")
        if not session_url or not session_url.startswith("https://"):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="YouTube не вернул безопасный resumable upload URL.",
            )
        resource = self._upload_resumable(
            session_url=session_url,
            video_path=video_path,
            video_size=video_size,
            video_media_type=video_media_type,
            idempotency_key=request.idempotency_key,
        )
        snapshot = self._snapshot_from_resource(resource)
        if snapshot.state != "prepared" or snapshot.payload_sha256 != request.payload_sha256:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube подтвердил другой private video receipt.",
            )
        if cover_path is not None:
            self._upload_thumbnail(
                remote_id=snapshot.remote_id,
                cover_path=cover_path,
                idempotency_key=request.idempotency_key,
            )
        return PreparedPublication(
            platform="youtube",
            state="prepared",
            remote_id=snapshot.remote_id,
            known_url=snapshot.known_url,
            payload_sha256=snapshot.payload_sha256,
        )

    def preflight(self, remote_id: str, *, payload_sha256: str) -> None:
        snapshot = self.status(remote_id)
        if snapshot.state not in {"prepared", "armed"} or snapshot.payload_sha256 != payload_sha256:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube preflight не подтвердил исходный private receipt.",
            )

    def arm(
        self, remote_id: str, *, target_at_utc: datetime, operation_key: str
    ) -> PublicationSnapshot:
        target = self._utc(target_at_utc)
        resource = self._fetch_video(remote_id, idempotency_key=operation_key)
        existing = self._snapshot_from_resource(resource)
        if existing.state == "public":
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Нельзя заново назначить уже публичное видео YouTube.",
            )
        if existing.state == "armed" and existing.target_at_utc == target:
            return existing
        snippet = self._object(resource, "snippet", provider="YouTube")
        status = self._object(resource, "status", provider="YouTube")
        tags = self._strings(snippet.get("tags", []), provider="YouTube")
        tags = [tag for tag in tags if not tag.startswith(_TARGET_TAG_PREFIX)]
        tags.append(self._target_tag(target))
        update = {
            "id": remote_id,
            "snippet": {
                "title": self._string(snippet, "title", provider="YouTube"),
                "description": self._string(snippet, "description", provider="YouTube"),
                "tags": tags,
                "categoryId": str(snippet.get("categoryId", "22")),
            },
            "status": {
                "privacyStatus": "private",
                "publishAt": self._iso(target),
                "selfDeclaredMadeForKids": bool(status.get("selfDeclaredMadeForKids", False)),
            },
        }
        response = self._send(
            HttpRequest(
                method="PUT",
                url=with_query(f"{self._api_base}/videos", {"part": "snippet,status"}),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "X-Goog-Request-Id": operation_key,
                },
                body=json_body(update),
                idempotency_key=operation_key,
            ),
            provider="YouTube",
            accepted_statuses={200},
        )
        armed = self._snapshot_from_resource(self._json(response, provider="YouTube"))
        if armed.remote_id != remote_id or armed.state != "armed" or armed.target_at_utc != target:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube не подтвердил запрошенный publishAt.",
            )
        return armed

    def status(self, remote_id: str, *, now: datetime | None = None) -> PublicationSnapshot:
        del now
        snapshot = self._snapshot_from_resource(
            self._fetch_video(remote_id, idempotency_key=f"status:{remote_id}")
        )
        if remote_id in self._cancelled:
            return snapshot.model_copy(update={"state": "cancelled"})
        return snapshot

    def cancel(self, remote_id: str, *, operation_key: str) -> PublicationSnapshot:
        snapshot = self.status(remote_id)
        if snapshot.state == "public":
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Публичное видео YouTube нельзя автоматически снять с публикации.",
            )
        response = self._send(
            HttpRequest(
                method="PUT",
                url=with_query(f"{self._api_base}/videos", {"part": "status"}),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "X-Goog-Request-Id": operation_key,
                },
                body=json_body(
                    {
                        "id": remote_id,
                        "status": {
                            "privacyStatus": "private",
                            "publishAt": None,
                            "selfDeclaredMadeForKids": False,
                        },
                    }
                ),
                idempotency_key=operation_key,
            ),
            provider="YouTube",
            accepted_statuses={200},
        )
        confirmed = self._snapshot_from_resource(self._json(response, provider="YouTube"))
        if confirmed.remote_id != remote_id or confirmed.state == "public":
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube не подтвердил отмену native schedule.",
            )
        self._cancelled.add(remote_id)
        return confirmed.model_copy(
            update={"state": "cancelled", "target_at_utc": snapshot.target_at_utc}
        )

    def execute(self, remote_id: str, *, operation_key: str, now: datetime) -> PublicationSnapshot:
        """YouTube is native-scheduled, so execution is deliberately status-only."""

        del operation_key, now
        return self.status(remote_id)

    def _prepare_input(
        self, request: PublicationRequest
    ) -> tuple[Path, int, str, Mapping[str, object], Path | None]:
        master = self._object(request.payload, "master", provider="YouTube")
        path = Path(self._string(master, "path", provider="YouTube"))
        self._verify_file(
            path,
            expected_sha256=self._string(master, "sha256", provider="YouTube"),
            provider="YouTube master",
        )
        metadata = self._object(request.payload, "metadata", provider="YouTube")
        title = self._string(metadata, "title", provider="YouTube")
        description = self._string(metadata, "description", provider="YouTube")
        if len(title) > 100 or len(description) > 5000:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="YouTube metadata превышают согласованные ограничения.",
            )
        cover_value = request.payload.get("cover")
        cover_path: Path | None = None
        if cover_value is not None:
            cover = self._mapping(cover_value, provider="YouTube")
            cover_path = Path(self._string(cover, "path", provider="YouTube"))
            self._verify_file(
                cover_path,
                expected_sha256=self._string(cover, "sha256", provider="YouTube"),
                provider="YouTube cover",
            )
        media_type = str(master.get("media_type", "video/mp4"))
        if not media_type.startswith("video/"):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="YouTube master должен быть видеофайлом.",
            )
        return path, path.stat().st_size, media_type, metadata, cover_path

    def _upload_resumable(
        self,
        *,
        session_url: str,
        video_path: Path,
        video_size: int,
        video_media_type: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        if video_size <= 0:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="YouTube master не должен быть пустым.",
            )
        offset = 0
        with video_path.open("rb") as stream:
            while offset < video_size:
                stream.seek(offset)
                chunk = stream.read(min(self._chunk_size, video_size - offset))
                if not chunk:
                    raise ProviderOperationError(
                        code="INVALID_PAYLOAD",
                        sanitized_detail="Не удалось прочитать YouTube master для upload.",
                    )
                end = offset + len(chunk) - 1
                response = self._send(
                    HttpRequest(
                        method="PUT",
                        url=session_url,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Type": video_media_type,
                            "Content-Range": f"bytes {offset}-{end}/{video_size}",
                            "X-Goog-Request-Id": idempotency_key,
                        },
                        body=chunk,
                        idempotency_key=idempotency_key,
                    ),
                    provider="YouTube",
                    accepted_statuses={200, 201, 308},
                )
                if response.status_code in {200, 201}:
                    return self._json(response, provider="YouTube")
                next_offset = self._resumable_offset(response, current_end=end)
                if next_offset <= offset:
                    raise ProviderOperationError(
                        code="UNKNOWN_PROVIDER_OUTCOME",
                        sanitized_detail=(
                            "YouTube resumable upload не подтвердил продвижение chunk."
                        ),
                    )
                offset = next_offset
        raise ProviderOperationError(
            code="UNKNOWN_PROVIDER_OUTCOME",
            sanitized_detail="YouTube resumable upload завершился без video receipt.",
        )

    def _upload_thumbnail(self, *, remote_id: str, cover_path: Path, idempotency_key: str) -> None:
        response = self._send(
            HttpRequest(
                method="POST",
                url=with_query(f"{self._upload_base}/thumbnails/set", {"videoId": remote_id}),
                headers={
                    "Content-Type": "image/png",
                    "X-Goog-Request-Id": f"{idempotency_key}:thumbnail",
                },
                body=cover_path.read_bytes(),
                idempotency_key=f"{idempotency_key}:thumbnail",
            ),
            provider="YouTube",
            accepted_statuses={200},
        )
        thumbnail = self._json(response, provider="YouTube")
        if self._string(thumbnail, "videoId", provider="YouTube") != remote_id:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube thumbnail относится к другому видео.",
            )

    def _fetch_video(self, remote_id: str, *, idempotency_key: str) -> dict[str, object]:
        response = self._send(
            HttpRequest(
                method="GET",
                url=with_query(
                    f"{self._api_base}/videos", {"id": remote_id, "part": "snippet,status"}
                ),
                headers={"X-Goog-Request-Id": idempotency_key},
                idempotency_key=idempotency_key,
            ),
            provider="YouTube",
            accepted_statuses={200},
            missing_code="RECEIPT_MISMATCH",
        )
        payload = self._json(response, provider="YouTube")
        items = payload.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube не нашёл ровно один video receipt.",
            )
        resource = items[0]
        if self._string(resource, "id", provider="YouTube") != remote_id:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube вернул другой video receipt.",
            )
        return resource

    def _snapshot_from_resource(self, resource: Mapping[str, object]) -> PublicationSnapshot:
        remote_id = self._string(resource, "id", provider="YouTube")
        snippet = self._object(resource, "snippet", provider="YouTube")
        status = self._object(resource, "status", provider="YouTube")
        if self._string(snippet, "channelId", provider="YouTube") != self._channel_id:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube video относится к другому каналу.",
            )
        payload_sha256, _request_identity, tag_target = self._identity_tags(
            self._strings(snippet.get("tags", []), provider="YouTube")
        )
        privacy = self._string(status, "privacyStatus", provider="YouTube")
        publish_at = self._datetime_optional(status.get("publishAt"), provider="YouTube")
        target = publish_at or tag_target
        if publish_at is not None and tag_target is not None and publish_at != tag_target:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube publishAt расходится с immutable target tag.",
            )
        if privacy == "public":
            public_at = self._datetime_optional(snippet.get("publishedAt"), provider="YouTube")
            if public_at is None or target is None:
                raise ProviderOperationError(
                    code="RECEIPT_MISMATCH",
                    sanitized_detail=(
                        "YouTube public video не вернул исходный target или publishedAt."
                    ),
                )
            state = "public"
        elif privacy == "private":
            public_at = None
            state = "armed" if publish_at is not None else "prepared"
        else:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube вернул неподдерживаемый privacy state.",
            )
        return PublicationSnapshot(
            platform="youtube",
            state=cast(PublicationState, state),
            remote_id=remote_id,
            known_url=f"https://youtu.be/{remote_id}",
            payload_sha256=payload_sha256,
            target_at_utc=target,
            public_at=public_at,
        )

    def _send(
        self,
        request: HttpRequest,
        *,
        provider: str,
        accepted_statuses: set[int],
        missing_code: RecoveryErrorCode = "RECEIPT_MISMATCH",
    ) -> HttpResponse:
        try:
            response = self._transport.send(request)
        except ProviderOperationError:
            raise
        except Exception as exc:
            raise provider_error_from_exception(exc, provider=provider) from None
        if response.status_code not in accepted_statuses:
            raise provider_error_from_response(
                response, provider=provider, missing_code=missing_code
            )
        return response

    @staticmethod
    def _resumable_offset(response: HttpResponse, *, current_end: int) -> int:
        range_header = YouTubePublisher._header(response.headers, "range")
        if range_header is None:
            return current_end + 1
        try:
            _unit, accepted = range_header.split("=", maxsplit=1)
            _start, accepted_end = accepted.rsplit("-", maxsplit=1)
            return int(accepted_end) + 1
        except (ValueError, TypeError):
            raise ProviderOperationError(
                code="UNKNOWN_PROVIDER_OUTCOME",
                sanitized_detail="YouTube вернул некорректный Range resumable upload.",
            ) from None

    @staticmethod
    def _validate_request(request: PublicationRequest) -> None:
        if request.platform != "youtube":
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="YouTube adapter получил request другой площадки.",
            )

    @staticmethod
    def _verify_file(path: Path, *, expected_sha256: str, provider: str) -> None:
        if not path.is_file():
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail=f"{provider} artifact отсутствует.",
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail=f"{provider} artifact изменился после утверждения.",
            )

    @staticmethod
    def _metadata_tags(tags: list[str], request: PublicationRequest) -> list[str]:
        identities = [
            f"{_PAYLOAD_TAG_PREFIX}{request.payload_sha256}",
            f"{_REQUEST_TAG_PREFIX}{hashlib.sha256(request.idempotency_key.encode()).hexdigest()}",
        ]
        return list(dict.fromkeys([*tags, *identities]))

    @staticmethod
    def _identity_tags(tags: list[str]) -> tuple[str, str, datetime | None]:
        payloads = [
            tag.removeprefix(_PAYLOAD_TAG_PREFIX)
            for tag in tags
            if tag.startswith(_PAYLOAD_TAG_PREFIX)
        ]
        requests = [
            tag.removeprefix(_REQUEST_TAG_PREFIX)
            for tag in tags
            if tag.startswith(_REQUEST_TAG_PREFIX)
        ]
        targets = [
            tag.removeprefix(_TARGET_TAG_PREFIX)
            for tag in tags
            if tag.startswith(_TARGET_TAG_PREFIX)
        ]
        def valid_hex(value: str) -> bool:
            return len(value) == 64 and all(char in "0123456789abcdef" for char in value)

        if len(payloads) != 1 or not valid_hex(payloads[0]):
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube video не содержит единственный payload identity tag.",
            )
        if len(requests) != 1 or not valid_hex(requests[0]):
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube video не содержит единственный request identity tag.",
            )
        if len(targets) > 1:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube video содержит несколько target identity tags.",
            )
        tag_target = (
            YouTubePublisher._datetime_optional(targets[0], provider="YouTube") if targets else None
        )
        return payloads[0], requests[0], tag_target

    @staticmethod
    def _target_tag(target: datetime) -> str:
        return f"{_TARGET_TAG_PREFIX}{YouTubePublisher._iso(target)}"

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="YouTube publishAt должен содержать timezone.",
            )
        return value.astimezone(UTC)

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        lowered = name.lower()
        return next((value for key, value in headers.items() if key.lower() == lowered), None)

    @staticmethod
    def _json(response: HttpResponse, *, provider: str) -> dict[str, object]:
        try:
            return response.json_object()
        except ValueError:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail=f"{provider} вернул некорректный JSON receipt.",
            ) from None

    @staticmethod
    def _mapping(value: object, *, provider: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail=f"{provider} payload имеет неверную структуру.",
            )
        return value

    @classmethod
    def _object(
        cls, value: Mapping[str, object], key: str, *, provider: str
    ) -> Mapping[str, object]:
        return cls._mapping(value.get(key), provider=provider)

    @staticmethod
    def _string(value: Mapping[str, object], key: str, *, provider: str) -> str:
        candidate = value.get(key)
        if not isinstance(candidate, str) or not candidate:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail=f"{provider} payload не содержит {key}.",
            )
        return candidate

    @staticmethod
    def _strings(value: object, *, provider: str) -> list[str]:
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail=f"{provider} tags имеют неверный формат.",
            )
        return list(value)

    @staticmethod
    def _datetime_optional(value: object, *, provider: str) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail=f"{provider} вернул некорректный timestamp.",
            )
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail=f"{provider} вернул некорректный timestamp.",
            ) from None
        if result.tzinfo is None:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail=f"{provider} вернул timestamp без timezone.",
            )
        return result.astimezone(UTC)
