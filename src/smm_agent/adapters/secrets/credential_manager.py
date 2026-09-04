"""A narrowly scoped Windows Credential Manager availability adapter.

The setup validator only needs to know whether a named credential is present;
it must never copy a token into a capability report, a log, or config.  A
provider adapter that later needs a token can receive a separate, audited
secret-reading port rather than broadening this setup-time interface.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, Structure, byref, wintypes
from dataclasses import dataclass
from typing import Literal, Protocol

from smm_agent.platform.config import credential_target

CredentialAvailabilityState = Literal["available", "unavailable"]


@dataclass(frozen=True, slots=True)
class CredentialAvailability:
    """Non-sensitive result of looking up one Credential Manager target."""

    state: CredentialAvailabilityState
    message: str

    @property
    def available(self) -> bool:
        return self.state == "available"


class CredentialStore(Protocol):
    """The least-privilege secret-store port needed by setup validation."""

    def inspect(self, reference: str) -> CredentialAvailability:
        """Report availability without returning a credential value."""


class _CredentialW(Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialManager:
    """Inspect generic credentials through the current Windows account only."""

    _CRED_TYPE_GENERIC = 1
    _ERROR_NOT_FOUND = 1168

    def __init__(self, *, is_windows: bool | None = None) -> None:
        self._is_windows = os.name == "nt" if is_windows is None else is_windows

    def inspect(self, reference: str) -> CredentialAvailability:
        target = credential_target(reference)
        if not self._is_windows:
            return CredentialAvailability(
                state="unavailable",
                message="Windows Credential Manager недоступен вне Windows; secret не читался.",
            )

        try:
            return self._inspect_windows(target)
        except OSError:
            return CredentialAvailability(
                state="unavailable",
                message="Не удалось проверить Windows Credential Manager; secret не читался.",
            )

    def _inspect_windows(self, target: str) -> CredentialAvailability:
        """Call CredReadW and free its native memory without inspecting its blob."""
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        pointer_type = POINTER(_CredentialW)
        credential = pointer_type()
        cred_read = advapi32.CredReadW
        cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            POINTER(pointer_type),
        ]
        cred_read.restype = wintypes.BOOL
        cred_free = advapi32.CredFree
        cred_free.argtypes = [ctypes.c_void_p]
        cred_free.restype = None
        if not cred_read(target, self._CRED_TYPE_GENERIC, 0, byref(credential)):
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            if error == self._ERROR_NOT_FOUND:
                return CredentialAvailability(
                    state="unavailable",
                    message="Указанный credential не найден в Credential Manager.",
                )
            raise ctypes.WinError(error)  # type: ignore[attr-defined]
        try:
            return CredentialAvailability(
                state="available",
                message="Credential найден в Windows Credential Manager.",
            )
        finally:
            cred_free(credential)
