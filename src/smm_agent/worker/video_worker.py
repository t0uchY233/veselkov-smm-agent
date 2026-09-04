"""One restart-safe polling step for the recording and media pipeline."""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from smm_agent.adapters.files.recording_watcher import RecordingWatcher
from smm_agent.application.release_service import StateConflict
from smm_agent.application.video_service import (
    RecordingSourceInvalid,
    accept_recording,
    record_video_issue,
    render_video,
)
from smm_agent.contracts.cli import ReleaseResult
from smm_agent.contracts.video import AlignmentProfile
from smm_agent.domain.video.ports import MediaTool, SpeechRecognizer
from smm_agent.domain.video.service import AlignmentLowConfidence
from smm_agent.platform.db import Database
from smm_agent.platform.video_store import VideoStore


@dataclass(frozen=True, slots=True)
class WorkerTick:
    outcome: str
    release: ReleaseResult | None = None
    candidates: tuple[str, ...] = ()


class VideoWorker:
    def __init__(
        self,
        *,
        database: Database,
        inbox: Path,
        alignment_profile: AlignmentProfile,
        recognizer: SpeechRecognizer,
        media_tool: MediaTool,
        stable_seconds: int = 30,
    ) -> None:
        self.database = database
        self.watcher = RecordingWatcher(inbox=inbox, stable_seconds=stable_seconds)
        self.alignment_profile = alignment_profile
        self.recognizer = recognizer
        self.media_tool = media_tool

    @staticmethod
    def _command_id(prefix: str, release_id: str, revision: int, suffix: str = "") -> str:
        value = f"{prefix}:{release_id}:{revision}:{suffix}".encode()
        return f"worker-{hashlib.sha256(value).hexdigest()[:32]}"

    def run_once(self, *, now: datetime | None = None) -> WorkerTick:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        observed_text = observed_at.isoformat().replace("+00:00", "Z")
        with self.database.connect() as connection:
            release = self.database.active_release(connection)
            if release is None:
                return WorkerTick("idle")
            accepts_recording = release.state == "awaiting_recording" or (
                release.state == "revision_requested" and release.revision_target == "recording"
            )
            renders_video = release.state == "video_processing" or (
                release.state == "revision_requested" and release.revision_target == "video"
            )
            if renders_video or accepts_recording:
                previous = VideoStore.observations(connection, release.release_id)
            else:
                return WorkerTick("idle")

        if renders_video:
            return self._render_or_record_issue(release.release_id, release.revision)

        with self.database.connect() as connection:
            initialized = VideoStore.watch_initialized(connection, release.release_id)
            window_opened_at = VideoStore.recording_window_opened_at(
                connection, release.release_id
            )
        if not initialized:
            baseline = self.watcher.observe({}, now=observed_at)
            with self.database.transaction() as connection:
                VideoStore.initialize_watch(
                    connection,
                    release_id=release.release_id,
                    baseline=baseline.observations,
                    window_opened_at=window_opened_at,
                    initialized_at=observed_text,
                )
            return WorkerTick("waiting")

        scan = self.watcher.observe(previous, now=observed_at)
        with self.database.transaction() as connection:
            VideoStore.save_observations(
                connection,
                release_id=release.release_id,
                observations=scan.observations,
                observed_at=observed_text,
            )
            if scan.ambiguous:
                VideoStore.mark_ambiguous(
                    connection,
                    release.release_id,
                    [str(path) for path in scan.ambiguous],
                    observed_text,
                )
                ambiguous_rows = VideoStore.ambiguous_candidates(
                    connection, release.release_id
                )
        if scan.ambiguous:
            result = record_video_issue(
                self.database,
                command_id=self._command_id("issue", release.release_id, release.revision),
                expected_revision=release.revision,
                code="AMBIGUOUS_RECORDING",
                details={
                    "candidates": [
                        {
                            "candidate_id": str(row["candidate_id"]),
                            "name": Path(str(row["observed_path"])).name,
                        }
                        for row in ambiguous_rows
                    ]
                },
                action="Выберите candidate_id нужной записи в Codex.",
                remediation_target="recording",
            )
            return WorkerTick(
                "needs_attention",
                release=result,
                candidates=tuple(path.name for path in scan.ambiguous),
            )
        if scan.accepted is None:
            return WorkerTick("waiting")

        try:
            accepted = accept_recording(
                self.database,
                command_id=self._command_id(
                    "accept", release.release_id, release.revision, str(scan.accepted)
                ),
                expected_revision=release.revision,
                inbox=self.watcher.inbox,
                source=scan.accepted,
                observation=scan.observations[str(scan.accepted)],
                media_tool=self.media_tool,
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            code = (
                "DURATION_OUT_OF_RANGE"
                if "300.000" in str(error)
                else "RECORDING_VALIDATION_FAILED"
            )
            issue = record_video_issue(
                self.database,
                command_id=self._command_id("issue", release.release_id, release.revision, code),
                expected_revision=release.revision,
                code=code,
                details={"candidate_name": scan.accepted.name, "error_type": type(error).__name__},
                action="Удалите неподходящий файл и сохраните новую запись в папку.",
                remediation_target="recording",
            )
            return WorkerTick("needs_attention", release=issue)
        return self._render_or_record_issue(release.release_id, accepted.revision)

    def _render_or_record_issue(self, release_id: str, revision: int) -> WorkerTick:
        try:
            rendered = render_video(
                self.database,
                command_id=self._command_id("render", release_id, revision),
                expected_revision=revision,
                alignment_profile=self.alignment_profile,
                recognizer=self.recognizer,
                media_tool=self.media_tool,
            )
            return WorkerTick("rendered", release=rendered)
        except StateConflict:
            return WorkerTick("busy")
        except AlignmentLowConfidence as error:
            code = "ALIGNMENT_LOW_CONFIDENCE"
            details: dict[str, object] = {"unmatched_anchor_ids": error.unmatched_anchors}
            action = "Перезапишите ролик по утверждённому тексту или запросите новый монтаж."
            remediation_target = "recording"
        except RecordingSourceInvalid as error:
            code = "RECORDING_SOURCE_INVALID"
            details = {"error_type": type(error).__name__}
            action = "Сохраните новую запись в папку для безопасного повторного монтажа."
            remediation_target = "recording"
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            code = "MEDIA_PROCESSING_FAILED"
            details = {"error_type": type(error).__name__}
            action = "Проверьте media diagnostics и повторите обработку после исправления."
            remediation_target = "video"
        result = record_video_issue(
            self.database,
            command_id=self._command_id("issue", release_id, revision, code),
            expected_revision=revision,
            code=code,
            details=details,
            action=action,
            remediation_target=remediation_target,
        )
        return WorkerTick("needs_attention", release=result)
