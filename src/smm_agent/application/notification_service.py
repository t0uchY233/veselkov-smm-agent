"""Delivery of durable technical alerts with recovery-safe Telegram semantics."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from smm_agent.contracts.publication import (
    SARDOR_TELEGRAM_RECIPIENT,
    NotificationReceipt,
    NotificationRequest,
    Platform,
    RecoveryError,
    RecoveryErrorCode,
)
from smm_agent.domain.notification.ports import AlertTransport
from smm_agent.domain.publication.service import retry_decision
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7
from smm_agent.platform.jobs import JobClaim, JobStore


@dataclass(frozen=True, slots=True)
class NotificationDelivery:
    notification_id: str
    outcome: str
    retry_after_seconds: int | None = None


class NotificationDeliveryError(RuntimeError):
    """A transport boundary can report a pre-classified, sanitized failure."""

    def __init__(self, error: RecoveryError) -> None:
        super().__init__(error.code)
        self.error = error


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _notification_request(
    connection: sqlite3.Connection, notification_id: str
) -> NotificationRequest:
    row = connection.execute(
        """
        SELECT n.notification_id, n.incident_id, n.recipient, n.suppression_key,
               i.release_id, i.platform, i.error_code, i.safe_next_action, i.created_at
        FROM notifications n JOIN incidents i ON i.incident_id = n.incident_id
        WHERE n.notification_id = ?
        """,
        (notification_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Notification job ссылается на отсутствующее уведомление.")
    recipient = str(row["recipient"])
    if recipient != SARDOR_TELEGRAM_RECIPIENT:
        raise RuntimeError("Нарушен recipient технического уведомления.")
    return NotificationRequest(
        notification_id=str(row["notification_id"]),
        incident_id=str(row["incident_id"]),
        recipient=SARDOR_TELEGRAM_RECIPIENT,
        release_id=str(row["release_id"]),
        platform=cast(Platform, str(row["platform"])) if row["platform"] else None,
        error_code=cast(RecoveryErrorCode, str(row["error_code"])),
        occurred_at=datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")),
        safe_next_action=str(row["safe_next_action"]),
        suppression_key=str(row["suppression_key"]),
    )


def alert_body(request: NotificationRequest) -> str:
    """Build the intentionally small, non-secret operational alert body."""

    platform = request.platform or "все площадки"
    occurred = _iso(request.occurred_at)
    # Error code is a closed contract enum.  The potentially provider-derived
    # detail remains only in local SQLite and is never copied to a chat.
    return (
        "Авария публикации\n"
        f"Выпуск: {request.release_id}\n"
        f"Площадка: {platform}\n"
        f"Ошибка: {request.error_code}\n"
        f"Время: {occurred}\n"
        f"Действие: {request.safe_next_action}"
    )


def _start_attempt(
    connection: sqlite3.Connection, *, claim: JobClaim, notification_id: str, now: str
) -> NotificationRequest:
    request = _notification_request(connection, notification_id)
    row = connection.execute(
        "SELECT state FROM notifications WHERE notification_id = ?", (notification_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Notification job ссылается на отсутствующее уведомление.")
    if row["state"] == "delivered":
        return request
    connection.execute(
        """
        UPDATE notifications
        SET state = 'sending', attempts = attempts + 1, updated_at = ?
        WHERE notification_id = ? AND state != 'delivered'
        """,
        (now, notification_id),
    )
    connection.execute(
        """
        INSERT INTO notification_attempts (
            attempt_id, notification_id, job_attempt_id, started_at
        ) VALUES (?, ?, ?, ?)
        """,
        (uuid7(), notification_id, claim.attempt_id, now),
    )
    return request


def _mark_delivered(
    connection: sqlite3.Connection,
    *,
    claim: JobClaim,
    receipt: NotificationReceipt,
    now: str,
) -> bool:
    cursor = connection.execute(
        """
        UPDATE notifications
        SET state = 'delivered', delivered_at = ?, updated_at = ?,
            last_error_code = NULL, last_error_detail = NULL, last_error_at = NULL
        WHERE notification_id = ?
        """,
        (_iso(receipt.delivered_at), now, receipt.notification_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Уведомление исчезло во время доставки.")
    attempt = connection.execute(
        """
        UPDATE notification_attempts
        SET finished_at = ?, outcome = 'delivered', provider_receipt_id = ?
        WHERE notification_id = ? AND job_attempt_id = ? AND finished_at IS NULL
        """,
        (now, receipt.provider_receipt_id, receipt.notification_id, claim.attempt_id),
    )
    if attempt.rowcount > 1:
        raise RuntimeError("Для одного job attempt создано несколько notification attempts.")
    return JobStore.mark_succeeded(connection, claim=claim, now=now)


def _mark_failure(
    connection: sqlite3.Connection,
    *,
    claim: JobClaim,
    request: NotificationRequest,
    error: RecoveryError,
    now: datetime,
) -> NotificationDelivery:
    now_text = _iso(now)
    job = connection.execute(
        "SELECT attempts FROM jobs WHERE job_id = ?", (claim.job_id,)
    ).fetchone()
    if job is None:
        raise RuntimeError("Notification job исчез до фиксации ошибки.")
    decision = retry_decision(error, operation="notification", attempt_number=int(job["attempts"]))
    if decision.disposition == "retry":
        assert decision.retry_after_seconds is not None
        due_at = _iso(now + timedelta(seconds=decision.retry_after_seconds))
        connection.execute(
            """
            UPDATE notifications
            SET state = 'retry_wait', last_error_code = ?, last_error_detail = ?,
                last_error_at = ?, updated_at = ?
            WHERE notification_id = ?
            """,
            (error.code, error.sanitized_detail, now_text, now_text, request.notification_id),
        )
        connection.execute(
            """
            UPDATE notification_attempts
            SET finished_at = ?, outcome = 'retry_wait', error_code = ?, sanitized_detail = ?
            WHERE notification_id = ? AND job_attempt_id = ? AND finished_at IS NULL
            """,
            (
                now_text,
                error.code,
                error.sanitized_detail,
                request.notification_id,
                claim.attempt_id,
            ),
        )
        if not JobStore.mark_retry_wait(
            connection, claim=claim, error=error, due_at=due_at, now=now_text
        ):
            return NotificationDelivery(request.notification_id, "stale")
        return NotificationDelivery(
            request.notification_id, "retry_wait", decision.retry_after_seconds
        )

    # Both a closed-contract error and retry exhaustion leave the local
    # incident open.  The job is terminal so a later worker cannot keep
    # sending the same alert forever.
    connection.execute(
        """
        UPDATE notifications
        SET state = 'failed', last_error_code = ?, last_error_detail = ?,
            last_error_at = ?, updated_at = ?
        WHERE notification_id = ?
        """,
        (error.code, error.sanitized_detail, now_text, now_text, request.notification_id),
    )
    connection.execute(
        """
        UPDATE notification_attempts
        SET finished_at = ?, outcome = 'failed', error_code = ?, sanitized_detail = ?
        WHERE notification_id = ? AND job_attempt_id = ? AND finished_at IS NULL
        """,
        (now_text, error.code, error.sanitized_detail, request.notification_id, claim.attempt_id),
    )
    if not JobStore.mark_failed(connection, claim=claim, error=error, now=now_text):
        return NotificationDelivery(request.notification_id, "stale")
    return NotificationDelivery(request.notification_id, "failed")


def _validate_receipt(request: NotificationRequest, receipt: NotificationReceipt) -> None:
    if receipt.notification_id != request.notification_id or receipt.recipient != request.recipient:
        raise NotificationDeliveryError(
            RecoveryError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Telegram вернул receipt другого технического уведомления.",
            )
        )


def deliver_notification(
    database: Database,
    *,
    claim: JobClaim,
    notification_id: str,
    transport: AlertTransport,
    now: datetime,
) -> NotificationDelivery:
    """Deliver one claimed alert, reconciling before a potentially duplicate send."""

    instant = now.astimezone(UTC)
    now_text = _iso(instant)
    with database.transaction() as connection:
        request = _start_attempt(
            connection, claim=claim, notification_id=notification_id, now=now_text
        )
        state = connection.execute(
            "SELECT state FROM notifications WHERE notification_id = ?", (notification_id,)
        ).fetchone()
        if state is not None and state["state"] == "delivered":
            if JobStore.mark_succeeded(connection, claim=claim, now=now_text):
                return NotificationDelivery(notification_id, "already_delivered")
            return NotificationDelivery(notification_id, "stale")

    try:
        receipt = transport.lookup(
            notification_id=request.notification_id, recipient=request.recipient
        )
        if receipt is None:
            receipt = transport.send(request=request, body=alert_body(request))
        _validate_receipt(request, receipt)
    except NotificationDeliveryError as exc:
        error = exc.error
    except Exception:
        # Transport exceptions may contain access tokens or remote response
        # bodies.  Keep a stable, non-secret classification only.
        error = RecoveryError(
            code="PROVIDER_TIMEOUT",
            sanitized_detail="Техническое уведомление Telegram временно недоступно.",
        )
    else:
        with database.transaction() as connection:
            if _mark_delivered(connection, claim=claim, receipt=receipt, now=now_text):
                return NotificationDelivery(notification_id, "delivered")
            return NotificationDelivery(notification_id, "stale")

    with database.transaction() as connection:
        return _mark_failure(
            connection, claim=claim, request=request, error=error, now=instant
        )
