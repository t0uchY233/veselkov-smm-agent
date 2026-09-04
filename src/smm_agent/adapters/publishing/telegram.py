"""Telegram Bot API adapter with a durable local task receipt.

Telegram has no native scheduler. ``prepare`` and ``arm`` therefore persist a
local task; the Windows scheduler invokes ``execute`` at target time. Every
execute attempt asks Bot API for recent matching channel posts before sending,
so a process crash after ``sendVideo`` can be reconciled without a duplicate.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from smm_agent.adapters.publishing.http import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    json_body,
    provider_error_from_exception,
    provider_error_from_response,
)
from smm_agent.contracts.publication import (
    PreparedPublication,
    PublicationRequest,
    PublicationSnapshot,
    PublicationState,
    RecoveryErrorCode,
)
from smm_agent.domain.publication.ports import ProviderOperationError
from smm_agent.domain.publication.service import (
    PublicationInvariantFailed,
    validate_telegram_caption,
)

MAX_TELEGRAM_VIDEO_BYTES = 49_000_000


def telegram_request(
    *,
    release_id: str,
    schedule_key: str,
    payload_sha256: str,
    payload: dict[str, object],
) -> PublicationRequest:
    """Build the stable Telegram task identity used across local retries."""

    caption = payload.get("caption")
    if not isinstance(caption, str):
        raise ValueError("Telegram payload должен содержать caption.")
    validate_telegram_caption(caption)
    return PublicationRequest(
        release_id=release_id,
        platform="telegram",
        idempotency_key=f"publication:{release_id}:telegram:{schedule_key}",
        payload_sha256=payload_sha256,
        payload=payload,
    )


@dataclass(frozen=True, slots=True)
class TelegramTask:
    """Non-secret local identity needed by a future Windows task invocation."""

    remote_id: str
    idempotency_key: str
    payload_sha256: str
    video_path: str
    video_sha256: str
    video_size: int
    caption: str
    state: str
    target_at_utc: str | None = None
    operation_key: str | None = None
    message_id: str | None = None
    public_at: str | None = None

    def target(self) -> datetime | None:
        return _parse_datetime(self.target_at_utc, provider="Telegram")

    def published(self) -> datetime | None:
        return _parse_datetime(self.public_at, provider="Telegram")


class TelegramTaskStore(Protocol):
    """Durable local task receipt; it contains no Bot token or OAuth data."""

    def load(self, remote_id: str) -> TelegramTask | None: ...

    def save(self, task: TelegramTask) -> None: ...


class InMemoryTelegramTaskStore:
    """Test and single-process implementation of the durable-task port."""

    def __init__(self) -> None:
        self._tasks: dict[str, TelegramTask] = {}

    def load(self, remote_id: str) -> TelegramTask | None:
        return self._tasks.get(remote_id)

    def save(self, task: TelegramTask) -> None:
        self._tasks[task.remote_id] = task


class JsonTelegramTaskStore:
    """Atomic JSON store used until the Windows composition root binds SQLite.

    The file may be safely copied into a support bundle: it intentionally has
    no credential, access token, raw provider response, or message body beyond
    the already-approved release caption.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, remote_id: str) -> TelegramTask | None:
        raw = self._all().get(remote_id)
        if raw is None:
            return None
        return _task_from_json(raw)

    def save(self, task: TelegramTask) -> None:
        values = self._all()
        values[task.remote_id] = {
            "remote_id": task.remote_id,
            "idempotency_key": task.idempotency_key,
            "payload_sha256": task.payload_sha256,
            "video_path": task.video_path,
            "video_sha256": task.video_sha256,
            "video_size": task.video_size,
            "caption": task.caption,
            "state": task.state,
            "target_at_utc": task.target_at_utc,
            "operation_key": task.operation_key,
            "message_id": task.message_id,
            "public_at": task.public_at,
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
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Локальный Telegram task store повреждён.",
            ) from exc
        if not isinstance(value, dict):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Локальный Telegram task store имеет неверный формат.",
            )
        tasks: dict[str, dict[str, object]] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                raise ProviderOperationError(
                    code="INVALID_PAYLOAD",
                    sanitized_detail="Локальный Telegram task store имеет неверный формат.",
                )
            tasks[key] = {str(field): field_value for field, field_value in item.items()}
        return tasks


