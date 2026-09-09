"""SQLite queries owned by the video bounded context."""

import sqlite3
from datetime import datetime
from typing import cast

from smm_agent.adapters.files.recording_watcher import FileObservation
from smm_agent.platform.ids import uuid7


def encode_file_identity(value: int) -> int | str:
    """Preserve wide Windows volume/file IDs without SQLite REAL rounding."""
    return value if -(2**63) <= value < 2**63 else hex(value)


def decode_file_identity(value: int | str) -> int:
    return int(value, 16) if isinstance(value, str) else int(value)


class VideoStore:
    @staticmethod
    def acquire_media_lease(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        release_revision: int,
        owner_id: str,
        now: str,
        lease_until: str,
    ) -> bool:
        cursor = connection.execute(
            """
            INSERT INTO media_leases (
                release_id, release_revision, owner_id, lease_until
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(release_id) DO UPDATE SET
                release_revision = excluded.release_revision,
                owner_id = excluded.owner_id,
                lease_until = excluded.lease_until
            WHERE media_leases.lease_until <= ?
               OR media_leases.release_revision != excluded.release_revision
            """,
            (release_id, release_revision, owner_id, lease_until, now),
        )
        return cursor.rowcount == 1

    @staticmethod
    def media_lease_owned(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        release_revision: int,
        owner_id: str,
        now: str,
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1 FROM media_leases
            WHERE release_id = ? AND release_revision = ? AND owner_id = ?
              AND lease_until > ?
            """,
            (release_id, release_revision, owner_id, now),
        ).fetchone()
        return row is not None

    @staticmethod
    def renew_media_lease(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        release_revision: int,
        owner_id: str,
        now: str,
        lease_until: str,
    ) -> bool:
        cursor = connection.execute(
            """
            UPDATE media_leases SET lease_until = ?
            WHERE release_id = ? AND release_revision = ? AND owner_id = ?
              AND lease_until > ?
            """,
            (lease_until, release_id, release_revision, owner_id, now),
        )
        return cursor.rowcount == 1

    @staticmethod
    def open_recording_window(
        connection: sqlite3.Connection, release_id: str, opened_at: str
    ) -> None:
        connection.execute(
            "UPDATE releases SET recording_window_opened_at = ? WHERE release_id = ?",
            (opened_at, release_id),
        )

    @staticmethod
    def reset_recording_window(
        connection: sqlite3.Connection, release_id: str, opened_at: str
    ) -> None:
        connection.execute(
            """
            UPDATE recording_candidates
            SET state = 'rejected', reason = 'recording window reset', last_seen_at = ?
            WHERE release_id = ? AND state IN ('observed', 'stabilizing')
            """,
            (opened_at, release_id),
        )
        connection.execute(
            """
            UPDATE releases
            SET recording_window_opened_at = ?, recording_watch_initialized_at = NULL
            WHERE release_id = ?
            """,
            (opened_at, release_id),
        )

    @staticmethod
    def invalidate_recording(connection: sqlite3.Connection, release_id: str) -> None:
        connection.execute(
            "UPDATE recordings SET valid = 0 WHERE release_id = ? AND valid = 1",
            (release_id,),
        )

    @staticmethod
    def insert_accepted_recording(
        connection: sqlite3.Connection,
        *,
        candidate_id: str,
        recording_id: str,
        release_id: str,
        observed_path: str,
        size: int,
        mtime_ns: int,
        ctime_ns: int,
        device: int,
        inode: int,
        fingerprint: str,
        source_artifact_id: str,
        duration_seconds: float,
        width: int,
        height: int,
        accepted_at: str,
    ) -> None:
        sql_device = encode_file_identity(device)
        sql_inode = encode_file_identity(inode)
        existing = connection.execute(
            """
            SELECT candidate_id FROM recording_candidates
            WHERE release_id = ? AND (
                (candidate_id = ? AND state = 'ambiguous')
                OR (observed_path = ? AND state IN ('observed', 'stabilizing'))
            )
            ORDER BY CASE WHEN candidate_id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (release_id, candidate_id, observed_path, candidate_id),
        ).fetchone()
        actual_candidate_id = str(existing["candidate_id"]) if existing else candidate_id
        if existing:
            connection.execute(
                """
                UPDATE recording_candidates
                SET size = ?, mtime_ns = ?, ctime_ns = ?, device = ?, inode = ?,
                    fingerprint = ?, state = 'accepted',
                    reason = NULL, last_seen_at = ?
                WHERE candidate_id = ?
                """,
                (
                    size,
                    mtime_ns,
                    ctime_ns,
                    sql_device,
                    sql_inode,
                    fingerprint,
                    accepted_at,
                    actual_candidate_id,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO recording_candidates (
                    candidate_id, release_id, observed_path, size, mtime_ns, ctime_ns,
                    device, inode,
                    fingerprint, state, reason, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', NULL, ?, ?)
                """,
                (
                    actual_candidate_id,
                    release_id,
                    observed_path,
                    size,
                    mtime_ns,
                    ctime_ns,
                    sql_device,
                    sql_inode,
                    fingerprint,
                    accepted_at,
                    accepted_at,
                ),
            )
        connection.execute(
            """
            INSERT INTO recordings (
                recording_id, release_id, candidate_id, source_artifact_id,
                duration_seconds, width, height, accepted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recording_id,
                release_id,
                actual_candidate_id,
                source_artifact_id,
                duration_seconds,
                width,
                height,
                accepted_at,
            ),
        )

    @staticmethod
    def video_inputs(
        connection: sqlite3.Connection, release_id: str
    ) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
        recording = connection.execute(
            """
            SELECT r.*, a.path AS source_path, a.sha256 AS source_sha256,
                   a.size AS source_size
            FROM recordings r JOIN artifact_versions a ON a.artifact_id = r.source_artifact_id
            WHERE r.release_id = ? AND r.valid = 1 AND a.valid = 1
            """,
            (release_id,),
        ).fetchone()
        if recording is None:
            raise RuntimeError("accepted recording is missing")
        visuals = connection.execute(
            """
            SELECT v.visual_id, v.position, v.anchor_text, a.path, a.sha256
            FROM visual_specs v JOIN artifact_versions a ON a.artifact_id = v.artifact_id
            WHERE v.release_id = ? AND a.valid = 1
            ORDER BY v.position
            """,
            (release_id,),
        ).fetchall()
        return cast(sqlite3.Row, recording), list(visuals)

    @staticmethod
    def observations(connection: sqlite3.Connection, release_id: str) -> dict[str, FileObservation]:
        rows = connection.execute(
            """
            SELECT observed_path, size, mtime_ns, ctime_ns, device, inode, first_seen_at
            FROM recording_candidates
            WHERE release_id = ? AND state IN ('observed', 'stabilizing')
            """,
            (release_id,),
        ).fetchall()
        return {
            str(row["observed_path"]): FileObservation(
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                ctime_ns=int(row["ctime_ns"]),
                device=decode_file_identity(row["device"]),
                inode=decode_file_identity(row["inode"]),
                unchanged_since=datetime.fromisoformat(
                    str(row["first_seen_at"]).replace("Z", "+00:00")
                ),
            )
            for row in rows
        }

    @staticmethod
    def watch_initialized(connection: sqlite3.Connection, release_id: str) -> bool:
        row = connection.execute(
            "SELECT recording_watch_initialized_at FROM releases WHERE release_id = ?",
            (release_id,),
        ).fetchone()
        return bool(row and row["recording_watch_initialized_at"])

    @staticmethod
    def recording_window_opened_at(connection: sqlite3.Connection, release_id: str) -> datetime:
        row = connection.execute(
            "SELECT recording_window_opened_at FROM releases WHERE release_id = ?",
            (release_id,),
        ).fetchone()
        if not row or not row["recording_window_opened_at"]:
            raise RuntimeError("recording window has not been opened")
        return datetime.fromisoformat(str(row["recording_window_opened_at"]).replace("Z", "+00:00"))

    @staticmethod
    def initialize_watch(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        baseline: dict[str, FileObservation],
        window_opened_at: datetime,
        initialized_at: str,
    ) -> None:
        connection.execute(
            "UPDATE releases SET recording_watch_initialized_at = ? WHERE release_id = ?",
            (initialized_at, release_id),
        )
        for path, observation in baseline.items():
            saved_at = datetime.fromtimestamp(
                max(observation.ctime_ns, observation.mtime_ns) / 1_000_000_000,
                tz=window_opened_at.tzinfo,
            )
            is_new = saved_at >= window_opened_at
            state = "stabilizing" if is_new else "rejected"
            reason = None if is_new else "present before recording window"
            connection.execute(
                """
                INSERT INTO recording_candidates (
                    candidate_id, release_id, observed_path, size, mtime_ns, ctime_ns,
                    device, inode,
                    state, reason, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid7(),
                    release_id,
                    path,
                    observation.size,
                    observation.mtime_ns,
                    observation.ctime_ns,
                    encode_file_identity(observation.device),
                    encode_file_identity(observation.inode),
                    state,
                    reason,
                    initialized_at,
                    initialized_at,
                ),
            )

    @staticmethod
    def save_observations(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        observations: dict[str, FileObservation],
        observed_at: str,
    ) -> None:
        for path, observation in observations.items():
            unchanged_since = observation.unchanged_since.isoformat().replace("+00:00", "Z")
            open_candidate = connection.execute(
                """
                SELECT candidate_id FROM recording_candidates
                WHERE release_id = ? AND observed_path = ?
                  AND state IN ('observed', 'stabilizing')
                """,
                (release_id, path),
            ).fetchone()
            rejected_same_identity = connection.execute(
                """
                SELECT 1 FROM recording_candidates
                WHERE release_id = ? AND observed_path = ? AND state = 'rejected'
                  AND ctime_ns = ? AND device = ? AND inode = ?
                  AND size = ? AND mtime_ns = ?
                """,
                (
                    release_id,
                    path,
                    observation.ctime_ns,
                    encode_file_identity(observation.device),
                    encode_file_identity(observation.inode),
                    observation.size,
                    observation.mtime_ns,
                ),
            ).fetchone()
            if rejected_same_identity:
                continue
            if open_candidate:
                connection.execute(
                    """
                    UPDATE recording_candidates
                    SET size = ?, mtime_ns = ?, ctime_ns = ?, device = ?, inode = ?,
                        first_seen_at = ?,
                        last_seen_at = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        observation.size,
                        observation.mtime_ns,
                        observation.ctime_ns,
                        encode_file_identity(observation.device),
                        encode_file_identity(observation.inode),
                        unchanged_since,
                        observed_at,
                        open_candidate["candidate_id"],
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO recording_candidates (
                        candidate_id, release_id, observed_path, size, mtime_ns, ctime_ns,
                        device, inode,
                        state, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'stabilizing', ?, ?)
                    """,
                    (
                        uuid7(),
                        release_id,
                        path,
                        observation.size,
                        observation.mtime_ns,
                        observation.ctime_ns,
                        encode_file_identity(observation.device),
                        encode_file_identity(observation.inode),
                        unchanged_since,
                        observed_at,
                    ),
                )

    @staticmethod
    def mark_ambiguous(
        connection: sqlite3.Connection, release_id: str, paths: list[str], observed_at: str
    ) -> None:
        connection.executemany(
            """
            UPDATE recording_candidates
            SET state = 'ambiguous', reason = 'multiple stable candidates', last_seen_at = ?
            WHERE release_id = ? AND observed_path = ?
            """,
            [(observed_at, release_id, path) for path in paths],
        )

    @staticmethod
    def ambiguous_candidates(connection: sqlite3.Connection, release_id: str) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT candidate_id, observed_path, size, mtime_ns, ctime_ns, device, inode
                FROM recording_candidates
                WHERE release_id = ? AND state = 'ambiguous'
                ORDER BY observed_path
                """,
                (release_id,),
            ).fetchall()
        )

    @staticmethod
    def candidate_for_selection(
        connection: sqlite3.Connection, release_id: str, candidate_id: str
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT candidate_id, observed_path, size, mtime_ns, ctime_ns, device, inode,
                       first_seen_at
                FROM recording_candidates
                WHERE release_id = ? AND candidate_id = ? AND state = 'ambiguous'
                """,
                (release_id, candidate_id),
            ).fetchone(),
        )
