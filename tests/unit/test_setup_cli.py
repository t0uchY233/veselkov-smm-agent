import json
import os
from pathlib import Path

from typer.testing import CliRunner

from smm_agent.cli.main import app


def _write_config(root: Path) -> Path:
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
    config = root / "smm-agent.toml"
    config.write_text(
        f'''schema_version = "1.0"
[runtime]
data_root = "{root / "runtime"}"
timezone = "Europe/Moscow"
[files]
recording_inbox = "{root / "recordings"}"
portrait_reference_dir = "{root / "portraits"}"
[media]
ffmpeg_path = "{root / "ffmpeg.exe"}"
ffprobe_path = "{root / "ffprobe.exe"}"
asr_asset = "{root / "model.bin"}"
calibration_corpus = "{root / "calibration.json"}"
crop_profile = "{root / "author-left.json"}"
[schedule]
task_folder = "\\\\VeselkovSmm"
worker_task_name = "worker"
worker_executable = "{root / "smm-worker.exe"}"
run_as_user = "SERGEY-LAPTOP\\\\setup"
task_credential_ref = "windows-credential:VeselkovSmmAgent/TaskAccount"
preflight_offset_minutes = 30
[youtube]
channel_id = "youtube-channel"
credential_ref = "windows-credential:VeselkovSmmAgent/YouTubeOAuth"
[dzen]
channel_url = "https://dzen.ru/ekonomikadliavseh"
browser_profile = "{root / "dzen-profile"}"
[telegram]
channel_id = "@veselkoveconomy"
bot_credential_ref = "windows-credential:VeselkovSmmAgent/TelegramBot"
alert_recipient_id = "276042853"
[codex]
humanizer_skill_version = "humanizer-lock-sha"
tone_of_voice_sha256 = "{'a' * 64}"
[observability]
log_path = "{root / "logs" / "agent.jsonl"}"
retention_days = 90
[delivery]
app_version = "0.1.0"
commit_sha = "abcdef0"
[backup]
backup_root = "{root / "backups"}"
''',
        encoding="utf-8",
    )
    return config


def test_setup_validate_emits_capability_report(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["setup", "validate", "--config", str(_write_config(tmp_path))],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["schemaValid"] is True
    assert report["productionReadiness"] == "not_assessed"


def test_setup_login_dzen_requires_headful_windows_user_action(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["setup", "login", "dzen", "--config", str(_write_config(tmp_path))],
    )

    handoff = json.loads(result.output)
    if os.name == "nt":
        assert result.exit_code == 0
        assert handoff["state"] == "requires_headful_windows"
    else:
        assert result.exit_code == 2
        assert handoff["state"] == "unavailable"


def test_capability_smoke_is_non_production_and_never_runs_without_execute(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["capability", "smoke", "--config", str(_write_config(tmp_path))],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["nonProduction"] is True
    assert report["executionRequested"] is False
    assert report["productionReadiness"] == "blocked"
    assert {item["state"] for item in report["capabilities"]} == {"not_run"}
