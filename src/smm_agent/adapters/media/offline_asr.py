"""Adapter contract for a pinned local ASR executable with canonical JSON output."""

import json
from pathlib import Path

from smm_agent.adapters.media.process import CommandTimedOut, run_bounded
from smm_agent.contracts.video import Transcript


class OfflineAsrFailed(RuntimeError):
    pass


class OfflineJsonRecognizer:
    """Run a local-only executable; ``{source}`` is replaced without shell parsing."""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        runtime_id: str,
        model_sha256: str,
        timeout_seconds: float = 1800,
    ) -> None:
        if not command or not any("{source}" in part for part in command):
            raise ValueError("ASR command must contain a {source} placeholder")
        self.command = command
        self.runtime_id = runtime_id
        self.model_sha256 = model_sha256
        self.timeout_seconds = timeout_seconds

    def transcribe(self, source: Path) -> Transcript:
        command = [part.replace("{source}", str(source)) for part in self.command]
        try:
            completed = run_bounded(command, timeout_seconds=self.timeout_seconds)
        except CommandTimedOut as error:
            raise OfflineAsrFailed(f"offline ASR {error}") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else ""
            raise OfflineAsrFailed(f"offline ASR failed ({completed.returncode}): {detail}")
        try:
            payload = json.loads(completed.stdout)
            return Transcript.model_validate(payload)
        except (json.JSONDecodeError, ValueError) as error:
            raise OfflineAsrFailed("offline ASR returned an invalid transcript") from error
