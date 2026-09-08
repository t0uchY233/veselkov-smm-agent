"""Small, redacted Windows account and NTFS ACL inspection boundary."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from smm_agent.contracts.setup import CapabilityCheck


@dataclass(frozen=True, slots=True)
class SecurityCommandResult:
    returncode: int
    stdout: str = ""


class SecurityCommandRunner(Protocol):
    def run(self, arguments: Sequence[str]) -> SecurityCommandResult: ...


class WindowsSecurityInspector(Protocol):
    """A read-only capability seam for the Windows deployment composition."""

    def inspect(
        self,
        *,
        data_root: str,
        browser_profile: str,
        run_as_user: str,
    ) -> tuple[CapabilityCheck, ...]: ...


def _decode_windows_command_stdout(
    executable: str,
    stdout: bytes,
    *,
    is_windows: bool,
    oem_encoding: str = "oem",
) -> str:
    encoding = (
        oem_encoding
        if is_windows and Path(executable).name.casefold() == "icacls.exe"
        else "utf-8"
    )
    return stdout.decode(encoding, errors="replace")


class SubprocessSecurityCommandRunner:
    def run(self, arguments: Sequence[str]) -> SecurityCommandResult:
        completed = subprocess.run(  # noqa: S603 -- fixed Windows commands, list argv
            list(arguments),
            check=False,
            capture_output=True,
        )
        stdout = _decode_windows_command_stdout(
            arguments[0], completed.stdout, is_windows=os.name == "nt"
        )
        return SecurityCommandResult(returncode=completed.returncode, stdout=stdout)


class WindowsAclInspector:
    """Check account consistency and explicit access to sensitive local roots.

    This never changes ACLs. It reports only generic outcomes: account names and
    raw ``icacls`` output are intentionally absent from the returned contract.
    """

    def __init__(
        self,
        *,
        runner: SecurityCommandRunner | None = None,
        is_windows: bool | None = None,
    ) -> None:
        self._runner = runner or SubprocessSecurityCommandRunner()
        self._is_windows = os.name == "nt" if is_windows is None else is_windows

    def inspect(
        self,
        *,
        data_root: str,
        browser_profile: str,
        run_as_user: str,
    ) -> tuple[CapabilityCheck, ...]:
        if not self._is_windows:
            return (
                CapabilityCheck(
                    name="windows.current_account",
                    state="unavailable",
                    message="Windows account нельзя проверить вне Windows.",
                    remediation="Запустите проверку на целевом Windows ноутбуке.",
                ),
                CapabilityCheck(
                    name="windows.data_root_acl",
                    state="unavailable",
                    message="NTFS ACL data root нельзя проверить вне Windows.",
                    remediation="Запустите проверку на целевом Windows ноутбуке.",
                ),
                CapabilityCheck(
                    name="windows.dzen_profile_acl",
                    state="unavailable",
                    message="NTFS ACL Dzen profile нельзя проверить вне Windows.",
                    remediation="Запустите проверку на целевом Windows ноутбуке.",
                ),
            )

        account_check = self._account_check(run_as_user)
        return (
            account_check,
            self._acl_check("windows.data_root_acl", Path(data_root), run_as_user),
            self._acl_check("windows.dzen_profile_acl", Path(browser_profile), run_as_user),
        )

    def _account_check(self, expected: str) -> CapabilityCheck:
        result = self._runner.run(("whoami.exe",))
        observed = " ".join(result.stdout.split())
        if result.returncode != 0 or not observed:
            return CapabilityCheck(
                name="windows.current_account",
                state="unavailable",
                message="Не удалось определить current Windows account.",
                remediation="Запустите setup и worker под account из schedule.run_as_user.",
            )
        if _normalize_account(observed) != _normalize_account(expected):
            return CapabilityCheck(
                name="windows.current_account",
                state="unavailable",
                message="Current Windows account не совпадает с schedule.run_as_user.",
                remediation=(
                    "Используйте один setup/task account или исправьте config после проверки."
                ),
            )
        return CapabilityCheck(
            name="windows.current_account",
            state="available",
            message="Current Windows account совпадает с task account из config.",
        )

    def _acl_check(self, name: str, path: Path, account: str) -> CapabilityCheck:
        result = self._runner.run(("icacls.exe", str(path)))
        listing = result.stdout.casefold()
        required = _normalize_account(account)
        if result.returncode != 0:
            return CapabilityCheck(
                name=name,
                state="unavailable",
                message="Не удалось прочитать NTFS ACL защищённой папки.",
                remediation="Проверьте существование папки и её ACL под setup account.",
            )
        account_lines = [
            line for line in listing.splitlines() if required in _normalize_account(line)
        ]
        if not account_lines or not any(re.search(r"\((?:f|m)\)", line) for line in account_lines):
            return CapabilityCheck(
                name=name,
                state="unavailable",
                message=(
                    "Task account не имеет подтверждённого Modify/Full доступа "
                    "к защищённой папке."
                ),
                remediation=(
                    "Выдайте task account необходимый NTFS доступ и повторите setup validate."
                ),
            )
        if _has_broad_write_grant(listing):
            return CapabilityCheck(
                name=name,
                state="warning",
                message=(
                    "ACL содержит широкую write-группу; production readiness "
                    "останется заблокированной."
                ),
                remediation=(
                    "Ограничьте Modify/Full доступ setup account и системным администраторам."
                ),
            )
        return CapabilityCheck(
            name=name,
            state="available",
            message="NTFS ACL подтверждает доступ task account без широкой write-группы.",
        )


def _normalize_account(value: str) -> str:
    return "".join(value.split()).casefold()


def _has_broad_write_grant(listing: str) -> bool:
    # The built-in English identifiers are stable enough for an explicit warning;
    # unrecognised localized groups are never guessed to be safe.
    return any(
        any(group in line and re.search(r"\((?:f|m)\)", line) for line in listing.splitlines())
        for group in ("everyone", "builtin\\users", "users")
    )
