"""Narrow secret-reading port for provider OAuth refreshes.

Configuration carries only a Credential Manager reference.  This adapter is
the intentionally separate capability that reads a secret at dispatch time;
it never persists, logs, or renders its value.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, Structure, byref, wintypes
from dataclasses import dataclass
from typing import Protocol

from smm_agent.platform.config import credential_target


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    """An in-memory secret that redacts itself in diagnostics by construction."""

    _value: str

    def reveal(self) -> str:
        """Return the value only at the immediate credential-use boundary."""

        return self._value

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    __str__ = __repr__


class SecretReader(Protocol):
    """Reads one configured secret reference without exposing it to callers."""

    def read(self, reference: str) -> SecretValue: ...


class SecretUnavailableError(RuntimeError):
    """Safe failure: it intentionally omits native error and secret details."""

    def __init__(self, message: str = "Provider credential недоступен.") -> None:
        super().__init__(message)


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


class WindowsCredentialSecretReader:
    """Read a generic Credential Manager secret through the current account.

    The native blob is copied only long enough to obtain the OAuth refresh
    secret.  Python cannot guarantee zeroisation of immutable strings, so this
    class keeps the API deliberately small and callers must not retain or log
    the returned value.
    """

    _CRED_TYPE_GENERIC = 1

    def __init__(self, *, is_windows: bool | None = None) -> None:
        self._is_windows = os.name == "nt" if is_windows is None else is_windows

    def read(self, reference: str) -> SecretValue:
        target = credential_target(reference)
        if not self._is_windows:
            raise SecretUnavailableError("Windows Credential Manager недоступен вне Windows.")
        try:
            return self._read_windows(target)
        except SecretUnavailableError:
            raise
        except OSError:
            raise SecretUnavailableError() from None

    def _read_windows(self, target: str) -> SecretValue:
        win_dll = getattr(ctypes, "WinDLL")
        advapi32 = win_dll("advapi32", use_last_error=True)
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
            raise SecretUnavailableError()
        try:
            blob_size = int(credential.contents.CredentialBlobSize)
            blob = credential.contents.CredentialBlob
            if blob_size <= 0 or not blob:
                raise SecretUnavailableError("Provider credential пустой.")
            try:
                value = ctypes.string_at(blob, blob_size).decode("utf-16-le").rstrip("\x00")
            except UnicodeDecodeError:
                raise SecretUnavailableError("Provider credential имеет неверный формат.") from None
            if not value:
                raise SecretUnavailableError("Provider credential пустой.")
            return SecretValue(value)
        finally:
            cred_free(credential)
