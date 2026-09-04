"""Integration coverage for Slice 5 technical alert durability."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smm_agent.adapters.notification.replay import ReplayAlertTransport
from smm_agent.application.notification_service import alert_body, deliver_notification
from smm_agent.contracts.publication import SARDOR_TELEGRAM_RECIPIENT, RecoveryError
from smm_agent.platform.db import Database
from smm_agent.platform.incidents import IncidentStore
from smm_agent.platform.jobs import JobStore

NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
NOW_TEXT = "2026-09-04T10:00:00Z"


def _database(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "data")
    database.initialize()
    release_id = "release-notification"
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO releases (
                release_id, topic, state, revision, active, created_at, updated_at
            ) VALUES (?, 'Тема', 'needs_attention', 1, 1, ?, ?)
            """,
            (release_id, NOW_TEXT, NOW_TEXT),
        )
    return database, release_id


def _open(database: Database, release_id: str):
    with database.transaction() as connection:
        return IncidentStore.open_with_notification(
            connection,
            release_id=release_id,
            # UC-16 also permits a release-level incident, where no original
            # recovery job exists (for example a setup-time terminal failure).
            job_id=None,
            platform="telegram",
            error=RecoveryError(
                code="PROVIDER_TIMEOUT", sanitized_detail="Bearer secret-not-for-chat"
            ),
            safe_next_action="Проверить подключение Telegram и повторить recovery.",
            now=NOW,
        )


def _claim(database: Database, release_id: str):
    with database.transaction() as connection:
        claim = JobStore.claim_due(
            connection,
            release_id=release_id,
            kind="notification_deliver",
            worker_id="notification-worker",
            now=NOW_TEXT,
        )
    assert claim is not None
    return claim


def test_open_incident_notification_and_job_are_atomic_and_suppressed(tmp_path: Path) -> None:
    database, release_id = _database(tmp_path)
    first = _open(database, release_id)
    second = _open(database, release_id)

    assert first.incident.incident_id == second.incident.incident_id
    assert first.notification.notification_id == second.notification.notification_id
    assert first.job_id == second.job_id
    assert first.notification.recipient == SARDOR_TELEGRAM_RECIPIENT
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM incidents").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
        notification_jobs = connection.execute(
            "SELECT count(*) FROM jobs WHERE kind = 'notification_deliver'"
        ).fetchone()
        assert notification_jobs is not None and notification_jobs[0] == 1


def test_incident_persistence_redacts_provider_secrets(tmp_path: Path) -> None:
    database, release_id = _database(tmp_path)
    _open(database, release_id)

    with database.connect() as connection:
        incident = connection.execute("SELECT sanitized_detail FROM incidents").fetchone()

    assert incident is not None
    assert "secret-not-for-chat" not in str(incident["sanitized_detail"])
    assert "[REDACTED]" in str(incident["sanitized_detail"])


def test_notification_payload_must_match_incident_recipient_hash_and_release(
    tmp_path: Path,
) -> None:
    database, release_id = _database(tmp_path)
    opened = _open(database, release_id)
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT job_id, payload_json FROM jobs WHERE kind = 'notification_deliver'"
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload_json"]))
        payload["release_id"] = "other-release"
        connection.execute(
            "UPDATE jobs SET payload_json = ? WHERE job_id = ?",
            (json.dumps(payload, sort_keys=True), str(row["job_id"])),
        )
    claim = _claim(database, release_id)
    transport = ReplayAlertTransport()

    with pytest.raises(ValueError, match="не совпадает"):
        deliver_notification(
            database,
            claim=claim,
            notification_id=opened.notification.notification_id,
            transport=transport,
            now=NOW,
        )

    assert transport.calls == []
    with database.connect() as connection:
        notification = connection.execute("SELECT state, attempts FROM notifications").fetchone()
    assert notification is not None and tuple(notification) == ("queued", 0)


def test_stale_notification_claim_cannot_change_alert_or_attempt_state(tmp_path: Path) -> None:
    database, release_id = _database(tmp_path)
    opened = _open(database, release_id)
    first = _claim(database, release_id)
    later = NOW + timedelta(seconds=61)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET lease_until = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00Z", first.job_id),
        )
        replacement = JobStore.claim_due(
            connection,
            release_id=release_id,
            kind="notification_deliver",
            worker_id="replacement-worker",
            now=later.isoformat().replace("+00:00", "Z"),
        )
    assert replacement is not None

    outcome = deliver_notification(
        database,
        claim=first,
        notification_id=opened.notification.notification_id,
        transport=ReplayAlertTransport(),
        now=NOW,
    )

    assert outcome.outcome == "stale"
    with database.connect() as connection:
        notification = connection.execute("SELECT state, attempts FROM notifications").fetchone()
        attempts = connection.execute("SELECT count(*) FROM notification_attempts").fetchone()
        job = connection.execute("SELECT active_attempt_id FROM jobs").fetchone()
    assert notification is not None and tuple(notification) == ("queued", 0)
    assert attempts is not None and attempts[0] == 0
    assert job is not None and job["active_attempt_id"] == replacement.attempt_id


