from pathlib import Path

import pytest

from smm_agent.contracts.publication import RecoveryError
from smm_agent.platform.db import Database
from smm_agent.platform.jobs import JobClaim, JobStore

NOW = "2026-09-04T10:00:00Z"


def _database(tmp_path: Path) -> tuple[Database, str, str]:
    database = Database(tmp_path / "data")
    database.initialize()
    release_id = "release-jobs"
    job_id = "job-jobs"
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO releases (
                release_id, topic, state, revision, active, created_at, updated_at
            ) VALUES (?, 'Тема', 'scheduled', 1, 1, ?, ?)
            """,
            (release_id, NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, release_id, kind, state, due_at, attempts, retry_policy_id,
                idempotency_key, payload_json, created_at, updated_at
            ) VALUES (?, ?, 'publication_preflight', 'queued', ?, 0,
                'provider-30s-2m-5m-15m', 'job-jobs-key', '{}', ?, ?)
            """,
            (job_id, release_id, NOW, NOW, NOW),
        )
    return database, release_id, job_id


def _claim(database: Database, release_id: str, *, now: str = NOW) -> JobClaim:
    with database.transaction() as connection:
        claim = JobStore.claim_due(
            connection,
            release_id=release_id,
            kind="publication_preflight",
            worker_id="worker-a",
            now=now,
        )
    assert claim is not None
    return claim


def test_claim_creates_fenced_attempt_and_default_sixty_second_lease(tmp_path: Path) -> None:
    database, release_id, job_id = _database(tmp_path)

    claim = _claim(database, release_id)

    assert claim.job_id == job_id
    assert claim.attempt_id != claim.worker_id
    assert claim.lease_epoch == 1
    assert claim.lease_until == "2026-09-04T10:01:00Z"
    with database.connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        attempt = connection.execute(
            "SELECT * FROM job_attempts WHERE attempt_id = ?", (claim.attempt_id,)
        ).fetchone()
    assert job is not None
    assert job["state"] == "running"
    assert job["active_attempt_id"] == claim.attempt_id
    assert job["lease_epoch"] == 1
    assert job["last_heartbeat_at"] == NOW
    assert attempt is not None
    assert attempt["worker_id"] == "worker-a"
    assert attempt["fence_token"] == claim.attempt_id
    assert attempt["lease_epoch"] == 1


def test_expired_claim_is_closed_before_reclaim_with_next_epoch(tmp_path: Path) -> None:
    database, release_id, job_id = _database(tmp_path)
    first = _claim(database, release_id)

    with database.transaction() as connection:
        second = JobStore.claim_due(
            connection,
            release_id=release_id,
            kind="publication_preflight",
            worker_id="worker-b",
            now="2026-09-04T10:01:01Z",
        )
    assert second is not None
    assert second.attempt_id != first.attempt_id
    assert second.lease_epoch == 2
    with database.connect() as connection:
        old_attempt = connection.execute(
            "SELECT outcome, finished_at, error_code FROM job_attempts WHERE attempt_id = ?",
            (first.attempt_id,),
        ).fetchone()
        job = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert old_attempt is not None
    assert tuple(old_attempt) == ("lease_expired", "2026-09-04T10:01:01Z", "LEASE_EXPIRED")
    assert job is not None and job["active_attempt_id"] == second.attempt_id


def test_legacy_running_job_without_a_lease_is_reclaimable(tmp_path: Path) -> None:
    database, release_id, job_id = _database(tmp_path)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'running', attempts = 1 WHERE job_id = ?", (job_id,)
        )
        claim = JobStore.claim_due(
            connection,
            release_id=release_id,
            kind="publication_preflight",
            worker_id="worker-recovery",
            now=NOW,
        )
    assert claim is not None
    assert claim.lease_epoch == 1
    with database.connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert job is not None
    assert job["state"] == "running"
    assert job["lease_until"] == "2026-09-04T10:01:00Z"


def test_stale_claim_cannot_heartbeat_finish_retry_or_fail(tmp_path: Path) -> None:
    database, release_id, _ = _database(tmp_path)
    first = _claim(database, release_id)
    error = RecoveryError(code="PROVIDER_TIMEOUT", sanitized_detail="Сетевая ошибка")
    with database.transaction() as connection:
        second = JobStore.claim_due(
            connection,
            release_id=release_id,
            kind="publication_preflight",
            worker_id="worker-b",
            now="2026-09-04T10:01:01Z",
        )
        assert second is not None
        assert not JobStore.heartbeat(connection, claim=first, now="2026-09-04T10:01:02Z")
        assert not JobStore.mark_succeeded(connection, claim=first, now="2026-09-04T10:01:02Z")
        assert not JobStore.mark_retry_wait(
            connection,
            claim=first,
            error=error,
            due_at="2026-09-04T10:02:00Z",
            now="2026-09-04T10:01:02Z",
        )
        assert not JobStore.mark_failed(
            connection, claim=first, error=error, now="2026-09-04T10:01:02Z"
        )
        assert JobStore.heartbeat(connection, claim=second, now="2026-09-04T10:01:02Z")
        assert JobStore.mark_succeeded(connection, claim=second, now="2026-09-04T10:01:03Z")


