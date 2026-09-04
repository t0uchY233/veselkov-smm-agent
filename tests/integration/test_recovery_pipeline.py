"""End-to-end Slice 5 worker coverage for GC-04 and terminal alerts."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smm_agent.adapters.notification.replay import ReplayAlertTransport
from smm_agent.adapters.publishing.replay import ReplayPublisher
from smm_agent.application.editorial_artifacts import artifact_set_hash, store_artifact
from smm_agent.contracts.publication import Platform, PublicationSnapshot
from smm_agent.domain.release.model import Release
from smm_agent.platform.db import Database
from smm_agent.platform.jobs import JobStore
from smm_agent.platform.publications import PublicationStore
from smm_agent.worker.publication_worker import PublicationWorker

TARGET = datetime(2099, 9, 10, 11, 0, tzinfo=UTC)
BEFORE_TARGET = TARGET - timedelta(minutes=61)


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "data")
    database.initialize()
    release = Release(
        release_id="release-recovery-pipeline",
        topic="Контракт и деньги",
        state="publication_preparing",
        revision=9,
        active=True,
        created_at="2026-09-04T10:00:00Z",
        updated_at="2026-09-04T10:00:00Z",
        target_at_utc=TARGET.isoformat().replace("+00:00", "Z"),
    )
    manifest = {
        "youtube": {"title": "Контракт", "description": "Деньги", "tags": []},
        "dzen": {"title": "Контракт"},
        "visuals": [{"visual_id": "v1", "caption": "Денежный цикл"}],
        "sources": [{"source_id": "s1", "url": "https://example.com/source"}],
    }
    artifacts = {
        "master": b"master-video",
        "telegram_video": b"telegram-video",
        "cover": b"cover",
        "dzen": b"article",
        "telegram_template": (
            "Читайте в блоге {{dzen_url}} или смотрите на YouTube {{youtube_url}}"
        ).encode(),
        "editorial_manifest": json.dumps(manifest, ensure_ascii=False).encode(),
    }
    with database.transaction() as connection:
        database.insert_release(connection, release, actor="author")
        records = [
            store_artifact(
                database,
                connection,
                release_id=release.release_id,
                kind=kind,
                filename=f"{kind}.bin",
                payload=payload,
                media_type="application/octet-stream",
                created_at=release.created_at,
            )
            for kind, payload in artifacts.items()
        ]
        database.insert_approval(
            connection,
            approval_id="approval-final",
            release_id=release.release_id,
            gate="final",
            release_revision=8,
            artifact_set_hash=artifact_set_hash(records),
            artifacts=records,
            created_at=release.created_at,
        )
        database.decide_approval(
            connection,
            approval_id="approval-final",
            decision="approved",
            actor="author",
            reason=None,
            decided_at=release.created_at,
        )
    return database


def _publishers() -> dict[Platform, ReplayPublisher]:
    return {
        "youtube": ReplayPublisher("youtube"),
        "dzen": ReplayPublisher("dzen"),
        "telegram": ReplayPublisher("telegram"),
    }


def _schedule(database: Database, publishers: dict[Platform, ReplayPublisher]) -> PublicationWorker:
    worker = PublicationWorker(database=database, publishers=publishers)
    assert worker.run_once(now=BEFORE_TARGET).outcome == "scheduled"
    assert worker.run_once(now=TARGET - timedelta(minutes=29)).outcome == "preflight_ok"
    for publisher in publishers.values():
        publisher.calls.clear()
    return worker


def test_gc04_worker_hands_partial_target_to_recovery_without_duplicate_remote_work(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    publishers = _publishers()
    publishers["telegram"].fail("execute")
    worker = _schedule(database, publishers)
    with database.connect() as connection:
        before = {
            str(row["platform"]): (
                str(row["remote_id"]),
                str(row["idempotency_key"]),
                str(row["operation_key"]),
            )
            for row in PublicationStore.rows(connection, "release-recovery-pipeline")
        }

    partial = worker.run_once(now=TARGET)

    assert partial.outcome == "recovering"
    with database.connect() as connection:
        jobs = JobStore.rows(connection, "release-recovery-pipeline")
        release = database.release_by_id(connection, "release-recovery-pipeline")
    assert release is not None and release.state == "recovering"
    assert len([job for job in jobs if job["kind"] == "publication_recovery"]) == 1
    target_job = next(job for job in jobs if job["kind"] == "telegram_publish_reconcile")
    assert target_job["state"] == "succeeded"

    publishers["telegram"].recover("execute")
    recovered = worker.run_once(now=TARGET + timedelta(minutes=1))

    assert recovered.outcome == "published"
    assert not [
        call
        for publisher in publishers.values()
        for call in publisher.calls
        if call[0] == "prepare"
    ]
    assert not [
        call
        for publisher in publishers.values()
        for call in publisher.calls
        if call[0] == "arm"
    ]
    # The temporary target-time call failed before the provider accepted it;
    # recovery performs the one successful execute using the same operation.
    assert len([call for call in publishers["telegram"].calls if call[0] == "execute"]) == 1
    with database.connect() as connection:
        after = {
            str(row["platform"]): (
                str(row["remote_id"]),
                str(row["idempotency_key"]),
                str(row["operation_key"]),
            )
            for row in PublicationStore.rows(connection, "release-recovery-pipeline")
        }
    assert after == before


def test_terminal_recovery_opens_one_incident_and_alert_job_then_preserves_it_on_failure(
    tmp_path: Path,
) -> None:
    class TerminalTelegram(ReplayPublisher):
        terminal = False

        def status(
            self, remote_id: str, *, now: datetime | None = None
        ) -> PublicationSnapshot:
            if self.terminal:
                raise PermissionError("Telegram permission denied")
            return super().status(remote_id, now=now)

    database = _database(tmp_path)
    publishers = _publishers()
    telegram = TerminalTelegram("telegram")
    telegram.fail("execute")
    publishers["telegram"] = telegram
    alerts = ReplayAlertTransport()
    alerts.fail()
    worker = PublicationWorker(
        database=database,
        publishers=publishers,
        alert_transport=alerts,
    )
    assert worker.run_once(now=BEFORE_TARGET).outcome == "scheduled"
    assert worker.run_once(now=TARGET - timedelta(minutes=29)).outcome == "preflight_ok"
    assert worker.run_once(now=TARGET).outcome == "recovering"

    telegram.recover("execute")
    telegram.terminal = True
    terminal = worker.run_once(now=TARGET + timedelta(minutes=1))

    assert terminal.outcome == "needs_attention"
    with database.connect() as connection:
        incidents = connection.execute("SELECT * FROM incidents").fetchall()
        notifications = connection.execute("SELECT * FROM notifications").fetchall()
        jobs = JobStore.rows(connection, "release-recovery-pipeline")
    assert len(incidents) == 1
    assert incidents[0]["state"] == "open"
    assert len(notifications) == 1
    assert notifications[0]["recipient"] == "276042853"
    assert len([job for job in jobs if job["kind"] == "notification_deliver"]) == 1

    notification = worker.run_once(now=TARGET + timedelta(minutes=2))

    assert notification.outcome == "notification_retry_wait"
    with database.connect() as connection:
        incident = connection.execute("SELECT state FROM incidents").fetchone()
        delivery = connection.execute("SELECT state FROM notifications").fetchone()
    assert incident is not None and incident["state"] == "open"
    assert delivery is not None and delivery["state"] == "retry_wait"


def test_crash_after_telegram_side_effect_is_reconciled_without_second_execute(
    tmp_path: Path,
) -> None:
    class CrashAfterSendTelegram(ReplayPublisher):
        crashed = False

        def execute(
            self, remote_id: str, *, operation_key: str, now: datetime
        ) -> PublicationSnapshot:
            snapshot = super().execute(remote_id, operation_key=operation_key, now=now)
            if not self.crashed:
                self.crashed = True
                raise KeyboardInterrupt
            return snapshot

    database = _database(tmp_path)
    publishers = _publishers()
    publishers["telegram"] = CrashAfterSendTelegram("telegram")
    worker = _schedule(database, publishers)

    with pytest.raises(KeyboardInterrupt):
        worker.run_once(now=TARGET)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET lease_until = ? WHERE kind = 'telegram_publish_reconcile'",
            ("2000-01-01T00:00:00Z",),
        )

    recovered = worker.run_once(now=TARGET + timedelta(minutes=1))

    assert recovered.outcome == "published"
    assert len([call for call in publishers["telegram"].calls if call[0] == "execute"]) == 1
