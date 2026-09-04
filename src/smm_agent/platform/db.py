"""SQLite persistence and transaction boundary for release commands."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import cast

from smm_agent.domain.release.model import Release


class Database:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.path = data_root / "state" / "smm.sqlite3"

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            repo_migrations = Path(__file__).parents[3] / "migrations"
            migrations = (
                list(repo_migrations.glob("[0-9][0-9][0-9][0-9]_*.sql"))
                if repo_migrations.is_dir()
                else [
                    item
                    for item in files("smm_agent").joinpath("migrations").iterdir()
                    if item.name[:4].isdigit() and item.name.endswith(".sql")
                ]
            )
            for migration in sorted(migrations, key=lambda item: item.name):
                version = int(migration.name.split("_", 1)[0])
                migration_table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
                applied = (
                    connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                    ).fetchone()
                    if migration_table_exists
                    else None
                )
                if applied:
                    continue
                script = migration.read_text(encoding="utf-8")
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + script
                    + "\nINSERT INTO schema_migrations(version, applied_at) "
                    + f"VALUES ({version}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                    + "COMMIT;"
                )

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
    def update_release(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        expected_revision: int,
        state: str,
        updated_at: str,
        revision_target: str | None = None,
    ) -> int:
        cursor = connection.execute(
            """
            UPDATE releases
            SET state = ?, revision = revision + 1, updated_at = ?, revision_target = ?
            WHERE release_id = ? AND revision = ?
            """,
            (state, updated_at, revision_target, release_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("stale release revision")
        return expected_revision + 1

    @staticmethod
    def add_transition(
        connection: sqlite3.Connection,
        *,
        transition_id: str,
        release_id: str,
        from_state: str,
        to_state: str,
        actor: str,
        reason: str,
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO transitions (
                transition_id, release_id, from_state, to_state, actor,
                reason, correlation_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition_id,
                release_id,
                from_state,
                to_state,
                actor,
                reason,
                release_id,
                occurred_at,
            ),
        )

    @staticmethod
    def add_event(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        name: str,
        occurred_at: str,
        release_id: str,
        actor: str,
        payload: dict[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO domain_events (
                event_id, name, schema_version, occurred_at, aggregate_type,
                aggregate_id, actor, correlation_id, causation_id, payload_json
            ) VALUES (?, ?, '1.0', ?, 'release', ?, ?, ?, NULL, ?)
            """,
            (
                event_id,
                name,
                occurred_at,
                release_id,
                actor,
                release_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )

    @staticmethod
    def next_artifact_version(
        connection: sqlite3.Connection, release_id: str, kind: str
    ) -> int:
        row = connection.execute(
            "SELECT coalesce(max(version), 0) + 1 FROM artifact_versions "
            "WHERE release_id = ? AND kind = ?",
            (release_id, kind),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def insert_artifact(
        connection: sqlite3.Connection,
        *,
        artifact_id: str,
        release_id: str,
        kind: str,
        version: int,
        path: str,
        sha256: str,
        size: int,
        media_type: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO artifact_versions (
                artifact_id, release_id, kind, version, path, sha256, size,
                media_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, release_id, kind, version, path, sha256, size, media_type, created_at),
        )

    @staticmethod
    def insert_approval(
        connection: sqlite3.Connection,
        *,
        approval_id: str,
        release_id: str,
        gate: str,
        release_revision: int,
        artifact_set_hash: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO approvals (
                approval_id, release_id, gate, decision, release_revision,
                artifact_set_hash, created_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
            """,
            (approval_id, release_id, gate, release_revision, artifact_set_hash, created_at),
        )

    @staticmethod
    def pending_approval(connection: sqlite3.Connection, release_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM approvals WHERE release_id = ? AND decision = 'pending'",
                (release_id,),
            ).fetchone(),
        )

    @staticmethod
    def decide_approval(
        connection: sqlite3.Connection,
        *,
        approval_id: str,
        decision: str,
        actor: str,
        reason: str | None,
        decided_at: str,
    ) -> None:
        connection.execute(
            """
            UPDATE approvals
            SET decision = ?, actor = ?, reason = ?, decided_at = ?
            WHERE approval_id = ? AND decision = 'pending'
            """,
            (decision, actor, reason, decided_at, approval_id),
        )

    @staticmethod
    def invalidate_approvals(
        connection: sqlite3.Connection, release_id: str, gates: tuple[str, ...], at: str
    ) -> None:
        placeholders = ",".join("?" for _ in gates)
        connection.execute(
            f"UPDATE approvals SET decision = 'invalidated', decided_at = ? "
            f"WHERE release_id = ? AND gate IN ({placeholders}) "
            "AND decision IN ('pending', 'approved')",
            (at, release_id, *gates),
        )

    @staticmethod
    def latest_artifact_paths(connection: sqlite3.Connection, release_id: str) -> dict[str, str]:
        rows = connection.execute(
            """
            SELECT kind, path FROM artifact_versions a
            WHERE release_id = ? AND valid = 1 AND version = (
                SELECT max(version) FROM artifact_versions b
                WHERE b.release_id = a.release_id AND b.kind = a.kind AND b.valid = 1
            )
            """,
            (release_id,),
        ).fetchall()
        return {str(row["kind"]): str(row["path"]) for row in rows}

    @staticmethod
    def invalidate_artifacts(
        connection: sqlite3.Connection, release_id: str, target: str
    ) -> None:
        conditions = {
            "plan": "1 = 1",
            "main_text": (
                "kind IN "
                "('editorial_manifest', 'main_text', 'teleprompter', 'dzen', 'docx')"
            ),
            "visuals": (
                "(kind = 'editorial_manifest' OR kind = 'docx' OR kind LIKE 'visual_%')"
            ),
            "cover": "kind IN ('editorial_manifest', 'cover', 'docx')",
            "metadata": "kind = 'editorial_manifest'",
            "telegram": "kind IN ('editorial_manifest', 'telegram_template')",
        }
        condition = conditions.get(target)
        if condition is None:
            raise ValueError(f"unknown artifact invalidation target: {target}")
        connection.execute(
            f"UPDATE artifact_versions SET valid = 0 WHERE release_id = ? AND {condition}",
            (release_id,),
        )

    @staticmethod
    def insert_source(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        source_id: str,
        url: str,
        title: str,
        publisher: str,
        checked_at: str,
        evidence_excerpt_hash: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO sources (
                source_id, release_id, url, title, publisher, checked_at,
                evidence_excerpt_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(release_id, source_id) DO UPDATE SET
                url = excluded.url,
                title = excluded.title,
                publisher = excluded.publisher,
                checked_at = excluded.checked_at,
                evidence_excerpt_hash = excluded.evidence_excerpt_hash
            """,
            (
                source_id,
                release_id,
                url,
                title,
                publisher,
                checked_at,
                evidence_excerpt_hash,
            ),
        )

    @staticmethod
    def clear_editorial_records(connection: sqlite3.Connection, release_id: str) -> None:
        connection.execute("DELETE FROM visual_specs WHERE release_id = ?", (release_id,))
        connection.execute("DELETE FROM claim_sources WHERE release_id = ?", (release_id,))
        connection.execute("DELETE FROM claims WHERE release_id = ?", (release_id,))
        connection.execute("DELETE FROM sources WHERE release_id = ?", (release_id,))

    @staticmethod
    def insert_claim(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        claim_id: str,
        exact_text: str,
        materiality: str,
        status: str,
        source_ids: list[str],
    ) -> None:
        connection.execute(
            """
            INSERT INTO claims (
                claim_id, release_id, exact_text, materiality, status
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(release_id, claim_id) DO UPDATE SET
                exact_text = excluded.exact_text,
                materiality = excluded.materiality,
                status = excluded.status
            """,
            (claim_id, release_id, exact_text, materiality, status),
        )
        connection.execute(
            "DELETE FROM claim_sources WHERE release_id = ? AND claim_id = ?",
            (release_id, claim_id),
        )
        connection.executemany(
            """
            INSERT INTO claim_sources (release_id, claim_id, source_id, relation)
            VALUES (?, ?, ?, 'supports')
            """,
            [(release_id, claim_id, source_id) for source_id in source_ids],
        )

    @staticmethod
    def insert_visual_spec(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        visual_id: str,
        position: int,
        kind: str,
        anchor_text: str,
        purpose: str,
        artifact_id: str,
        claim_ids: list[str],
    ) -> None:
        connection.execute(
            """
            INSERT INTO visual_specs (
                visual_id, release_id, position, kind, anchor_text, purpose,
                artifact_id, claim_ids_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(release_id, visual_id) DO UPDATE SET
                position = excluded.position,
                kind = excluded.kind,
                anchor_text = excluded.anchor_text,
                purpose = excluded.purpose,
                artifact_id = excluded.artifact_id,
                claim_ids_json = excluded.claim_ids_json
            """,
            (
                visual_id,
                release_id,
                position,
                kind,
                anchor_text,
                purpose,
                artifact_id,
                json.dumps(claim_ids, ensure_ascii=False),
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
        revision_target=row["revision_target"],
    )
