import sqlite3
from pathlib import Path

from smm_agent.platform.db import Database


def test_existing_slice_one_database_upgrades_to_current_schema(tmp_path: Path) -> None:
    database = Database(tmp_path)
    database.path.parent.mkdir(parents=True)
    connection = sqlite3.connect(database.path)
    try:
        connection.executescript(
            (Path(__file__).parents[2] / "migrations" / "0001_initial.sql").read_text(
                encoding="utf-8"
            )
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-09-03T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()

    database.initialize()

    with database.connect() as upgraded:
        versions = upgraded.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        release_columns = {
            row["name"] for row in upgraded.execute("PRAGMA table_info(releases)").fetchall()
        }
        tables = {
            row["name"]
            for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert [row["version"] for row in versions] == [1, 2, 3, 4]
    assert "revision_target" in release_columns
    assert "recording_watch_initialized_at" in release_columns
    assert {
        "artifact_versions",
        "approvals",
        "visual_specs",
        "recording_candidates",
        "recordings",
        "media_leases",
        "approval_artifacts",
        "accepted_media_profiles",
        "publications",
        "publication_leases",
        "publication_attempts",
        "jobs",
        "job_attempts",
    } <= tables


def test_slice_two_pending_gate_is_safely_reopened_on_upgrade(tmp_path: Path) -> None:
    database = Database(tmp_path)
    database.path.parent.mkdir(parents=True)
    migrations = Path(__file__).parents[2] / "migrations"
    connection = sqlite3.connect(database.path)
    try:
        connection.executescript((migrations / "0001_initial.sql").read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            ("2026-09-03T00:00:00Z",),
        )
        connection.executescript((migrations / "0002_editorial.sql").read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
            ("2026-09-03T01:00:00Z",),
        )
        connection.execute(
            """
            INSERT INTO releases (
                release_id, topic, state, revision, active, created_at, updated_at
            ) VALUES ('release-old', 'Тема', 'plan_pending', 2, 1, ?, ?)
            """,
            ("2026-09-03T00:00:00Z", "2026-09-03T00:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO approvals (
                approval_id, release_id, gate, decision, release_revision,
                artifact_set_hash, created_at
            ) VALUES ('approval-old', 'release-old', 'plan', 'pending', 2, ?, ?)
            """,
            ("a" * 64, "2026-09-03T00:00:00Z"),
        )
        connection.commit()
    finally:
        connection.close()

    database.initialize()

    with database.connect() as upgraded:
        release = upgraded.execute(
            "SELECT state, revision, revision_target FROM releases WHERE release_id = ?",
            ("release-old",),
        ).fetchone()
        approval = upgraded.execute(
            "SELECT decision, reason FROM approvals WHERE approval_id = ?",
            ("approval-old",),
        ).fetchone()

    assert release is not None
    assert tuple(release) == ("revision_requested", 3, "plan")
    assert approval is not None and approval["decision"] == "invalidated"
