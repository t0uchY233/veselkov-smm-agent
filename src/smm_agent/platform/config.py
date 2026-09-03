"""Runtime path resolution for Windows production and portable development."""

import os
from pathlib import Path


def default_data_root() -> Path:
    explicit = os.environ.get("SMM_AGENT_DATA_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "VeselkovSmm"

    return Path.cwd() / ".runtime"

