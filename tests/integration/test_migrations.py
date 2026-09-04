import sqlite3
from pathlib import Path

from smm_agent.platform.db import Database


def test_existing_slice_one_database_upgrades_to_editorial_schema(tmp_path: Path) -> None:
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

    assert [row["version"] for row in versions] == [1, 2]
    assert "revision_target" in release_columns
    assert {"artifact_versions", "approvals", "visual_specs"} <= tables
