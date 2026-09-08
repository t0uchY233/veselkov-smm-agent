"""Provider-independent port for the technical Telegram alert channel."""

from typing import Protocol

from smm_agent.contracts.publication import NotificationReceipt, NotificationRequest


class AlertTransport(Protocol):
    """Send or reconcile an alert identified by its durable notification id.

    ``lookup`` is deliberately required before every ``send``.  Both methods
    are idempotent by ``notification_id``: a repeated send for the same id
    must return the original receipt, never create a second Telegram message.
    This lets a later job attempt confirm a successful send if the process
    died after the remote side effect but before the local receipt was
    committed.
    """

    def lookup(
        self, *, notification_id: str, recipient: str
    ) -> NotificationReceipt | None: ...

    def send(self, *, request: NotificationRequest, body: str) -> NotificationReceipt: ...
