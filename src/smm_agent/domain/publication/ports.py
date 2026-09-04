"""Provider-independent publication ports."""

from datetime import datetime
from typing import Protocol

from smm_agent.contracts.publication import (
    Platform,
    PreparedPublication,
    PublicationRequest,
    PublicationSnapshot,
)


class Publisher(Protocol):
    platform: Platform

    def prepare(self, request: PublicationRequest) -> PreparedPublication: ...

    def preflight(self, remote_id: str, *, payload_sha256: str) -> None: ...

    def arm(
        self, remote_id: str, *, target_at_utc: datetime, operation_key: str
    ) -> PublicationSnapshot: ...

    def status(
        self, remote_id: str, *, now: datetime | None = None
    ) -> PublicationSnapshot: ...

    def cancel(self, remote_id: str, *, operation_key: str) -> PublicationSnapshot: ...

    def execute(
        self, remote_id: str, *, operation_key: str, now: datetime
    ) -> PublicationSnapshot: ...
