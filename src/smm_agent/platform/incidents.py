"""Narrow SQLite persistence for recovery incidents and their alert jobs."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from smm_agent.contracts.publication import (
    SARDOR_TELEGRAM_RECIPIENT,
    IncidentRecord,
    NotificationJobPayload,
    NotificationRequest,
    Platform,
    RecoveryError,
    RecoveryErrorCode,
    notification_request_sha256,
)
from smm_agent.platform.ids import uuid7
from smm_agent.platform.jobs import JobStore
from smm_agent.platform.redaction import sanitize_recovery_error


@dataclass(frozen=True, slots=True)
class IncidentNotification:
    incident: IncidentRecord
    notification: NotificationRequest
    job_id: str


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


class IncidentStore:
    """Create one local incident and one alert job in the caller's transaction."""

    @staticmethod
    def open_with_notification(
        connection: sqlite3.Connection,
        *,
        release_id: str,
        job_id: str | None,
        platform: Platform | None,
        error: RecoveryError,
        safe_next_action: str,
        now: datetime,
    ) -> IncidentNotification:
        """Return the existing suppressed alert or atomically create all records.

        This method does not begin a transaction: callers that transition a
        failed recovery job pass their existing SQLite transaction, so no
        committed state can exist without the other two durable records.
        """

        now_text = _iso(now)
        error = sanitize_recovery_error(error)
        platform_part = platform or "global"
        job_part = job_id or "release"
        suppression_key = f"recovery-exhausted:{release_id}:{platform_part}:{job_part}"
        existing = connection.execute(
            """
            SELECT i.*, n.notification_id, n.recipient, n.suppression_key
            FROM notifications n JOIN incidents i ON i.incident_id = n.incident_id
            WHERE n.suppression_key = ?
            """,
            (suppression_key,),
        ).fetchone()
        if existing is not None:
            stored_platform = (
                cast(Platform, str(existing["platform"])) if existing["platform"] else None
            )
            stored_recipient = str(existing["recipient"])
            if stored_recipient != SARDOR_TELEGRAM_RECIPIENT:
                raise RuntimeError("Нарушен recipient технического уведомления.")
            incident = IncidentRecord(
                incident_id=str(existing["incident_id"]),
                release_id=str(existing["release_id"]),
                job_id=str(existing["job_id"]) if existing["job_id"] else None,
                platform=stored_platform,
                error=RecoveryError(
                    code=cast(RecoveryErrorCode, str(existing["error_code"])),
                    sanitized_detail=str(existing["sanitized_detail"] or "Ошибка без деталей."),
                ),
                safe_next_action=str(existing["safe_next_action"]),
                opened_at=datetime.fromisoformat(
                    str(existing["created_at"]).replace("Z", "+00:00")
                ),
            )
            notification = NotificationRequest(
                notification_id=str(existing["notification_id"]),
                incident_id=incident.incident_id,
                recipient=SARDOR_TELEGRAM_RECIPIENT,
                release_id=incident.release_id,
                platform=incident.platform,
                error_code=incident.error.code,
                occurred_at=incident.opened_at,
                safe_next_action=incident.safe_next_action,
                suppression_key=str(existing["suppression_key"]),
            )
            job = connection.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key = ?",
                (f"notification:{notification.notification_id}",),
            ).fetchone()
            if job is None:
                payload = NotificationJobPayload(
                    notification_id=notification.notification_id,
                    incident_id=notification.incident_id,
                    recipient=notification.recipient,
                    release_id=notification.release_id,
                    request_sha256=notification_request_sha256(notification),
                )
                queued_job_id = JobStore.enqueue(
                    connection,
                    release_id=release_id,
                    kind="notification_deliver",
                    due_at=now_text,
                    retry_policy_id="notification-1m-5m-15m",
                    idempotency_key=f"notification:{notification.notification_id}",
                    payload=payload.model_dump(mode="json"),
                    now=now_text,
                )
            else:
                queued_job_id = str(job["job_id"])
            return IncidentNotification(incident, notification, queued_job_id)

        incident = IncidentRecord(
            incident_id=uuid7(),
            release_id=release_id,
            job_id=job_id,
            platform=platform,
            error=error,
            safe_next_action=safe_next_action,
            opened_at=now,
        )
        notification = NotificationRequest(
            notification_id=uuid7(),
            incident_id=incident.incident_id,
            recipient=SARDOR_TELEGRAM_RECIPIENT,
            release_id=release_id,
            platform=platform,
            error_code=error.code,
            occurred_at=now,
            safe_next_action=safe_next_action,
            suppression_key=suppression_key,
        )
        connection.execute(
            """
            INSERT INTO incidents (
                incident_id, release_id, job_id, platform, error_code,
                sanitized_detail, safe_next_action, state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                incident.incident_id,
                release_id,
                job_id,
                platform,
                error.code,
                error.sanitized_detail,
                safe_next_action,
                now_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO notifications (
                notification_id, incident_id, recipient, channel, state,
                suppression_key, created_at, updated_at
            ) VALUES (?, ?, ?, 'telegram_alert', 'queued', ?, ?, ?)
            """,
            (
                notification.notification_id,
                notification.incident_id,
                notification.recipient,
                notification.suppression_key,
                now_text,
                now_text,
            ),
        )
        payload = NotificationJobPayload(
            notification_id=notification.notification_id,
            incident_id=notification.incident_id,
            recipient=notification.recipient,
            release_id=notification.release_id,
            request_sha256=notification_request_sha256(notification),
        )
        queued_job_id = JobStore.enqueue(
            connection,
            release_id=release_id,
            kind="notification_deliver",
            due_at=now_text,
            retry_policy_id="notification-1m-5m-15m",
            idempotency_key=f"notification:{notification.notification_id}",
            payload=payload.model_dump(mode="json"),
            now=now_text,
        )
        return IncidentNotification(incident, notification, queued_job_id)
