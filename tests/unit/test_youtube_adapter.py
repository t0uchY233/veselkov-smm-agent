import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from smm_agent.adapters.publishing.http import HttpRequest, HttpResponse
from smm_agent.adapters.publishing.youtube import (
    JsonYouTubeUploadReceiptStore,
    YouTubePublisher,
    YouTubeUploadReceipt,
    youtube_request,
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


def response(value: dict[str, object], *, status: int = 200) -> HttpResponse:
    return HttpResponse(status_code=status, body=json.dumps(value).encode())


def session_progress(accepted_end: int | None) -> HttpResponse:
    return HttpResponse(
        status_code=308,
        headers={} if accepted_end is None else {"Range": f"bytes=0-{accepted_end}"},
    )


def resource(
    request_key: str,
    payload_sha256: str,
    *,
    target: datetime | None = None,
    processing: str = "succeeded",
) -> dict[str, object]:
    tags = [
        "финансы",
        f"smm-agent-payload-{payload_sha256}",
        f"smm-agent-request-{hashlib.sha256(request_key.encode()).hexdigest()}",
    ]
    status: dict[str, object] = {"privacyStatus": "private"}
    if target is not None:
        text = target.isoformat().replace("+00:00", "Z")
        tags.append(f"smm-agent-target-{text}")
        status["publishAt"] = text
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
        "processingDetails": {"processingStatus": processing},
    }


def video_get(value: dict[str, object]) -> HttpResponse:
    return response({"items": [value]})


