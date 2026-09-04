import sqlite3
from pathlib import Path

import pytest

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

    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5]
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
        "incidents",
        "notifications",
        "notification_attempts",
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


def test_existing_slice_four_publication_rows_keep_unknown_recovery_identity(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path)
    database.path.parent.mkdir(parents=True)
    migrations = Path(__file__).parents[2] / "migrations"
    migration_names = {
        1: "0001_initial.sql",
        2: "0002_editorial.sql",
        3: "0003_video.sql",
        4: "0004_publication.sql",
    }
    connection = sqlite3.connect(database.path)
    try:
        for version in range(1, 5):
            connection.executescript(
                (migrations / migration_names[version]).read_text(encoding="utf-8")
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, f"2026-09-0{version}T00:00:00Z"),
            )
        connection.execute(
            """
            INSERT INTO releases (
                release_id, topic, state, revision, active, created_at, updated_at
            ) VALUES ('release-slice4', 'Тема', 'scheduled', 4, 1, ?, ?)
            """,
            ("2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, release_id, kind, state, due_at, attempts, retry_policy_id,
                idempotency_key, payload_json, created_at, updated_at
            ) VALUES ('job-slice4', 'release-slice4', 'publication_preflight', 'running',
                ?, 1, 'provider-30s-2m-5m-15m', 'preflight:release-slice4', '{}', ?, ?)
            """,
            ("2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO job_attempts (attempt_id, job_id, started_at)
            VALUES ('attempt-slice4', 'job-slice4', ?)
            """,
            ("2026-09-04T00:00:00Z",),
        )
        connection.execute(
            """
            INSERT INTO publications (
                release_id, platform, state, payload_sha256, remote_id, known_url,
                target_at_utc, public_at, updated_at
            ) VALUES ('release-slice4', 'youtube', 'armed', ?, 'video-old',
                'https://youtu.be/video-old', ?, NULL, ?)
            """,
            (
                "a" * 64,
                "2026-09-10T11:00:00Z",
                "2026-09-04T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    database.initialize()

    with database.connect() as upgraded:
        job = upgraded.execute("SELECT * FROM jobs WHERE job_id = 'job-slice4'").fetchone()
        attempt = upgraded.execute(
            "SELECT * FROM job_attempts WHERE attempt_id = 'attempt-slice4'"
        ).fetchone()
        publication = upgraded.execute(
            "SELECT * FROM publications WHERE release_id = 'release-slice4'"
        ).fetchone()
        notification_columns = {
            row["name"] for row in upgraded.execute("PRAGMA table_info(notifications)").fetchall()
        }

    assert job is not None
    assert job["lease_epoch"] == 0
    assert job["active_attempt_id"] is None
    assert job["last_heartbeat_at"] is None
    assert job["last_error_code"] is None
    assert attempt is not None
    assert attempt["worker_id"] is None
    assert attempt["fence_token"] is None
    assert attempt["lease_epoch"] == 0
    assert publication is not None
    assert publication["remote_id"] == "video-old"
    assert publication["idempotency_key"] is None
    assert publication["operation_key"] is None
    assert {"recipient", "suppression_key", "last_error_code"} <= notification_columns

    with database.transaction() as upgraded:
        upgraded.execute(
            """
            INSERT INTO incidents (
                incident_id, release_id, error_code, safe_next_action, state, created_at
            ) VALUES ('incident-slice5', 'release-slice4', 'PROVIDER_TIMEOUT',
                'Проверить состояние площадки.', 'open', ?)
            """,
            ("2026-09-04T00:00:00Z",),
        )
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.execute(
                """
                INSERT INTO notifications (
                    notification_id, incident_id, recipient, channel, state, suppression_key,
                    created_at, updated_at
                ) VALUES ('notification-invalid', 'incident-slice5', 'not-sardor',
                    'telegram_alert', 'queued', 'incident-slice5', ?, ?)
                """,
                ("2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z"),
            )
        upgraded.execute(
            """
            INSERT INTO notifications (
                notification_id, incident_id, channel, state, suppression_key,
                created_at, updated_at
            ) VALUES ('notification-slice5', 'incident-slice5', 'telegram_alert', 'queued',
                'incident-slice5:telegram', ?, ?)
            """,
            ("2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z"),
        )
        recipient = upgraded.execute(
            "SELECT recipient FROM notifications WHERE notification_id = 'notification-slice5'"
        ).fetchone()

    assert recipient is not None and recipient["recipient"] == "276042853"