class TelegramPublisher:
    """Local Telegram task state plus deterministic Bot API request building."""

    platform = "telegram"

    def __init__(
        self,
        *,
        transport: HttpTransport,
        chat_id: str,
        bot_endpoint: str,
        task_store: TelegramTaskStore,
    ) -> None:
        if not chat_id:
            raise ValueError("Для Telegram adapter нужен chat_id.")
        if not bot_endpoint.startswith("https://"):
            raise ValueError("Telegram Bot API endpoint должен использовать HTTPS.")
        self._transport = transport
        self._chat_id = chat_id
        self._bot_endpoint = bot_endpoint.rstrip("/")
        self._task_store = task_store

    def prepare(self, request: PublicationRequest) -> PreparedPublication:
        if request.platform != "telegram":
            raise self._error(
                "INVALID_PAYLOAD", "Telegram adapter получил request другой площадки."
            )
        video_path, video_sha256, video_size, caption = self._prepare_input(request)
        remote_id = (
            f"telegram-task-{hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:32]}"
        )
        existing = self._task_store.load(remote_id)
        if existing is not None:
            if (
                existing.idempotency_key != request.idempotency_key
                or existing.payload_sha256 != request.payload_sha256
                or existing.video_sha256 != video_sha256
                or existing.caption != caption
            ):
                raise self._error(
                    "RECEIPT_MISMATCH",
                    "Telegram task identity уже привязана к другому утверждённому payload.",
                )
            return self._prepared(existing)
        task = TelegramTask(
            remote_id=remote_id,
            idempotency_key=request.idempotency_key,
            payload_sha256=request.payload_sha256,
            video_path=str(video_path),
            video_sha256=video_sha256,
            video_size=video_size,
            caption=caption,
            state="prepared",
        )
        self._task_store.save(task)
        return self._prepared(task)

    def preflight(self, remote_id: str, *, payload_sha256: str) -> None:
        task = self._task(remote_id)
        if task.payload_sha256 != payload_sha256 or task.state == "cancelled":
            raise self._error(
                "RECEIPT_MISMATCH", "Telegram preflight не подтвердил исходный local task receipt."
            )
        self._verify_video(task)
        response = self._send_json(
            method="POST",
            method_name="getChat",
            value={"chat_id": self._chat_id},
            idempotency_key=f"preflight:{remote_id}",
        )
        chat = self._object(response, "result", provider="Telegram")
        if str(chat.get("id")) != self._chat_id:
            raise self._error("RECEIPT_MISMATCH", "Telegram подтвердил другой канал.")

    def arm(
        self, remote_id: str, *, target_at_utc: datetime, operation_key: str
    ) -> PublicationSnapshot:
        target = _as_utc(target_at_utc, provider="Telegram")
        task = self._task(remote_id)
        if task.state == "public":
            raise self._error(
                "RECEIPT_MISMATCH", "Нельзя заново назначить публичный Telegram post."
            )
        if task.state == "cancelled":
            raise self._error("RECEIPT_MISMATCH", "Нельзя назначить отменённый Telegram task.")
        existing_target = task.target()
        if existing_target is not None and existing_target != target:
            raise self._error(
                "RECEIPT_MISMATCH", "Telegram task уже содержит другой target publication time."
            )
        if task.operation_key is not None and task.operation_key != operation_key:
            raise self._error(
                "RECEIPT_MISMATCH", "Telegram task уже содержит другой operation key."
            )
        armed = replace(
            task,
            state="armed",
            target_at_utc=_iso(target),
            operation_key=operation_key,
        )
        self._task_store.save(armed)
        return self._snapshot(armed)

    def status(self, remote_id: str, *, now: datetime | None = None) -> PublicationSnapshot:
        del now
        task = self._task(remote_id)
        if task.state in {"prepared", "public", "cancelled"}:
            return self._snapshot(task)
        looked_up = self._lookup_recent_message(task)
        if looked_up is not None:
            task = replace(
                task,
                state="public",
                message_id=looked_up[0],
                public_at=_iso(looked_up[1]),
            )
            self._task_store.save(task)
        return self._snapshot(task)

    def cancel(self, remote_id: str, *, operation_key: str) -> PublicationSnapshot:
        del operation_key
        task = self._task(remote_id)
        snapshot = self.status(remote_id)
        if snapshot.state in {"public", "publishing"}:
            raise self._error(
                "RECEIPT_MISMATCH",
                "Telegram post с начатой или публичной отправкой нельзя автоматически отменить.",
            )
        cancelled = replace(task, state="cancelled")
        self._task_store.save(cancelled)
        return self._snapshot(cancelled)

    def execute(self, remote_id: str, *, operation_key: str, now: datetime) -> PublicationSnapshot:
        task = self._task(remote_id)
        if task.operation_key != operation_key:
            raise self._error("RECEIPT_MISMATCH", "Telegram execute получил другой operation key.")
        current = self.status(remote_id, now=now)
        if current.state == "public":
            return current
        if current.state == "publishing":
            raise self._error(
                "UNKNOWN_PROVIDER_OUTCOME",
                "Telegram sendVideo уже начат и требует status-first reconciliation.",
            )
        if current.state != "armed" or current.target_at_utc is None:
            raise self._error("INVALID_PAYLOAD", "Telegram task не поставлена в расписание.")
        if now.astimezone(UTC) < current.target_at_utc.astimezone(UTC):
            raise self._error("INVALID_PAYLOAD", "Telegram task запущена раньше target времени.")
        task = self._task(remote_id)
        self._verify_video(task)
        sending = replace(task, state="sending")
        self._task_store.save(sending)
        response = self._send(
            self._multipart_request(sending), provider="Telegram", accepted_statuses={200}
        )
        message_id, published_at = self._message_receipt(response, task=sending)
        published = replace(
            sending,
            state="public",
            message_id=message_id,
            public_at=_iso(published_at),
        )
        self._task_store.save(published)
        return self._snapshot(published)

    def _prepare_input(self, request: PublicationRequest) -> tuple[Path, str, int, str]:
        video = self._object(request.payload, "video", provider="Telegram")
        path_value = video.get("path")
        hash_value = video.get("sha256")
        caption = request.payload.get("caption")
        if (
            not isinstance(path_value, str)
            or not isinstance(hash_value, str)
            or not isinstance(caption, str)
        ):
            raise self._error("INVALID_PAYLOAD", "Telegram payload не содержит video и caption.")
        try:
            validate_telegram_caption(caption)
        except PublicationInvariantFailed as exc:
            raise self._error("INVALID_PAYLOAD", "Telegram caption не прошла проверку.") from exc
        path = Path(path_value)
        if not path.is_file():
            raise self._error("INVALID_PAYLOAD", "Telegram video artifact отсутствует.")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != hash_value:
            raise self._error(
                "INVALID_PAYLOAD", "Telegram video artifact изменился после утверждения."
            )
        size = path.stat().st_size
        if size <= 0 or size > MAX_TELEGRAM_VIDEO_BYTES:
            raise self._error(
                "INVALID_PAYLOAD", "Telegram video превышает лимит 49 MB для cloud Bot API."
            )
        return path, hash_value, size, caption

    def _verify_video(self, task: TelegramTask) -> None:
        path = Path(task.video_path)
        if not path.is_file() or path.stat().st_size != task.video_size:
            raise self._error(
                "INVALID_PAYLOAD", "Telegram video artifact отсутствует или изменился."
            )
        if hashlib.sha256(path.read_bytes()).hexdigest() != task.video_sha256:
            raise self._error(
                "INVALID_PAYLOAD", "Telegram video artifact изменился после утверждения."
            )
        if task.video_size > MAX_TELEGRAM_VIDEO_BYTES:
            raise self._error(
                "INVALID_PAYLOAD", "Telegram video превышает лимит 49 MB для cloud Bot API."
            )
        try:
            validate_telegram_caption(task.caption)
        except PublicationInvariantFailed as exc:
            raise self._error("INVALID_PAYLOAD", "Telegram caption не прошла проверку.") from exc

    def _lookup_recent_message(self, task: TelegramTask) -> tuple[str, datetime] | None:
        """Read recent ``channel_post`` updates before every possible send.

        Bot API keeps updates only for a limited period. If the transport cannot
        prove a prior send from those updates, the caller gets a typed unknown
        outcome and recovery retries status first instead of manufacturing a
        second message.
        """

        response = self._send_json(
            method="POST",
            method_name="getUpdates",
            value={"allowed_updates": ["channel_post"], "limit": 100, "timeout": 0},
            idempotency_key=f"lookup:{task.idempotency_key}",
        )
        updates = response.get("result")
        if not isinstance(updates, list):
            raise self._error("RECEIPT_MISMATCH", "Telegram getUpdates вернул неверный result.")
        target = task.target()
        for update in updates:
            if not isinstance(update, Mapping):
                continue
            post = update.get("channel_post")
            if not isinstance(post, Mapping):
                continue
            chat = post.get("chat")
            if not isinstance(chat, Mapping) or str(chat.get("id")) != self._chat_id:
                continue
            if post.get("caption") != task.caption:
                continue
            video = post.get("video")
            if not isinstance(video, Mapping) or video.get("file_size") != task.video_size:
                continue
            date = _telegram_timestamp(post.get("date"), provider="Telegram")
            if target is not None and date < target:
                continue
            message_id = post.get("message_id")
            if not isinstance(message_id, int) or message_id < 1:
                continue
            return str(message_id), date
        return None

    def _multipart_request(self, task: TelegramTask) -> HttpRequest:
        video_path = Path(task.video_path)
        boundary = f"smm-{hashlib.sha256(task.idempotency_key.encode()).hexdigest()[:32]}"
        fields = [
            ("chat_id", self._chat_id),
            ("caption", task.caption),
            ("supports_streaming", "true"),
        ]
        chunks: list[bytes] = []
        for key, value in fields:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="video"; '
                    f'filename="{video_path.name}"\r\n'
                ).encode(),
                b"Content-Type: video/mp4\r\n\r\n",
                video_path.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        body = b"".join(chunks)
        return HttpRequest(
            method="POST",
            url=f"{self._bot_endpoint}/sendVideo",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
                "X-Idempotency-Key": task.idempotency_key,
            },
            body=body,
            idempotency_key=task.idempotency_key,
        )

    def _message_receipt(
        self, response: HttpResponse, *, task: TelegramTask
    ) -> tuple[str, datetime]:
        payload = self._json(response)
        message = self._object(payload, "result", provider="Telegram")
        chat = self._object(message, "chat", provider="Telegram")
        if str(chat.get("id")) != self._chat_id:
            raise self._error("RECEIPT_MISMATCH", "Telegram sendVideo вернул другой канал.")
        if message.get("caption") != task.caption:
            raise self._error("RECEIPT_MISMATCH", "Telegram sendVideo вернул другой caption.")
        message_id = message.get("message_id")
        if not isinstance(message_id, int) or message_id < 1:
            raise self._error("RECEIPT_MISMATCH", "Telegram sendVideo не вернул message_id.")
        video = self._object(message, "video", provider="Telegram")
        if video.get("file_size") != task.video_size:
            raise self._error("RECEIPT_MISMATCH", "Telegram sendVideo вернул другой видеофайл.")
        return str(message_id), _telegram_timestamp(message.get("date"), provider="Telegram")

    def _send_json(
        self,
        *,
        method: str,
        method_name: str,
        value: Mapping[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        body = json_body(value)
        response = self._send(
            HttpRequest(
                method=method,
                url=f"{self._bot_endpoint}/{method_name}",
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=body,
                idempotency_key=idempotency_key,
            ),
            provider="Telegram",
            accepted_statuses={200},
        )
        return self._json(response)

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

    def _json(self, response: HttpResponse) -> dict[str, object]:
        try:
            payload = response.json_object()
        except ValueError:
            raise self._error(
                "INVALID_PAYLOAD", "Telegram вернул некорректный JSON receipt."
            ) from None
        if payload.get("ok") is not True:
            status = payload.get("error_code")
            if status == 401:
                code: RecoveryErrorCode = "PROVIDER_AUTH_REQUIRED"
            elif status == 403:
                code = "PROVIDER_PERMISSION_DENIED"
            elif status == 429:
                code = "PROVIDER_HTTP_429"
            elif isinstance(status, int) and 500 <= status <= 599:
                code = "PROVIDER_HTTP_5XX"
            else:
                code = "INVALID_PAYLOAD"
            raise self._error(code, f"Telegram Bot API отклонил запрос ({status or 'unknown'}).")
        return payload

    def _task(self, remote_id: str) -> TelegramTask:
        task = self._task_store.load(remote_id)
        if task is None:
            raise self._error("RECEIPT_MISMATCH", "Локальный Telegram task receipt отсутствует.")
        return task

    @staticmethod
    def _prepared(task: TelegramTask) -> PreparedPublication:
        return PreparedPublication(
            platform="telegram",
            state="prepared",
            remote_id=task.remote_id,
            payload_sha256=task.payload_sha256,
        )

    @staticmethod
    def _snapshot(task: TelegramTask) -> PublicationSnapshot:
        target = task.target()
        state = task.state
        states = {
            "prepared": "prepared",
            "armed": "armed",
            "sending": "publishing",
            "public": "public",
            "cancelled": "cancelled",
        }
        snapshot_state = states.get(state)
        if snapshot_state is None:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Локальный Telegram task содержит неподдерживаемый state.",
            )
        return PublicationSnapshot(
            platform="telegram",
            state=cast(PublicationState, snapshot_state),
            remote_id=task.remote_id,
            payload_sha256=task.payload_sha256,
            target_at_utc=target,
            public_at=task.published(),
        )

    @staticmethod
    def _object(value: Mapping[str, object], key: str, *, provider: str) -> Mapping[str, object]:
        candidate = value.get(key)
        if not isinstance(candidate, Mapping):
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail=f"{provider} вернул некорректный {key} receipt.",
            )
        return candidate

    @staticmethod
    def _error(code: RecoveryErrorCode, sanitized_detail: str) -> ProviderOperationError:
        return ProviderOperationError(code=code, sanitized_detail=sanitized_detail)


