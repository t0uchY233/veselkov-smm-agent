"""GC-04 recovery: status first, preserve receipts, and retry safely."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smm_agent.adapters.publishing.replay import ReplayPublisher
from smm_agent.application.recovery_service import reconcile_publication_recovery
from smm_agent.contracts.publication import Platform, PublicationRequest, PublicationSnapshot
from smm_agent.domain.release.model import Release
from smm_agent.platform.db import Database
from smm_agent.platform.jobs import JobClaim, JobStore
from smm_agent.platform.publications import PublicationStore

TARGET = datetime(2099, 9, 10, 11, 0, tzinfo=UTC)
NOW = TARGET + timedelta(minutes=1)
TARGET_TEXT = "2099-09-10T11:00:00Z"


class RecordingPublisher(ReplayPublisher):
    def __init__(self, platform: Platform, *, operations: list[str]) -> None:
        super().__init__(platform)
        self.operations = operations

    def status(
        self, remote_id: str, *, now: datetime | None = None
    ) -> PublicationSnapshot:
        self.operations.append(f"{self.platform}.status")
        return super().status(remote_id, now=now)

    def execute(
        self, remote_id: str, *, operation_key: str, now: datetime
    ) -> PublicationSnapshot:
        self.operations.append(f"{self.platform}.execute:{operation_key}")
        return super().execute(remote_id, operation_key=operation_key, now=now)


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _recovery_fixture(
    tmp_path: Path,
    *,
    publisher_factory: Callable[[Platform, list[str]], ReplayPublisher] | None = None,
) -> tuple[
    Database,
    dict[Platform, ReplayPublisher],
    list[str],
    dict[Platform, PublicationSnapshot],
]:
    database = Database(tmp_path / "data")
    database.initialize()
    operations: list[str] = []
    factory = publisher_factory or (
        lambda platform, trace: RecordingPublisher(platform, operations=trace)
    )
    publishers: dict[Platform, ReplayPublisher] = {
        platform: factory(platform, operations)
        for platform in ("youtube", "dzen", "telegram")
    }
    release = Release(
        release_id="release-recovery",
        topic="Контракт и деньги",
        state="recovering",
        revision=3,
        active=True,
        created_at="2099-09-01T10:00:00Z",
        updated_at="2099-09-01T10:00:00Z",
        target_at_utc=TARGET_TEXT,
    )
    snapshots: dict[Platform, PublicationSnapshot] = {}
    with database.transaction() as connection:
        database.insert_release(connection, release, actor="worker")
        for platform, publisher in publishers.items():
            payload_sha256 = _sha256(platform[0])
            request = PublicationRequest(
                release_id=release.release_id,
                platform=platform,
                idempotency_key=f"prepare:{release.release_id}:{platform}",
                payload_sha256=payload_sha256,
                payload={"platform": platform},
            )
            prepared = publisher.prepare(request)
            armed = publisher.arm(
                prepared.remote_id,
                target_at_utc=TARGET,
                operation_key=f"arm:{release.release_id}:{platform}:{TARGET.isoformat()}",
            )
            snapshot = (
                publisher.make_public(prepared.remote_id, public_at=TARGET)
                if platform in {"youtube", "dzen"}
                else armed
            )
            snapshots[platform] = snapshot
            PublicationStore.upsert(
                connection,
                release_id=release.release_id,
                snapshot=snapshot,
                target_at_utc=TARGET_TEXT,
                updated_at="2099-09-01T10:00:00Z",
                idempotency_key=request.idempotency_key,
                operation_key=(
                    f"execute:{release.release_id}:telegram:{TARGET.isoformat()}"
                    if platform == "telegram"
                    else f"arm:{release.release_id}:{platform}:{TARGET.isoformat()}"
                ),
            )
        JobStore.enqueue(
            connection,
            release_id=release.release_id,
            kind="publication_recovery",
            due_at=TARGET_TEXT,
            retry_policy_id="provider-30s-2m-5m-15m",
            idempotency_key=f"recovery:{release.release_id}:telegram:{TARGET.isoformat()}",
            payload={
                "release_id": release.release_id,
                "platform": "telegram",
                "target_at_utc": TARGET,
                "payload_sha256": snapshots["telegram"].payload_sha256,
                "publication_idempotency_key": f"prepare:{release.release_id}:telegram",
                "operation_key": f"execute:{release.release_id}:telegram:{TARGET.isoformat()}",
                "remote_id": snapshots["telegram"].remote_id,
            },
            now="2099-09-01T10:00:00Z",
        )
    for publisher in publishers.values():
        publisher.calls.clear()
    operations.clear()
    return database, publishers, operations, snapshots


def _claim(database: Database) -> JobClaim:
    with database.transaction() as connection:
        claim = JobStore.claim_due(
            connection,
            release_id="release-recovery",
            kind="publication_recovery",
            worker_id="recovery-worker",
            now=NOW.isoformat().replace("+00:00", "Z"),
        )
    assert claim is not None
    return claim


def test_gc04_statuses_all_receipts_before_only_missing_telegram_execute(
    tmp_path: Path,
) -> None:
    database, publishers, operations, before = _recovery_fixture(tmp_path)
    claim = _claim(database)

    outcome = reconcile_publication_recovery(
        database, claim=claim, publishers=publishers, now=NOW
    )

    assert outcome.state == "published"
    assert outcome.missing_platforms == ()
    assert operations == [
        "youtube.status",
        "dzen.status",
        "telegram.status",
        f"telegram.execute:execute:release-recovery:telegram:{TARGET.isoformat()}",
    ]
    assert all(
        call[0] not in {"prepare", "arm"}
        for publisher in publishers.values()
        for call in publisher.calls
    )
    with database.connect() as connection:
        release = database.release_by_id(connection, "release-recovery")
        rows = PublicationStore.rows(connection, "release-recovery")
        job = JobStore.rows(connection, "release-recovery")[0]
    assert release is not None and release.state == "published" and not release.active
    assert job["state"] == "succeeded"
    assert {row["state"] for row in rows} == {"public"}
    assert {
        str(row["platform"]): str(row["remote_id"])
        for row in rows
    } == {platform: snapshot.remote_id for platform, snapshot in before.items()}


def test_gc04_crash_after_telegram_side_effect_is_reconciled_without_second_execute(
    tmp_path: Path,
) -> None:
    class CrashAfterExecute(RecordingPublisher):
        crashed = False

        def execute(
            self, remote_id: str, *, operation_key: str, now: datetime
        ) -> PublicationSnapshot:
            result = super().execute(remote_id, operation_key=operation_key, now=now)
            if not self.crashed:
                self.crashed = True
                raise KeyboardInterrupt
            return result

    database, publishers, operations, before = _recovery_fixture(
        tmp_path,
        publisher_factory=lambda platform, trace: CrashAfterExecute(
            platform, operations=trace
        ),
    )
    first = _claim(database)

    with pytest.raises(KeyboardInterrupt):
        reconcile_publication_recovery(database, claim=first, publishers=publishers, now=NOW)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET lease_until = '2000-01-01T00:00:00Z' WHERE job_id = ?",
            (first.job_id,),
        )
    second = _claim(database)
    recovered = reconcile_publication_recovery(
        database, claim=second, publishers=publishers, now=NOW + timedelta(seconds=1)
    )

    assert recovered.state == "published"
    execute_calls = [entry for entry in operations if entry.startswith("telegram.execute:")]
    assert len(execute_calls) == 1
    with database.connect() as connection:
        telegram = next(
            row
            for row in PublicationStore.rows(connection, "release-recovery")
            if row["platform"] == "telegram"
        )
    assert telegram["remote_id"] == before["telegram"].remote_id


def test_recovery_status_failure_enters_exact_first_retry_window(tmp_path: Path) -> None:
    database, publishers, _, _ = _recovery_fixture(tmp_path)
    publishers["youtube"].fail("status")
    claim = _claim(database)

    outcome = reconcile_publication_recovery(
        database, claim=claim, publishers=publishers, now=NOW
    )

    assert outcome.state == "retry_wait"
    assert outcome.error is not None and outcome.error.code == "UNKNOWN_PROVIDER_OUTCOME"
    assert outcome.decision is not None and outcome.decision.retry_after_seconds == 30
    with database.connect() as connection:
        job = JobStore.rows(connection, "release-recovery")[0]
    assert job["state"] == "retry_wait"
    assert job["due_at"] == "2099-09-10T11:01:30Z"


def test_terminal_recovery_exposes_outcome_to_atomic_integration_recorder(
    tmp_path: Path,
) -> None:
    class PermissionDeniedYoutube(RecordingPublisher):
        def status(
            self, remote_id: str, *, now: datetime | None = None
        ) -> PublicationSnapshot:
            if self.platform == "youtube":
                raise PermissionError("forbidden")
            return super().status(remote_id, now=now)

    database, publishers, _, _ = _recovery_fixture(
        tmp_path,
        publisher_factory=lambda platform, trace: PermissionDeniedYoutube(
            platform, operations=trace
        ),
    )
    received: list[tuple[str, str]] = []
    claim = _claim(database)

    outcome = reconcile_publication_recovery(
        database,
        claim=claim,
        publishers=publishers,
        now=NOW,
        terminal_recorder=lambda _connection, terminal: received.append(
            (terminal.release_id, terminal.error.code if terminal.error else "")
        ),
    )

    assert outcome.state == "needs_attention"
    assert outcome.requires_attention
    assert received == [("release-recovery", "PROVIDER_PERMISSION_DENIED")]
    with database.connect() as connection:
        release = database.release_by_id(connection, "release-recovery")
        job = JobStore.rows(connection, "release-recovery")[0]
        event = connection.execute(
            "SELECT name FROM domain_events WHERE aggregate_id = ? "
            "ORDER BY occurred_at DESC LIMIT 1",
            ("release-recovery",),
        ).fetchone()
    assert release is not None and release.state == "needs_attention"
    assert job["state"] == "failed"
    assert event is not None and event["name"] == "RecoveryExhausted.v1"


def test_publication_store_backfills_legacy_nullable_identity_only_once(tmp_path: Path) -> None:
    database, _, _, snapshots = _recovery_fixture(tmp_path)
    telegram = snapshots["telegram"]
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE publications
            SET remote_id = NULL, idempotency_key = NULL, operation_key = NULL
            WHERE release_id = ? AND platform = 'telegram'
            """,
            ("release-recovery",),
        )
        connection.execute(
            """
            UPDATE publication_attempts
            SET remote_id = NULL, idempotency_key = NULL, operation_key = NULL
            WHERE release_id = ? AND platform = 'telegram' AND target_at_utc = ?
            """,
            ("release-recovery", TARGET_TEXT),
        )
        PublicationStore.upsert(
            connection,
            release_id="release-recovery",
            snapshot=telegram,
            target_at_utc=TARGET_TEXT,
            updated_at="2099-09-10T11:01:00Z",
            idempotency_key="prepare:release-recovery:telegram",
            operation_key=f"execute:release-recovery:telegram:{TARGET.isoformat()}",
        )
        with pytest.raises(ValueError, match="remote receipt"):
            PublicationStore.upsert(
                connection,
                release_id="release-recovery",
                snapshot=telegram.model_copy(update={"remote_id": "different-task"}),
                target_at_utc=TARGET_TEXT,
                updated_at="2099-09-10T11:01:01Z",
                idempotency_key="prepare:release-recovery:telegram",
                operation_key=f"execute:release-recovery:telegram:{TARGET.isoformat()}",
            )
    with database.connect() as connection:
        row = next(
            item
            for item in PublicationStore.rows(connection, "release-recovery")
            if item["platform"] == "telegram"
        )
    assert row["remote_id"] == telegram.remote_id
    assert row["idempotency_key"] == "prepare:release-recovery:telegram"
