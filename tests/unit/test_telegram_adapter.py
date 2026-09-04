import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from smm_agent.adapters.publishing.http import HttpRequest, HttpResponse
from smm_agent.adapters.publishing.telegram import TelegramPublisher, telegram_request
from smm_agent.contracts.publication import PublicationRequest
from smm_agent.domain.publication.ports import ProviderOperationError
from smm_agent.domain.publication.service import PublicationInvariantFailed
from smm_agent.platform.db import Database
from smm_agent.platform.telegram_tasks import SQLiteTelegramTaskStore, TelegramTaskConflict


class FakeTransport:
    def __init__(self, outcomes: list[HttpResponse | Exception]) -> None:
        self._outcomes = outcomes
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def response(value: dict[str, object]) -> HttpResponse:
    return HttpResponse(status_code=200, body=json.dumps(value).encode())


def task_request(tmp_path: Path) -> tuple[PublicationRequest, Path]:
    video = tmp_path / "telegram.mp4"
    video.write_bytes(b"telegram-video")
    request = telegram_request(
        release_id="release-telegram",
        schedule_key="2099-09-10T11:00:00+00:00",
        payload_sha256="b" * 64,
        payload={
            "video": {"path": str(video), "sha256": hashlib.sha256(video.read_bytes()).hexdigest()},
            "caption": "Контракт и деньги. Смотрите выпуск.",
        },
    )
    return request, video


def store(tmp_path: Path) -> SQLiteTelegramTaskStore:
    database = Database(tmp_path)
    database.initialize()
    return SQLiteTelegramTaskStore(database)


def publisher(
    *, transport: FakeTransport, task_store: SQLiteTelegramTaskStore
) -> TelegramPublisher:
    return TelegramPublisher(
        transport=transport,
        chat_id="-10042",
        api_base="https://api.telegram.example",
        task_store=task_store,
    )


def message(
    *, caption: str, size: int, target: datetime, message_id: int = 17
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "chat": {"id": "-10042"},
        "caption": caption,
        "video": {"file_size": size},
        "date": int(target.timestamp()),
    }


def update(*, update_id: int, post: dict[str, object]) -> dict[str, object]:
    return {"update_id": update_id, "channel_post": post}


def arm_for_target(item: TelegramPublisher, request: PublicationRequest, target: datetime) -> str:
    prepared = item.prepare(request)
    item.arm(prepared.remote_id, target_at_utc=target, operation_key="execute-42")
    return prepared.remote_id


def test_telegram_preflight_and_send_use_safe_streamed_bot_api_request(tmp_path: Path) -> None:
    request, video = task_request(tmp_path)
    target = datetime(2099, 9, 10, 11, tzinfo=UTC)
    caption = request.payload["caption"]
    assert isinstance(caption, str)
    transport = FakeTransport(
        [
            response({"ok": True, "result": {"id": "-10042"}}),
            response({"ok": True, "result": []}),
            response(
                {
                    "ok": True,
                    "result": message(caption=caption, size=video.stat().st_size, target=target),
                }
            ),
        ]
    )
    item = publisher(transport=transport, task_store=store(tmp_path))

    remote_id = arm_for_target(item, request, target)
    item.preflight(remote_id, payload_sha256=request.payload_sha256)
    public = item.execute(remote_id, operation_key="execute-42", now=target)

    assert public.state == "public"
    get_chat, lookup, send_video = transport.requests
    assert get_chat.url == "https://api.telegram.example/bot/getChat"
    assert json.loads((get_chat.body or b"").decode()) == {"chat_id": "-10042"}
    assert lookup.url == "https://api.telegram.example/bot/getUpdates"
    assert json.loads((lookup.body or b"").decode()) == {
        "allowed_updates": ["channel_post"],
        "limit": 100,
        "offset": 0,
        "timeout": 0,
    }
    assert send_video.url == "https://api.telegram.example/bot/sendVideo"
    assert send_video.body is None
    assert send_video.body_stream is not None
    assert "X-Idempotency-Key" not in send_video.headers
    streamed = b"".join(send_video.body_stream.iter_chunks())
    assert caption.encode() in streamed
    assert video.read_bytes() in streamed
    assert "token" not in repr(send_video).lower()
    assert "authorization" not in {key.lower() for key in send_video.headers}


def test_telegram_crash_after_send_reconciles_sqlite_receipt_without_second_send(
    tmp_path: Path,
) -> None:
    request, video = task_request(tmp_path)
    target = datetime(2099, 9, 10, 11, tzinfo=UTC)
    caption = request.payload["caption"]
    assert isinstance(caption, str)
    task_store = store(tmp_path)
    first_transport = FakeTransport([response({"ok": True, "result": []}), TimeoutError()])
    first = publisher(transport=first_transport, task_store=task_store)
    remote_id = arm_for_target(first, request, target)

    with pytest.raises(ProviderOperationError) as failure:
        first.execute(remote_id, operation_key="execute-42", now=target)
    assert failure.value.code == "UNKNOWN_PROVIDER_OUTCOME"
    intent = task_store.load(remote_id)
    assert intent is not None
    assert intent.state == "sending"
    assert intent.send_cursor == 0
    assert intent.send_intent_at is not None

    second_transport = FakeTransport(
        [
            response(
                {
                    "ok": True,
                    "result": [
                        update(
                            update_id=1,
                            post=message(caption=caption, size=video.stat().st_size, target=target),
                        )
                    ],
                }
            )
        ]
    )
    restarted = publisher(transport=second_transport, task_store=task_store)

    public = restarted.execute(remote_id, operation_key="execute-42", now=target)

    assert public.state == "public"
    reconciled = task_store.load(remote_id)
    assert reconciled is not None
    assert reconciled.confirmed_update_id == 1
    assert [item.url.rsplit("/", maxsplit=1)[-1] for item in first_transport.requests] == [
        "getUpdates",
        "sendVideo",
    ]
    assert [item.url.rsplit("/", maxsplit=1)[-1] for item in second_transport.requests] == [
        "getUpdates"
    ]


