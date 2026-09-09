from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from smm_agent.adapters.capability_smoke import WindowsLiveCapabilitySmokeProbes
from smm_agent.adapters.publishing.dzen import DzenPublisher
from smm_agent.adapters.publishing.dzen_receipts import JsonDzenReceiptStore
from smm_agent.adapters.secrets.credential_manager import CredentialAvailability
from smm_agent.adapters.secrets.secret_reader import SecretValue
from smm_agent.adapters.windows.security import _decode_windows_command_stdout
from smm_agent.adapters.windows.task_scheduler import (
    CommandResult,
    TaskScheduleSpec,
    WindowsTaskScheduler,
    build_release_task_plans,
    render_task_xml,
    task_name_for_release,
)
from smm_agent.application.capability_service import validate_capabilities
from smm_agent.application.capability_smoke_service import (
    SmokeProbeResult,
    run_capability_smoke,
)
from smm_agent.application.provider_factory import RuntimeCompositionError, WindowsProviderFactory
from smm_agent.contracts.publication import (
    PreparedPublication,
    PublicationRequest,
    PublicationSnapshot,
)
from smm_agent.contracts.setup import CapabilityCheck
from smm_agent.platform.config import SmmAgentConfig
from smm_agent.platform.db import Database


class AvailableCredentialStore:
    def inspect(self, reference: str) -> CredentialAvailability:
        assert reference.startswith("windows-credential:")
        return CredentialAvailability(state="available", message="Credential found.")


class AvailableSecurityInspector:
    def inspect(
        self, *, data_root: str, browser_profile: str, run_as_user: str
    ) -> tuple[CapabilityCheck, ...]:
        del data_root, browser_profile, run_as_user
        return (
            CapabilityCheck(
                name="windows.current_account", state="available", message="Account matches."
            ),
            CapabilityCheck(
                name="windows.data_root_acl", state="available", message="ACL ok."
            ),
            CapabilityCheck(
                name="windows.dzen_profile_acl", state="available", message="ACL ok."
            ),
        )


class PassingSmokeProbes:
    def _result(self) -> SmokeProbeResult:
        return SmokeProbeResult(
            passed=True,
            message="Synthetic test resource confirmed.",
            evidence={
                "target_at_utc": "2026-09-04T11:00:00Z",
                "token": "must-not-leak",
            },
        )

    def youtube_private_publish_at_readback(self, config: SmmAgentConfig) -> SmokeProbeResult:
        del config
        return self._result()

    def dzen_draft_schedule_url(self, config: SmmAgentConfig) -> SmokeProbeResult:
        del config
        return self._result()

    def telegram_test_send(self, config: SmmAgentConfig) -> SmokeProbeResult:
        del config
        return self._result()

    def windows_task_scheduler_registration_wake(self, config: SmmAgentConfig) -> SmokeProbeResult:
        del config
        return self._result()


class FakeSmokeVideoFactory:
    def __init__(self, path: Path) -> None:
        self.path = path

    def create(self, config: SmmAgentConfig) -> Path:
        del config
        return self.path


class FakeTelegramSmokePublisher:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.payload_sha256 = ""

    def prepare(self, request: PublicationRequest) -> PreparedPublication:
        self.calls.append("prepare")
        self.payload_sha256 = request.payload_sha256
        return PreparedPublication(
            platform="telegram",
            state="prepared",
            remote_id="smoke-task",
            payload_sha256=request.payload_sha256,
        )

    def preflight(self, remote_id: str, *, payload_sha256: str) -> None:
        assert remote_id == "smoke-task"
        assert payload_sha256 == self.payload_sha256
        self.calls.append("preflight")

    def arm(
        self, remote_id: str, *, target_at_utc: datetime, operation_key: str
    ) -> PublicationSnapshot:
        self.calls.append("arm")
        return PublicationSnapshot(
            platform="telegram",
            state="armed",
            remote_id=remote_id,
            payload_sha256=self.payload_sha256,
            target_at_utc=target_at_utc,
        )

    def execute(
        self, remote_id: str, *, operation_key: str, now: datetime
    ) -> PublicationSnapshot:
        del operation_key
        self.calls.append("execute")
        return PublicationSnapshot(
            platform="telegram",
            state="public",
            remote_id=remote_id,
            payload_sha256=self.payload_sha256,
            target_at_utc=now,
            public_at=now,
        )
class FakeSecretReader:
    def read(self, reference: str) -> SecretValue:
        assert reference.startswith("windows-credential:")
        return SecretValue("test-only-secret")