def _as_utc(value: datetime, *, provider: str) -> datetime:
    if value.tzinfo is None:
        raise ProviderOperationError(
            code="INVALID_PAYLOAD", sanitized_detail=f"{provider} target должен содержать timezone."
        )
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | None, *, provider: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ProviderOperationError(
            code="RECEIPT_MISMATCH",
            sanitized_detail=f"{provider} task содержит неверный timestamp.",
        ) from None
    if parsed.tzinfo is None:
        raise ProviderOperationError(
            code="RECEIPT_MISMATCH",
            sanitized_detail=f"{provider} task содержит timestamp без timezone.",
        )
    return parsed.astimezone(UTC)


def _telegram_timestamp(value: object, *, provider: str) -> datetime:
    if not isinstance(value, int) or value < 0:
        raise ProviderOperationError(
            code="RECEIPT_MISMATCH", sanitized_detail=f"{provider} не вернул timestamp сообщения."
        )
    return datetime.fromtimestamp(value, tz=UTC)


def _task_from_json(value: Mapping[str, object]) -> TelegramTask:
    required = (
        "remote_id",
        "idempotency_key",
        "payload_sha256",
        "video_path",
        "video_sha256",
        "caption",
        "state",
    )
    if any(not isinstance(value.get(field), str) or not value[field] for field in required):
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="Локальный Telegram task store содержит неполный task.",
        )
    video_size = value.get("video_size")
    if not isinstance(video_size, int) or video_size <= 0:
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="Локальный Telegram task store содержит неверный video size.",
        )
    optional = ("target_at_utc", "operation_key", "message_id", "public_at")
    if any(
        value.get(field) is not None and not isinstance(value.get(field), str) for field in optional
    ):
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="Локальный Telegram task store содержит неверный optional receipt.",
        )
    target_at_utc = value.get("target_at_utc")
    operation_key = value.get("operation_key")
    message_id = value.get("message_id")
    public_at = value.get("public_at")
    return TelegramTask(
        remote_id=str(value["remote_id"]),
        idempotency_key=str(value["idempotency_key"]),
        payload_sha256=str(value["payload_sha256"]),
        video_path=str(value["video_path"]),
        video_sha256=str(value["video_sha256"]),
        video_size=video_size,
        caption=str(value["caption"]),
        state=str(value["state"]),
        target_at_utc=target_at_utc if isinstance(target_at_utc, str) else None,
        operation_key=operation_key if isinstance(operation_key, str) else None,
        message_id=message_id if isinstance(message_id, str) else None,
        public_at=public_at if isinstance(public_at, str) else None,
    )
