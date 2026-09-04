"""Durable publication jobs; execution and retry leases arrive in Slice 5."""

import json
import sqlite3
from typing import cast

from smm_agent.contracts.publication import (
    CancellationJobPayload,
    PreflightJobPayload,
    PublicationContract,
    TelegramJobPayload,
)
from smm_agent.platform.ids import uuid7

JOB_PAYLOADS: dict[str, type[PublicationContract]] = {
    "publication_preflight": PreflightJobPayload,
    "telegram_publish_reconcile": TelegramJobPayload,
    "publication_cancel_reconcile": CancellationJobPayload,
}


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
        connection.execute(
            """
            UPDATE job_attempts
            SET finished_at = ?, outcome = 'cancelled'
            WHERE attempt_id IN (
                SELECT lease_owner_id FROM jobs
                WHERE release_id = ? AND state = 'running'
            )
            """,
            (now, release_id),
        )
        connection.execute(
            """
            UPDATE jobs
            SET state = 'cancelled', lease_until = NULL, lease_owner_id = NULL,
                updated_at = ?
            WHERE release_id = ? AND state IN ('queued', 'running', 'retry_wait')
            """,
            (now, release_id),
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
        release_id: str,
        kind: str,
        now: str,
        lease_until: str,
        owner_id: str,
    ) -> sqlite3.Row | None:
        candidate = connection.execute(
            """
            SELECT job_id FROM jobs
            WHERE release_id = ? AND kind = ? AND due_at <= ?
              AND (state IN ('queued', 'retry_wait')
                   OR (state = 'running' AND lease_until <= ?))
            ORDER BY due_at LIMIT 1
            """,
            (release_id, kind, now, now),
        ).fetchone()
        if candidate is None:
            return None
        cursor = connection.execute(
            """
            UPDATE jobs
            SET state = 'running', lease_until = ?, lease_owner_id = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE job_id = ?
              AND (state IN ('queued', 'retry_wait')
                   OR (state = 'running' AND lease_until <= ?))
            """,
            (lease_until, owner_id, now, candidate["job_id"], now),
        )
        if cursor.rowcount != 1:
            return None
        connection.execute(
            """
            INSERT INTO job_attempts (attempt_id, job_id, started_at)
            VALUES (?, ?, ?)
            """,
            (owner_id, candidate["job_id"], now),
        )
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (candidate["job_id"],)
            ).fetchone(),
        )

    @staticmethod
    def mark_succeeded(
        connection: sqlite3.Connection, *, job_id: str, owner_id: str, now: str
    ) -> bool:
        cursor = connection.execute(
            """
            UPDATE jobs
            SET state = 'succeeded', lease_until = NULL, lease_owner_id = NULL,
                updated_at = ?
            WHERE job_id = ? AND state = 'running' AND lease_owner_id = ?
            """,
            (now, job_id, owner_id),
        )
        if cursor.rowcount == 1:
            connection.execute(
                """
                UPDATE job_attempts SET finished_at = ?, outcome = 'succeeded'
                WHERE attempt_id = ?
                """,
                (now, owner_id),
            )
        return cursor.rowcount == 1
