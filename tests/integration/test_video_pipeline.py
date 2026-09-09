from datetime import UTC, datetime, timedelta
from pathlib import Path

from smm_agent.adapters.files.recording_watcher import FileObservation
from smm_agent.application.editorial_artifacts import store_artifact
from smm_agent.application.editorial_service import (
    decide_gate,
    request_revision,
    set_publication_target,
)
from smm_agent.application.release_service import StateConflict
from smm_agent.application.setup_service import (
    MEDIA_PROFILE_CONFIRMATION,
    accept_media_profile,
    require_accepted_media_profile,
)
from smm_agent.application.video_service import (
    accept_recording,
    render_video,
    select_recording_candidate,
)
from smm_agent.contracts.video import (
    AlignmentProfile,
    MediaProbe,
    TimelineEntry,
    Transcript,
    VideoQc,
    WordTiming,
)
from smm_agent.domain.release.model import Release
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7
from smm_agent.platform.video_store import VideoStore
from smm_agent.worker.video_worker import VideoWorker

ANCHORS = [
    "акт фиксирует долг",
    "срок запускает оплату",
    "деньги возвращаются в оборот",
]


def alignment_profile() -> AlignmentProfile:
    return AlignmentProfile(
        asr_runtime="fixture-asr@1",
        asr_argv_template=["fixture-asr", "--model", "fixture.bin", "{source}"],
        asr_executable_sha256="c" * 64,
        model_sha256="a" * 64,
        corpus_sha256="b" * 64,
        ffmpeg_sha256="d" * 64,
        ffprobe_sha256="e" * 64,
        author_focal_x=0.5,
        author_focal_y=0.45,
        confidence_threshold=0.85,
        accepted_by="operator",
        accepted_at="2026-09-04T08:00:00Z",
    )


def observation(path: Path) -> FileObservation:
    stat = path.stat()
    return FileObservation(
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
        device=stat.st_dev,
        inode=stat.st_ino,
        unchanged_since=datetime.now(UTC),
    )


class FakeRecognizer:
    runtime_id = "fixture-asr@1"
    model_sha256 = "a" * 64

    def transcribe(self, source: Path) -> Transcript:
        words = []
        for start, phrase in zip((30.0, 180.0, 330.0), ANCHORS, strict=True):
            for offset, word in enumerate(phrase.split()):
                words.append(
                    WordTiming(
                        word=word,
                        start=start + offset * 0.2,
                        end=start + (offset + 1) * 0.2,
                        confidence=0.98,
                    )
                )
        return Transcript(language="ru", words=words)


class LowConfidenceRecognizer(FakeRecognizer):
    def transcribe(self, source: Path) -> Transcript:
        transcript = super().transcribe(source)
        return transcript.model_copy(
            update={
                "words": [word.model_copy(update={"confidence": 0.2}) for word in transcript.words]
            }
        )


class FakeMediaTool:
    def probe(self, source: Path) -> MediaProbe:
        return MediaProbe(
            duration_seconds=450.0,
            video_streams=1,
            audio_streams=1,
            width=1920,
            height=1080,
            frame_rate=30.0,
            video_codec="h264",
            audio_codec="aac",
        )
    def render_master(
        self,
        *,
        source: Path,
        visuals: list[Path],
        timeline: list[TimelineEntry],
        output: Path,
    ) -> None:
        assert timeline[-1].end == 450.0
        output.write_bytes(b"master")

    def render_telegram(self, *, master: Path, output: Path, max_bytes: int) -> None:
        assert max_bytes == 49_000_000
        output.write_bytes(b"telegram")

    def validate_master(
        self,
        path: Path,
        expected_duration: float,
        *,
        source: Path,
        visuals: list[Path],
        timeline: list[TimelineEntry],
    ) -> VideoQc:
        assert source.is_file() and len(visuals) == len(timeline)
        return self._qc(path, expected_duration, 1920, 1080)

    def validate_telegram(
        self,
        path: Path,
        expected_duration: float,
        max_bytes: int,
        *,
        master: Path,
        timeline: list[TimelineEntry],
    ) -> VideoQc:
        assert path.stat().st_size <= max_bytes and master.is_file() and timeline
        return self._qc(path, expected_duration, 1280, 720)

    @staticmethod
    def _qc(path: Path, duration: float, width: int, height: int) -> VideoQc:
        return VideoQc(
            passed=True,
            duration_seconds=duration,
            width=width,
            height=height,
            video_codec="h264",
            audio_codec="aac",
            decoded=True,
            size_bytes=path.stat().st_size,
            checks={"fixture": True},
        )