def test_alert_body_has_required_operational_fields_without_provider_detail(tmp_path: Path) -> None:
    database, release_id = _database(tmp_path)
    opened = _open(database, release_id)

    body = alert_body(opened.notification)

    assert release_id in body
    assert "telegram" in body
    assert "PROVIDER_TIMEOUT" in body
    assert NOW_TEXT in body
    assert "Проверить подключение" in body
    assert "secret-not-for-chat" not in body


def test_delivery_looks_up_remote_receipt_before_send_after_crash_point(tmp_path: Path) -> None:
    database, release_id = _database(tmp_path)
    opened = _open(database, release_id)
    transport = ReplayAlertTransport(state_path=tmp_path / "alerts.json")

    # This models a process death after Bot API accepted send but before local
    # receipt persistence. The next leased attempt must observe the remote id.
    transport.send(request=opened.notification, body=alert_body(opened.notification))
    claim = _claim(database, release_id)

    outcome = deliver_notification(
        database,
        claim=claim,
        notification_id=opened.notification.notification_id,
        transport=transport,
        now=NOW,
    )

    assert outcome.outcome == "delivered"
    assert transport.calls == [
        ("send", opened.notification.notification_id),
        ("lookup", opened.notification.notification_id),
    ]
    with database.connect() as connection:
        notification = connection.execute("SELECT * FROM notifications").fetchone()
        job = connection.execute(
            "SELECT state FROM jobs WHERE job_id = ?", (claim.job_id,)
        ).fetchone()
    assert notification is not None and notification["state"] == "delivered"
    assert job is not None and job["state"] == "succeeded"


def test_notification_retries_on_exact_schedule_then_keeps_incident_open(tmp_path: Path) -> None:
    database, release_id = _database(tmp_path)
    opened = _open(database, release_id)
    transport = ReplayAlertTransport()
    transport.fail()
    claim = _claim(database, release_id)

    outcome = deliver_notification(
        database,
        claim=claim,
        notification_id=opened.notification.notification_id,
        transport=transport,
        now=NOW,
    )

    assert (outcome.outcome, outcome.retry_after_seconds) == ("retry_wait", 60)
    with database.connect() as connection:
        notification = connection.execute("SELECT * FROM notifications").fetchone()
        job = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (claim.job_id,)).fetchone()
        incident = connection.execute("SELECT state FROM incidents").fetchone()
    assert notification is not None and notification["state"] == "retry_wait"
    assert job is not None and job["due_at"] == "2026-09-04T10:01:00Z"
    assert incident is not None and incident["state"] == "open"


def test_notification_exhaustion_uses_one_five_fifteen_minute_delays(tmp_path: Path) -> None:
    database, release_id = _database(tmp_path)
    opened = _open(database, release_id)
    transport = ReplayAlertTransport()
    transport.fail()
    schedule = [
        (NOW, "retry_wait", 60),
        (datetime(2026, 9, 4, 10, 1, tzinfo=UTC), "retry_wait", 300),
        (datetime(2026, 9, 4, 10, 6, tzinfo=UTC), "retry_wait", 900),
        (datetime(2026, 9, 4, 10, 21, tzinfo=UTC), "failed", None),
    ]

    for index, (instant, expected_outcome, expected_delay) in enumerate(schedule):
        if index == 0:
            claim = _claim(database, release_id)
        else:
            with database.transaction() as connection:
                claim = JobStore.claim_due(
                    connection,
                    release_id=release_id,
                    kind="notification_deliver",
                    worker_id=f"notification-worker-{index}",
                    now=instant.isoformat().replace("+00:00", "Z"),
                )
            assert claim is not None
        outcome = deliver_notification(
            database,
            claim=claim,
            notification_id=opened.notification.notification_id,
            transport=transport,
            now=instant,
        )
        assert (outcome.outcome, outcome.retry_after_seconds) == (
            expected_outcome,
            expected_delay,
        )

    with database.connect() as connection:
        notification = connection.execute("SELECT state FROM notifications").fetchone()
        incident = connection.execute("SELECT state FROM incidents").fetchone()
        job = connection.execute(
            "SELECT state FROM jobs WHERE idempotency_key = ?",
            (f"notification:{opened.notification.notification_id}",),
        ).fetchone()
    assert notification is not None and notification["state"] == "failed"
    assert incident is not None and incident["state"] == "open"
    assert job is not None and job["state"] == "failed"
