"""Versioned contracts for publication preparation and scheduling."""

import hashlib
import json
from datetime import datetime
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Platform = Literal["youtube", "dzen", "telegram"]
RecoveryOperation = Literal["provider", "notification"]
RetryPolicyId = Literal[
    "provider-30s-2m-5m-15m",
    "notification-1m-5m-15m",
]
RecoveryErrorCode = Literal[
    "PROVIDER_TIMEOUT",
    "PROVIDER_HTTP_429",
    "PROVIDER_HTTP_5XX",
    "UNKNOWN_PROVIDER_OUTCOME",
    "PROVIDER_AUTH_REQUIRED",
    "PROVIDER_PERMISSION_DENIED",
    "DZEN_DOM_MISMATCH",
    "INVALID_PAYLOAD",
    "RECEIPT_MISMATCH",
]
RecoveryClassification = Literal["retryable", "terminal"]
RecoveryDisposition = Literal["retry", "exhausted", "terminal"]
NotificationState = Literal["queued", "sending", "delivered", "retry_wait", "failed"]
SARDOR_TELEGRAM_RECIPIENT: Final[Literal["276042853"]] = "276042853"

# Delays are applied after failed attempts one through N.  The engine must
# reconcile provider status before retrying any request that can have had an
# unknown remote outcome.
PROVIDER_RETRY_DELAYS_SECONDS: tuple[int, ...] = (30, 120, 300, 900)
NOTIFICATION_RETRY_DELAYS_SECONDS: tuple[int, ...] = (60, 300, 900)
RETRYABLE_RECOVERY_ERROR_CODES: frozenset[str] = frozenset(
    {
        "PROVIDER_TIMEOUT",
        "PROVIDER_HTTP_429",
        "PROVIDER_HTTP_5XX",
        "UNKNOWN_PROVIDER_OUTCOME",
    }
)
TERMINAL_RECOVERY_ERROR_CODES: frozenset[str] = frozenset(
    {
        "PROVIDER_AUTH_REQUIRED",
        "PROVIDER_PERMISSION_DENIED",
        "DZEN_DOM_MISMATCH",
        "INVALID_PAYLOAD",
        "RECEIPT_MISMATCH",
    }
)
PublicationState = Literal[
    "absent",
    "preparing",
    "prepared",
    "armed",
    "publishing",
    "public",
    "retry_wait",
    "failed",
    "cancelled",
]


class PublicationContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicationRequest(PublicationContract):
    schema_version: Literal["1.0"] = "1.0"
    release_id: str = Field(min_length=1)
    platform: Platform
    idempotency_key: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, object]


class PreparedPublication(PublicationContract):
    schema_version: Literal["1.0"] = "1.0"
    platform: Platform
    state: PublicationState
    remote_id: str = Field(min_length=1)
    known_url: str | None = None
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicationSnapshot(PreparedPublication):
    target_at_utc: datetime | None = None
    public_at: datetime | None = None


class PublicationResult(PublicationContract):
    schema_version: Literal["1.0"] = "1.0"
    release_id: str
    revision: int
    state: str
    target_at_utc: str
    publications: dict[Platform, PublicationSnapshot]
    telegram_caption_path: str | None = None
    next_action: str


class PreflightJobPayload(PublicationContract):
    schema_version: Literal["1.0"] = "1.0"
    target_at_utc: AwareDatetime


class TelegramJobPayload(PublicationContract):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    target_at_utc: AwareDatetime
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    video_path: str = Field(min_length=1)
    video_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    caption_path: str = Field(min_length=1)
    caption_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CancellationJobPayload(PublicationContract):
    schema_version: Literal["1.0"] = "1.0"
    platforms: list[Platform] = Field(min_length=1)


class RecoveryError(PublicationContract):
    """A sanitized recovery error; credentials and provider bodies never enter it."""

    schema_version: Literal["1.0"] = "1.0"
    code: RecoveryErrorCode
    sanitized_detail: str = Field(min_length=1, max_length=1000)


class RetryDecision(PublicationContract):
    """Pure retry result after a failed attempt, with an explicit exhausted state."""

    schema_version: Literal["1.0"] = "1.0"
    operation: RecoveryOperation
    retry_policy_id: RetryPolicyId
    error_code: RecoveryErrorCode
    classification: RecoveryClassification
    disposition: RecoveryDisposition
    attempt_number: int = Field(ge=1)
    retry_after_seconds: int | None = Field(default=None, gt=0)


class RecoveryJobPayload(PublicationContract):
    """A status-first, idempotent reconciliation job for one missing platform."""

    schema_version: Literal["1.0"] = "1.0"
    release_id: str = Field(min_length=1)
    platform: Platform
    target_at_utc: AwareDatetime
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_idempotency_key: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    remote_id: str | None = Field(default=None, min_length=1)


class IncidentRecord(PublicationContract):
    """Durable local incident retained even if its alert cannot be delivered."""

    schema_version: Literal["1.0"] = "1.0"
    incident_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    job_id: str | None = Field(default=None, min_length=1)
    platform: Platform | None = None
    error: RecoveryError
    safe_next_action: str = Field(min_length=1, max_length=500)
    opened_at: AwareDatetime


class NotificationRequest(PublicationContract):
    """The technical Telegram alert request; it is separate from release content."""

    schema_version: Literal["1.0"] = "1.0"
    notification_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    recipient: Literal["276042853"] = SARDOR_TELEGRAM_RECIPIENT
    release_id: str = Field(min_length=1)
    platform: Platform | None = None
    error_code: RecoveryErrorCode
    occurred_at: AwareDatetime
    safe_next_action: str = Field(min_length=1, max_length=500)
    suppression_key: str = Field(min_length=1)


class NotificationJobPayload(PublicationContract):
    """Durable delivery job for a previously created notification request."""

    schema_version: Literal["1.0"] = "1.0"
    notification_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    recipient: Literal["276042853"] = SARDOR_TELEGRAM_RECIPIENT
    release_id: str = Field(min_length=1)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NotificationReceipt(PublicationContract):
    """Validated delivery receipt; a mismatch is terminal and leaves the incident open."""

    schema_version: Literal["1.0"] = "1.0"
    notification_id: str = Field(min_length=1)
    recipient: Literal["276042853"] = SARDOR_TELEGRAM_RECIPIENT
    provider_receipt_id: str = Field(min_length=1)
    delivered_at: AwareDatetime


def notification_request_sha256(request: NotificationRequest) -> str:
    """Return the canonical identity of a durable technical alert request."""

    encoded = json.dumps(
        request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
