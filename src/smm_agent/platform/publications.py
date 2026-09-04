"""SQLite persistence for per-platform publication state."""

import sqlite3
from datetime import UTC, datetime, timedelta

from smm_agent.contracts.publication import PublicationSnapshot


class PublicationStore:
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
    ) -> None:
        values = (
            release_id,
            snapshot.platform,
            snapshot.state,
            snapshot.payload_sha256,
            snapshot.remote_id,
            snapshot.known_url,
            target_at_utc,
            snapshot.public_at.isoformat().replace("+00:00", "Z")
            if snapshot.public_at
            else None,
            updated_at,
        )
        connection.execute(
            """
            INSERT INTO publications (
                release_id, platform, state, payload_sha256, remote_id,
                known_url, target_at_utc, public_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(release_id, platform) DO UPDATE SET
                state = excluded.state,
                payload_sha256 = excluded.payload_sha256,
                remote_id = excluded.remote_id,
                known_url = excluded.known_url,
                target_at_utc = excluded.target_at_utc,
                public_at = excluded.public_at,
                updated_at = excluded.updated_at
            WHERE publications.state != 'public' OR excluded.state = 'public'
            """,
            values,
        )
        connection.execute(
            """
            INSERT INTO publication_attempts (
                release_id, platform, state, payload_sha256, remote_id,
                known_url, target_at_utc, public_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(release_id, platform, target_at_utc) DO UPDATE SET
                state = excluded.state,
                payload_sha256 = excluded.payload_sha256,
                remote_id = excluded.remote_id,
                known_url = excluded.known_url,
                public_at = excluded.public_at,
                updated_at = excluded.updated_at
            WHERE publication_attempts.state != 'public' OR excluded.state = 'public'
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
