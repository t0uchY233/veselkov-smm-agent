"""Secret-store adapters.  Configuration contains references, never secret values."""

from smm_agent.adapters.secrets.credential_manager import (
    CredentialAvailability,
    CredentialStore,
    WindowsCredentialManager,
)

__all__ = ["CredentialAvailability", "CredentialStore", "WindowsCredentialManager"]
