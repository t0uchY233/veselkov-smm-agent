"""Technical Telegram alerts with durable no-resend handling for ambiguous sends."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from smm_agent.adapters.publishing.http import HttpRequest, HttpTransport
from smm_agent.contracts.publication import NotificationReceipt, NotificationRequest
from smm_agent.domain.publication.ports import ProviderOperationError
from smm_agent.platform.db import Database


def _unconfirmed() -> ProviderOperationError:
    return ProviderOperationError(
        code="RECEIPT_MISMATCH",
        sanitized_detail="Alert delivery is unconfirmed; automatic resend is blocked.",
    )


class TelegramAlertTransport:
    def __init__(self, *, database: Database, transport: HttpTransport) -> None:
        self._database = database
        self._transport = transport

    def lookup(self, *, notification_id: str, recipient: str) -> NotificationReceipt | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT recipient, receipt_json FROM telegram_alert_receipts "
                "WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
        if row is None:
            return None
        if row["recipient"] != recipient or row["receipt_json"] is None:
            raise _unconfirmed()
        receipt = NotificationReceipt.model_validate_json(row["receipt_json"])
        if receipt.notification_id != notification_id or receipt.recipient != recipient:
            raise _unconfirmed()
        return receipt

    def send(self, *, request: NotificationRequest, body: str) -> NotificationReceipt:
        payload_hash = hashlib.sha256(
            json.dumps(
                {"request": request.model_dump(mode="json"), "body": body},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT recipient, payload_sha256, receipt_json FROM telegram_alert_receipts "
                "WHERE notification_id = ?",
                (request.notification_id,),
            ).fetchone()
            if row is not None:
                if row["recipient"] != request.recipient or row["payload_sha256"] != payload_hash:
                    raise _unconfirmed()
                if row["receipt_json"] is None:
                    raise _unconfirmed()
                receipt = NotificationReceipt.model_validate_json(row["receipt_json"])
                if (
                    receipt.notification_id != request.notification_id
                    or receipt.recipient != request.recipient
                ):
                    raise _unconfirmed()
                return receipt
            connection.execute(
                "INSERT INTO telegram_alert_receipts "
                "(notification_id, recipient, payload_sha256) VALUES (?, ?, ?)",
                (request.notification_id, request.recipient, payload_hash),
            )
        # An unresolved intent survives a crash/timeout. Bot API does not supply
        # our notification_id as a server-side idempotency receipt.
        try:
            response = self._transport.send(
                HttpRequest(
                    method="POST",
                    url="https://api.telegram.org/bot/sendMessage",
                    headers={"Content-Type": "application/json"},
                    body=json.dumps({"chat_id": request.recipient, "text": body}).encode("utf-8"),
                    idempotency_key=request.notification_id,
                )
            )
            payload = response.json_object()
            result = payload.get("result")
            if response.status_code != 200 or payload.get("ok") is not True:
                raise _unconfirmed()
            if not isinstance(result, dict) or not isinstance(result.get("chat"), dict):
                raise _unconfirmed()
            if str(result["chat"].get("id")) != request.recipient:
                raise _unconfirmed()
            message_id = result.get("message_id")
            timestamp = result.get("date")
            if type(message_id) is not int or message_id <= 0 or type(timestamp) is not int:
                raise _unconfirmed()
            receipt = NotificationReceipt(
                notification_id=request.notification_id,
                recipient=request.recipient,
                provider_receipt_id=str(message_id),
                delivered_at=datetime.fromtimestamp(timestamp, UTC),
            )
        except Exception:
            raise _unconfirmed() from None
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE telegram_alert_receipts SET receipt_json = ? "
                "WHERE notification_id = ? AND payload_sha256 = ? AND receipt_json IS NULL",
                (receipt.model_dump_json(), request.notification_id, payload_hash),
            )
        return receipt
