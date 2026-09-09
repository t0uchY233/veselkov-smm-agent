from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smm_agent.adapters.files.recording_watcher import RecordingWatcher


def test_watcher_accepts_only_one_unchanged_non_symlink_candidate(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recording = inbox / "recording.mp4"
    recording.write_bytes(b"video")
    watcher = RecordingWatcher(inbox=inbox, stable_seconds=30)
    first_seen = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)

    first = watcher.observe({}, now=first_seen)
    second = watcher.observe(first.observations, now=first_seen + timedelta(seconds=31))

    assert first.accepted is None
    assert second.accepted == recording.resolve()
    assert second.ambiguous == []


def test_watcher_never_chooses_between_two_stable_candidates(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("one.mp4", "two.mov"):
        (inbox / name).write_bytes(name.encode())
    watcher = RecordingWatcher(inbox=inbox, stable_seconds=30)
    first_seen = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)

    first = watcher.observe({}, now=first_seen)
    second = watcher.observe(first.observations, now=first_seen + timedelta(seconds=31))

    assert second.accepted is None
    assert [path.name for path in second.ambiguous] == ["one.mp4", "two.mov"]


def test_watcher_ignores_symlinks_and_unsupported_extensions(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private")
    try:
        (inbox / "outside.mp4").symlink_to(outside)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink privilege; covered on Linux CI")
        raise
    (inbox / "notes.txt").write_text("not a recording", encoding="utf-8")
    watcher = RecordingWatcher(inbox=inbox, stable_seconds=30)

    result = watcher.observe({}, now=datetime(2026, 9, 4, tzinfo=UTC))

    assert result.observations == {}


def test_watcher_keeps_unopenable_file_stabilizing(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recording = inbox / "recording.mp4"
    recording.write_bytes(b"video")
    watcher = RecordingWatcher(inbox=inbox, stable_seconds=1)
    first_seen = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    first = watcher.observe({}, now=first_seen)

    def deny_open(*args, **kwargs):
        raise PermissionError("sharing violation")

    monkeypatch.setattr("smm_agent.adapters.files.recording_watcher.os.open", deny_open)
    second = watcher.observe(first.observations, now=first_seen + timedelta(seconds=2))

    assert second.accepted is None
