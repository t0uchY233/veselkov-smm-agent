"""Polling watcher that never scans outside the configured recording inbox."""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from smm_agent.domain.video.service import ALLOWED_RECORDING_SUFFIXES


@dataclass(frozen=True, slots=True)
class FileObservation:
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    unchanged_since: datetime


@dataclass(frozen=True, slots=True)
class WatchResult:
    observations: dict[str, FileObservation]
    accepted: Path | None
    ambiguous: list[Path]


class RecordingWatcher:
    def __init__(self, *, inbox: Path, stable_seconds: int = 30) -> None:
        if stable_seconds < 1:
            raise ValueError("stable_seconds must be positive")
        if not inbox.is_dir() or inbox.is_symlink():
            raise ValueError("recording inbox must be a real directory")
        self.inbox = inbox.resolve()
        self.stable_seconds = stable_seconds

    def observe(
        self, previous: dict[str, FileObservation], *, now: datetime
    ) -> WatchResult:
        observations: dict[str, FileObservation] = {}
        stable: list[Path] = []
        for candidate in sorted(self.inbox.iterdir(), key=lambda path: path.name.casefold()):
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or candidate.suffix.lower() not in ALLOWED_RECORDING_SUFFIXES
            ):
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self.inbox):
                continue
            stat = candidate.stat()
            key = str(resolved)
            old = previous.get(key)
            unchanged_since = (
                old.unchanged_since
                if old
                and old.size == stat.st_size
                and old.mtime_ns == stat.st_mtime_ns
                and old.ctime_ns == stat.st_ctime_ns
                and old.device == stat.st_dev
                and old.inode == stat.st_ino
                else now
            )
            observation = FileObservation(
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_dev,
                stat.st_ino,
                unchanged_since,
            )
            observations[key] = observation
            if (
                (now - unchanged_since).total_seconds() >= self.stable_seconds
                and self._can_open(candidate)
            ):
                stable.append(resolved)

        return WatchResult(
            observations=observations,
            accepted=stable[0] if len(stable) == 1 else None,
            ambiguous=stable if len(stable) > 1 else [],
        )

    @staticmethod
    def _can_open(path: Path) -> bool:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return False
        os.close(descriptor)
        return True
