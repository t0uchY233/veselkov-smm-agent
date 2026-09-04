import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from smm_agent.adapters.publishing.http import HttpRequest, HttpResponse
from smm_agent.adapters.publishing.telegram import (
    InMemoryTelegramTaskStore,
    JsonTelegramTaskStore,
    TelegramPublisher,
    telegram_request,
)
from smm_agent.contracts.publication import PublicationRequest
from smm_agent.domain.publication.ports import ProviderOperationError


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


def message(*, caption: str, size: int, target: datetime) -> dict[str, object]:
    return {
        "message_id": 17,
        "chat": {"id": "-10042"},
        "caption": caption,
        "video": {"file_size": size},
        "date": int(target.timestamp()),
    }


def test_telegram_preflight_and_send_build_exact_bot_api_requests(tmp_path: Path) -> None:
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
    publisher = TelegramPublisher(
        transport=transport,
        chat_id="-10042",
        bot_endpoint="https://api.telegram.example/bot-token",
        task_store=InMemoryTelegramTaskStore(),
    )

    prepared = publisher.prepare(request)
    armed = publisher.arm(prepared.remote_id, target_at_utc=target, operation_key="execute-42")
    publisher.preflight(prepared.remote_id, payload_sha256=request.payload_sha256)
    public = publisher.execute(prepared.remote_id, operation_key="execute-42", now=target)

    assert armed.state == "armed"
    assert public.state == "public"
    get_chat, lookup, send_video = transport.requests
    assert get_chat.url.endswith("/getChat")
    assert json.loads((get_chat.body or b"").decode()) == {"chat_id": "-10042"}
    assert lookup.url.endswith("/getUpdates")
    assert json.loads((lookup.body or b"").decode()) == {
        "allowed_updates": ["channel_post"],
        "limit": 100,
        "timeout": 0,
    }
    assert send_video.url.endswith("/sendVideo")
    assert send_video.headers["X-Idempotency-Key"] == request.idempotency_key
    assert caption.encode() in (send_video.body or b"")
    assert video.read_bytes() in (send_video.body or b"")


def test_telegram_retry_reconciles_recent_post_after_crash_without_second_send(
    tmp_path: Path,
) -> None:
    request, video = task_request(tmp_path)
    target = datetime(2099, 9, 10, 11, tzinfo=UTC)
    caption = request.payload["caption"]
    assert isinstance(caption, str)
    store = JsonTelegramTaskStore(tmp_path / "telegram-tasks.json")
    first_transport = FakeTransport([response({"ok": True, "result": []}), TimeoutError()])
    first = TelegramPublisher(
        transport=first_transport,
        chat_id="-10042",
        bot_endpoint="https://api.telegram.example/bot-token",
        task_store=store,
    )
    prepared = first.prepare(request)
    first.arm(prepared.remote_id, target_at_utc=target, operation_key="execute-42")

    with pytest.raises(ProviderOperationError) as failure:
        first.execute(prepared.remote_id, operation_key="execute-42", now=target)
    assert failure.value.code == "PROVIDER_TIMEOUT"

    second_transport = FakeTransport(
        [
            response(
                {
                    "ok": True,
                    "result": [
                        {
                            "channel_post": message(
                                caption=caption, size=video.stat().st_size, target=target
                            )
                        }
                    ],
                }
            )
        ]
    )
    restarted = TelegramPublisher(
        transport=second_transport,
        chat_id="-10042",
        bot_endpoint="https://api.telegram.example/bot-token",
        task_store=store,
    )

    public = restarted.execute(prepared.remote_id, operation_key="execute-42", now=target)

    assert public.state == "public"
    assert [item.url.rsplit("/", maxsplit=1)[-1] for item in first_transport.requests] == [
        "getUpdates",
        "sendVideo",
    ]
    assert [item.url.rsplit("/", maxsplit=1)[-1] for item in second_transport.requests] == [
        "getUpdates"
    ]