class FakeDzenSession:
    def goto(self, url: str) -> None:
        del url

    def is_visible(self, selector: str) -> bool:
        del selector
        return True

    def text_content(self, selector: str) -> str | None:
        del selector
        return None

    def get_attribute(self, selector: str, name: str) -> str | None:
        del selector, name
        return None

    def fill(self, selector: str, value: str) -> None:
        del selector, value

    def click(self, selector: str) -> None:
        del selector

    def set_input_files(self, selector: str, paths: list[Path]) -> None:
        del selector, paths

    def screenshot(self, path: Path) -> None:
        del path


class FakeHttpTransport:
    def send(self, request: object) -> object:
        raise AssertionError(f"provider I/O was not expected: {request!r}")


class FakeAlertTransport:
    def lookup(self, *, notification_id: str, recipient: str) -> None:
        del notification_id, recipient
        return None

    def send(self, *, request: object, body: str) -> object:
        raise AssertionError(f"alert I/O was not expected: {request!r} {body!r}")


class FakeProviderBindings:
    def youtube_transport(
        self, *, config: SmmAgentConfig, secrets: FakeSecretReader
    ) -> FakeHttpTransport:
        del config, secrets
        return FakeHttpTransport()

    def dzen_session(self, *, config: SmmAgentConfig) -> FakeDzenSession:
        assert config.dzen.author_identity == "veselkoveconomy"
        return FakeDzenSession()

    def alert_transport(
        self, *, config: SmmAgentConfig, secrets: FakeSecretReader
    ) -> FakeAlertTransport:
        del config, secrets
        return FakeAlertTransport()

def _configured_resources(root: Path) -> SmmAgentConfig:
    for directory in (
        root / "runtime",
        root / "recordings",
        root / "portraits",
        root / "dzen-profile",
        root / "logs",
        root / "backups",
    ):
        directory.mkdir()
    for file in (
        root / "ffmpeg.exe",
        root / "ffprobe.exe",
        root / "model.bin",
        root / "calibration.json",
        root / "author-left.json",
        root / "smm-worker.exe",
    ):
        file.touch()
    return SmmAgentConfig.model_validate(
        {
            "schema_version": "1.0",
            "runtime": {"data_root": str(root / "runtime"), "timezone": "Europe/Moscow"},
            "files": {
                "recording_inbox": str(root / "recordings"),
                "portrait_reference_dir": str(root / "portraits"),
            },
            "media": {
                "ffmpeg_path": str(root / "ffmpeg.exe"),
                "ffprobe_path": str(root / "ffprobe.exe"),
                "asr_asset": str(root / "model.bin"),
                "calibration_corpus": str(root / "calibration.json"),
                "crop_profile": str(root / "author-left.json"),
            },
            "schedule": {
                "task_folder": "\\VeselkovSmm",
                "worker_task_name": "worker",
                "worker_executable": str(root / "smm-worker.exe"),
                "run_as_user": "SERGEY-LAPTOP\\setup",
                "task_credential_ref": "windows-credential:VeselkovSmmAgent/TaskAccount",
                "preflight_offset_minutes": 30,
            },
            "youtube": {
                "channel_id": "youtube-channel",
                "oauth_client_id": "1234567890-testclient.apps.googleusercontent.com",
                "client_secret_credential_ref": (
                    "windows-credential:VeselkovSmmAgent/YouTubeClientSecret"
                ),
                "credential_ref": "windows-credential:VeselkovSmmAgent/YouTubeOAuth",
            },
            "dzen": {
                "channel_url": "https://dzen.ru/ekonomikadliavseh",
            "publisher_id": "64dca43ac311451c1a90cbd7",
                "author_identity": "veselkoveconomy",
                "browser_profile": str(root / "dzen-profile"),
            },
            "telegram": {
                "channel_id": "@veselkoveconomy",
                "bot_credential_ref": "windows-credential:VeselkovSmmAgent/TelegramBot",
                "alert_recipient_id": "276042853",
            },
            "codex": {
                "humanizer_skill_version": "humanizer-lock-sha",
                "tone_of_voice_sha256": "a" * 64,
            },
            "observability": {
                "log_path": str(root / "logs" / "agent.jsonl"),
                "retention_days": 90,
            },
            "delivery": {"app_version": "0.1.0", "commit_sha": "abcdef0"},
            "backup": {"backup_root": str(root / "backups")},
        }
    )


def test_icacls_output_uses_windows_oem_encoding_for_cyrillic_account() -> None:
    raw = "DESKTOP-R00H9C9\\Ассистент:(OI)(CI)(M)".encode("cp866")

    decoded = _decode_windows_command_stdout(
        "icacls.exe", raw, is_windows=True, oem_encoding="cp866"
    )

    assert decoded == "DESKTOP-R00H9C9\\Ассистент:(OI)(CI)(M)"


