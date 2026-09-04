"""Telegram Bot API adapter with a fenced, durable local send intent.

Telegram has neither a native publication scheduler nor a supported generic
idempotency header.  The adapter therefore persists an intent before I/O and
uses the Bot API update cursor to reconcile first.  If a process crashes after
the intent, but before a receipt can prove the result, it returns an explicit
``UNKNOWN_PROVIDER_OUTCOME`` rather than risk a duplicate channel post.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

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
from smm_agent.platform.telegram_tasks import TelegramTaskConflict

MAX_TELEGRAM_VIDEO_BYTES = 49_000_000
_STREAM_CHUNK_SIZE = 1024 * 1024


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
    lookup_cursor: int = 0
    send_cursor: int | None = None
    send_intent_at: str | None = None
    confirmed_update_id: int | None = None
    version: int = 0

    def target(self) -> datetime | None:
        return _parse_datetime(self.target_at_utc, provider="Telegram")

    def published(self) -> datetime | None:
        return _parse_datetime(self.public_at, provider="Telegram")


class TelegramTaskStore(Protocol):
    """Durable local task receipt with explicit transition fencing.

    Production composition must bind :class:`SQLiteTelegramTaskStore`.  The
    in-memory implementation below exists solely to keep unit tests isolated.
    """

    def load(self, remote_id: str) -> TelegramTask | None: ...

    def create(self, task: TelegramTask) -> TelegramTask: ...

    def compare_and_swap(
        self, *, expected: TelegramTask, replacement: TelegramTask
    ) -> TelegramTask: ...


class InMemoryTelegramTaskStore:
    """Test fake only; production uses SQLiteTelegramTaskStore."""

    def __init__(self) -> None:
        self._tasks: dict[str, TelegramTask] = {}

    def load(self, remote_id: str) -> TelegramTask | None:
        return self._tasks.get(remote_id)

    def create(self, task: TelegramTask) -> TelegramTask:
        if task.remote_id in self._tasks:
            raise TelegramTaskConflict("Telegram task already exists.")
        self._tasks[task.remote_id] = task
        return task

    def compare_and_swap(
        self, *, expected: TelegramTask, replacement: TelegramTask
    ) -> TelegramTask:
        current = self._tasks.get(expected.remote_id)
        if current is None or current.version != expected.version:
            raise TelegramTaskConflict("Telegram task version fence lost.")
        updated = replace(replacement, version=expected.version + 1)
        self._tasks[expected.remote_id] = updated
        return updated


class TelegramPublisher:
    """Local Telegram task state plus deterministic Bot API request building."""

    platform = "telegram"

    def __init__(
        self,
        *,
        transport: HttpTransport,
        chat_id: str,
        api_base: str,
        task_store: TelegramTaskStore,
    ) -> None:
        if not chat_id:
            raise ValueError("Для Telegram adapter нужен chat_id.")
        self._api_base = _safe_api_base(api_base)
        self._transport = transport
        self._chat_id = chat_id
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
        try:
            created = self._task_store.create(task)
        except TelegramTaskConflict:
            concurrent = self._task_store.load(remote_id)
            if concurrent is None:
                raise self._error(
                    "UNKNOWN_PROVIDER_OUTCOME", "Не удалось сохранить Telegram task."
                ) from None
            if (
                concurrent.idempotency_key != request.idempotency_key
                or concurrent.payload_sha256 != request.payload_sha256
                or concurrent.video_sha256 != video_sha256
                or concurrent.caption != caption
            ):
                raise self._error(
                    "RECEIPT_MISMATCH",
                    "Telegram task identity уже привязана к другому утверждённому payload.",
                ) from None
            created = concurrent
        return self._prepared(created)

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
        if task.state == "sending":
            raise self._error(
                "UNKNOWN_PROVIDER_OUTCOME",
                "Нельзя менять расписание Telegram task с начатой отправкой.",
            )
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
        return self._snapshot(self._replace(task, armed))

    def status(self, remote_id: str, *, now: datetime | None = None) -> PublicationSnapshot:
        del now
        task = self._task(remote_id)
        if task.state in {"prepared", "public", "cancelled"}:
            return self._snapshot(task)
        try:
            lookup = self._lookup_recent_message(task)
        except ProviderOperationError:
            if task.state == "sending":
                raise self._error(
                    "UNKNOWN_PROVIDER_OUTCOME",
                    "Невозможно подтвердить результат уже начатой Telegram отправки.",
                ) from None
            raise
        task = self._persist_lookup_cursor(task, lookup.next_cursor)
        looked_up = lookup.receipt if task.state == "sending" else None
        if looked_up is not None:
            task = replace(
                task,
                state="public",
                message_id=looked_up[0],
                public_at=_iso(looked_up[1]),
                confirmed_update_id=looked_up[2],
            )
            task = self._replace(self._task(remote_id), task)
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
        task = self._task(remote_id)
        cancelled = replace(task, state="cancelled")
        return self._snapshot(self._replace(task, cancelled))

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
        # The intent is the irreversible boundary: a crash anywhere after its
        # fenced save is *never* retried as sendVideo.  Status reconciliation
        # either proves a message or raises UNKNOWN_PROVIDER_OUTCOME for an
        # incident/human decision.
        sending = self._replace(
            task,
            replace(
                task,
                state="sending",
                send_cursor=task.lookup_cursor,
                send_intent_at=_iso(now.astimezone(UTC)),
            ),
        )
        try:
            response = self._send(
                self._multipart_request(sending), provider="Telegram", accepted_statuses={200}
            )
            message_id, published_at = self._message_receipt(response, task=sending)
        except ProviderOperationError:
            raise self._error(
                "UNKNOWN_PROVIDER_OUTCOME",
                "Telegram sendVideo начат; его результат требует status-first reconciliation.",
            ) from None
        published = replace(
            sending,
            state="public",
            message_id=message_id,
            public_at=_iso(published_at),
            # A direct Bot API receipt has a stronger response identity than
            # a later update.  ``0`` explicitly records that fact in SQLite.
            confirmed_update_id=0,
        )
        try:
            return self._snapshot(self._replace(sending, published))
        except TelegramTaskConflict:
            current_after_send = self._task(remote_id)
            if current_after_send.state == "public":
                return self._snapshot(current_after_send)
            raise self._error(
                "UNKNOWN_PROVIDER_OUTCOME",
                "Telegram sendVideo завершился, но local receipt потерял fence.",
            ) from None

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
        actual_hash = _sha256_file(path)
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
        if _sha256_file(path) != task.video_sha256:
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

    def _lookup_recent_message(self, task: TelegramTask) -> _TelegramLookup:
        """Read updates from the durable cursor before every possible send.

        A matching caption alone is not a receipt: it could be a manually
        posted lookalike.  A recovery candidate must be newer than the cursor
        snapshotted with the send intent and is persisted as the composite
        ``(update_id, chat_id, message_id)`` identity.  Older same-caption
        posts only advance the cursor and can never become this task's receipt.
        """

        response = self._send_json(
            method="POST",
            method_name="getUpdates",
            value={
                "allowed_updates": ["channel_post"],
                "limit": 100,
                "timeout": 0,
                "offset": task.lookup_cursor,
            },
            idempotency_key=f"lookup:{task.idempotency_key}",
        )
        updates = response.get("result")
        if not isinstance(updates, list):
            raise self._error("RECEIPT_MISMATCH", "Telegram getUpdates вернул неверный result.")
        next_cursor = task.lookup_cursor
        target = task.target()
        sent_at = _parse_datetime(task.send_intent_at, provider="Telegram")
        minimum_update_id = task.send_cursor if task.send_cursor is not None else None
        for update in updates:
            if not isinstance(update, Mapping):
                continue
            update_id = update.get("update_id")
            if not isinstance(update_id, int) or update_id < 0:
                continue
            next_cursor = max(next_cursor, update_id + 1)
            if task.state != "sending" or minimum_update_id is None:
                continue
            if update_id < minimum_update_id:
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
            if sent_at is None or date < sent_at:
                continue
            message_id = post.get("message_id")
            if not isinstance(message_id, int) or message_id < 1:
                continue
            return _TelegramLookup(
                receipt=(str(message_id), date, update_id), next_cursor=next_cursor
            )
        return _TelegramLookup(receipt=None, next_cursor=next_cursor)

    def _multipart_request(self, task: TelegramTask) -> HttpRequest:
        video_path = Path(task.video_path)
        boundary = f"smm-{hashlib.sha256(task.idempotency_key.encode()).hexdigest()[:32]}"
        fields = [
            ("chat_id", self._chat_id),
            ("caption", task.caption),
            ("supports_streaming", "true"),
        ]
        prefix_chunks: list[bytes] = []
        for key, value in fields:
            prefix_chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        suffix_chunks = (
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
        prefix_chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="video"; '
                    f'filename="{video_path.name}"\r\n'
                ).encode(),
                b"Content-Type: video/mp4\r\n\r\n",
            ]
        )
        stream = _MultipartVideoStream(
            prefix=tuple(prefix_chunks),
            suffix=suffix_chunks,
            video_path=video_path,
            video_size=task.video_size,
            video_sha256=task.video_sha256,
        )
        return HttpRequest(
            method="POST",
            url=self._method_url("sendVideo"),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(stream.content_length),
            },
            body_stream=stream,
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
                url=self._method_url(method_name),
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=body,
                idempotency_key=idempotency_key,
            ),
            provider="Telegram",
            accepted_statuses={200},
        )
        return self._json(response)

    def _method_url(self, method_name: str) -> str:
        # Credential-aware transport inserts Bot token material only at the I/O
        # edge.  This URL is intentionally safe to retain in diagnostics.
        return f"{self._api_base}/bot/{method_name}"

    def _persist_lookup_cursor(self, task: TelegramTask, cursor: int) -> TelegramTask:
        if cursor < task.lookup_cursor:
            raise self._error("RECEIPT_MISMATCH", "Telegram update cursor откатился.")
        if cursor == task.lookup_cursor:
            return task
        try:
            return self._replace(task, replace(task, lookup_cursor=cursor))
        except TelegramTaskConflict:
            current = self._task(task.remote_id)
            if current.lookup_cursor >= cursor:
                return current
            raise self._error(
                "UNKNOWN_PROVIDER_OUTCOME", "Потеряна версия Telegram update cursor."
            ) from None

    def _replace(self, expected: TelegramTask, replacement: TelegramTask) -> TelegramTask:
        try:
            return self._task_store.compare_and_swap(expected=expected, replacement=replacement)
        except TelegramTaskConflict:
            raise

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


@dataclass(frozen=True, slots=True)
class _TelegramLookup:
    """A cursor advance plus an optional composite remote receipt."""

    receipt: tuple[str, datetime, int] | None
    next_cursor: int


@dataclass(frozen=True, slots=True)
class _MultipartVideoStream:
    """Multipart file body that never materialises the 49 MB video in RAM."""

    prefix: tuple[bytes, ...]
    suffix: tuple[bytes, ...]
    video_path: Path
    video_size: int
    video_sha256: str

    @property
    def content_length(self) -> int:
        return (
            sum(len(chunk) for chunk in self.prefix)
            + self.video_size
            + sum(len(chunk) for chunk in self.suffix)
        )

    def iter_chunks(self) -> Iterator[bytes]:
        yield from self.prefix
        read_size = 0
        digest = hashlib.sha256()
        with self.video_path.open("rb") as stream:
            while chunk := stream.read(_STREAM_CHUNK_SIZE):
                read_size += len(chunk)
                digest.update(chunk)
                yield chunk
        if read_size != self.video_size:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Telegram video изменился во время streaming upload.",
            )
        if digest.hexdigest() != self.video_sha256:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Telegram video изменился во время streaming upload.",
            )
        yield from self.suffix


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_STREAM_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_api_base(value: str) -> str:
    """Accept only a credential-free HTTPS Bot API origin/path.

    A standard Bot token is injected by ``RedactingHttpsTransport`` after the
    adapter has finished building this safe request.  Rejecting user info,
    queries, fragments, and a token-bearing ``/bot<token>`` path prevents a
    configuration mistake from reaching request reprs or diagnostics.
    """

    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Telegram api_base должен быть credential-free HTTPS URL.")
    parts = [part for part in parsed.path.split("/") if part]
    if any(part.lower().startswith("bot") and len(part) > 3 for part in parts):
        raise ValueError("Telegram api_base не должен содержать Bot token.")
    return value.rstrip("/")


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
