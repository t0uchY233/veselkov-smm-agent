"""Small transport boundary for provider adapters.

The production adapters build complete, deterministic HTTP requests but do not
own sockets, OAuth refreshes, or secret storage.  Those capabilities are
introduced by the Windows composition root.  Keeping the transport injected
makes request construction and failure classification testable without network
access or credentials.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode

from smm_agent.contracts.publication import RecoveryErrorCode
from smm_agent.domain.publication.ports import ProviderOperationError


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One provider request with an operation identity safe for transport logs.

    ``idempotency_key`` is deliberately separate from the HTTP headers: some
    provider APIs ignore a generic idempotency header, while a credential-aware
    transport may still use the identity to reconcile a timed-out request.
    """

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Raw provider response.  Callers never persist ``body`` verbatim."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json_object(self) -> dict[str, object]:
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Provider вернул некорректный JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("Provider вернул JSON не в формате object.")
        return value


class HttpTransport(Protocol):
    """Credential-aware transport supplied by the Windows composition root."""

    def send(self, request: HttpRequest) -> HttpResponse: ...


def json_body(value: Mapping[str, object]) -> bytes:
    """Canonical JSON bytes so retries repeat the same provider payload."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def with_query(url: str, parameters: Mapping[str, str]) -> str:
    """Append deterministic query parameters to an endpoint URL."""

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(sorted(parameters.items()))}"


def provider_error_from_response(
    response: HttpResponse,
    *,
    provider: str,
    missing_code: RecoveryErrorCode = "RECEIPT_MISMATCH",
) -> ProviderOperationError:
    """Map an HTTP result to the closed recovery taxonomy without body leaks."""

    status = response.status_code
    if status in {401}:
        code: RecoveryErrorCode = "PROVIDER_AUTH_REQUIRED"
    elif status in {403}:
        code = "PROVIDER_PERMISSION_DENIED"
    elif status == 404:
        code = missing_code
    elif status == 429:
        code = "PROVIDER_HTTP_429"
    elif 500 <= status <= 599:
        code = "PROVIDER_HTTP_5XX"
    else:
        code = "INVALID_PAYLOAD"
    return ProviderOperationError(
        code=code,
        sanitized_detail=f"{provider} вернул HTTP {status}.",
    )


def provider_error_from_exception(exception: Exception, *, provider: str) -> ProviderOperationError:
    """Classify transport exceptions while never retaining raw exception text."""

    if isinstance(exception, TimeoutError):
        code: RecoveryErrorCode = "PROVIDER_TIMEOUT"
    else:
        code = "UNKNOWN_PROVIDER_OUTCOME"
    return ProviderOperationError(
        code=code,
        sanitized_detail=f"Не удалось подтвердить результат операции {provider}.",
    )
