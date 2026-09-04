"""Redaction at persistence boundaries for provider-derived diagnostic text."""

import re

from smm_agent.contracts.publication import RecoveryError

_NAMED_SECRET = re.compile(
    r"(?i)\b(authorization|bearer|token|access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|secret|password)\b(\s*[:=]\s*|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access[_-]?token|refresh[_-]?token|token|api[_-]?key|secret|password)=)[^&#\s]+"
)
_PROVIDER_BODY_MARKER = re.compile(r"(?i)\b(response\s*body|provider\s*body|html\s*body)\b")


def redact_persisted_detail(detail: str) -> str:
    """Return bounded diagnostics which cannot retain credentials or raw bodies."""

    normalized = " ".join(detail.split())
    if not normalized:
        return "Ошибка без безопасных деталей."
    if _PROVIDER_BODY_MARKER.search(normalized) or ("{" in normalized and "}" in normalized):
        return "Детали ответа площадки скрыты."
    normalized = _NAMED_SECRET.sub(r"\1\2[REDACTED]", normalized)
    normalized = _QUERY_SECRET.sub(r"\1[REDACTED]", normalized)
    if len(normalized) > 1000:
        return "Детали ответа площадки скрыты из-за размера."
    return normalized


def sanitize_recovery_error(error: RecoveryError) -> RecoveryError:
    """Copy an error with its detail redacted immediately before persistence."""

    return error.model_copy(
        update={"sanitized_detail": redact_persisted_detail(error.sanitized_detail)}
    )
