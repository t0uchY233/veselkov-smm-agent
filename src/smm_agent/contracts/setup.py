"""Versioned setup, runtime and non-production smoke contracts."""

from typing import Literal

from pydantic import Field

from smm_agent.contracts.cli import StrictContract

CapabilityState = Literal["available", "unavailable", "warning"]
SmokeState = Literal["passed", "failed", "not_run"]
SmokeName = Literal[
    "youtube.private_publish_at_readback",
    "dzen.draft_schedule_url",
    "telegram.test_send",
    "windows.task_scheduler_registration_wake",
]


class CapabilityCheck(StrictContract):
    name: str
    state: CapabilityState
    message: str
    remediation: str | None = None


class CapabilityReport(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    config_path: str = Field(alias="configPath")
    config_schema_version: Literal["1.0"] = Field(alias="configSchemaVersion")
    schema_valid: bool = Field(alias="schemaValid")
    local_foundation_ready: bool = Field(alias="localFoundationReady")
    production_readiness: Literal["not_assessed"] = Field(
        default="not_assessed", alias="productionReadiness"
    )
    capabilities: tuple[CapabilityCheck, ...]


class SmokeEvidence(StrictContract):
    """A bounded, secret-free fact captured by a non-production smoke probe."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    value: str = Field(min_length=1, max_length=256)


class CapabilitySmokeCheck(StrictContract):
    name: SmokeName
    state: SmokeState
    message: str = Field(min_length=1, max_length=512)
    evidence: tuple[SmokeEvidence, ...] = ()
    remediation: str | None = None


class CapabilitySmokeReport(StrictContract):
    """Result of intentional probes against non-production provider resources.

    A smoke report is evidence, not production approval.  The value remains
    ``blocked`` even after every probe passes because the final risk decision
    belongs to Sardor on the target laptop.
    """

    schema_version: Literal["1.0"] = "1.0"
    config_path: str = Field(alias="configPath")
    config_schema_version: Literal["1.0"] = Field(alias="configSchemaVersion")
    non_production: Literal[True] = Field(default=True, alias="nonProduction")
    execution_requested: bool = Field(alias="executionRequested")
    all_required_smokes_passed: bool = Field(alias="allRequiredSmokesPassed")
    production_readiness: Literal["blocked"] = Field(
        default="blocked", alias="productionReadiness"
    )
    capabilities: tuple[CapabilitySmokeCheck, ...]


class DzenLoginHandoff(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    state: Literal["requires_headful_windows", "unavailable"]
    browser_profile: str = Field(alias="browserProfile")
    message: str
    next_action: str = Field(alias="nextAction")
