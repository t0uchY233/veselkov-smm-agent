"""Concrete, isolated Windows capability probes for setup acceptance.

The probes in this module are reachable only through the explicit
smmctl capability smoke --execute boundary. They use synthetic assets and the
configured test resources; they never discover or substitute a channel.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from smm_agent.adapters.media.process import run_bounded
from smm_agent.adapters.publishing.http import (
    RedactingHttpsTransport,
    TelegramBotCredential,
    TelegramBotHttpsTransport,
)
from smm_agent.adapters.publishing.oauth import (
    RefreshingOAuthCredential,
    StdlibOAuthTokenRefresher,
)
from smm_agent.adapters.publishing.telegram import (
    MAX_TELEGRAM_VIDEO_BYTES,
    InMemoryTelegramTaskStore,
    TelegramPublisher,
    telegram_request,
)
from smm_agent.adapters.publishing.youtube import (
    JsonYouTubeUploadReceiptStore,
    YouTubePublisher,
    youtube_request,
)
from smm_agent.adapters.secrets.secret_reader import (
    SecretReader,
    WindowsCredentialSecretReader,
)
from smm_agent.application.capability_smoke_service import SmokeProbeResult
from smm_agent.contracts.publication import (
    PreparedPublication,
    PublicationRequest,
    PublicationSnapshot,
)
from smm_agent.domain.publication.ports import ProviderOperationError
from smm_agent.platform.config import SmmAgentConfig, configured_path

_TELEGRAM_SMOKE_CAPTION = (
    "Техническая проверка SMM Agent. Тестовый канал. Сообщение можно удалить."
)


class SmokeVideoFactory(Protocol):
    def create(self, config: SmmAgentConfig) -> Path: ...


class TelegramSmokePublisher(Protocol):
    def prepare(self, request: PublicationRequest) -> PreparedPublication: ...

    def preflight(self, remote_id: str, *, payload_sha256: str) -> None: ...

    def arm(
        self, remote_id: str, *, target_at_utc: datetime, operation_key: str
    ) -> PublicationSnapshot: ...

    def execute(
        self, remote_id: str, *, operation_key: str, now: datetime
    ) -> PublicationSnapshot: ...


class YouTubeSmokePublisher(Protocol):
    def prepare(self, request: PublicationRequest) -> PreparedPublication: ...

    def preflight(self, remote_id: str, *, payload_sha256: str) -> None: ...

    def arm(
        self, remote_id: str, *, target_at_utc: datetime, operation_key: str
    ) -> PublicationSnapshot: ...

    def cancel(self, remote_id: str, *, operation_key: str) -> PublicationSnapshot: ...

class FFmpegSmokeVideoFactory:
    """Create a tiny deterministic native-video fixture outside the repository."""

    def create(self, config: SmmAgentConfig) -> Path:
        output = configured_path(config.runtime.data_root) / "smoke" / "telegram-smoke.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = configured_path(config.media.ffmpeg_path)
        if not ffmpeg.is_file():
            raise RuntimeError("Настроенный FFmpeg отсутствует.")
        completed = run_bounded(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x12304a:s=640x360:r=25:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000:duration=2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                "-shortest",
                str(output),
            ],
            timeout_seconds=60,
        )
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
            output.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg не создал синтетический Telegram smoke asset.")
        if output.stat().st_size > MAX_TELEGRAM_VIDEO_BYTES:
            output.unlink(missing_ok=True)
            raise RuntimeError("Синтетический Telegram smoke asset превысил лимит.")
        return output


class _TelegramCredential(TelegramBotCredential):
    def __init__(self, *, secrets: SecretReader, reference: str) -> None:
        self._secrets = secrets
        self._reference = reference

    def bot_token(self) -> str:
        return self._secrets.read(self._reference).reveal()


class WindowsLiveCapabilitySmokeProbes:
    """Run implemented probes and fail closed for remaining Slice 6 checks."""

    def __init__(
        self,
        *,
        secrets: SecretReader | None = None,
        video_factory: SmokeVideoFactory | None = None,
        telegram_publisher_factory: Callable[[SmmAgentConfig], TelegramSmokePublisher]
        | None = None,
        youtube_publisher_factory: Callable[[SmmAgentConfig], YouTubeSmokePublisher]
        | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._secrets = secrets or WindowsCredentialSecretReader()
        self._video_factory = video_factory or FFmpegSmokeVideoFactory()
        self._telegram_publisher_factory = telegram_publisher_factory or (
            lambda config: TelegramPublisher(
                transport=TelegramBotHttpsTransport(
                    credential=_TelegramCredential(
                        secrets=self._secrets,
                        reference=config.telegram.bot_credential_ref,
                    )
                ),
                chat_id=config.telegram.channel_id,
                api_base="https://api.telegram.org",
                task_store=InMemoryTelegramTaskStore(),
            )
        )
        self._youtube_publisher_factory = (
            youtube_publisher_factory or self._new_youtube_publisher
        )
        self._now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _pending(name: str) -> SmokeProbeResult:
        return SmokeProbeResult(
            passed=False,
            message=f"{name} live probe ещё не подключён.",
            remediation="Завершите отдельный non-production tracer bullet для этой capability.",
        )

    def _new_youtube_publisher(self, config: SmmAgentConfig) -> YouTubeSmokePublisher:
        client_secret = self._secrets.read(config.youtube.client_secret_credential_ref)
        refresher = StdlibOAuthTokenRefresher(
            token_url="https://oauth2.googleapis.com/token",
            client_id=config.youtube.oauth_client_id,
            client_secret=client_secret,
        )
        credential = RefreshingOAuthCredential(
            secret_reader=self._secrets,
            credential_reference=config.youtube.credential_ref,
            refresher=refresher,
        )
        receipt_path = (
            configured_path(config.runtime.data_root) / "smoke" / "youtube-receipts.json"
        )
        return YouTubePublisher(
            transport=RedactingHttpsTransport(credential=credential, timeout_seconds=60),
            channel_id=config.youtube.channel_id,
            receipt_store=JsonYouTubeUploadReceiptStore(receipt_path),
            processing_max_polls=30,
            processing_poll=lambda _attempt: time.sleep(2),
        )

    def youtube_private_publish_at_readback(self, config: SmmAgentConfig) -> SmokeProbeResult:
        """Upload private synthetic video, verify publishAt readback, then delete it."""

        now = self._now().astimezone(UTC)
        target = (now + timedelta(days=2)).replace(microsecond=0)
        run_key = now.strftime("%Y%m%dT%H%M%S%fZ")
        publisher: YouTubeSmokePublisher | None = None
        remote_id: str | None = None
        armed: PublicationSnapshot | None = None
        failure: str | None = None
        try:
            video_path = self._video_factory.create(config)
            video_sha256 = _sha256_file(video_path)
            payload: dict[str, object] = {
                "master": {
                    "path": str(video_path),
                    "sha256": video_sha256,
                    "media_type": "video/mp4",
                },
                "metadata": {
                    "title": f"SMM Agent smoke {run_key}",
                    "description": "Закрытая техническая проверка отложенной публикации.",
                    "tags": ["smm-agent-smoke"],
                },
            }
            payload_sha256 = hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            request = youtube_request(
                release_id=f"smoke-{run_key}",
                schedule_key=target.isoformat(),
                payload_sha256=payload_sha256,
                payload=payload,
            )
            publisher = self._youtube_publisher_factory(config)
            prepared = publisher.prepare(request)
            remote_id = prepared.remote_id
            publisher.preflight(remote_id, payload_sha256=payload_sha256)
            armed = publisher.arm(
                remote_id,
                target_at_utc=target,
                operation_key=f"smoke-arm:{run_key}",
            )
            if armed.state != "armed" or armed.target_at_utc != target:
                failure = "YouTube не подтвердил точный publishAt readback."
        except ProviderOperationError as error:
            failure = f"YouTube smoke не подтверждён: {error.code}."
        except Exception:
            failure = "YouTube smoke не завершён."

        cleanup_confirmed = False
        if publisher is not None and remote_id is not None:
            try:
                cleanup = publisher.cancel(remote_id, operation_key=f"smoke-delete:{run_key}")
                cleanup_confirmed = cleanup.state == "cancelled"
            except Exception:
                cleanup_confirmed = False
        if not cleanup_confirmed and remote_id is not None:
            return SmokeProbeResult(
                passed=False,
                message="YouTube test video не удалось удалить после readback.",
                remediation="Удалите закрытое тестовое видео вручную и проверьте права канала.",
            )
        if failure is not None:
            return SmokeProbeResult(
                passed=False,
                message=failure,
                remediation="Проверьте учётные данные, quota и доступ к выбранному каналу.",
            )
        if armed is None or remote_id is None:
            return SmokeProbeResult(
                passed=False,
                message="YouTube smoke не вернул проверяемый receipt.",
                remediation="Повторите проверку с новым run identity.",
            )
        return SmokeProbeResult(
            passed=True,
            message="YouTube подтвердил private upload и точный publishAt readback.",
            evidence={
                "video_id": remote_id,
                "target_at_utc": target.isoformat(),
                "cleanup": "deleted",
            },
        )

    def dzen_draft_schedule_url(self, config: SmmAgentConfig) -> SmokeProbeResult:
        del config
        return SmokeProbeResult(
            passed=False,
            message="Dzen production adapter live verification is pending.",
            remediation="Bind verified Studio operations before running another live smoke.",
        )

    def windows_task_scheduler_registration_wake(
        self, config: SmmAgentConfig
    ) -> SmokeProbeResult:
        del config
        return self._pending("Task Scheduler wake")

    def telegram_test_send(self, config: SmmAgentConfig) -> SmokeProbeResult:
        """Send one synthetic native video to the configured test channel."""

        try:
            video_path = self._video_factory.create(config)
            video_sha256 = _sha256_file(video_path)
            now = self._now().astimezone(UTC)
            run_key = now.strftime("%Y%m%dT%H%M%S%fZ")
            payload: dict[str, object] = {
                "video": {"path": str(video_path), "sha256": video_sha256},
                "caption": _TELEGRAM_SMOKE_CAPTION,
            }
            payload_sha256 = hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            request = telegram_request(
                release_id=f"smoke-{run_key}",
                schedule_key=run_key,
                payload_sha256=payload_sha256,
                payload=payload,
            )
            publisher = self._telegram_publisher_factory(config)
            prepared = publisher.prepare(request)
            publisher.preflight(prepared.remote_id, payload_sha256=payload_sha256)
            operation_key = f"smoke:{run_key}"
            publisher.arm(
                prepared.remote_id,
                target_at_utc=now,
                operation_key=operation_key,
            )
            receipt = publisher.execute(
                prepared.remote_id,
                operation_key=operation_key,
                now=now,
            )
        except ProviderOperationError as error:
            return SmokeProbeResult(
                passed=False,
                message=f"Telegram test send не подтверждён: {error.code}.",
                remediation="Проверьте test channel, права бота и локальный smoke asset.",
            )
        except Exception:
            return SmokeProbeResult(
                passed=False,
                message="Telegram test send не завершён.",
                remediation="Проверьте FFmpeg, test channel и credential в Windows.",
            )
        if receipt.state != "public" or receipt.public_at is None:
            return SmokeProbeResult(
                passed=False,
                message="Telegram не подтвердил public receipt тестового видео.",
                remediation="Проверьте test channel и повторите smoke с новым run identity.",
            )
        return SmokeProbeResult(
            passed=True,
            message="Telegram подтвердил native-video post в настроенном тестовом канале.",
            evidence={
                "receipt_id": receipt.remote_id,
                "video_bytes": str(video_path.stat().st_size),
                "public_at_utc": receipt.public_at.astimezone(UTC).isoformat(),
            },
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


