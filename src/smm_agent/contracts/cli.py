"""Versioned JSON contracts emitted by ``smmctl``."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ErrorDetail(StrictContract):
    code: str
    message: str
    hint: str
    request_id: str = Field(alias="requestId")
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    error: ErrorDetail


class ReleaseResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    release_id: str
    revision: int
    state: str
    topic: str
    next_action: str
    pending_gate: str | None = None
    target_at_utc: str | None = None
    target_timezone: str = "Europe/Moscow"
    artifacts: dict[str, str] = Field(default_factory=dict)
