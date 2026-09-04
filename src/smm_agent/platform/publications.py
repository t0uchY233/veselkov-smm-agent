"""SQLite persistence for per-platform publication state."""

import sqlite3
from datetime import UTC, datetime, timedelta

from smm_agent.contracts.publication import PublicationSnapshot


class PublicationStore:
    @staticmethod
    def _snapshot_public_at(snapshot: PublicationSnapshot) -> str | None:
        return (
            snapshot.public_at.isoformat().replace("+00:00", "Z")
            if snapshot.public_at
            else None
        )

    @staticmethod
    def _assert_immutable_receipt(
        existing: sqlite3.Row,
        *,
        snapshot: PublicationSnapshot,
        target_at_utc: str,
        idempotency_key: str | None,
        operation_key: str | None,
    ) -> None:
        """Reject a different remote publication before changing local state.

        A retry may repeat the exact same receipt.  It may also fill a nullable
        identity left by a pre-Slice-5 row after that identity has been
        reconciled from the provider.  It must never silently turn one remote
        publication into another one, or change the content/target of a public
        publication.
        """

        if str(existing["payload_sha256"]) != snapshot.payload_sha256:
            raise ValueError("Нельзя изменить payload уже созданной публикации.")
        if str(existing["target_at_utc"]) != target_at_utc:
            raise ValueError("Нельзя изменить target уже созданной публикации.")

        stored_remote_id = existing["remote_id"]
        if stored_remote_id is not None and str(stored_remote_id) != snapshot.remote_id:
            raise ValueError("Нельзя заменить remote receipt публикации.")
        stored_idempotency_key = existing["idempotency_key"]
        if (
            stored_idempotency_key is not None
            and idempotency_key is not None
            and str(stored_idempotency_key) != idempotency_key
        ):
            raise ValueError("Нельзя заменить idempotency key публикации.")
        stored_operation_key = existing["operation_key"]
        if (
            stored_operation_key is not None
            and operation_key is not None
            and str(stored_operation_key) != operation_key
        ):
            raise ValueError("Нельзя заменить operation key публикации.")

        stored_public_at = existing["public_at"]
        incoming_public_at = PublicationStore._snapshot_public_at(snapshot)
        if stored_public_at is not None:
            if snapshot.state != "public" or incoming_public_at != str(stored_public_at):
                raise ValueError("Нельзя изменить подтверждённую публичную публикацию.")
        elif str(existing["state"]) == "public" and snapshot.state != "public":
            raise ValueError("Нельзя вернуть публичную публикацию в непубличное состояние.")

    @staticmethod
    def acquire_lease(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        release_revision: int,
        owner_id: str,
        now: datetime,
        duration: timedelta = timedelta(minutes=20),
    ) -> bool:
        now_text = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        lease_until = (now.astimezone(UTC) + duration).isoformat().replace(
            "+00:00", "Z"
        )
        cursor = connection.execute(
            """
            INSERT INTO publication_leases (
                release_id, release_revision, owner_id, lease_until
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(release_id) DO UPDATE SET
                release_revision = excluded.release_revision,
                owner_id = excluded.owner_id,
                lease_until = excluded.lease_until
            WHERE publication_leases.lease_until <= ?
               OR publication_leases.owner_id = excluded.owner_id
            """,
            (release_id, release_revision, owner_id, lease_until, now_text),
        )
        return cursor.rowcount == 1

    @staticmethod
    def lease_owned(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        release_revision: int,
        owner_id: str,
        now: datetime,
    ) -> bool:
        now_text = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        row = connection.execute(
            """
            SELECT 1 FROM publication_leases
            WHERE release_id = ? AND release_revision = ? AND owner_id = ?
              AND lease_until > ?
            """,
            (release_id, release_revision, owner_id, now_text),
        ).fetchone()
        return row is not None

    @staticmethod
    def release_lease(
        connection: sqlite3.Connection, *, release_id: str, owner_id: str
    ) -> None:
        connection.execute(
            "DELETE FROM publication_leases WHERE release_id = ? AND owner_id = ?",
            (release_id, owner_id),
        )

    @staticmethod
    def upsert(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        snapshot: PublicationSnapshot,
        target_at_utc: str,
        updated_at: str,
        idempotency_key: str | None = None,
        operation_key: str | None = None,
    ) -> None:
        current = connection.execute(
            "SELECT * FROM publications WHERE release_id = ? AND platform = ?",
            (release_id, snapshot.platform),
        ).fetchone()
        if current is not None:
            PublicationStore._assert_immutable_receipt(
                current,
                snapshot=snapshot,
                target_at_utc=target_at_utc,
                idempotency_key=idempotency_key,
                operation_key=operation_key,
            )
        previous_attempt = connection.execute(
            """
            SELECT * FROM publication_attempts
            WHERE release_id = ? AND platform = ? AND target_at_utc = ?
            """,
            (release_id, snapshot.platform, target_at_utc),
        ).fetchone()
        if previous_attempt is not None:
            PublicationStore._assert_immutable_receipt(
                previous_attempt,
                snapshot=snapshot,
                target_at_utc=target_at_utc,
                idempotency_key=idempotency_key,
                operation_key=operation_key,
            )
        values = (
            release_id,
            snapshot.platform,
            snapshot.state,
            snapshot.payload_sha256,
            snapshot.remote_id,
            snapshot.known_url,
            target_at_utc,
            PublicationStore._snapshot_public_at(snapshot),
            idempotency_key,
            operation_key,
            updated_at,
        )
        connection.execute(
            """
            INSERT INTO publications (
                release_id, platform, state, payload_sha256, remote_id,
                known_url, target_at_utc, public_at, idempotency_key,
                operation_key, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(release_id, platform) DO UPDATE SET
                state = excluded.state,
                remote_id = coalesce(publications.remote_id, excluded.remote_id),
                known_url = coalesce(publications.known_url, excluded.known_url),
                public_at = coalesce(publications.public_at, excluded.public_at),
                idempotency_key = coalesce(
                    publications.idempotency_key, excluded.idempotency_key
                ),
                operation_key = coalesce(publications.operation_key, excluded.operation_key),
                updated_at = excluded.updated_at
            """,
            values,
        )
        connection.execute(
            """
            INSERT INTO publication_attempts (
                release_id, platform, state, payload_sha256, remote_id,
                known_url, target_at_utc, public_at, idempotency_key,
                operation_key, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(release_id, platform, target_at_utc) DO UPDATE SET
                state = excluded.state,
                remote_id = coalesce(publication_attempts.remote_id, excluded.remote_id),
                known_url = coalesce(publication_attempts.known_url, excluded.known_url),
                public_at = coalesce(publication_attempts.public_at, excluded.public_at),
                idempotency_key = coalesce(
                    publication_attempts.idempotency_key, excluded.idempotency_key
                ),
                operation_key = coalesce(
                    publication_attempts.operation_key, excluded.operation_key
                ),
                updated_at = excluded.updated_at
            """,
            values,
        )

    @staticmethod
    def rows(connection: sqlite3.Connection, release_id: str) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                "SELECT * FROM publications WHERE release_id = ? ORDER BY platform",
                (release_id,),
            ).fetchall()
        )

    @staticmethod
    def delete_release(connection: sqlite3.Connection, release_id: str) -> None:
        connection.execute("DELETE FROM publications WHERE release_id = ?", (release_id,))