class TooShortMediaTool(FakeMediaTool):
    def probe(self, source: Path) -> MediaProbe:
        return super().probe(source).model_copy(update={"duration_seconds": 299.0})


def prepared_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "data")
    database.initialize()
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    release_id = uuid7()
    release = Release(
        release_id=release_id,
        topic="Контракт и деньги",
        state="awaiting_recording",
        revision=5,
        active=True,
        created_at=now,
        updated_at=now,
    )
    with database.transaction() as connection:
        database.insert_release(connection, release, actor="author")
        VideoStore.open_recording_window(connection, release_id, now)
        for position, anchor in enumerate(ANCHORS, start=1):
            artifact = store_artifact(
                database,
                connection,
                release_id=release_id,
                kind=f"visual_visual-{position}",
                filename=f"visual-{position}.png",
                payload=f"visual-{position}".encode(),
                media_type="image/png",
                created_at=now,
            )
            database.insert_visual_spec(
                connection,
                release_id=release_id,
                visual_id=f"visual-{position}",
                position=position,
                kind="image",
                anchor_text=anchor,
                purpose="Объяснение",
                artifact_id=str(artifact["artifact_id"]),
                claim_ids=[],
            )
    accept_media_profile(
        database,
        profile=alignment_profile(),
        actor="operator",
        confirmation=MEDIA_PROFILE_CONFIRMATION,
    )
    return database


