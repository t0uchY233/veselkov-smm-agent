"""Ports implemented by local media adapters."""

from pathlib import Path
from typing import Protocol

from smm_agent.contracts.video import MediaProbe, TimelineEntry, Transcript, VideoQc


class SpeechRecognizer(Protocol):
    runtime_id: str
    model_sha256: str

    def transcribe(self, source: Path) -> Transcript: ...


class MediaTool(Protocol):
    def probe(self, source: Path) -> MediaProbe: ...

    def render_master(
        self,
        *,
        source: Path,
        visuals: list[Path],
        timeline: list[TimelineEntry],
        output: Path,
    ) -> None: ...

    def render_telegram(self, *, master: Path, output: Path, max_bytes: int) -> None: ...

    def validate_master(
        self,
        path: Path,
        expected_duration: float,
        *,
        source: Path,
        visuals: list[Path],
        timeline: list[TimelineEntry],
    ) -> VideoQc: ...

    def validate_telegram(
        self,
        path: Path,
        expected_duration: float,
        max_bytes: int,
        *,
        master: Path,
        timeline: list[TimelineEntry],
    ) -> VideoQc: ...