def test_capability_report_is_typed_and_does_not_claim_live_production(tmp_path: Path) -> None:
    report = validate_capabilities(
        _configured_resources(tmp_path),
        config_path=tmp_path / "smm-agent.toml",
        credential_store=AvailableCredentialStore(),
        security_inspector=AvailableSecurityInspector(),
        is_windows=True,
    )

    assert report.schema_valid is True
    assert report.local_foundation_ready is True
    assert report.production_readiness == "not_assessed"
    assert {item.name for item in report.capabilities} >= {
        "runtime.data_root",
        "dzen.browser_profile",
        "dzen.author_identity",
        "windows.task_scheduler",
        "youtube.client_secret_credential_ref",
        "youtube.credential_ref",
        "telegram.bot_credential_ref",
        "schedule.task_credential_ref",
        "windows.current_account",
    }


def test_non_windows_reports_scheduler_and_secret_store_unavailable(tmp_path: Path) -> None:
    report = validate_capabilities(
        _configured_resources(tmp_path),
        config_path=tmp_path / "smm-agent.toml",
        is_windows=False,
    )
    checks = {item.name: item for item in report.capabilities}

    assert report.local_foundation_ready is False
    assert checks["windows.task_scheduler"].state == "unavailable"
    assert checks["youtube.client_secret_credential_ref"].state == "unavailable"
    assert checks["youtube.credential_ref"].state == "unavailable"
    assert "secret не читался" in checks["youtube.credential_ref"].message


def test_task_scheduler_xml_is_utc_wakeable_logged_off_and_non_live() -> None:
    name = task_name_for_release("018f14b7-4d8e-7e00-a1bb-123456789abc", "telegram")
    specification = TaskScheduleSpec(
        task_name=name,
        target_at=datetime(2026, 9, 4, 11, 0, tzinfo=UTC),
        executable=r"C:\VeselkovSmm\bin\smmctl.exe",
        arguments=("release", "publish", "--release-id", "abc def"),
        working_directory=r"C:\VeselkovSmm",
        run_as_user=r"SERGEY-LAPTOP\setup",
        description="Deliver Telegram at target",
        purpose="telegram",
    )

    plan = render_task_xml(specification)

    assert plan.live_registration is False
    assert plan.task_name == name
    assert "<WakeToRun>true</WakeToRun>" in plan.xml
    assert "<LogonType>Password</LogonType>" in plan.xml
    assert "<UserId>SERGEY-LAPTOP\\setup</UserId>" in plan.xml
    assert "2026-09-04T11:00:00Z" in plan.xml
    assert '"abc def"' in plan.xml
    assert "schtasks.exe" not in plan.xml
    assert plan.command_argv == (
        r"C:\VeselkovSmm\bin\smmctl.exe",
        "release",
        "publish",
        "--release-id",
        "abc def",
    )


def test_task_names_and_task_specs_reject_unsafe_input() -> None:
    with pytest.raises(ValueError, match="release_id"):
        task_name_for_release("release;whoami", "telegram")

    with pytest.raises(ValueError, match="working_directory"):
        TaskScheduleSpec(
            task_name=r"\VeselkovSmm\worker",
            target_at=datetime.now(UTC),
            executable=r"C:\VeselkovSmm\bin\smm-worker.exe",
            arguments=(),
            working_directory="relative",
            run_as_user=r"SERGEY-LAPTOP\setup",
            description="worker",
            purpose="worker",
        )


def test_release_task_plans_bind_t_minus_30_and_target_to_one_release() -> None:
    plans = build_release_task_plans(
        release_id="018f14b7-4d8e-7e00-a1bb-123456789abc",
        target_at=datetime(2026, 9, 4, 11, 0, tzinfo=UTC),
        preflight_offset_minutes=30,
        task_folder=r"\VeselkovSmm",
        worker_executable=r"C:\VeselkovSmm\current\smm-worker.exe",
        config_path=r"C:\VeselkovSmm\config\smm-agent.toml",
        run_as_user=r"SERGEY-LAPTOP\setup",
        working_directory=r"C:\VeselkovSmm",
    )

    assert plans.preflight.task_name.endswith("-preflight")
    assert plans.target.task_name.endswith("-telegram")
    assert "2026-09-04T10:30:00Z" in plans.preflight.xml
    assert "2026-09-04T11:00:00Z" in plans.target.xml
    assert plans.preflight.command_argv[1:] == (
        "--config",
        r"C:\VeselkovSmm\config\smm-agent.toml",
        "--once",
        "--scheduled-task",
        plans.preflight.task_name,
    )


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments: Sequence[str]) -> CommandResult:
        self.calls.append(tuple(arguments))
        return CommandResult(returncode=0)


