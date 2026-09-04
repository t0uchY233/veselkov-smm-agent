"""Versioned setup and capability contracts emitted by ``smmctl``."""

from typing import Literal

from pydantic import Field

from smm_agent.contracts.cli import StrictContract

CapabilityState = Literal["available", "unavailable", "warning"]


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


class DzenLoginHandoff(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    state: Literal["requires_headful_windows", "unavailable"]
    browser_profile: str = Field(alias="browserProfile")
    message: str
    next_action: str = Field(alias="nextAction")