def test_telegram_crash_after_intent_before_send_never_repeats_ambiguous_operation(
    tmp_path: Path,
) -> None:
    request, _ = task_request(tmp_path)
    target = datetime(2099, 9, 10, 11, tzinfo=UTC)
    task_store = store(tmp_path)
    first = publisher(transport=FakeTransport([]), task_store=task_store)
    remote_id = arm_for_target(first, request, target)
    armed = task_store.load(remote_id)
    assert armed is not None
    task_store.compare_and_swap(
        expected=armed,
        replacement=replace(
            armed,
            state="sending",
            send_cursor=armed.lookup_cursor,
            send_intent_at=target.isoformat().replace("+00:00", "Z"),
        ),
    )

    restarted_transport = FakeTransport([response({"ok": True, "result": []})])
    restarted = publisher(transport=restarted_transport, task_store=task_store)

    with pytest.raises(ProviderOperationError) as failure:
        restarted.execute(remote_id, operation_key="execute-42", now=target)
    assert failure.value.code == "UNKNOWN_PROVIDER_OUTCOME"
    assert [item.url.rsplit("/", maxsplit=1)[-1] for item in restarted_transport.requests] == [
        "getUpdates"
    ]


def test_telegram_old_same_caption_post_is_not_used_as_recovery_receipt(tmp_path: Path) -> None:
    request, video = task_request(tmp_path)
    target = datetime(2099, 9, 10, 11, tzinfo=UTC)
    caption = request.payload["caption"]
    assert isinstance(caption, str)
    task_store = store(tmp_path)
    initial = publisher(transport=FakeTransport([]), task_store=task_store)
    remote_id = arm_for_target(initial, request, target)
    old_same_caption = update(
        update_id=72,
        post=message(caption=caption, size=video.stat().st_size, target=target, message_id=99),
    )
    transport = FakeTransport(
        [
            response({"ok": True, "result": [old_same_caption]}),
            TimeoutError(),
        ]
    )
    sending = publisher(transport=transport, task_store=task_store)

    with pytest.raises(ProviderOperationError) as failure:
        sending.execute(remote_id, operation_key="execute-42", now=target)
    assert failure.value.code == "UNKNOWN_PROVIDER_OUTCOME"
    persisted = task_store.load(remote_id)
    assert persisted is not None
    assert persisted.state == "sending"
    assert persisted.send_cursor == 73

    recovery_transport = FakeTransport([response({"ok": True, "result": [old_same_caption]})])
    recovery = publisher(transport=recovery_transport, task_store=task_store)
    with pytest.raises(ProviderOperationError) as retry:
        recovery.execute(remote_id, operation_key="execute-42", now=target)
    assert retry.value.code == "UNKNOWN_PROVIDER_OUTCOME"
    assert [item.url.rsplit("/", maxsplit=1)[-1] for item in recovery_transport.requests] == [
        "getUpdates"
    ]


def test_telegram_unavailable_updates_after_send_intent_becomes_typed_unknown(
    tmp_path: Path,
) -> None:
    request, _ = task_request(tmp_path)
    target = datetime(2099, 9, 10, 11, tzinfo=UTC)
    task_store = store(tmp_path)
    item = publisher(transport=FakeTransport([]), task_store=task_store)
    remote_id = arm_for_target(item, request, target)
    armed = task_store.load(remote_id)
    assert armed is not None
    task_store.compare_and_swap(
        expected=armed,
        replacement=replace(
            armed,
            state="sending",
            send_cursor=0,
            send_intent_at=target.isoformat().replace("+00:00", "Z"),
        ),
    )
    transport = FakeTransport([TimeoutError()])

    with pytest.raises(ProviderOperationError) as failure:
        publisher(transport=transport, task_store=task_store).status(remote_id)
    assert failure.value.code == "UNKNOWN_PROVIDER_OUTCOME"
    assert [item.url.rsplit("/", maxsplit=1)[-1] for item in transport.requests] == ["getUpdates"]


def test_telegram_sqlite_store_rejects_stale_version_fence(tmp_path: Path) -> None:
    request, _ = task_request(tmp_path)
    task_store = store(tmp_path)
    item = publisher(transport=FakeTransport([]), task_store=task_store)
    remote_id = item.prepare(request).remote_id
    first = task_store.load(remote_id)
    second = task_store.load(remote_id)
    assert first is not None and second is not None
    task_store.compare_and_swap(expected=first, replacement=replace(first, state="cancelled"))
    with pytest.raises(TelegramTaskConflict):
        task_store.compare_and_swap(expected=second, replacement=replace(second, state="cancelled"))


def test_telegram_rejects_token_bearing_api_base_and_utf16_overflow(tmp_path: Path) -> None:
    task_store = store(tmp_path)
    with pytest.raises(ValueError):
        TelegramPublisher(
            transport=FakeTransport([]),
            chat_id="-10042",
            api_base="https://api.telegram.example/bot123456:secret",
            task_store=task_store,
        )
    request, _ = task_request(tmp_path)
    too_long = dict(request.payload)
    too_long["caption"] = "😀" * 501
    with pytest.raises(PublicationInvariantFailed):
        telegram_request(
            release_id="release-telegram",
            schedule_key="2099-09-10T11:00:00+00:00",
            payload_sha256="b" * 64,
            payload=too_long,
        )
