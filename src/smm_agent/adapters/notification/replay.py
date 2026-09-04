"""A deterministic durable replay transport for notification recovery tests."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from smm_agent.contracts.publication import NotificationReceipt, NotificationRequest


class ReplayAlertError(RuntimeError):
    pass


class ReplayAlertTransport:
    def __init__(self, *, state_path: Path | None = None) -> None:
        self.state_path = state_path
        self._receipts: dict[str, NotificationReceipt] = {}
        self._failures: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self._load()

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._receipts = {
            identifier: NotificationReceipt.model_validate(receipt)
            for identifier, receipt in payload.get("receipts", {}).items()
        }

    def _flush(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "receipts": {
                        key: value.model_dump(mode="json")
                        for key, value in self._receipts.items()
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def fail(self, operation: str = "send") -> None:
        self._failures.add(operation)

    def recover(self, operation: str = "send") -> None:
        self._failures.discard(operation)

    def lookup(self, *, notification_id: str, recipient: str) -> NotificationReceipt | None:
        self.calls.append(("lookup", notification_id))
        if "lookup" in self._failures:
            raise ReplayAlertError("replay alert lookup failed")
        receipt = self._receipts.get(notification_id)
        if receipt is not None and receipt.recipient != recipient:
            raise ReplayAlertError("recipient mismatch")
        return receipt

    def send(self, *, request: NotificationRequest, body: str) -> NotificationReceipt:
        self.calls.append(("send", request.notification_id))
        if "send" in self._failures:
            raise ReplayAlertError("replay alert send failed")
        existing = self._receipts.get(request.notification_id)
        if existing is not None:
            return existing
        receipt = NotificationReceipt(
            notification_id=request.notification_id,
            recipient=request.recipient,
            provider_receipt_id=f"replay-alert-{request.notification_id}",
            delivered_at=datetime.now(UTC),
        )
        self._receipts[request.notification_id] = receipt
        self._flush()
        return receipt
