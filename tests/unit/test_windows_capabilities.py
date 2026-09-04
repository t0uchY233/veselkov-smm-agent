from datetime import UTC, datetime
from pathlib import Path

import pytest

from smm_agent.adapters.secrets.credential_manager import CredentialAvailability
from smm_agent.adapters.windows.task_scheduler import (
    TaskScheduleSpec,
    render_task_xml,
    task_name_for_release,
)
from smm_agent.application.capability_service import validate_capabilities
from smm_agent.platform.config import SmmAgentConfig


class AvailableCredentialStore:
    def inspect(self, reference: str) -> CredentialAvailability:
        assert reference.startswith("windows-credential:")
        return CredentialAvailability(state="available", message="Credential found.")


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
    for file in (root / "ffmpeg.exe", root / "model.bin", root / "author-left.json"):
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
                "asr_asset": str(root / "model.bin"),
                "crop_profile": str(root / "author-left.json"),
            },
            "schedule": {
                "task_folder": "\\VeselkovSmm",
                "worker_task_name": "worker",
                "run_as_user": "SERGEY-LAPTOP\\setup",
                "preflight_offset_minutes": 30,
            },
            "youtube": {
                "channel_id": "youtube-channel",
                "credential_ref": "windows-credential:VeselkovSmmAgent/YouTubeOAuth",
            },
            "dzen": {
                "channel_url": "https://dzen.ru/ekonomikadliavseh",
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


def test_capability_report_is_typed_and_does_not_claim_live_production(tmp_path: Path) -> None:
    report = validate_capabilities(
        _configured_resources(tmp_path),
        config_path=tmp_path / "smm-agent.toml",
        credential_store=AvailableCredentialStore(),
        is_windows=True,
    )

    assert report.schema_valid is True
    assert report.local_foundation_ready is True
    assert report.production_readiness == "not_assessed"
    assert {item.name for item in report.capabilities} >= {
        "runtime.data_root",
        "dzen.browser_profile",
        "windows.task_scheduler",
        "youtube.credential_ref",
        "telegram.bot_credential_ref",
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
