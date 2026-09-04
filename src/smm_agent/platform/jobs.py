"""Fenced durable jobs backed by SQLite.

The queue is deliberately small, but it must survive a process crash. A claim
is therefore more than a worker name: every execution gets an immutable
attempt id and a monotonically increasing lease epoch. All state-changing
operations fence on both values, so an expired worker cannot finish or
reschedule work claimed by its replacement.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from smm_agent.contracts.publication import (
    RETRYABLE_RECOVERY_ERROR_CODES,
    CancellationJobPayload,
    NotificationJobPayload,
    PreflightJobPayload,
    PublicationContract,
    RecoveryError,
    RecoveryJobPayload,
    TelegramJobPayload,
)
from smm_agent.platform.ids import uuid7
from smm_agent.platform.redaction import sanitize_recovery_error

DEFAULT_LEASE_SECONDS = 60

JOB_PAYLOADS: dict[str, type[PublicationContract]] = {
    "publication_preflight": PreflightJobPayload,
    "telegram_publish_reconcile": TelegramJobPayload,
    "publication_cancel_reconcile": CancellationJobPayload,
    "publication_recovery": RecoveryJobPayload,
    "notification_deliver": NotificationJobPayload,
}


@dataclass(frozen=True, slots=True)
class JobClaim:
    """The stable authority to mutate one running job.

    ``__getitem__`` is a narrow compatibility bridge for the Slice 4 worker,
    which still reads ``job_id`` and ``payload_json`` as if a claim were a
    SQLite row. New callers must use the named fields.
    """

    job_id: str
    attempt_id: str
    lease_epoch: int
    worker_id: str
    lease_until: str
    release_id: str
    kind: str
    payload_json: str
    due_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row, *, attempt_id: str, worker_id: str) -> "JobClaim":
        return cls(
            job_id=str(row["job_id"]),
            attempt_id=attempt_id,
            lease_epoch=int(row["lease_epoch"]),
            worker_id=worker_id,
            lease_until=str(row["lease_until"]),
            release_id=str(row["release_id"]),
            kind=str(row["kind"]),
            payload_json=str(row["payload_json"]),
            due_at=str(row["due_at"]),
        )

    def __getitem__(self, key: str) -> str | int:
        values: dict[str, str | int] = {
            "job_id": self.job_id,
            "lease_epoch": self.lease_epoch,
            "lease_owner_id": self.worker_id,
            "active_attempt_id": self.attempt_id,
            "lease_until": self.lease_until,
            "release_id": self.release_id,
            "kind": self.kind,
            "payload_json": self.payload_json,
            "due_at": self.due_at,
        }
        return values[key]


def _lease_after(now: str, *, seconds: int) -> str:
    if seconds <= 0:
        raise ValueError("Длительность lease должна быть положительной.")
    instant = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("Время lease должно содержать timezone.")
    return (instant.astimezone(UTC) + timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z"
    )


class JobStore:
    @staticmethod
    def enqueue(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        kind: str,
        due_at: str,
        retry_policy_id: str,
        idempotency_key: str,
        payload: dict[str, object],
        now: str,
    ) -> str:
        contract = JOB_PAYLOADS.get(kind)
        if contract is None:
            raise ValueError(f"Неизвестный вид publication job: {kind}")
        validated_payload = contract.model_validate(payload).model_dump(mode="json")
        existing = connection.execute(
            "SELECT job_id FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing:
            return str(existing["job_id"])
        job_id = uuid7()
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, release_id, kind, state, due_at, attempts,
                retry_policy_id, idempotency_key, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                release_id,
                kind,
                due_at,
                retry_policy_id,
                idempotency_key,
                json.dumps(validated_payload, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        return job_id

    @staticmethod
    def cancel_release_jobs(
        connection: sqlite3.Connection, *, release_id: str, now: str
    ) -> None:
        """Cancel unfinished jobs and close their current attempts atomically."""

        connection.execute("SAVEPOINT cancel_release_jobs")
        try:
            connection.execute(
                """
                UPDATE job_attempts
                SET finished_at = ?, outcome = 'cancelled'
                WHERE attempt_id IN (
                    SELECT active_attempt_id FROM jobs
                    WHERE release_id = ? AND state = 'running'
                ) AND finished_at IS NULL
                """,
                (now, release_id),
            )
            connection.execute(
                """
                UPDATE jobs
                SET state = 'cancelled', lease_until = NULL, lease_owner_id = NULL,
                    active_attempt_id = NULL, updated_at = ?
                WHERE release_id = ? AND state IN ('queued', 'running', 'retry_wait')
                """,
                (now, release_id),
            )
        except BaseException:
            connection.execute("ROLLBACK TO cancel_release_jobs")
            connection.execute("RELEASE cancel_release_jobs")
            raise
        connection.execute("RELEASE cancel_release_jobs")

    @staticmethod
    def cancel_kind_jobs(
        connection: sqlite3.Connection, *, release_id: str, kind: str, now: str
    ) -> None:
        """Close obsolete work without invoking any provider side effect."""

        connection.execute(
            """
            UPDATE job_attempts
            SET finished_at = ?, outcome = 'cancelled', detail = 'Задача устарела.'
            WHERE attempt_id IN (
                SELECT active_attempt_id FROM jobs
                WHERE release_id = ? AND kind = ? AND state = 'running'
            ) AND finished_at IS NULL
            """,
            (now, release_id, kind),
        )
        connection.execute(
            """
            UPDATE jobs
            SET state = 'cancelled', lease_until = NULL, lease_owner_id = NULL,
                active_attempt_id = NULL, updated_at = ?
            WHERE release_id = ? AND kind = ? AND state IN ('queued', 'running', 'retry_wait')
            """,
            (now, release_id, kind),
        )

    @staticmethod
    def rows(connection: sqlite3.Connection, release_id: str) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                "SELECT * FROM jobs WHERE release_id = ? ORDER BY due_at, kind",
                (release_id,),
            ).fetchall()
        )

    @staticmethod
    def claim_due(
        connection: sqlite3.Connection,
        *,
        now: str,
        release_id: str | None = None,
        kind: str | None = None,
        worker_id: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        lease_until: str | None = None,
        owner_id: str | None = None,
    ) -> JobClaim | None:
        """Atomically claim a due or abandoned job.

        ``owner_id`` and an explicit ``lease_until`` remain as a temporary
        bridge for the Slice 4 worker. New code supplies a stable ``worker_id``
        and uses the mandatory 60-second lease (or a duration for tests).
        """

        if worker_id is not None and owner_id is not None and worker_id != owner_id:
            raise ValueError("worker_id и устаревший owner_id не могут расходиться.")
        actual_worker_id = worker_id or owner_id or uuid7()
        actual_lease_until = lease_until or _lease_after(now, seconds=lease_seconds)
        predicates = [
            "((state IN ('queued', 'retry_wait') AND due_at <= ?) "
            "OR (state = 'running' AND (lease_until IS NULL OR lease_until <= ?)))"
        ]
        parameters: list[object] = [now, now]
        if release_id is not None:
            predicates.append("release_id = ?")
            parameters.append(release_id)
        if kind is not None:
            predicates.append("kind = ?")
            parameters.append(kind)
        where_clause = " AND ".join(predicates)

        connection.execute("SAVEPOINT claim_due")
        try:
            candidate = cast(
                sqlite3.Row | None,
                connection.execute(
                    f"""
                    SELECT job_id, active_attempt_id FROM jobs
                    WHERE {where_clause}
                    ORDER BY due_at, job_id LIMIT 1
                    """,
                    parameters,
                ).fetchone(),
            )
            if candidate is None:
                connection.execute("RELEASE claim_due")
                return None
            attempt_id = uuid7()
            legacy_fence_token = owner_id or attempt_id
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = 'running', lease_until = ?, lease_owner_id = ?,
                    active_attempt_id = ?, lease_epoch = lease_epoch + 1,
                    last_heartbeat_at = ?, attempts = attempts + 1, updated_at = ?
                WHERE job_id = ?
                  AND ((state IN ('queued', 'retry_wait') AND due_at <= ?)
                       OR (state = 'running' AND (lease_until IS NULL OR lease_until <= ?)))
                """,
                (
                    actual_lease_until,
                    actual_worker_id,
                    attempt_id,
                    now,
                    now,
                    candidate["job_id"],
                    now,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                connection.execute("RELEASE claim_due")
                return None
            previous_attempt_id = candidate["active_attempt_id"]
            if previous_attempt_id is not None:
                connection.execute(
                    """
                    UPDATE job_attempts
                    SET finished_at = ?, outcome = 'lease_expired',
                        error_code = 'LEASE_EXPIRED', detail = 'Lease истекла до завершения.'
                    WHERE attempt_id = ? AND finished_at IS NULL
                    """,
                    (now, previous_attempt_id),
                )
            row = cast(
                sqlite3.Row,
                connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (candidate["job_id"],)
                ).fetchone(),
            )
            connection.execute(
                """
                INSERT INTO job_attempts (
                    attempt_id, job_id, started_at, worker_id, fence_token,
                    lease_epoch, last_heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    candidate["job_id"],
                    now,
                    actual_worker_id,
                    legacy_fence_token,
                    row["lease_epoch"],
                    now,
                ),
            )
            claim = JobClaim.from_row(row, attempt_id=attempt_id, worker_id=actual_worker_id)
        except BaseException:
            connection.execute("ROLLBACK TO claim_due")
            connection.execute("RELEASE claim_due")
            raise
        connection.execute("RELEASE claim_due")
        return claim

    @staticmethod
    def heartbeat(
        connection: sqlite3.Connection,
        *,
        claim: JobClaim,
        now: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        lease_until: str | None = None,
    ) -> bool:
        """Extend one live fenced lease; a stale claim is a harmless no-op."""

        renewed_until = lease_until or _lease_after(now, seconds=lease_seconds)
        connection.execute("SAVEPOINT job_heartbeat")
        try:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET lease_until = ?, last_heartbeat_at = ?, updated_at = ?
                WHERE job_id = ? AND state = 'running' AND active_attempt_id = ?
                  AND lease_epoch = ? AND lease_until > ?
                """,
                (
                    renewed_until,
                    now,
                    now,
                    claim.job_id,
                    claim.attempt_id,
                    claim.lease_epoch,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                connection.execute("RELEASE job_heartbeat")
                return False
            attempt = connection.execute(
                """
                UPDATE job_attempts SET last_heartbeat_at = ?
                WHERE attempt_id = ? AND job_id = ? AND lease_epoch = ? AND finished_at IS NULL
                """,
                (now, claim.attempt_id, claim.job_id, claim.lease_epoch),
            )
            if attempt.rowcount != 1:
                raise RuntimeError("Активный job attempt отсутствует после fenced heartbeat.")
        except BaseException:
            connection.execute("ROLLBACK TO job_heartbeat")
            connection.execute("RELEASE job_heartbeat")
            raise
        connection.execute("RELEASE job_heartbeat")
        return True

    @staticmethod
    def is_live_claim(
        connection: sqlite3.Connection, *, claim: JobClaim, now: str
    ) -> bool:
        """Check the complete fence before a second table is mutated."""

        return (
            connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE job_id = ? AND state = 'running' AND active_attempt_id = ?
                  AND lease_epoch = ? AND lease_until > ?
                """,
                (claim.job_id, claim.attempt_id, claim.lease_epoch, now),
            ).fetchone()
            is not None
        )

    @staticmethod
    def mark_succeeded(
        connection: sqlite3.Connection,
        *,
        now: str,
        claim: JobClaim | None = None,
        job_id: str | None = None,
        active_attempt_id: str | None = None,
        lease_epoch: int | None = None,
        owner_id: str | None = None,
    ) -> bool:
        resolved = JobStore._resolve_claim(
            connection,
            claim=claim,
            job_id=job_id,
            active_attempt_id=active_attempt_id,
            lease_epoch=lease_epoch,
            owner_id=owner_id,
        )
        if resolved is None:
            return False
        return JobStore._finish(
            connection, claim=resolved, now=now, outcome="succeeded", state="succeeded"
        )

    @staticmethod
    def mark_retry_wait(
        connection: sqlite3.Connection,
        *,
        claim: JobClaim,
        error: RecoveryError,
        due_at: str,
        now: str,
    ) -> bool:
        """Record a retryable failure. The caller supplies the exact due time."""

        if error.code not in RETRYABLE_RECOVERY_ERROR_CODES:
            raise ValueError("Терминальная ошибка не может переводить job в retry_wait.")
        return JobStore._finish(
            connection,
            claim=claim,
            now=now,
            outcome="retry_wait",
            state="retry_wait",
            due_at=due_at,
            error=error,
        )

    @staticmethod
    def mark_failed(
        connection: sqlite3.Connection,
        *,
        claim: JobClaim,
        error: RecoveryError,
        now: str,
    ) -> bool:
        """Persist a terminal/exhausted sanitized error and close the attempt."""

        return JobStore._finish(
            connection,
            claim=claim,
            now=now,
            outcome="failed",
            state="failed",
            error=error,
        )

    @staticmethod
    def _resolve_claim(
        connection: sqlite3.Connection,
        *,
        claim: JobClaim | None,
        job_id: str | None,
        active_attempt_id: str | None,
        lease_epoch: int | None,
        owner_id: str | None,
    ) -> JobClaim | None:
        if claim is not None:
            fence_values = (job_id, active_attempt_id, lease_epoch, owner_id)
            if any(value is not None for value in fence_values):
                raise ValueError("Передайте JobClaim либо его fence-поля, но не оба варианта.")
            return claim
        if job_id is not None and active_attempt_id is not None and lease_epoch is not None:
            row = cast(
                sqlite3.Row | None,
                connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone(),
            )
            if row is None:
                return None
            return JobClaim(
                job_id=job_id,
                attempt_id=active_attempt_id,
                lease_epoch=lease_epoch,
                worker_id=str(row["lease_owner_id"] or ""),
                lease_until=str(row["lease_until"] or ""),
                release_id=str(row["release_id"]),
                kind=str(row["kind"]),
                payload_json=str(row["payload_json"]),
                due_at=str(row["due_at"]),
            )
        if job_id is not None and owner_id is not None:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT jobs.* FROM jobs
                    JOIN job_attempts ON job_attempts.attempt_id = jobs.active_attempt_id
                    WHERE jobs.job_id = ? AND jobs.state = 'running'
                      AND job_attempts.fence_token = ?
                    """,
                    (job_id, owner_id),
                ).fetchone(),
            )
            if row is None or row["active_attempt_id"] is None:
                return None
            return JobClaim.from_row(
                row, attempt_id=str(row["active_attempt_id"]), worker_id=owner_id
            )
        raise ValueError("Для изменения job нужен JobClaim или полный fence.")

    @staticmethod
    def _finish(
        connection: sqlite3.Connection,
        *,
        claim: JobClaim,
        now: str,
        outcome: str,
        state: str,
        due_at: str | None = None,
        error: RecoveryError | None = None,
    ) -> bool:
        persisted_error = sanitize_recovery_error(error) if error is not None else None
        assignments = [
            "state = ?",
            "lease_until = NULL",
            "lease_owner_id = NULL",
            "active_attempt_id = NULL",
            "updated_at = ?",
        ]
        parameters: list[object] = [state, now]
        if due_at is not None:
            assignments.append("due_at = ?")
            parameters.append(due_at)
        if persisted_error is not None:
            assignments.extend(
                [
                    "last_error_code = ?",
                    "last_error_detail = ?",
                    "last_error_at = ?",
                ]
            )
            parameters.extend(
                [persisted_error.code, persisted_error.sanitized_detail, now]
            )
        parameters.extend([claim.job_id, claim.attempt_id, claim.lease_epoch, now])
        connection.execute("SAVEPOINT finish_job")
        try:
            cursor = connection.execute(
                f"""
                UPDATE jobs SET {", ".join(assignments)}
                WHERE job_id = ? AND state = 'running' AND active_attempt_id = ?
                  AND lease_epoch = ? AND lease_until > ?
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                connection.execute("RELEASE finish_job")
                return False
            attempt_parameters: list[object] = [now, outcome]
            attempt_assignments = ["finished_at = ?", "outcome = ?"]
            if persisted_error is not None:
                attempt_assignments.extend(["error_code = ?", "detail = ?"])
                attempt_parameters.extend(
                    [persisted_error.code, persisted_error.sanitized_detail]
                )
            attempt_parameters.extend([claim.attempt_id, claim.job_id, claim.lease_epoch])
            attempt = connection.execute(
                f"""
                UPDATE job_attempts SET {", ".join(attempt_assignments)}
                WHERE attempt_id = ? AND job_id = ? AND lease_epoch = ? AND finished_at IS NULL
                """,
                attempt_parameters,
            )
            if attempt.rowcount != 1:
                raise RuntimeError("Активный job attempt отсутствует после fenced завершения.")
        except BaseException:
            connection.execute("ROLLBACK TO finish_job")
            connection.execute("RELEASE finish_job")
            raise
        connection.execute("RELEASE finish_job")
        return True
