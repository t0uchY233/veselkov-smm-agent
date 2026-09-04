"""Versioned contracts for publication preparation and scheduling."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Platform = Literal["youtube", "dzen", "telegram"]
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
