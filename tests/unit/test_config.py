import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from smm_agent.platform.config import ConfigLoadError, SmmAgentConfig, load_config


def config_payload(root: Path) -> dict[str, object]:
    return {
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
        "observability": {"log_path": str(root / "logs" / "agent.jsonl"), "retention_days": 90},
        "delivery": {"app_version": "0.1.0", "commit_sha": "abcdef0"},
        "backup": {"backup_root": str(root / "backups")},
    }


def test_config_requires_explicit_paths_and_credential_references(tmp_path: Path) -> None:
    payload = config_payload(tmp_path)
    payload["files"] = {
        "recording_inbox": "recordings",
        "portrait_reference_dir": str(tmp_path / "portraits"),
    }
    payload["youtube"] = {"channel_id": "channel", "credential_ref": "secret-value"}

    with pytest.raises(ValidationError) as error:
        SmmAgentConfig.model_validate(payload)

    messages = [item["msg"] for item in error.value.errors(include_input=False)]
    assert any("абсолютным" in message for message in messages)
    assert any("windows-credential" in message for message in messages)


def test_config_pins_v1_alert_recipient(tmp_path: Path) -> None:
    payload = config_payload(tmp_path)
    telegram = payload["telegram"]
    assert isinstance(telegram, dict)
    telegram["alert_recipient_id"] = "999"

    with pytest.raises(ValidationError, match="276042853"):
        SmmAgentConfig.model_validate(payload)


def test_load_config_is_versioned_and_does_not_echo_invalid_input(tmp_path: Path) -> None:
    config_path = tmp_path / "smm-agent.toml"
    config_path.write_text(
        'schema_version = "9.0"\n[youtube]\ncredential_ref = "not-a-secret"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError) as error:
        load_config(config_path)

    message = str(error.value)
    assert "schema_version" in message
    assert "not-a-secret" not in message


def test_load_config_parses_complete_toml_schema(tmp_path: Path) -> None:
    payload = config_payload(tmp_path)
    config_path = tmp_path / "smm-agent.toml"
    lines = [f"schema_version = {json.dumps(payload['schema_version'])}"]
    for group in (
        "runtime",
        "files",
        "media",
        "schedule",
        "youtube",
        "dzen",
        "telegram",
        "codex",
        "observability",
        "delivery",
        "backup",
    ):
        values = payload[group]
        assert isinstance(values, dict)
        lines.append(f"\n[{group}]")
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in values.items())
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    config = load_config(config_path)

    assert config.schema_version == "1.0"
    assert config.runtime.data_root == str(tmp_path / "runtime")