def test_retry_persists_sanitized_error_and_exact_due_time(tmp_path: Path) -> None:
    database, release_id, job_id = _database(tmp_path)
    first = _claim(database, release_id)
    error = RecoveryError(code="PROVIDER_HTTP_429", sanitized_detail="HTTP 429")
    due_at = "2026-09-04T10:02:30Z"
    with database.transaction() as connection:
        assert JobStore.mark_retry_wait(
            connection, claim=first, error=error, due_at=due_at, now="2026-09-04T10:00:20Z"
        )
        assert (
            JobStore.claim_due(
                connection,
                release_id=release_id,
                kind="publication_preflight",
                worker_id="worker-b",
                now="2026-09-04T10:02:29Z",
            )
            is None
        )
    with database.transaction() as connection:
        second = JobStore.claim_due(
            connection,
            release_id=release_id,
            kind="publication_preflight",
            worker_id="worker-b",
            now=due_at,
        )
    assert second is not None and second.lease_epoch == 2
    with database.connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        attempt = connection.execute(
            "SELECT * FROM job_attempts WHERE attempt_id = ?", (first.attempt_id,)
        ).fetchone()
    assert job is not None
    assert job["due_at"] == due_at
    assert job["last_error_code"] == "PROVIDER_HTTP_429"
    assert job["last_error_detail"] == "HTTP 429"
    assert attempt is not None
    assert attempt["outcome"] == "retry_wait"
    assert attempt["error_code"] == "PROVIDER_HTTP_429"


def test_terminal_failure_persists_sanitized_error_and_closes_attempt(tmp_path: Path) -> None:
    database, release_id, job_id = _database(tmp_path)
    claim = _claim(database, release_id)
    error = RecoveryError(
        code="PROVIDER_PERMISSION_DENIED", sanitized_detail="Нет права на публикацию"
    )

    with database.transaction() as connection:
        assert JobStore.mark_failed(
            connection, claim=claim, error=error, now="2026-09-04T10:00:20Z"
        )
    with database.connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        attempt = connection.execute(
            "SELECT * FROM job_attempts WHERE attempt_id = ?", (claim.attempt_id,)
        ).fetchone()
    assert job is not None
    assert job["state"] == "failed"
    assert job["last_error_code"] == "PROVIDER_PERMISSION_DENIED"
    assert job["last_error_detail"] == "Нет права на публикацию"
    assert attempt is not None
    assert attempt["outcome"] == "failed"
    assert attempt["finished_at"] == "2026-09-04T10:00:20Z"


def test_terminal_error_cannot_be_scheduled_for_retry(tmp_path: Path) -> None:
    database, release_id, _ = _database(tmp_path)
    claim = _claim(database, release_id)
    error = RecoveryError(
        code="PROVIDER_AUTH_REQUIRED", sanitized_detail="Нужно войти в аккаунт"
    )

    with database.transaction() as connection, pytest.raises(ValueError, match="Терминальная"):
        JobStore.mark_retry_wait(
            connection,
            claim=claim,
            error=error,
            due_at="2026-09-04T10:02:00Z",
            now="2026-09-04T10:00:20Z",
        )


def test_cancel_closes_active_attempt_and_prevents_late_completion(tmp_path: Path) -> None:
    database, release_id, job_id = _database(tmp_path)
    claim = _claim(database, release_id)

    with database.transaction() as connection:
        JobStore.cancel_release_jobs(connection, release_id=release_id, now="2026-09-04T10:00:20Z")
        assert not JobStore.mark_succeeded(connection, claim=claim, now="2026-09-04T10:00:21Z")
    with database.connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        attempt = connection.execute(
            "SELECT * FROM job_attempts WHERE attempt_id = ?", (claim.attempt_id,)
        ).fetchone()
    assert job is not None
    assert job["state"] == "cancelled"
    assert job["active_attempt_id"] is None
    assert attempt is not None
    assert tuple(attempt)[3:5] == ("2026-09-04T10:00:20Z", "cancelled")
