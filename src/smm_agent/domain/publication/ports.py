"""Provider-independent publication ports."""

from datetime import datetime
from typing import Protocol

from smm_agent.contracts.publication import (
    Platform,
    PreparedPublication,
    PublicationRequest,
    PublicationSnapshot,
    RecoveryErrorCode,
)


class ProviderOperationError(RuntimeError):
    """A provider-classified failure whose detail is safe to persist.

    Concrete transports must never expose raw HTTP/Dzen browser bodies through
    this type.  The recovery boundary still redacts every persisted detail as a
    defence in depth measure.
    """

    def __init__(self, *, code: RecoveryErrorCode, sanitized_detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.sanitized_detail = sanitized_detail


class DzenDomMismatchError(ProviderOperationError):
    """The Dzen UI no longer matches the explicitly supported page contract."""

    def __init__(self) -> None:
        super().__init__(
            code="DZEN_DOM_MISMATCH",
            sanitized_detail="Страница Дзен изменилась и требует обновления адаптера.",
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
