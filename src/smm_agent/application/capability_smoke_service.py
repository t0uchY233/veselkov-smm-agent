"""Fail-closed, non-production capability smoke orchestration.

Concrete provider probes belong at the composition edge.  This service keeps
their result contract deterministic and makes the safe default explicit: a
normal command reports that no live probe was run; it never turns a status
command into a publication attempt.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from smm_agent.contracts.setup import (
    CapabilitySmokeCheck,
    CapabilitySmokeReport,
    SmokeEvidence,
    SmokeName,
)
from smm_agent.platform.config import SmmAgentConfig

_ALL_SMOKES: tuple[SmokeName, ...] = (
    "youtube.private_publish_at_readback",
    "dzen.draft_schedule_url",
    "telegram.test_send",
    "windows.task_scheduler_registration_wake",
)
_EVIDENCE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_UNSAFE_EVIDENCE = re.compile(
    r"(?i)(authorization|bearer|token|secret|password|cookie|oauth|"
    r"[a-z]:\\|/users/|/home/|https?://|@)"
)


@dataclass(frozen=True, slots=True)
class SmokeProbeResult:
    """The narrow output a live probe may return to the application layer."""

    passed: bool
    message: str
    evidence: dict[str, str] | None = None
    remediation: str | None = None


class CapabilitySmokeProbes(Protocol):
    """Injected side-effect boundary for deliberately isolated test resources."""

    def youtube_private_publish_at_readback(self, config: SmmAgentConfig) -> SmokeProbeResult: ...

    def dzen_draft_schedule_url(self, config: SmmAgentConfig) -> SmokeProbeResult: ...

    def telegram_test_send(self, config: SmmAgentConfig) -> SmokeProbeResult: ...

    def windows_task_scheduler_registration_wake(
        self, config: SmmAgentConfig
    ) -> SmokeProbeResult: ...


class UnavailableCapabilitySmokeProbes:
    """Safe default: production providers are never discovered implicitly."""

    def _unavailable(self) -> SmokeProbeResult:
        return SmokeProbeResult(
            passed=False,
            message="Live smoke adapter не подключён к этому process composition.",
            remediation=(
                "Подключите явный non-production probe factory; production публикация "
                "без этого остаётся заблокированной."
            ),
        )

    def youtube_private_publish_at_readback(self, config: SmmAgentConfig) -> SmokeProbeResult:
        del config
        return self._unavailable()

    def dzen_draft_schedule_url(self, config: SmmAgentConfig) -> SmokeProbeResult:
        del config
        return self._unavailable()

    def telegram_test_send(self, config: SmmAgentConfig) -> SmokeProbeResult:
        del config
        return self._unavailable()

    def windows_task_scheduler_registration_wake(
        self, config: SmmAgentConfig
    ) -> SmokeProbeResult:
        del config
        return self._unavailable()


class CapabilitySmokeService:
    """Run only named, explicitly requested non-production probes."""

    def __init__(self, *, probes: CapabilitySmokeProbes | None = None) -> None:
        self._probes = probes or UnavailableCapabilitySmokeProbes()

    def run(
        self,
        config: SmmAgentConfig,
        *,
        config_path: str,
        execute: bool,
        names: tuple[SmokeName, ...] = _ALL_SMOKES,
    ) -> CapabilitySmokeReport:
        selected = set(names)
        if not selected or not selected.issubset(_ALL_SMOKES):
            raise ValueError("Набор capability smoke содержит неизвестную проверку.")

        checks = tuple(
            self._check(name, config=config, execute=execute, selected=name in selected)
            for name in _ALL_SMOKES
        )
        return CapabilitySmokeReport(
            configPath=config_path,
            configSchemaVersion=config.schema_version,
            executionRequested=execute,
            allRequiredSmokesPassed=all(check.state == "passed" for check in checks),
            # A green synthetic/test-channel probe is evidence only.  It is not
            # permission to publish from this laptop.
            productionReadiness="blocked",
            capabilities=checks,
        )

    def _check(
        self,
        name: SmokeName,
        *,
        config: SmmAgentConfig,
        execute: bool,
        selected: bool,
    ) -> CapabilitySmokeCheck:
        if not selected:
            return CapabilitySmokeCheck(
                name=name,
                state="not_run",
                message="Проверка не выбрана в этом запуске.",
            )
        if not execute:
            return CapabilitySmokeCheck(
                name=name,
                state="not_run",
                message="Live probe не запускался без явного --execute.",
                remediation=(
                    "Запускайте только с тестовым каналом, draft и отдельным task identity."
                ),
            )

        method_name = name.replace(".", "_")
        probe = getattr(self._probes, method_name)
        try:
            result = probe(config)
        except Exception:
            return CapabilitySmokeCheck(
                name=name,
                state="failed",
                message="Live smoke завершился ошибкой без сохраняемых деталей.",
                remediation="Сохраните локальные diagnostics без секретов и устраните причину.",
            )
        return CapabilitySmokeCheck(
            name=name,
            state="passed" if result.passed else "failed",
            message=_safe_message(result.message),
            evidence=_safe_evidence(result.evidence or {}),
            remediation=_safe_message(result.remediation) if result.remediation else None,
        )


def run_capability_smoke(
    config: SmmAgentConfig,
    *,
    config_path: str,
    execute: bool,
    names: tuple[SmokeName, ...] = _ALL_SMOKES,
    probes: CapabilitySmokeProbes | None = None,
) -> CapabilitySmokeReport:
    """Convenience seam used by CLI and isolated composition tests."""

    return CapabilitySmokeService(probes=probes).run(
        config,
        config_path=config_path,
        execute=execute,
        names=names,
    )


def _safe_message(value: str | None) -> str:
    normalized = " ".join((value or "").split())
    if not normalized:
        return "Проверка не вернула безопасного сообщения."
    if _UNSAFE_EVIDENCE.search(normalized) or len(normalized) > 512:
        return "Детали live smoke скрыты для защиты данных."
    return normalized


def _safe_evidence(values: dict[str, str]) -> tuple[SmokeEvidence, ...]:
    evidence: list[SmokeEvidence] = []
    for key, value in sorted(values.items()):
        if not _EVIDENCE_KEY.fullmatch(key):
            continue
        if _UNSAFE_EVIDENCE.search(key):
            continue
        normalized = " ".join(value.split())
        if not normalized:
            continue
        if _UNSAFE_EVIDENCE.search(normalized) or len(normalized) > 256:
            normalized = f"sha256:{hashlib.sha256(normalized.encode()).hexdigest()[:16]}"
        evidence.append(SmokeEvidence(key=key, value=normalized))
    return tuple(evidence)
