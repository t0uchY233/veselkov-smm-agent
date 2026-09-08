"""Deterministic, non-mutating validation of local setup capabilities."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

from smm_agent.adapters.secrets.credential_manager import (
    CredentialStore,
    WindowsCredentialManager,
)
from smm_agent.adapters.windows.security import WindowsAclInspector, WindowsSecurityInspector
from smm_agent.contracts.setup import CapabilityCheck, CapabilityReport
from smm_agent.platform.config import SmmAgentConfig


class RuntimeCapabilityUnavailable(RuntimeError):
    """Configuration exists but cannot safely start a live worker."""


class CapabilityService:
    """Inspect configured local prerequisites without starting a provider or task.

    This is intentionally a setup boundary: it never creates directories,
    registers a task, opens a browser profile, or reads a secret value.
    """

    def __init__(
        self,
        *,
        credential_store: CredentialStore | None = None,
        security_inspector: WindowsSecurityInspector | None = None,
        is_windows: bool | None = None,
    ) -> None:
        self._is_windows = os.name == "nt" if is_windows is None else is_windows
        self._credential_store = credential_store or WindowsCredentialManager(
            is_windows=self._is_windows
        )
        self._security_inspector = security_inspector or WindowsAclInspector(
            is_windows=self._is_windows
        )

    def validate(self, config: SmmAgentConfig, *, config_path: Path) -> CapabilityReport:
        checks = (
            self._directory("runtime.data_root", config.runtime.data_root, required=True),
            self._directory("files.recording_inbox", config.files.recording_inbox, required=True),
            self._directory(
                "files.portrait_reference_dir", config.files.portrait_reference_dir, required=True
            ),
            self._file("media.ffmpeg_path", config.media.ffmpeg_path),
            self._file("media.ffprobe_path", config.media.ffprobe_path),
            self._file("media.asr_asset", config.media.asr_asset),
            self._file("media.calibration_corpus", config.media.calibration_corpus),
            self._file("media.crop_profile", config.media.crop_profile),
            self._file("schedule.worker_executable", config.schedule.worker_executable),
            self._directory("dzen.browser_profile", config.dzen.browser_profile, required=True),
            CapabilityCheck(
                name="dzen.author_identity",
                state="available",
                message="Dzen author identity прошла schema validation и привязана к config.",
            ),
            self._log_directory(config.observability.log_path),
            self._scheduler(),
            *self._security_inspector.inspect(
                data_root=config.runtime.data_root,
                browser_profile=config.dzen.browser_profile,
                run_as_user=config.schedule.run_as_user,
            ),
            self._credential("youtube.credential_ref", config.youtube.credential_ref),
            self._credential("telegram.bot_credential_ref", config.telegram.bot_credential_ref),
            self._credential(
                "schedule.task_credential_ref", config.schedule.task_credential_ref
            ),
            self._backup_directory(config.backup.backup_root),
            CapabilityCheck(
                name="delivery.identity",
                state="available",
                message="Версия приложения и commit SHA прошли schema validation.",
            ),
            CapabilityCheck(
                name="codex.policy_identity",
                state="available",
                message="Идентификаторы TOV и Humanize skill прошли schema validation.",
            ),
        )
        return CapabilityReport(
            configPath=str(config_path),
            configSchemaVersion=config.schema_version,
            schemaValid=True,
            localFoundationReady=all(check.state != "unavailable" for check in checks),
            capabilities=checks,
        )

    def _directory(self, name: str, value: str, *, required: bool) -> CapabilityCheck:
        foreign = self._foreign_windows_path(name, value)
        if foreign is not None:
            return foreign
        path = Path(value)
        if path.is_dir():
            return CapabilityCheck(
                name=name,
                state="available",
                message="Явно указанная папка доступна.",
            )
        remediation = (
            "Выберите существующую папку на ноутбуке и укажите абсолютный путь в config.toml."
        )
        if not required:
            remediation = "Создайте папку или укажите существующий абсолютный путь в config.toml."
        return CapabilityCheck(
            name=name,
            state="unavailable",
            message="Папка недоступна.",
            remediation=remediation,
        )

    def _file(self, name: str, value: str) -> CapabilityCheck:
        foreign = self._foreign_windows_path(name, value)
        if foreign is not None:
            return foreign
        if Path(value).is_file():
            return CapabilityCheck(
                name=name,
                state="available",
                message="Явно указанный файл доступен.",
            )
        return CapabilityCheck(
            name=name,
            state="unavailable",
            message="Файл недоступен.",
            remediation=(
                "Укажите существующий абсолютный путь к принятому runtime asset в config.toml."
            ),
        )

    def _log_directory(self, value: str) -> CapabilityCheck:
        foreign = self._foreign_windows_path("observability.log_path", value)
        if foreign is not None:
            return foreign
        if Path(value).parent.is_dir():
            return CapabilityCheck(
                name="observability.log_path",
                state="available",
                message="Папка для log path доступна; файл создаётся только при запуске worker.",
            )
        return CapabilityCheck(
            name="observability.log_path",
            state="unavailable",
            message="Папка для log path недоступна.",
            remediation="Создайте родительскую папку и сохраните явный log_path в config.toml.",
        )

    def _scheduler(self) -> CapabilityCheck:
        if self._is_windows:
            return CapabilityCheck(
                name="windows.task_scheduler",
                state="warning",
                message=(
                    "Windows host обнаружен, но registration и wake-from-sleep "
                    "ещё не подтверждены отдельным live smoke."
                ),
                remediation=(
                    "Зарегистрируйте non-production task и подтвердите его wake smoke "
                    "перед production scheduling."
                ),
            )
        return CapabilityCheck(
            name="windows.task_scheduler",
            state="unavailable",
            message="Windows Task Scheduler недоступен вне Windows.",
            remediation="Запустите setup validate на ноутбуке с Windows 11 Сергея Николаевича.",
        )

    def _credential(self, name: str, reference: str) -> CapabilityCheck:
        result = self._credential_store.inspect(reference)
        return CapabilityCheck(
            name=name,
            state=result.state,
            message=result.message,
            remediation=(
                None
                if result.available
                else (
                    "Добавьте credential в Windows Credential Manager и сохраните только его "
                    "reference в config.toml."
                )
            ),
        )

    def _backup_directory(self, value: str) -> CapabilityCheck:
        foreign = self._foreign_windows_path("backup.backup_root", value)
        if foreign is not None:
            return CapabilityCheck(
                name="backup.backup_root",
                state="warning",
                message=(
                    "Backup root нельзя проверить вне Windows; обновление production останется "
                    "заблокировано."
                ),
                remediation="Запустите setup validate на целевом Windows ноутбуке.",
            )
        if Path(value).is_dir():
            return CapabilityCheck(
                name="backup.backup_root",
                state="available",
                message="Явно указанная backup папка доступна.",
            )
        return CapabilityCheck(
            name="backup.backup_root",
            state="warning",
            message="Backup root недоступен: update production будет заблокирован.",
            remediation="Выберите существующую backup папку и повторите setup validate.",
        )

    def _foreign_windows_path(self, name: str, value: str) -> CapabilityCheck | None:
        if PureWindowsPath(value).is_absolute() and not self._is_windows:
            return CapabilityCheck(
                name=name,
                state="unavailable",
                message="Windows-путь нельзя проверить на текущем non-Windows host.",
                remediation="Проверьте этот config.toml на целевом Windows ноутбуке.",
            )
        return None


def validate_capabilities(
    config: SmmAgentConfig,
    *,
    config_path: Path,
    credential_store: CredentialStore | None = None,
    security_inspector: WindowsSecurityInspector | None = None,
    is_windows: bool | None = None,
) -> CapabilityReport:
    """Convenience entry point for CLI and focused tests."""
    return CapabilityService(
        credential_store=credential_store,
        security_inspector=security_inspector,
        is_windows=is_windows,
    ).validate(config, config_path=config_path)


def require_worker_capabilities(report: CapabilityReport) -> None:
    """Reject startup when a configured local prerequisite is unavailable.

    A warning (notably the still-unproven scheduler wake) remains visible and
    cannot be mistaken for production approval: live provider composition has
    a separate explicit factory gate.
    """

    unavailable = [check.name for check in report.capabilities if check.state == "unavailable"]
    if unavailable:
        raise RuntimeCapabilityUnavailable(
            "Live worker заблокирован: недоступны capability " + ", ".join(unavailable) + "."
        )
