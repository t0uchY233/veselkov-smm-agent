"""Secret-store adapters.  Configuration contains references, never secret values."""

from smm_agent.adapters.secrets.credential_manager import (
    CredentialAvailability,
    CredentialStore,
    WindowsCredentialManager,
)
from smm_agent.adapters.secrets.secret_reader import (
    SecretReader,
    SecretUnavailableError,
    SecretValue,
    WindowsCredentialSecretReader,
)

__all__ = [
    "CredentialAvailability",
    "CredentialStore",
    "SecretReader",
    "SecretUnavailableError",
    "SecretValue",
    "WindowsCredentialManager",
    "WindowsCredentialSecretReader",
]
