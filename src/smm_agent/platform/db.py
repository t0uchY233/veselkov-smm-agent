"""SQLite persistence and transaction boundary for release commands."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from smm_agent.domain.release.model import Release


class Database:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.path = data_root / "state" / "smm.sqlite3"

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migration = Path(__file__).parents[3] / "migrations" / "0001_initial.sql"
        with self.connect() as connection:
            connection.executescript(migration.read_text(encoding="utf-8"))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def find_command(connection: sqlite3.Connection, command_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
            "SELECT request_hash, outcome_json FROM command_results WHERE command_id = ?",
            (command_id,),
            ).fetchone(),
        )

    @staticmethod
    def active_release(connection: sqlite3.Connection) -> Release | None:
        row = connection.execute(
            "SELECT * FROM releases WHERE active = 1 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return _release_from_row(row) if row else None

    @staticmethod
    def release_by_id(connection: sqlite3.Connection, release_id: str) -> Release | None:
        row = connection.execute(
            "SELECT * FROM releases WHERE release_id = ?", (release_id,)
        ).fetchone()
        return _release_from_row(row) if row else None

    @staticmethod
    def insert_release(connection: sqlite3.Connection, release: Release, *, actor: str) -> None:
        connection.execute(
            """
            INSERT INTO releases (
                release_id, topic, state, revision, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                release.release_id,
                release.topic,
                release.state,
                release.revision,
                int(release.active),
                release.created_at,
                release.updated_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO domain_events (
                event_id, name, schema_version, occurred_at, aggregate_type,
                aggregate_id, actor, correlation_id, causation_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"event-{release.release_id}",
                "ReleaseStarted.v1",
                "1.0",
                release.created_at,
                "release",
                release.release_id,
                actor,
                release.release_id,
                None,
                json.dumps(
                    {"topic": release.topic, "release_revision": release.revision},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO transitions (
                transition_id, release_id, from_state, to_state, actor,
                reason, correlation_id, occurred_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                f"transition-{release.release_id}",
                release.release_id,
                release.state,
                actor,
                "UC-02 release start",
                release.release_id,
                release.created_at,
            ),
        )

    @staticmethod
    def save_command(
        connection: sqlite3.Connection,
        *,
        command_id: str,
        actor: str,
        command: str,
        request_hash: str,
        outcome: dict[str, object],
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO command_results (
                command_id, actor, command, request_hash, outcome_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                actor,
                command,
                request_hash,
                json.dumps(outcome, ensure_ascii=False, sort_keys=True),
                occurred_at,
            ),
        )


def _release_from_row(row: sqlite3.Row) -> Release:
    return Release(
        release_id=row["release_id"],
        topic=row["topic"],
        state=row["state"],
        revision=row["revision"],
        active=bool(row["active"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
