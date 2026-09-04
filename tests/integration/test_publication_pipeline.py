import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smm_agent.adapters.publishing.replay import ReplayPublisher
from smm_agent.application.editorial_artifacts import artifact_set_hash, store_artifact
from smm_agent.application.publication_service import (
    preflight_scheduled_release,
    reschedule_release,
    schedule_release,
)
from smm_agent.application.release_service import StateConflict, get_release_status
from smm_agent.contracts.publication import (
    Platform,
    PreparedPublication,
    PublicationRequest,
    PublicationSnapshot,
)
from smm_agent.domain.release.model import Release
from smm_agent.platform.db import Database
from smm_agent.platform.jobs import JobStore
from smm_agent.platform.publications import PublicationStore
from smm_agent.worker.publication_worker import PublicationWorker


def publication_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "data")
    database.initialize()
    release_id = "release-publication"
    created = "2026-09-04T10:00:00Z"
    release = Release(
        release_id=release_id,
        topic="Контракт и деньги",
        state="publication_preparing",
        revision=9,
        active=True,
        created_at=created,
        updated_at=created,
        target_at_utc="2099-09-10T11:00:00Z",
    )
    manifest = {
        "youtube": {
            "title": "Контракт есть, а денег нет",
            "description": "Как превратить работу в денежное требование.",
            "tags": ["финансы"],
        },
        "dzen": {"title": "Контракт есть, а денег нет"},
        "visuals": [{"visual_id": "v1", "caption": "Денежный цикл"}],
        "sources": [{"source_id": "s1", "url": "https://example.com/source"}],
    }
    payloads = {
        "master": b"master-video",
        "telegram_video": b"telegram-video",
        "cover": b"cover",
        "dzen": b"article",
        "telegram_template": (
            "КОНТРАКТ И ДЕНЬГИ\n\nЧитайте в блоге {{dzen_url}} "
            "или смотрите на YouTube {{youtube_url}}"
        ).encode(),
        "editorial_manifest": json.dumps(manifest, ensure_ascii=False).encode(),
    }
    with database.transaction() as connection:
        database.insert_release(connection, release, actor="author")
        records = []
        for kind, payload in payloads.items():
            records.append(
                store_artifact(
                    database,
                    connection,
                    release_id=release_id,
                    kind=kind,
                    filename=f"{kind}.bin",
                    payload=payload,
                    media_type="application/octet-stream",
                    created_at=created,
                )
            )
        database.insert_approval(
            connection,
            approval_id="approval-final",
            release_id=release_id,
            gate="final",
            release_revision=8,
            artifact_set_hash=artifact_set_hash(records),
            artifacts=records,
            created_at=created,
        )
        database.decide_approval(
            connection,
            approval_id="approval-final",
            decision="approved",
            actor="author",
            reason=None,
            decided_at=created,
        )
    return database


def replay_publishers() -> dict[Platform, ReplayPublisher]:
    return {
        "youtube": ReplayPublisher("youtube"),
        "dzen": ReplayPublisher("dzen"),
        "telegram": ReplayPublisher("telegram"),
    }


