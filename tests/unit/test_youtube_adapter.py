import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from smm_agent.adapters.publishing.http import HttpRequest, HttpResponse
from smm_agent.adapters.publishing.youtube import YouTubePublisher, youtube_request
from smm_agent.contracts.publication import PublicationRequest


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


def response(value: dict[str, object], *, status: int = 200) -> HttpResponse:
    return HttpResponse(status_code=status, body=json.dumps(value).encode())


def youtube_resource(
    request_key: str,
    payload_sha256: str,
    *,
    target: datetime | None = None,
) -> dict[str, object]:
    tags = [
        "финансы",
        f"smm-agent-payload-{payload_sha256}",
        f"smm-agent-request-{hashlib.sha256(request_key.encode()).hexdigest()}",
    ]
    status: dict[str, object] = {"privacyStatus": "private"}
    if target is not None:
        target_text = target.isoformat().replace("+00:00", "Z")
        tags.append(f"smm-agent-target-{target_text}")
        status["publishAt"] = target_text
    return {
        "id": "video-42",
        "snippet": {
            "channelId": "channel-42",
            "title": "Контракт и деньги",
            "description": "Практическое правило для стройки.",
            "tags": tags,
            "categoryId": "22",
        },
        "status": status,
    }


def request_for(tmp_path: Path) -> tuple[PublicationRequest, Path, str]:
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master-video")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")
    payload = {
        "master": {
            "path": str(master),
            "sha256": hashlib.sha256(master.read_bytes()).hexdigest(),
            "media_type": "video/mp4",
        },
        "cover": {"path": str(cover), "sha256": hashlib.sha256(cover.read_bytes()).hexdigest()},
        "metadata": {
            "title": "Контракт и деньги",
            "description": "Практическое правило для стройки.",
            "tags": ["финансы"],
        },
    }
    payload_sha256 = "a" * 64
    return (
        youtube_request(
            release_id="release-42",
            schedule_key="2099-09-10T11:00:00+00:00",
            payload_sha256=payload_sha256,
            payload=payload,
        ),
        master,
        payload_sha256,
    )


def test_youtube_prepare_uses_private_resumable_upload_and_immutable_identities(
    tmp_path: Path,
) -> None:
    request, master, payload_sha256 = request_for(tmp_path)
    resource = youtube_resource(request.idempotency_key, payload_sha256)
    transport = FakeTransport(
        [
            HttpResponse(
                status_code=200, headers={"Location": "https://upload.example/session/42"}
            ),
            response(resource),
            response({"videoId": "video-42"}),
        ]
    )
    publisher = YouTubePublisher(transport=transport, channel_id="channel-42")

    prepared = publisher.prepare(request)

    assert prepared.remote_id == "video-42"
    initialize, upload, thumbnail = transport.requests
    assert initialize.method == "POST"
    assert parse_qs(urlparse(initialize.url).query) == {
        "part": ["snippet,status"],
        "uploadType": ["resumable"],
    }
    metadata = json.loads((initialize.body or b"").decode())
    assert metadata["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    assert f"smm-agent-payload-{payload_sha256}" in metadata["snippet"]["tags"]
    assert initialize.headers["X-Goog-Request-Id"] == request.idempotency_key
    assert upload.url == "https://upload.example/session/42"
    assert upload.body == master.read_bytes()
    assert upload.headers["Content-Range"] == (
        f"bytes 0-{master.stat().st_size - 1}/{master.stat().st_size}"
    )
    assert thumbnail.method == "POST"
    assert "videoId=video-42" in thumbnail.url


def test_youtube_resumes_partial_chunk_then_reads_publish_at_back(tmp_path: Path) -> None:
    request, master, payload_sha256 = request_for(tmp_path)
    master.write_bytes(b"abcdef")
    master_payload = request.payload["master"]
    assert isinstance(master_payload, dict)
    master_payload["sha256"] = hashlib.sha256(master.read_bytes()).hexdigest()
    request.payload.pop("cover")
    target = datetime(2099, 9, 10, 11, tzinfo=UTC)
    resource = youtube_resource(request.idempotency_key, payload_sha256)
    armed_resource = youtube_resource(request.idempotency_key, payload_sha256, target=target)
    transport = FakeTransport(
        [
            HttpResponse(
                status_code=200, headers={"Location": "https://upload.example/session/42"}
            ),
            HttpResponse(status_code=308, headers={"Range": "bytes=0-2"}),
            response(resource),
            response({"items": [resource]}),
            response(armed_resource),
        ]
    )
    publisher = YouTubePublisher(transport=transport, channel_id="channel-42", chunk_size=3)

    prepared = publisher.prepare(request)
    armed = publisher.arm(prepared.remote_id, target_at_utc=target, operation_key="arm-42")

    assert armed.state == "armed"
    assert armed.target_at_utc == target
    uploads = [item for item in transport.requests if item.url.startswith("https://upload.example")]
    assert [item.headers["Content-Range"] for item in uploads] == ["bytes 0-2/6", "bytes 3-5/6"]
    update = json.loads((transport.requests[-1].body or b"").decode())
    assert update["status"]["publishAt"] == "2099-09-10T11:00:00Z"