def test_scheduler_registration_and_deletion_use_exact_list_argv_without_execution() -> None:
    runner = RecordingRunner()
    scheduler = WindowsTaskScheduler(runner=runner, is_windows=True)
    plan = render_task_xml(
        TaskScheduleSpec(
            task_name=r"\VeselkovSmm\worker",
            target_at=datetime(2026, 9, 4, 11, 0, tzinfo=UTC),
            executable=r"C:\VeselkovSmm\bin\smm-worker.exe",
            arguments=("--config", r"C:\VeselkovSmm\config\smm-agent.toml", "--once"),
            working_directory=r"C:\VeselkovSmm",
            run_as_user=r"SERGEY-LAPTOP\setup",
            description="worker",
            purpose="worker",
        )
    )

    registered = scheduler.register(plan, task_password="not-persisted")
    deleted = scheduler.delete(plan.task_name)

    assert registered.action == "registered"
    assert deleted.action == "deleted"
    assert runner.calls[0][:5] == ("schtasks.exe", "/Create", "/TN", plan.task_name, "/XML")
    assert runner.calls[0][-5:] == ("/RU", plan.run_as_user, "/RP", "not-persisted", "/F")
    assert runner.calls[1] == ("schtasks.exe", "/Delete", "/TN", plan.task_name, "/F")


def test_capability_smoke_redacts_evidence_and_stays_production_blocked(tmp_path: Path) -> None:
    report = run_capability_smoke(
        _configured_resources(tmp_path),
        config_path=str(tmp_path / "smm-agent.toml"),
        execute=True,
        probes=PassingSmokeProbes(),
    )

    assert report.all_required_smokes_passed is True
    assert report.production_readiness == "blocked"
    assert {check.state for check in report.capabilities} == {"passed"}
    assert all(
        item.key != "token" for check in report.capabilities for item in check.evidence
    )


def test_windows_live_telegram_smoke_uses_synthetic_asset_and_confirms_receipt(
    tmp_path: Path,
) -> None:
    video = tmp_path / "telegram-smoke.mp4"
    video.write_bytes(b"synthetic-video")
    publisher = FakeTelegramSmokePublisher()
    probes = WindowsLiveCapabilitySmokeProbes(
        video_factory=FakeSmokeVideoFactory(video),
        telegram_publisher_factory=lambda config: publisher,
        now=lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
    )

    result = probes.telegram_test_send(_configured_resources(tmp_path))

    assert result.passed is True
    assert publisher.calls == ["prepare", "preflight", "arm", "execute"]
    assert result.evidence is not None
    assert result.evidence["video_bytes"] == str(len(b"synthetic-video"))
def test_provider_factory_requires_explicit_production_gate_and_uses_injected_seams(
    tmp_path: Path,
) -> None:
    config = _configured_resources(tmp_path)
    database = Database(tmp_path / "runtime")
    database.initialize()
    bindings = FakeProviderBindings()

    with pytest.raises(RuntimeCompositionError, match="заблокирована"):
        WindowsProviderFactory(bindings=bindings).create(
            config=config,
            database=database,
            secrets=FakeSecretReader(),
        )

    runtime = WindowsProviderFactory(bindings=bindings, production_ready=True).create(
        config=config,
        database=database,
        secrets=FakeSecretReader(),
    )

    assert set(runtime.publishers) == {"youtube", "dzen", "telegram"}
    assert runtime.capability_label == "windows-live-provider-bindings"

    dzen = runtime.publishers["dzen"]
    assert isinstance(dzen, DzenPublisher)
    assert dzen._page.expected_identity is not None
    assert dzen._page.expected_identity.channel_url == config.dzen.channel_url
    assert dzen._page.expected_identity.author_identity == config.dzen.author_identity
    assert isinstance(dzen._receipt_store, JsonDzenReceiptStore)
    assert dzen._receipt_store._path == database.data_root / "state/dzen-receipts.json"


def test_selected_smoke_does_not_claim_all_required_capabilities(tmp_path: Path) -> None:
    report = run_capability_smoke(
        _configured_resources(tmp_path),
        config_path=str(tmp_path / "smm-agent.toml"),
        execute=True,
        names=("telegram.test_send",),
        probes=PassingSmokeProbes(),
    )
    assert report.all_required_smokes_passed is False
    assert report.production_readiness == "blocked"
    assert sum(check.state == "passed" for check in report.capabilities) == 1
