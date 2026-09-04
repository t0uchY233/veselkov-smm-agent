"""Versioned, explicit local configuration for the Windows deployment.

This module deliberately does not invent a location for any machine-owned
resource.  Environment variables still provide a portable development data
root for the existing CLI, but production setup is driven by a versioned TOML
file whose paths and secret *references* are supplied by its operator.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

CONFIG_SCHEMA_VERSION = "1.0"
CREDENTIAL_REFERENCE_PREFIX = "windows-credential:"


def default_data_root() -> Path:
    """Return the portable development root used by legacy CLI commands only."""
    explicit = os.environ.get("SMM_AGENT_DATA_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "VeselkovSmm"

    return Path.cwd() / ".runtime"


def _explicit_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Путь должен быть строкой.")
    if not value or value != value.strip():
        raise ValueError("Путь не может быть пустым или иметь пробелы по краям.")
    if value.startswith("~") or "$" in value or "%" in value:
        raise ValueError("Путь должен быть указан явно, без переменных среды и ~.")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("Путь содержит недопустимый управляющий символ.")
    if not Path(value).is_absolute() and not PureWindowsPath(value).is_absolute():
        raise ValueError("Путь должен быть абсолютным и выбранным оператором.")
    return value


def _credential_reference(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Ссылка на credential должна быть строкой.")
    if not value.startswith(CREDENTIAL_REFERENCE_PREFIX):
        raise ValueError(
            "В config допускается только ссылка windows-credential:<target>, а не secret value."
        )
    target = value.removeprefix(CREDENTIAL_REFERENCE_PREFIX)
    if not target or len(target) > 256 or any(char in target for char in "\r\n\x00"):
        raise ValueError("Credential target пустой или содержит недопустимые символы.")
    return value


type ExplicitPath = Annotated[str, BeforeValidator(_explicit_path)]
type CredentialReference = Annotated[str, BeforeValidator(_credential_reference)]


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeConfig(StrictConfigModel):
    data_root: ExplicitPath
    timezone: str

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Нужен существующий IANA timezone, например Europe/Moscow.") from error
        return value


class FilesConfig(StrictConfigModel):
    recording_inbox: ExplicitPath
    portrait_reference_dir: ExplicitPath


class MediaConfig(StrictConfigModel):
    ffmpeg_path: ExplicitPath
    ffprobe_path: ExplicitPath
    asr_asset: ExplicitPath
    calibration_corpus: ExplicitPath
    crop_profile: ExplicitPath


class ScheduleConfig(StrictConfigModel):
    task_folder: str
    worker_task_name: str
    worker_executable: ExplicitPath
    run_as_user: str = Field(min_length=1, max_length=256)
    task_credential_ref: CredentialReference
    preflight_offset_minutes: int = Field(ge=1, le=24 * 60)

    @field_validator("task_folder")
    @classmethod
    def validate_task_folder(cls, value: str) -> str:
        if not re.fullmatch(r"\\[A-Za-z0-9_-]+(?:\\[A-Za-z0-9_-]+)*", value):
            raise ValueError(
                "task_folder должен быть безопасным путём Task Scheduler, например \\VeselkovSmm."
            )
        return value

    @field_validator("worker_task_name")
    @classmethod
    def validate_task_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("worker_task_name допускает только буквы, цифры, _ и -.")
        return value

    @field_validator("run_as_user")
    @classmethod
    def validate_run_as_user(cls, value: str) -> str:
        if value != value.strip() or any(char in value for char in "\x00\r\n"):
            raise ValueError("run_as_user должен быть явно указан без управляющих символов.")
        return value


class YouTubeConfig(StrictConfigModel):
    channel_id: str = Field(min_length=1, max_length=128)
    credential_ref: CredentialReference


class DzenConfig(StrictConfigModel):
    channel_url: str
    author_identity: str = Field(min_length=1, max_length=256)
    browser_profile: ExplicitPath

    @field_validator("channel_url")
    @classmethod
    def validate_channel_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {"dzen.ru", "www.dzen.ru"}:
            raise ValueError("Dzen channel_url должен быть HTTPS URL домена dzen.ru.")
        return value

    @field_validator("author_identity")
    @classmethod
    def validate_author_identity(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\x00\r\n"):
            raise ValueError("Dzen author_identity должен быть явной непустой строкой.")
        return value


class TelegramConfig(StrictConfigModel):
    channel_id: str = Field(min_length=1, max_length=128)
    bot_credential_ref: CredentialReference
    alert_recipient_id: str = Field(min_length=1, max_length=64)

    @field_validator("alert_recipient_id")
    @classmethod
    def validate_alert_recipient(cls, value: str) -> str:
        if not re.fullmatch(r"[1-9][0-9]{0,18}", value):
            raise ValueError("alert_recipient_id должен быть положительным числовым Telegram ID.")
        return value


class CodexConfig(StrictConfigModel):
    humanizer_skill_version: str = Field(min_length=1, max_length=128)
    tone_of_voice_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ObservabilityConfig(StrictConfigModel):
    log_path: ExplicitPath
    retention_days: int = Field(ge=1, le=3650)


class DeliveryConfig(StrictConfigModel):
    app_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    commit_sha: str = Field(pattern=r"^[a-f0-9]{7,64}$")


class BackupConfig(StrictConfigModel):
    backup_root: ExplicitPath


class SmmAgentConfig(StrictConfigModel):
    """The complete production configuration schema.

    A missing setting is rejected by schema validation.  Whether an explicit
    path, credential reference, or Windows capability is usable is reported by
    ``application.capability_service`` rather than silently repaired here.
    """

    schema_version: Literal["1.0"]
    runtime: RuntimeConfig
    files: FilesConfig
    media: MediaConfig
    schedule: ScheduleConfig
    youtube: YouTubeConfig
    dzen: DzenConfig
    telegram: TelegramConfig
    codex: CodexConfig
    observability: ObservabilityConfig
    delivery: DeliveryConfig
    backup: BackupConfig

    @model_validator(mode="after")
    def validate_alert_target(self) -> SmmAgentConfig:
        if self.telegram.alert_recipient_id != "276042853":
            raise ValueError("alert_recipient_id для v1 должен быть 276042853.")
        return self


class ConfigLoadError(ValueError):
    """Raised when a TOML file cannot be parsed into the public config schema."""


def load_config(config_path: Path) -> SmmAgentConfig:
    """Load one UTF-8 TOML config without expanding or writing any path."""
    try:
        with config_path.open("rb") as stream:
            payload = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigLoadError(f"Не удалось прочитать config.toml: {error}") from error
    try:
        return SmmAgentConfig.model_validate(payload)
    except ValidationError as error:
        issues = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_input=False, include_context=False)
        )
        raise ConfigLoadError(
            f"config.toml не соответствует schema {CONFIG_SCHEMA_VERSION}: {issues}"
        ) from error


def credential_target(reference: str) -> str:
    """Extract a validated Credential Manager target without reading a secret."""
    return _credential_reference(reference).removeprefix(CREDENTIAL_REFERENCE_PREFIX)


def configured_path(value: str) -> Path:
    """Return a local path only after the explicit-path invariant was checked.

    ``Path`` intentionally receives the raw absolute spelling: resolving it
    could follow a missing path or guess a machine-specific home directory.
    """
    return Path(_explicit_path(value))