def request_for(tmp_path: Path) -> tuple[PublicationRequest, Path, str]:
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master-video")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")
    payload: dict[str, object] = {
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
    digest = "a" * 64
    return (
        youtube_request(
            release_id="release-42",
            schedule_key="2099-09-10T11:00:00+00:00",
            payload_sha256=digest,
            payload=payload,
        ),
        master,
        digest,
    )


def test_prepare_uses_private_resumable_upload_and_processing_before_thumbnail(
    tmp_path: Path,
) -> None:
    request, master, digest = request_for(tmp_path)
    video = resource(request.idempotency_key, digest)
    transport = FakeTransport(
        [
            HttpResponse(status_code=200, headers={"Location": "https://upload.example/session/42"}),
            session_progress(None),
            response(video),
            video_get(video),
            response({"videoId": "video-42"}),
        ]
    )

    prepared = YouTubePublisher(transport=transport, channel_id="channel-42").prepare(request)

    assert prepared.remote_id == "video-42"
    initialize, status_query, upload, processing, thumbnail = transport.requests
    assert parse_qs(urlparse(initialize.url).query) == {
        "part": ["snippet,status"],
        "uploadType": ["resumable"],
    }
    metadata = json.loads((initialize.body or b"").decode())
    assert metadata["status"]["privacyStatus"] == "private"
    assert f"smm-agent-payload-{digest}" in metadata["snippet"]["tags"]
    assert status_query.headers["Content-Range"] == f"bytes */{master.stat().st_size}"
    assert upload.headers["Content-Range"] == (
        f"bytes 0-{master.stat().st_size - 1}/{master.stat().st_size}"
    )
    assert "processingDetails" in processing.url
    assert "thumbnails" in thumbnail.url


def test_308_without_range_queries_session_status_before_next_chunk(tmp_path: Path) -> None:
    request, master, digest = request_for(tmp_path)
    master.write_bytes(b"abcdef")
    master_payload = request.payload["master"]
    assert isinstance(master_payload, dict)
    master_payload["sha256"] = hashlib.sha256(master.read_bytes()).hexdigest()
    request.payload.pop("cover")
    video = resource(request.idempotency_key, digest)
    transport = FakeTransport(
        [
            HttpResponse(status_code=200, headers={"Location": "https://upload.example/session/42"}),
            session_progress(None),
            session_progress(None),
            session_progress(2),
            response(video),
            video_get(video),
        ]
    )

    YouTubePublisher(transport=transport, channel_id="channel-42", chunk_size=3).prepare(request)

    uploads = [item for item in transport.requests if item.url.startswith("https://upload.example")]
    assert [item.headers["Content-Range"] for item in uploads] == [
        "bytes */6",
        "bytes 0-2/6",
        "bytes */6",
        "bytes 3-5/6",
    ]


def test_timeout_after_upload_acceptance_queries_same_session_without_second_insert(
    tmp_path: Path,
) -> None:
    request, _master, digest = request_for(tmp_path)
    video = resource(request.idempotency_key, digest)
    transport = FakeTransport(
        [
            HttpResponse(status_code=200, headers={"Location": "https://upload.example/session/42"}),
            session_progress(None),
            TimeoutError(),
            response(video),
            video_get(video),
            response({"videoId": "video-42"}),
        ]
    )

    prepared = YouTubePublisher(transport=transport, channel_id="channel-42").prepare(request)

    assert prepared.remote_id == "video-42"
    assert len(
        [
            item
            for item in transport.requests
            if item.method == "POST"
            and item.url.startswith("https://www.googleapis.com/upload/youtube/v3/videos")
        ]
    ) == 1
    assert all(
        item.url == "https://upload.example/session/42"
        for item in transport.requests
        if item.method == "PUT" and item.url.startswith("https://upload.example")
    )


def test_timeout_before_upload_acceptance_retries_only_saved_session(tmp_path: Path) -> None:
    request, _master, digest = request_for(tmp_path)
    video = resource(request.idempotency_key, digest)
    transport = FakeTransport(
        [
            HttpResponse(status_code=200, headers={"Location": "https://upload.example/session/42"}),
            session_progress(None),
            TimeoutError(),
            session_progress(None),
            response(video),
            video_get(video),
            response({"videoId": "video-42"}),
        ]
    )

    YouTubePublisher(transport=transport, channel_id="channel-42").prepare(request)

    assert len(
        [
            item
            for item in transport.requests
            if item.method == "POST"
            and item.url.startswith("https://www.googleapis.com/upload/youtube/v3/videos")
        ]
    ) == 1
    assert all(
        item.url == "https://upload.example/session/42"
        for item in transport.requests
        if item.method == "PUT" and item.url.startswith("https://upload.example")
    )


def test_restart_uses_saved_session_and_never_creates_second_video(tmp_path: Path) -> None:
    request, master, digest = request_for(tmp_path)
    store = JsonYouTubeUploadReceiptStore(tmp_path / "youtube-receipts.json")
    store.save(
        YouTubeUploadReceipt(
            idempotency_key=request.idempotency_key,
            payload_sha256=digest,
            video_sha256=hashlib.sha256(master.read_bytes()).hexdigest(),
            video_size=master.stat().st_size,
            video_media_type="video/mp4",
            state="uploading",
            initialization_attempted=True,
            session_url="https://upload.example/session/42",
        )
    )
    video = resource(request.idempotency_key, digest)
    transport = FakeTransport(
        [
            session_progress(None),
            response(video),
            video_get(video),
            response({"videoId": "video-42"}),
        ]
    )

    YouTubePublisher(
        transport=transport, channel_id="channel-42", receipt_store=store
    ).prepare(request)

    assert all(
        item.method != "POST"
        for item in transport.requests
        if item.url.startswith("https://www.googleapis.com/upload/youtube/v3/videos")
    )
    assert store.load(request.idempotency_key).remote_id == "video-42"  # type: ignore[union-attr]
    assert "token" not in (tmp_path / "youtube-receipts.json").read_text(encoding="utf-8")


def test_processing_failure_is_terminal_and_arm_reads_publish_at_back(tmp_path: Path) -> None:
    request, _master, digest = request_for(tmp_path)
    failed = resource(request.idempotency_key, digest, processing="failed")
    failure_transport = FakeTransport(
        [
            HttpResponse(status_code=200, headers={"Location": "https://upload.example/session/42"}),
            session_progress(None),
            response(failed),
            video_get(failed),
        ]
    )
    with pytest.raises(ProviderOperationError, match="INVALID_PAYLOAD"):
        YouTubePublisher(transport=failure_transport, channel_id="channel-42").prepare(request)
    assert all("thumbnails" not in item.url for item in failure_transport.requests)

    target = datetime(2099, 9, 10, 11, tzinfo=UTC)
    ready = resource(request.idempotency_key, digest)
    armed = resource(request.idempotency_key, digest, target=target)
    store = JsonYouTubeUploadReceiptStore(tmp_path / "armed-receipts.json")
    store.save(
        YouTubeUploadReceipt(
            idempotency_key=request.idempotency_key,
            payload_sha256=digest,
            video_sha256=hashlib.sha256((tmp_path / "master.mp4").read_bytes()).hexdigest(),
            video_size=(tmp_path / "master.mp4").stat().st_size,
            video_media_type="video/mp4",
            state="uploaded",
            remote_id="video-42",
        )
    )
    transport = FakeTransport(
        [video_get(ready), video_get(ready), response(armed), video_get(armed)]
    )
    result = YouTubePublisher(
        transport=transport, channel_id="channel-42", receipt_store=store
    ).arm(
        "video-42", target_at_utc=target, operation_key="arm-42"
    )
    assert result.target_at_utc == target
    assert len([item for item in transport.requests if item.method == "GET"]) == 3
    update = json.loads((transport.requests[-2].body or b"").decode())
    assert update["status"]["publishAt"] == "2099-09-10T11:00:00Z"


def test_cancel_deletes_remote_receipt_and_survives_process_restart(tmp_path: Path) -> None:
    request, master, digest = request_for(tmp_path)
    store = JsonYouTubeUploadReceiptStore(tmp_path / "cancel-receipts.json")
    store.save(
        YouTubeUploadReceipt(
            idempotency_key=request.idempotency_key,
            payload_sha256=digest,
            video_sha256=hashlib.sha256(master.read_bytes()).hexdigest(),
            video_size=master.stat().st_size,
            video_media_type="video/mp4",
            state="uploaded",
            remote_id="video-42",
        )
    )
    video = resource(request.idempotency_key, digest)
    cancellation = FakeTransport([video_get(video), HttpResponse(status_code=204)])

    result = YouTubePublisher(
        transport=cancellation, channel_id="channel-42", receipt_store=store
    ).cancel("video-42", operation_key="cancel-42")

    assert result.state == "cancelled"
    assert cancellation.requests[-1].method == "DELETE"
    after_restart = FakeTransport([HttpResponse(status_code=404)])
    assert (
        YouTubePublisher(
            transport=after_restart, channel_id="channel-42", receipt_store=store
        ).status("video-42").state
        == "cancelled"
    )


def test_request_and_receipt_diagnostics_redact_tokens(tmp_path: Path) -> None:
    request = HttpRequest(method="GET", url="https://api.example/path?access_token=secret-token")
    assert "secret-token" not in repr(request)
    store = JsonYouTubeUploadReceiptStore(tmp_path / "youtube-receipts.json")
    with pytest.raises(ProviderOperationError) as captured:
        store.save(
            YouTubeUploadReceipt(
                idempotency_key="release",
                payload_sha256="a" * 64,
                video_sha256="b" * 64,
                video_size=1,
                video_media_type="video/mp4",
                state="uploading",
                session_url="https://upload.example/session?access_token=secret-token",
            )
        )
    assert captured.value.code == "INVALID_PAYLOAD"
