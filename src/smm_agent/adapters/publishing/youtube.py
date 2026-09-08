"""Deterministic YouTube Data API adapter built around an injected transport.

The adapter deliberately contains no OAuth client and never writes a token to a
request log. A Windows composition root supplies an authenticated transport.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol, cast

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

YouTubeUploadState = Literal["initializing", "uploading", "uploaded", "cancelled"]


@dataclass(frozen=True, slots=True)
class YouTubeUploadReceipt:
    """Non-secret durable resumable-upload state for one approved payload.

    The resumable session URL is required to continue the very same YouTube
    upload after a process restart.  It is deliberately validated to exclude
    bearer-like query values and contains neither an OAuth token nor a raw
    provider response.  A remote ID, once known, makes all future calls status
    first rather than creating another video.
    """

    idempotency_key: str
    payload_sha256: str
    video_sha256: str
    video_size: int
    video_media_type: str
    state: YouTubeUploadState
    initialization_attempted: bool = False
    session_url: str | None = None
    remote_id: str | None = None
    thumbnail_uploaded: bool = False
    target_at_utc: str | None = None


class YouTubeUploadReceiptStore(Protocol):
    """Durable upload state.  Implementations persist no OAuth credential."""

    def load(self, idempotency_key: str) -> YouTubeUploadReceipt | None: ...

    def load_by_remote_id(self, remote_id: str) -> YouTubeUploadReceipt | None: ...

    def save(self, receipt: YouTubeUploadReceipt) -> None: ...


class InMemoryYouTubeUploadReceiptStore:
    """Test store which mirrors the durable JSON store protocol."""

    def __init__(self) -> None:
        self._receipts: dict[str, YouTubeUploadReceipt] = {}

    def load(self, idempotency_key: str) -> YouTubeUploadReceipt | None:
        return self._receipts.get(idempotency_key)

    def load_by_remote_id(self, remote_id: str) -> YouTubeUploadReceipt | None:
        return next(
            (receipt for receipt in self._receipts.values() if receipt.remote_id == remote_id), None
        )

    def save(self, receipt: YouTubeUploadReceipt) -> None:
        _validate_receipt(receipt)
        self._receipts[receipt.idempotency_key] = receipt


class JsonYouTubeUploadReceiptStore:
    """Atomic, SQLite-ready durable store for resumable session receipts.

    The Windows composition root can replace this implementation with SQLite
    without changing the publisher.  It never serializes token material,
    Authorization headers, request bodies, or provider response bodies.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, idempotency_key: str) -> YouTubeUploadReceipt | None:
        raw = self._all().get(idempotency_key)
        return _receipt_from_json(raw) if raw is not None else None

    def load_by_remote_id(self, remote_id: str) -> YouTubeUploadReceipt | None:
        for raw in self._all().values():
            receipt = _receipt_from_json(raw)
            if receipt.remote_id == remote_id:
                return receipt
        return None

    def save(self, receipt: YouTubeUploadReceipt) -> None:
        _validate_receipt(receipt)
        values = self._all()
        values[receipt.idempotency_key] = {
            "idempotency_key": receipt.idempotency_key,
            "payload_sha256": receipt.payload_sha256,
            "video_sha256": receipt.video_sha256,
            "video_size": receipt.video_size,
            "video_media_type": receipt.video_media_type,
            "state": receipt.state,
            "initialization_attempted": receipt.initialization_attempted,
            "session_url": receipt.session_url,
            "remote_id": receipt.remote_id,
            "thumbnail_uploaded": receipt.thumbnail_uploaded,
            "target_at_utc": receipt.target_at_utc,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self._path)

    def _all(self) -> dict[str, dict[str, object]]:
        if not self._path.is_file():
            return {}
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Локальный YouTube upload receipt store повреждён.",
            ) from None
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(item, dict) for key, item in value.items()
        ):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Локальный YouTube upload receipt store имеет неверный формат.",
            )
        return {key: dict(item) for key, item in value.items()}


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
        receipt_store: YouTubeUploadReceiptStore | None = None,
        processing_max_polls: int = 20,
        processing_poll: Callable[[int], None] | None = None,
    ) -> None:
        if not channel_id:
            raise ValueError("Для YouTube adapter нужен channel_id.")
        if chunk_size <= 0:
            raise ValueError("Размер YouTube upload chunk должен быть положительным.")
        if processing_max_polls < 1:
            raise ValueError("YouTube processing_max_polls должен быть не меньше 1.")
        self._transport = transport
        self._channel_id = channel_id
        self._api_base = api_base.rstrip("/")
        self._upload_base = upload_base.rstrip("/")
        self._chunk_size = chunk_size
        self._receipt_store = receipt_store or InMemoryYouTubeUploadReceiptStore()
        self._processing_max_polls = processing_max_polls
        self._processing_poll = processing_poll or (lambda _attempt: None)

    def prepare(self, request: PublicationRequest) -> PreparedPublication:
        self._validate_request(request)
        video_path, video_size, video_media_type, metadata, cover_path = self._prepare_input(
            request
        )
        video_sha256 = self._string(
            self._object(request.payload, "master", provider="YouTube"),
            "sha256",
            provider="YouTube",
        )
        receipt = self._load_or_start_receipt(
            request=request,
            video_sha256=video_sha256,
            video_size=video_size,
            video_media_type=video_media_type,
        )
        if receipt.state == "cancelled":
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Отменённый YouTube receipt нельзя повторно использовать.",
            )
        if receipt.remote_id is not None:
            resource = self._wait_for_processing(
                receipt.remote_id, idempotency_key=f"{request.idempotency_key}:processing"
            )
            snapshot = self._snapshot_from_resource(resource)
            return self._prepared_after_processing(
                snapshot=snapshot,
                receipt=receipt,
                cover_path=cover_path,
                idempotency_key=request.idempotency_key,
            )
        if receipt.session_url is None:
            # An initial POST may have reached YouTube even if its response was
            # lost.  Search for its immutable identity instead of risking a
            # second private video.  A missing result stays retryable.
            if not receipt.initialization_attempted:
                receipt = self._initialize_upload_session(
                    request=request,
                    metadata=metadata,
                    video_size=video_size,
                    video_media_type=video_media_type,
                    receipt=receipt,
                )
            else:
                recovered = self._find_video_by_identity(request)
                if recovered is None:
                    raise ProviderOperationError(
                        code="UNKNOWN_PROVIDER_OUTCOME",
                        sanitized_detail=(
                            "YouTube upload session не сохранён; нужен status-first recovery."
                        ),
                    )
                snapshot = self._snapshot_from_resource(recovered)
                receipt = replace(receipt, state="uploaded", remote_id=snapshot.remote_id)
                self._receipt_store.save(receipt)
                resource = self._wait_for_processing(
                    snapshot.remote_id, idempotency_key=f"{request.idempotency_key}:processing"
                )
                return self._prepared_after_processing(
                    snapshot=self._snapshot_from_resource(resource),
                    receipt=receipt,
                    cover_path=cover_path,
                    idempotency_key=request.idempotency_key,
                )
        resource = self._upload_resumable(
            receipt=receipt,
            video_path=video_path,
            video_size=video_size,
            video_media_type=video_media_type,
            idempotency_key=request.idempotency_key,
        )
        snapshot = self._snapshot_from_resource(resource)
        receipt = replace(receipt, state="uploaded", remote_id=snapshot.remote_id)
        self._receipt_store.save(receipt)
        resource = self._wait_for_processing(
            snapshot.remote_id, idempotency_key=f"{request.idempotency_key}:processing"
        )
        return self._prepared_after_processing(
            snapshot=self._snapshot_from_resource(resource),
            receipt=receipt,
            cover_path=cover_path,
            idempotency_key=request.idempotency_key,
        )

    def _initialize_upload_session(
        self,
        *,
        request: PublicationRequest,
        metadata: Mapping[str, object],
        video_size: int,
        video_media_type: str,
        receipt: YouTubeUploadReceipt,
    ) -> YouTubeUploadReceipt:
        receipt = replace(receipt, initialization_attempted=True)
        # Persist before the irreversible initial POST. If its response is
        # lost, a restart reconciles immutable tags and never posts again.
        self._receipt_store.save(receipt)
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
        updated = replace(receipt, state="uploading", session_url=session_url)
        self._receipt_store.save(updated)
        return updated

    def _prepared_after_processing(
        self,
        *,
        snapshot: PublicationSnapshot,
        receipt: YouTubeUploadReceipt,
        cover_path: Path | None,
        idempotency_key: str,
    ) -> PreparedPublication:
        if (
            snapshot.state not in {"prepared", "armed"}
            or snapshot.payload_sha256 != receipt.payload_sha256
        ):
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube подтвердил другой private video receipt.",
            )
        if cover_path is not None and not receipt.thumbnail_uploaded:
            self._upload_thumbnail(
                remote_id=snapshot.remote_id,
                cover_path=cover_path,
                idempotency_key=idempotency_key,
            )
            receipt = replace(receipt, thumbnail_uploaded=True)
            self._receipt_store.save(receipt)
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
        self._wait_for_processing(remote_id, idempotency_key=f"{operation_key}:processing")
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
        # A videos.update response is not the schedule receipt.  Read the
        # native publishAt back through videos.list before recording success.
        self._json(response, provider="YouTube")
        armed = self._snapshot_from_resource(
            self._fetch_video(remote_id, idempotency_key=f"{operation_key}:readback")
        )
        if armed.remote_id != remote_id or armed.state != "armed" or armed.target_at_utc != target:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube не подтвердил запрошенный publishAt.",
            )
        stored = self._receipt_store.load_by_remote_id(remote_id)
        if stored is not None:
            target_text = self._iso(target)
            if stored.target_at_utc != target_text:
                self._receipt_store.save(replace(stored, target_at_utc=target_text))
        return armed

    def status(self, remote_id: str, *, now: datetime | None = None) -> PublicationSnapshot:
        del now
        try:
            snapshot = self._snapshot_from_resource(
                self._fetch_video(remote_id, idempotency_key=f"status:{remote_id}")
            )
        except ProviderOperationError as exc:
            receipt = self._receipt_store.load_by_remote_id(remote_id)
            if (
                exc.code == "RECEIPT_MISMATCH"
                and receipt is not None
                and receipt.state == "cancelled"
            ):
                return self._cancelled_snapshot(receipt)
            raise
        receipt = self._receipt_store.load_by_remote_id(remote_id)
        if receipt is not None and receipt.state == "cancelled":
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Удалённый YouTube receipt неожиданно появился снова.",
            )
        return snapshot

    def cancel(self, remote_id: str, *, operation_key: str) -> PublicationSnapshot:
        try:
            snapshot = self.status(remote_id)
        except ProviderOperationError as exc:
            receipt = self._receipt_store.load_by_remote_id(remote_id)
            if (
                exc.code == "RECEIPT_MISMATCH"
                and receipt is not None
                and receipt.state == "cancelled"
            ):
                return self._cancelled_snapshot(receipt)
            raise
        if snapshot.state == "cancelled":
            return snapshot
        if snapshot.state == "public":
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Публичное видео YouTube нельзя автоматически снять с публикации.",
            )
        self._send(
            HttpRequest(
                method="DELETE",
                url=with_query(f"{self._api_base}/videos", {"id": remote_id}),
                headers={"X-Goog-Request-Id": operation_key},
                idempotency_key=operation_key,
            ),
            provider="YouTube",
            accepted_statuses={204, 404},
        )
        receipt = self._receipt_store.load_by_remote_id(remote_id)
        if receipt is None:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube cancellation не нашёл durable upload receipt.",
            )
        receipt = replace(
            receipt,
            state="cancelled",
            target_at_utc=self._iso(snapshot.target_at_utc) if snapshot.target_at_utc else None,
        )
        self._receipt_store.save(receipt)
        return self._cancelled_snapshot(receipt)

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

    def _load_or_start_receipt(
        self,
        *,
        request: PublicationRequest,
        video_sha256: str,
        video_size: int,
        video_media_type: str,
    ) -> YouTubeUploadReceipt:
        existing = self._receipt_store.load(request.idempotency_key)
        if existing is None:
            receipt = YouTubeUploadReceipt(
                idempotency_key=request.idempotency_key,
                payload_sha256=request.payload_sha256,
                video_sha256=video_sha256,
                video_size=video_size,
                video_media_type=video_media_type,
                state="initializing",
            )
            self._receipt_store.save(receipt)
            return receipt
        if (
            existing.payload_sha256 != request.payload_sha256
            or existing.video_sha256 != video_sha256
            or existing.video_size != video_size
            or existing.video_media_type != video_media_type
        ):
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="YouTube upload receipt привязан к другому утверждённому payload.",
            )
        return existing

    def _find_video_by_identity(self, request: PublicationRequest) -> dict[str, object] | None:
        """Reconcile a lost session-initialisation response without reposting.

        The bounded listing is only used after an initial POST has an unknown
        outcome.  It compares both immutable tags on each candidate; absence
        deliberately stays retryable rather than issuing another videos.insert.
        """

        response = self._send(
            HttpRequest(
                method="GET",
                url=with_query(
                    f"{self._api_base}/search",
                    {"forMine": "true", "maxResults": "50", "part": "id", "type": "video"},
                ),
                headers={"X-Goog-Request-Id": f"{request.idempotency_key}:reconcile"},
                idempotency_key=f"{request.idempotency_key}:reconcile",
            ),
            provider="YouTube",
            accepted_statuses={200},
        )
        payload = self._json(response, provider="YouTube")
        items = payload.get("items")
        if not isinstance(items, list):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="YouTube search не вернул список video receipts.",
            )
        for item in items:
            if not isinstance(item, Mapping):
                raise ProviderOperationError(
                    code="INVALID_PAYLOAD",
                    sanitized_detail="YouTube search вернул некорректный video receipt.",
                )
            identifier = item.get("id")
            if not isinstance(identifier, Mapping):
                continue
            remote_id = identifier.get("videoId")
            if not isinstance(remote_id, str) or not remote_id:
                continue
            resource = self._fetch_video(
                remote_id, idempotency_key=f"{request.idempotency_key}:reconcile:{remote_id}"
            )
            if self._matches_request(resource, request):
                return resource
        return None

    def _wait_for_processing(self, remote_id: str, *, idempotency_key: str) -> dict[str, object]:
        """Poll YouTube processingDetails until success or a bounded outcome."""

        for attempt in range(self._processing_max_polls):
            resource = self._fetch_video(
                remote_id, idempotency_key=f"{idempotency_key}:{attempt + 1}"
            )
            details = self._object(resource, "processingDetails", provider="YouTube")
            processing_state = self._string(details, "processingStatus", provider="YouTube")
            if processing_state == "succeeded":
                return resource
            if processing_state in {"failed", "terminated", "rejected"}:
                raise ProviderOperationError(
                    code="INVALID_PAYLOAD",
                    sanitized_detail="YouTube завершил обработку видео с terminal failure.",
                )
            if processing_state not in {"processing", "uploaded"}:
                raise ProviderOperationError(
                    code="INVALID_PAYLOAD",
                    sanitized_detail="YouTube вернул неподдерживаемый processing state.",
                )
            if attempt + 1 < self._processing_max_polls:
                self._processing_poll(attempt + 1)
        raise ProviderOperationError(
            code="UNKNOWN_PROVIDER_OUTCOME",
            sanitized_detail="YouTube processing не подтвердил успех в допустимое число опросов.",
        )

    def _matches_request(self, resource: Mapping[str, object], request: PublicationRequest) -> bool:
        snippet = self._object(resource, "snippet", provider="YouTube")
        tags = self._strings(snippet.get("tags", []), provider="YouTube")
        payload_sha256, request_hash, _target = self._identity_tags(tags)
        return payload_sha256 == request.payload_sha256 and request_hash == hashlib.sha256(
            request.idempotency_key.encode()
        ).hexdigest()

    def _cancelled_snapshot(self, receipt: YouTubeUploadReceipt) -> PublicationSnapshot:
        if receipt.remote_id is None:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Отменённый YouTube receipt не содержит remote video ID.",
            )
        return PublicationSnapshot(
            platform="youtube",
            state="cancelled",
            remote_id=receipt.remote_id,
            known_url=f"https://youtu.be/{receipt.remote_id}",
            payload_sha256=receipt.payload_sha256,
            target_at_utc=self._datetime_optional(receipt.target_at_utc, provider="YouTube"),
        )

    def _upload_resumable(
        self,
        *,
        receipt: YouTubeUploadReceipt,
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
        if receipt.session_url is None:
            raise ProviderOperationError(
                code="UNKNOWN_PROVIDER_OUTCOME",
                sanitized_detail="YouTube resumable upload не имеет сохранённого session URL.",
            )
        progress_offset, completed = self._query_upload_session(
            session_url=receipt.session_url,
            video_size=video_size,
            idempotency_key=idempotency_key,
        )
        if completed is not None:
            return completed
        offset = progress_offset
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
                try:
                    response = self._send(
                        HttpRequest(
                            method="PUT",
                            url=receipt.session_url,
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
                except ProviderOperationError as exc:
                    if exc.code not in {"PROVIDER_TIMEOUT", "UNKNOWN_PROVIDER_OUTCOME"}:
                        raise
                    # A timeout may occur before or after YouTube accepted the
                    # chunk.  Query the same session first; never re-init.
                    offset, completed = self._query_upload_session(
                        session_url=receipt.session_url,
                        video_size=video_size,
                        idempotency_key=idempotency_key,
                    )
                    if completed is not None:
                        return completed
                    continue
                if response.status_code in {200, 201}:
                    return self._json(response, provider="YouTube")
                next_offset = self._resumable_offset(response)
                if next_offset is None:
                    # 308 without Range is intentionally ambiguous.  A status
                    # probe has a different wire form and is the only source
                    # of truth for the next offset.
                    next_offset, completed = self._query_upload_session(
                        session_url=receipt.session_url,
                        video_size=video_size,
                        idempotency_key=idempotency_key,
                    )
                    if completed is not None:
                        return completed
                if next_offset < 0 or next_offset > video_size:
                    raise ProviderOperationError(
                        code="UNKNOWN_PROVIDER_OUTCOME",
                        sanitized_detail="YouTube resumable upload вернул неверный offset.",
                    )
                offset = next_offset
        raise ProviderOperationError(
            code="UNKNOWN_PROVIDER_OUTCOME",
            sanitized_detail="YouTube resumable upload завершился без video receipt.",
        )

    def _query_upload_session(
        self,
        *,
        session_url: str,
        video_size: int,
        idempotency_key: str,
    ) -> tuple[int, dict[str, object] | None]:
        """Return authoritative resumable progress for the saved session URL."""

        response = self._send(
            HttpRequest(
                method="PUT",
                url=session_url,
                headers={
                    "Content-Length": "0",
                    "Content-Range": f"bytes */{video_size}",
                    "X-Goog-Request-Id": f"{idempotency_key}:status",
                },
                idempotency_key=f"{idempotency_key}:status",
            ),
            provider="YouTube",
            accepted_statuses={200, 201, 308},
        )
        if response.status_code in {200, 201}:
            return video_size, self._json(response, provider="YouTube")
        offset = self._resumable_offset(response)
        # YouTube defines a 308 without Range on the status request as zero
        # accepted bytes.  It is safe to resend from byte zero on this same
        # session and it must never cause a new videos.insert.
        return (0 if offset is None else offset), None

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
                    f"{self._api_base}/videos",
                    {"id": remote_id, "part": "snippet,status,processingDetails"},
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
    def _resumable_offset(response: HttpResponse) -> int | None:
        range_header = YouTubePublisher._header(response.headers, "range")
        if range_header is None:
            return None
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


def _receipt_from_json(value: Mapping[str, object]) -> YouTubeUploadReceipt:
    try:
        receipt = YouTubeUploadReceipt(
            idempotency_key=_json_string(value, "idempotency_key"),
            payload_sha256=_json_string(value, "payload_sha256"),
            video_sha256=_json_string(value, "video_sha256"),
            video_size=_json_positive_int(value, "video_size"),
            video_media_type=_json_string(value, "video_media_type"),
            state=_json_upload_state(value.get("state")),
            initialization_attempted=_json_bool(
                value.get("initialization_attempted"), default=False
            ),
            session_url=_json_optional_string(value.get("session_url")),
            remote_id=_json_optional_string(value.get("remote_id")),
            thumbnail_uploaded=_json_bool(value.get("thumbnail_uploaded"), default=False),
            target_at_utc=_json_optional_string(value.get("target_at_utc")),
        )
    except (TypeError, ValueError):
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="Локальный YouTube upload receipt имеет неверный формат.",
        ) from None
    _validate_receipt(receipt)
    return receipt


def _validate_receipt(receipt: YouTubeUploadReceipt) -> None:
    if (
        not receipt.idempotency_key
        or len(receipt.payload_sha256) != 64
        or len(receipt.video_sha256) != 64
        or receipt.video_size <= 0
        or not receipt.video_media_type.startswith("video/")
    ):
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="Локальный YouTube upload receipt имеет неверный формат.",
        )
    if receipt.session_url is not None and (
        not receipt.session_url.startswith("https://")
        or _contains_secret_query(receipt.session_url)
    ):
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail=(
                "Локальный YouTube upload receipt содержит небезопасный session URL."
            ),
        )
    if receipt.state == "uploading" and receipt.session_url is None:
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="YouTube uploading receipt не содержит session URL.",
        )
    if receipt.state in {"uploaded", "cancelled"} and receipt.remote_id is None:
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="YouTube completed receipt не содержит remote video ID.",
        )


def _contains_secret_query(url: str) -> bool:
    from urllib.parse import parse_qsl, urlsplit

    return any(
        any(marker in key.lower() for marker in ("token", "secret", "authorization"))
        for key, _value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
    )


def _json_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(key)
    return item


def _json_optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("optional string")
    return value


def _json_positive_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
        raise ValueError(key)
    return item


def _json_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("bool")
    return value


def _json_upload_state(value: object) -> YouTubeUploadState:
    if value not in {"initializing", "uploading", "uploaded", "cancelled"}:
        raise ValueError("state")
    return cast(YouTubeUploadState, value)