def test_gc03_links_exist_before_telegram_payload_and_all_platforms_arm(
    tmp_path: Path,
) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()

    result = schedule_release(
        database,
        command_id="schedule-gc03",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert result.state == "scheduled"
    assert result.publications["youtube"].known_url.startswith("https://youtu.be/")
    assert result.publications["dzen"].known_url.startswith("https://dzen.ru/a/")
    caption = Path(str(result.telegram_caption_path)).read_text(encoding="utf-8")
    assert result.publications["youtube"].known_url in caption
    assert result.publications["dzen"].known_url in caption
    assert len(caption) <= 1000
    assert len(caption.encode("utf-16-le")) // 2 <= 1000
    with database.connect() as connection:
        publications = PublicationStore.rows(connection, result.release_id)
        jobs = JobStore.rows(connection, result.release_id)
    assert {row["state"] for row in publications} == {"armed"}
    assert {row["kind"] for row in jobs} == {
        "publication_preflight",
        "telegram_publish_reconcile",
    }
    status = get_release_status(database)
    assert status.publication_states == {
        "youtube": "armed",
        "dzen": "armed",
        "telegram": "armed",
    }


def test_missing_scheduled_dzen_link_blocks_telegram_and_cancels_prepared(
    tmp_path: Path,
) -> None:
    class NoLinkDzen(ReplayPublisher):
        def arm(
            self,
            remote_id: str,
            *,
            target_at_utc: datetime,
            operation_key: str,
        ) -> PublicationSnapshot:
            return super().arm(
                remote_id,
                target_at_utc=target_at_utc,
                operation_key=operation_key,
            ).model_copy(update={"known_url": None})

    database = publication_database(tmp_path)
    publishers = replay_publishers()
    publishers["dzen"] = NoLinkDzen("dzen")

    result = schedule_release(
        database,
        command_id="schedule-no-dzen-link",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert result.state == "delayed"
    assert result.telegram_caption_path is None
    assert publishers["telegram"].calls == []


def test_dzen_is_armed_before_telegram_payload_is_prepared(tmp_path: Path) -> None:
    operations: list[str] = []

    class RecordingPublisher(ReplayPublisher):
        def prepare(self, request: PublicationRequest) -> PreparedPublication:
            operations.append(f"{self.platform}.prepare")
            return super().prepare(request)

        def arm(
            self,
            remote_id: str,
            *,
            target_at_utc: datetime,
            operation_key: str,
        ) -> PublicationSnapshot:
            operations.append(f"{self.platform}.arm")
            return super().arm(
                remote_id,
                target_at_utc=target_at_utc,
                operation_key=operation_key,
            )

    database = publication_database(tmp_path)
    publishers = {
        platform: RecordingPublisher(platform)
        for platform in ("youtube", "dzen", "telegram")
    }

    schedule_release(
        database,
        command_id="schedule-order",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert operations.index("dzen.arm") < operations.index("telegram.prepare")


def test_wrong_armed_target_is_rejected_and_everything_is_cancelled(
    tmp_path: Path,
) -> None:
    class WrongTargetYoutube(ReplayPublisher):
        def arm(
            self,
            remote_id: str,
            *,
            target_at_utc: datetime,
            operation_key: str,
        ) -> PublicationSnapshot:
            armed = super().arm(
                remote_id,
                target_at_utc=target_at_utc,
                operation_key=operation_key,
            )
            return armed.model_copy(
                update={"target_at_utc": target_at_utc + timedelta(minutes=1)}
            )

    database = publication_database(tmp_path)
    publishers = replay_publishers()
    publishers["youtube"] = WrongTargetYoutube("youtube")

    result = schedule_release(
        database,
        command_id="schedule-wrong-target",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert result.state == "delayed"
    assert all(snapshot.state == "cancelled" for snapshot in result.publications.values())


def test_unconfirmed_cancellation_requires_attention_and_reconciliation(
    tmp_path: Path,
) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()
    publishers["youtube"].fail("arm")
    publishers["dzen"].fail("cancel")

    result = schedule_release(
        database,
        command_id="schedule-cancel-failure",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert result.state == "needs_attention"
    assert result.publications["dzen"].state == "failed"
    with database.connect() as connection:
        jobs = JobStore.rows(connection, result.release_id)
    reconcile = [row for row in jobs if row["kind"] == "publication_cancel_reconcile"]
    assert len(reconcile) == 1
    assert reconcile[0]["state"] == "queued"


def test_existing_publication_lease_blocks_all_remote_side_effects(tmp_path: Path) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()
    with database.transaction() as connection:
        acquired = PublicationStore.acquire_lease(
            connection,
            release_id="release-publication",
            release_revision=9,
            owner_id="another-worker",
            now=datetime.now(UTC),
        )
    assert acquired

    with pytest.raises(StateConflict, match="другой worker"):
        schedule_release(
            database,
            command_id="schedule-concurrent",
            expected_revision=9,
            publishers=publishers,
            now=datetime(2026, 9, 4, tzinfo=UTC),
        )

    assert all(adapter.calls == [] for adapter in publishers.values())


def test_target_inside_safety_window_blocks_all_remote_side_effects(tmp_path: Path) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()
    instant = datetime(2099, 9, 10, 10, 30, tzinfo=UTC)
    target = instant + timedelta(minutes=34)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE releases SET target_at_utc = ? WHERE release_id = ?",
            (target.isoformat().replace("+00:00", "Z"), "release-publication"),
        )

    with pytest.raises(ValueError, match="35 минут"):
        schedule_release(
            database,
            command_id="schedule-too-late",
            expected_revision=9,
            publishers=publishers,
            now=instant,
        )

    assert all(adapter.calls == [] for adapter in publishers.values())


def test_partial_arm_failure_cancels_every_available_remote_before_public(
    tmp_path: Path,
) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()
    publishers["dzen"].fail("arm")

    result = schedule_release(
        database,
        command_id="schedule-arm-failure",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert result.state == "delayed"
    assert all(snapshot.state == "cancelled" for snapshot in result.publications.values())
    assert any(call[0] == "cancel" for call in publishers["youtube"].calls)
    with database.connect() as connection:
        assert JobStore.rows(connection, result.release_id) == []


def test_t_minus_30_preflight_failure_cancels_schedules_and_jobs(tmp_path: Path) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()
    scheduled = schedule_release(
        database,
        command_id="schedule-before-preflight",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    publishers["youtube"].fail("preflight")

    delayed = preflight_scheduled_release(
        database,
        command_id="preflight-failure",
        expected_revision=scheduled.revision,
        publishers=publishers,
    )

    assert delayed.state == "delayed"
    assert all(snapshot.state == "cancelled" for snapshot in delayed.publications.values())
    with database.connect() as connection:
        jobs = JobStore.rows(connection, delayed.release_id)
    assert {row["state"] for row in jobs} == {"cancelled"}


def test_schedule_command_replays_without_duplicate_remote_ids(tmp_path: Path) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()
    first = schedule_release(
        database,
        command_id="schedule-replay",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    call_counts = {platform: len(adapter.calls) for platform, adapter in publishers.items()}

    replay = schedule_release(
        database,
        command_id="schedule-replay",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )

    assert replay == first
    assert {platform: len(adapter.calls) for platform, adapter in publishers.items()} == call_counts


def test_worker_schedules_preflights_and_executes_telegram_at_target(tmp_path: Path) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()
    worker = PublicationWorker(database=database, publishers=publishers)

    scheduled = worker.run_once(now=datetime(2099, 9, 10, 10, 0, tzinfo=UTC))
    preflight = worker.run_once(now=datetime(2099, 9, 10, 10, 31, tzinfo=UTC))
    published = worker.run_once(now=datetime(2099, 9, 10, 11, 0, tzinfo=UTC))

    assert scheduled.outcome == "scheduled"
    assert preflight.outcome == "preflight_ok"
    assert published.outcome == "published"
    assert published.publication is not None
    assert published.publication.publications["telegram"].state == "public"
    assert {snapshot.state for snapshot in published.publication.publications.values()} == {
        "public"
    }
    with database.connect() as connection:
        jobs = JobStore.rows(connection, "release-publication")
    preflight_job = next(row for row in jobs if row["kind"] == "publication_preflight")
    assert preflight_job["state"] == "succeeded"
    telegram_job = next(
        row for row in jobs if row["kind"] == "telegram_publish_reconcile"
    )
    assert telegram_job["state"] == "succeeded"


def test_crash_after_telegram_send_recovers_public_receipt_without_duplicate(
    tmp_path: Path,
) -> None:
    class CrashAfterSendTelegram(ReplayPublisher):
        crashed = False

        def execute(
            self, remote_id: str, *, operation_key: str, now: datetime
        ) -> PublicationSnapshot:
            result = super().execute(remote_id, operation_key=operation_key, now=now)
            if not self.crashed:
                self.crashed = True
                raise KeyboardInterrupt
            return result

    database = publication_database(tmp_path)
    publishers = replay_publishers()
    publishers["telegram"] = CrashAfterSendTelegram("telegram")
    worker = PublicationWorker(database=database, publishers=publishers)
    worker.run_once(now=datetime(2099, 9, 10, 10, 0, tzinfo=UTC))
    worker.run_once(now=datetime(2099, 9, 10, 10, 31, tzinfo=UTC))

    with pytest.raises(KeyboardInterrupt):
        worker.run_once(now=datetime(2099, 9, 10, 11, 0, tzinfo=UTC))
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE jobs SET lease_until = '2000-01-01T00:00:00Z'
            WHERE kind = 'telegram_publish_reconcile'
            """
        )

    recovered = worker.run_once(now=datetime(2099, 9, 10, 11, 1, tzinfo=UTC))

    assert recovered.outcome == "published"
    execute_calls = [
        call for call in publishers["telegram"].calls if call[0] == "execute"
    ]
    assert len(execute_calls) == 1


def test_overdue_preflight_preserves_public_receipt_and_never_cancels(
    tmp_path: Path,
) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()
    scheduled = schedule_release(
        database,
        command_id="schedule-before-overdue",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    youtube = scheduled.publications["youtube"]
    publishers["youtube"].make_public(
        youtube.remote_id, public_at=datetime(2099, 9, 10, 11, 0, tzinfo=UTC)
    )

    result = preflight_scheduled_release(
        database,
        command_id="preflight-overdue-public",
        expected_revision=scheduled.revision,
        publishers=publishers,
        now=datetime(2099, 9, 10, 11, 1, tzinfo=UTC),
    )

    assert result.state == "needs_attention"
    assert result.publications["youtube"].state == "public"
    assert not any(call[0] == "cancel" for adapter in publishers.values() for call in adapter.calls)
    with database.connect() as connection:
        rows = PublicationStore.rows(connection, result.release_id)
    youtube_row = next(row for row in rows if row["platform"] == "youtube")
    assert youtube_row["state"] == "public"


def test_restart_resumes_persisted_receipts_without_rearming_dzen(tmp_path: Path) -> None:
    class ProcessCrashTelegram(ReplayPublisher):
        crashed = False

        def prepare(self, request: PublicationRequest) -> PreparedPublication:
            if not self.crashed:
                self.crashed = True
                raise KeyboardInterrupt
            return super().prepare(request)

    database = publication_database(tmp_path)
    publishers = replay_publishers()
    publishers["telegram"] = ProcessCrashTelegram("telegram")

    with pytest.raises(KeyboardInterrupt):
        schedule_release(
            database,
            command_id="schedule-before-crash",
            expected_revision=9,
            publishers=publishers,
            now=datetime(2026, 9, 4, tzinfo=UTC),
        )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE publication_leases SET lease_until = '2000-01-01T00:00:00Z'"
        )

    result = schedule_release(
        database,
        command_id="schedule-after-restart",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert result.state == "scheduled"
    dzen_arm_calls = [call for call in publishers["dzen"].calls if call[0] == "arm"]
    assert len(dzen_arm_calls) == 1


def test_author_can_reschedule_before_publication_without_reusing_old_remotes(
    tmp_path: Path,
) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()
    scheduled = schedule_release(
        database,
        command_id="schedule-before-reschedule",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    old_remote_ids = {
        platform: snapshot.remote_id
        for platform, snapshot in scheduled.publications.items()
    }
    new_target = datetime(2099, 9, 17, 11, 0, tzinfo=UTC)

    moved = reschedule_release(
        database,
        command_id="reschedule",
        expected_revision=scheduled.revision,
        actor="author",
        target_at=new_target,
        publishers=publishers,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )
    rescheduled = schedule_release(
        database,
        command_id="schedule-after-reschedule",
        expected_revision=moved.revision,
        publishers=publishers,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )

    assert moved.state == "publication_preparing"
    assert rescheduled.state == "scheduled"
    assert rescheduled.target_at_utc == "2099-09-17T11:00:00Z"
    assert all(
        rescheduled.publications[platform].remote_id != old_remote_ids[platform]
        for platform in old_remote_ids
    )
    with database.connect() as connection:
        attempts = connection.execute(
            "SELECT * FROM publication_attempts WHERE release_id = ?",
            (rescheduled.release_id,),
        ).fetchall()
    assert len(attempts) == 6
    old_attempts = [
        row for row in attempts if row["target_at_utc"] == scheduled.target_at_utc
    ]
    assert {row["state"] for row in old_attempts} == {"cancelled"}


def test_persistent_replay_preflight_survives_new_process_instances(tmp_path: Path) -> None:
    database = publication_database(tmp_path)

    def persistent_publishers() -> dict[Platform, ReplayPublisher]:
        return {
            platform: ReplayPublisher(
                platform, state_path=tmp_path / f"replay-{platform}.json"
            )
            for platform in ("youtube", "dzen", "telegram")
        }

    scheduled = schedule_release(
        database,
        command_id="schedule-persistent-replay",
        expected_revision=9,
        publishers=persistent_publishers(),
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    result = preflight_scheduled_release(
        database,
        command_id="preflight-after-process-restart",
        expected_revision=scheduled.revision,
        publishers=persistent_publishers(),
        now=datetime(2099, 9, 10, 10, 30, tzinfo=UTC),
    )

    assert result.state == "scheduled"
    assert {snapshot.state for snapshot in result.publications.values()} == {"armed"}


def test_reclaimed_preflight_rechecks_remote_status_instead_of_replaying_success(
    tmp_path: Path,
) -> None:
    database = publication_database(tmp_path)
    publishers = replay_publishers()
    scheduled = schedule_release(
        database,
        command_id="schedule-preflight-crash",
        expected_revision=9,
        publishers=publishers,
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    claim_time = "2099-09-10T10:31:00Z"
    with database.transaction() as connection:
        job = JobStore.claim_due(
            connection,
            release_id=scheduled.release_id,
            kind="publication_preflight",
            now=claim_time,
            lease_until="2099-09-10T10:32:00Z",
            owner_id="crashed-attempt",
        )
    assert job is not None
    preflight_scheduled_release(
        database,
        command_id="preflight-crashed-attempt",
        expected_revision=scheduled.revision,
        publishers=publishers,
        now=datetime(2099, 9, 10, 10, 31, tzinfo=UTC),
    )
    publishers["youtube"].make_public(
        scheduled.publications["youtube"].remote_id,
        public_at=datetime(2099, 9, 10, 10, 32, tzinfo=UTC),
    )

    tick = PublicationWorker(database=database, publishers=publishers).run_once(
        now=datetime(2099, 9, 10, 10, 33, tzinfo=UTC)
    )

    assert tick.outcome == "needs_attention"
    assert tick.publication is not None
    assert tick.publication.state == "needs_attention"
    assert tick.publication.publications["youtube"].state == "public"