def test_recording_to_final_gate_is_restartable_two_step_pipeline(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recording = inbox / "recording.mp4"
    recording.write_bytes(b"source-video")
    media = FakeMediaTool()

    accepted = accept_recording(
        database,
        command_id="accept-1",
        expected_revision=5,
        inbox=inbox,
        source=recording,
        observation=observation(recording),
        media_tool=media,
    )
    assert accepted.state == "video_processing"
    assert Path(accepted.artifacts["recording_source"]).read_bytes() == b"source-video"

    rendered = render_video(
        database,
        command_id="render-1",
        expected_revision=accepted.revision,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=media,
    )

    assert rendered.state == "final_pending"
    assert rendered.pending_gate == "final"
    assert Path(rendered.artifacts["master"]).read_bytes() == b"master"
    assert Path(rendered.artifacts["telegram_video"]).stat().st_size < 49_000_000
    timeline = Path(rendered.artifacts["timeline"]).read_text(encoding="utf-8")
    assert '"end":450.0' in timeline


def test_committed_recording_accept_replays_without_recopying(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recording = inbox / "recording.mp4"
    recording.write_bytes(b"source-video")
    seen = observation(recording)
    first = accept_recording(
        database,
        command_id="accept-replay",
        expected_revision=5,
        inbox=inbox,
        source=recording,
        observation=seen,
        media_tool=FakeMediaTool(),
    )
    replay = accept_recording(
        database,
        command_id="accept-replay",
        expected_revision=5,
        inbox=inbox,
        source=recording,
        observation=seen,
        media_tool=FakeMediaTool(),
    )

    assert replay == first


def test_worker_persists_stability_observation_across_restart(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    media = FakeMediaTool()
    started = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    first_worker = VideoWorker(
        database=database,
        inbox=inbox,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=media,
        stable_seconds=30,
    )
    assert first_worker.run_once(now=started).outcome == "waiting"
    (inbox / "recording.mp4").write_bytes(b"source-video")
    assert first_worker.run_once(now=started + timedelta(seconds=1)).outcome == "waiting"

    restarted_worker = VideoWorker(
        database=database,
        inbox=inbox,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=media,
        stable_seconds=30,
    )
    result = restarted_worker.run_once(now=started + timedelta(seconds=32))

    assert result.outcome == "rendered"
    assert result.release is not None
    assert result.release.state == "final_pending"


def test_recording_saved_after_approval_before_first_poll_is_accepted(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "recording.mp4").write_bytes(b"source-video")
    started = datetime.now(UTC) + timedelta(seconds=1)
    worker = VideoWorker(
        database=database,
        inbox=inbox,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
        stable_seconds=30,
    )

    assert worker.run_once(now=started).outcome == "waiting"
    result = worker.run_once(now=started + timedelta(seconds=31))

    assert result.outcome == "rendered"
    assert result.release is not None and result.release.state == "final_pending"


def test_worker_never_accepts_file_present_before_watch_activation(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "old.mp4").write_bytes(b"old-recording")
    database = prepared_database(tmp_path)
    started = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    worker = VideoWorker(
        database=database,
        inbox=inbox,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
        stable_seconds=30,
    )

    assert worker.run_once(now=started).outcome == "waiting"
    assert worker.run_once(now=started + timedelta(seconds=31)).outcome == "waiting"
    assert worker.run_once(now=started + timedelta(seconds=62)).outcome == "waiting"
    with database.connect() as connection:
        release = database.active_release(connection)
        candidate = connection.execute(
            "SELECT state FROM recording_candidates WHERE observed_path LIKE '%old.mp4'"
        ).fetchone()
    assert release is not None and release.state == "awaiting_recording"
    assert candidate is not None and candidate["state"] == "rejected"


def test_final_approval_rechecks_every_final_package_byte(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recording = inbox / "recording.mp4"
    recording.write_bytes(b"source-video")
    accepted = accept_recording(
        database,
        command_id="accept-final",
        expected_revision=5,
        inbox=inbox,
        source=recording,
        observation=observation(recording),
        media_tool=FakeMediaTool(),
    )
    rendered = render_video(
        database,
        command_id="render-final",
        expected_revision=accepted.revision,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
    )
    targeted = set_publication_target(
        database,
        command_id="target-tampered",
        expected_revision=rendered.revision,
        actor="author",
        target_at="2099-09-10T14:00:00+03:00",
        timezone="Europe/Moscow",
    )
    Path(rendered.artifacts["visual_visual-1"]).write_bytes(b"tampered")

    try:
        decide_gate(
            database,
            command_id="approve-tampered",
            expected_revision=targeted.revision,
            actor="author",
            gate="final",
            decision="approved",
            reason=None,
        )
    except ValueError as error:
        assert "не совпадают" in str(error)
    else:
        raise AssertionError("tampered final package was approved")


def test_recording_acceptance_and_video_validation_emit_canonical_events(
    tmp_path: Path,
) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recording = inbox / "recording.mp4"
    recording.write_bytes(b"source-video")
    accepted = accept_recording(
        database,
        command_id="accept-events",
        expected_revision=5,
        inbox=inbox,
        source=recording,
        observation=observation(recording),
        media_tool=FakeMediaTool(),
    )
    render_video(
        database,
        command_id="render-events",
        expected_revision=accepted.revision,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
    )

    with database.connect() as connection:
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM domain_events WHERE aggregate_type = 'release'"
            )
        }
    assert {"RecordingAccepted.v1", "VideoValidated.v1"} <= names


def test_low_confidence_is_persisted_as_actionable_state(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    started = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    worker = VideoWorker(
        database=database,
        inbox=inbox,
        alignment_profile=alignment_profile(),
        recognizer=LowConfidenceRecognizer(),
        media_tool=FakeMediaTool(),
        stable_seconds=1,
    )
    worker.run_once(now=started)
    (inbox / "recording.mp4").write_bytes(b"source-video")
    worker.run_once(now=started + timedelta(seconds=1))

    result = worker.run_once(now=started + timedelta(seconds=2))

    assert result.outcome == "needs_attention"
    assert result.release is not None and result.release.state == "needs_attention"
    issue = Path(result.release.artifacts["video_issue"]).read_text(encoding="utf-8")
    assert "ALIGNMENT_LOW_CONFIDENCE" in issue
    assert "visual-1" in issue
    assert result.release.next_action
    revision = request_revision(
        database,
        command_id="replace-low-confidence",
        expected_revision=result.release.revision,
        actor="author",
        target="recording",
        reason="Перезаписываю ролик по суфлёру.",
    )
    replacement = inbox / "recording.mp4"
    replacement.write_bytes(b"better-source")
    accepted = accept_recording(
        database,
        command_id="accept-better-source",
        expected_revision=revision.revision,
        inbox=inbox,
        source=replacement,
        observation=observation(replacement),
        media_tool=FakeMediaTool(),
    )
    rendered = render_video(
        database,
        command_id="render-better-source",
        expected_revision=accepted.revision,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
    )

    assert rendered.state == "final_pending"


def test_media_profile_must_be_explicitly_accepted(tmp_path: Path) -> None:
    database = Database(tmp_path)
    database.initialize()
    profile = alignment_profile()
    try:
        require_accepted_media_profile(database, profile)
    except ValueError as error:
        assert "production заблокирован" in str(error)
    else:
        raise AssertionError("unaccepted media profile was trusted")

    acceptance = accept_media_profile(
        database,
        profile=profile,
        actor="operator",
        confirmation=MEDIA_PROFILE_CONFIRMATION,
    )
    require_accepted_media_profile(database, profile)

    assert acceptance.accepted_by == "operator"


def test_invalid_source_requests_recording_repair_not_video_retry(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    worker = VideoWorker(
        database=database,
        inbox=inbox,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=TooShortMediaTool(),
        stable_seconds=1,
    )
    started = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    worker.run_once(now=started)
    (inbox / "short.mp4").write_bytes(b"short")
    worker.run_once(now=started + timedelta(seconds=1))
    issue = worker.run_once(now=started + timedelta(seconds=2))
    assert issue.release is not None

    revision = request_revision(
        database,
        command_id="repair-recording",
        expected_revision=issue.release.revision,
        actor="author",
        target="recording",
        reason="Сохраню полную запись.",
    )

    assert revision.state == "revision_requested"
    assert "новую запись" in revision.next_action
    replacement = inbox / "short.mp4"
    replacement.write_bytes(b"replacement-source")
    restarted = VideoWorker(
        database=database,
        inbox=inbox,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
        stable_seconds=1,
    )
    restarted_at = datetime.now(UTC) + timedelta(seconds=1)
    assert restarted.run_once(now=restarted_at).outcome == "waiting"
    recovered = restarted.run_once(now=restarted_at + timedelta(seconds=2))

    assert recovered.outcome == "rendered"


def test_missing_accepted_source_requests_recording_repair(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recording = inbox / "recording.mp4"
    recording.write_bytes(b"source-video")
    accepted = accept_recording(
        database,
        command_id="accept-before-loss",
        expected_revision=5,
        inbox=inbox,
        source=recording,
        observation=observation(recording),
        media_tool=FakeMediaTool(),
    )
    lost_source = Path(accepted.artifacts["recording_source"])
    lost_source.chmod(lost_source.stat().st_mode | 0o200)
    lost_source.unlink()
    worker = VideoWorker(
        database=database,
        inbox=inbox,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
    )
    issue = worker.run_once()

    assert issue.release is not None
    revision = request_revision(
        database,
        command_id="repair-lost-source",
        expected_revision=issue.release.revision,
        actor="author",
        target="recording",
        reason="Сохраню запись повторно.",
    )
    assert revision.state == "revision_requested"


def test_ambiguous_recordings_are_persisted_and_never_chosen(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    started = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    worker = VideoWorker(
        database=database,
        inbox=inbox,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
        stable_seconds=1,
    )
    worker.run_once(now=started)
    (inbox / "one.mp4").write_bytes(b"one")
    (inbox / "two.mp4").write_bytes(b"two")
    worker.run_once(now=started + timedelta(seconds=1))

    result = worker.run_once(now=started + timedelta(seconds=2))

    assert result.outcome == "needs_attention"
    assert result.release is not None and result.release.state == "needs_attention"
    assert result.candidates == ("one.mp4", "two.mp4")
    with database.connect() as connection:
        candidate = connection.execute(
            "SELECT candidate_id FROM recording_candidates "
            "WHERE observed_path LIKE '%one.mp4' AND state = 'ambiguous'"
        ).fetchone()
    assert candidate is not None

    selected = select_recording_candidate(
        database,
        command_id="select-one",
        expected_revision=result.release.revision,
        actor="author",
        candidate_id=str(candidate["candidate_id"]),
        inbox=inbox,
        media_tool=FakeMediaTool(),
    )

    assert selected.state == "video_processing"


def test_recording_replaced_after_observation_is_rejected(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recording = inbox / "recording.mp4"
    recording.write_bytes(b"first-file")
    seen = observation(recording)
    recording.unlink()
    recording.write_bytes(b"replacement")

    try:
        accept_recording(
            database,
            command_id="accept-swapped",
            expected_revision=5,
            inbox=inbox,
            source=recording,
            observation=seen,
            media_tool=FakeMediaTool(),
        )
    except ValueError as error:
        assert "заменена" in str(error)
    else:
        raise AssertionError("replaced pathname was accepted")


def test_final_approval_requires_a_future_publication_target(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recording = inbox / "recording.mp4"
    recording.write_bytes(b"source-video")
    accepted = accept_recording(
        database,
        command_id="accept-target",
        expected_revision=5,
        inbox=inbox,
        source=recording,
        observation=observation(recording),
        media_tool=FakeMediaTool(),
    )
    rendered = render_video(
        database,
        command_id="render-target",
        expected_revision=accepted.revision,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
    )
    try:
        decide_gate(
            database,
            command_id="approve-without-target",
            expected_revision=rendered.revision,
            actor="author",
            gate="final",
            decision="approved",
            reason=None,
        )
    except StateConflict as error:
        assert "время публикации" in str(error)
    else:
        raise AssertionError("final package was approved without a target")

    targeted = set_publication_target(
        database,
        command_id="set-target",
        expected_revision=rendered.revision,
        actor="author",
        target_at="2099-09-10T14:00:00+03:00",
        timezone="Europe/Moscow",
    )
    approved = decide_gate(
        database,
        command_id="approve-with-target",
        expected_revision=targeted.revision,
        actor="author",
        gate="final",
        decision="approved",
        reason=None,
    )

    assert targeted.target_at_utc == "2099-09-10T11:00:00Z"
    assert approved.state == "publication_preparing"


def test_rejected_final_allows_a_new_recording_and_rerender(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    first = inbox / "recording.mp4"
    first.write_bytes(b"first-source")
    accepted = accept_recording(
        database,
        command_id="accept-first",
        expected_revision=5,
        inbox=inbox,
        source=first,
        observation=observation(first),
        media_tool=FakeMediaTool(),
    )
    rendered = render_video(
        database,
        command_id="render-first",
        expected_revision=accepted.revision,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
    )
    rejected = decide_gate(
        database,
        command_id="reject-final",
        expected_revision=rendered.revision,
        actor="author",
        gate="final",
        decision="rejected",
        reason="Нужно перезаписать ролик.",
    )
    second = inbox / "recording.mp4"
    second.write_bytes(b"second-source")

    replacement = accept_recording(
        database,
        command_id="accept-second",
        expected_revision=rejected.revision,
        inbox=inbox,
        source=second,
        observation=observation(second),
        media_tool=FakeMediaTool(),
    )
    rerendered = render_video(
        database,
        command_id="render-second",
        expected_revision=replacement.revision,
        alignment_profile=alignment_profile(),
        recognizer=FakeRecognizer(),
        media_tool=FakeMediaTool(),
    )

    assert rerendered.state == "final_pending"
    assert Path(rerendered.artifacts["recording_source"]).read_bytes() == b"second-source"


def test_only_one_worker_can_lease_the_same_media_revision(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    with database.transaction() as connection:
        release = database.active_release(connection)
        assert release is not None
        first = VideoStore.acquire_media_lease(
            connection,
            release_id=release.release_id,
            release_revision=release.revision,
            owner_id="worker-one",
            now="2026-09-04T10:00:00Z",
            lease_until="2026-09-04T10:40:00Z",
        )
    with database.transaction() as connection:
        second = VideoStore.acquire_media_lease(
            connection,
            release_id=release.release_id,
            release_revision=release.revision,
            owner_id="worker-two",
            now="2026-09-04T10:01:00Z",
            lease_until="2026-09-04T10:41:00Z",
        )
        owned_before_expiry = VideoStore.media_lease_owned(
            connection,
            release_id=release.release_id,
            release_revision=release.revision,
            owner_id="worker-one",
            now="2026-09-04T10:01:00Z",
        )

    assert first is True
    assert second is False
    assert owned_before_expiry is True


def test_author_can_withdraw_recording_while_render_is_in_flight(tmp_path: Path) -> None:
    database = prepared_database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recording = inbox / "recording.mp4"
    recording.write_bytes(b"source-video")
    accepted = accept_recording(
        database,
        command_id="accept-before-withdrawal",
        expected_revision=5,
        inbox=inbox,
        source=recording,
        observation=observation(recording),
        media_tool=FakeMediaTool(),
    )

    withdrawn = request_revision(
        database,
        command_id="withdraw-in-flight",
        expected_revision=accepted.revision,
        actor="author",
        target="recording",
        reason="Запись нужно заменить.",
    )

    assert withdrawn.state == "revision_requested"
    try:
        render_video(
            database,
            command_id="stale-render",
            expected_revision=accepted.revision,
            alignment_profile=alignment_profile(),
            recognizer=FakeRecognizer(),
            media_tool=FakeMediaTool(),
        )
    except StateConflict:
        pass
    else:
        raise AssertionError("withdrawn recording was rendered")


def test_wide_windows_file_identity_survives_sqlite_roundtrip(tmp_path: Path) -> None:
    from dataclasses import replace

    from smm_agent.platform.video_store import VideoStore

    database = prepared_database(tmp_path)
    with database.connect() as connection:
        release = database.active_release(connection)
    assert release is not None
    now = datetime.now(UTC)
    original = FileObservation(
        size=100, mtime_ns=10, ctime_ns=5,
        device=2**64 - 1, inode=2**100 + 123, unchanged_since=now,
    )
    with database.transaction() as connection:
        VideoStore.save_observations(
            connection, release_id=release.release_id,
            observations={"wide-id.mp4": original}, observed_at=now.isoformat(),
        )
    with database.connect() as connection:
        restored = VideoStore.observations(connection, release.release_id)
    assert restored["wide-id.mp4"] == original
    changed = replace(original, size=200, mtime_ns=20)
    with database.transaction() as connection:
        VideoStore.save_observations(
            connection, release_id=release.release_id,
            observations={"wide-id.mp4": changed}, observed_at=now.isoformat(),
        )
    with database.connect() as connection:
        assert VideoStore.observations(connection, release.release_id)["wide-id.mp4"] == changed
