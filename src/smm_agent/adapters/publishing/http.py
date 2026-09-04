"""Small transport boundary for provider adapters.

The production adapters build complete, deterministic HTTP requests but do not
own sockets, OAuth refreshes, or secret storage.  Those capabilities are
introduced by the Windows composition root.  Keeping the transport injected
makes request construction and failure classification testable without network
access or credentials.
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from smm_agent.contracts.publication import RecoveryErrorCode
from smm_agent.domain.publication.ports import ProviderOperationError


class HttpBodyStream(Protocol):
    """Repeatable request body used for large provider uploads.

    A stream belongs to a single ``HttpRequest`` attempt.  The transport reads
    only its yielded chunks after the headers are sent, so a caller does not
    need to materialise a video or multipart body in memory.  Implementations
    must yield exactly ``content_length`` bytes and be repeatable only when a
    caller deliberately creates a new request for a retry.
    """

    @property
    def content_length(self) -> int: ...

    def iter_chunks(self) -> Iterator[bytes]: ...


class AuthorizationCredential(Protocol):
    """Provides an ephemeral Authorization value without exposing a token."""

    def authorization_header(self) -> str: ...


class TelegramBotCredential(Protocol):
    """Supplies a Bot API token only at the socket-I/O boundary."""

    def bot_token(self) -> str: ...


@dataclass(frozen=True, slots=True, repr=False)
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
    body_stream: HttpBodyStream | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.body is not None and self.body_stream is not None:
            raise ValueError("HttpRequest не может содержать body и body_stream одновременно.")
        if self.body_stream is not None and self.body_stream.content_length < 0:
            raise ValueError("HttpBodyStream content_length не может быть отрицательным.")
        if any(key.lower() == "authorization" for key in self.headers):
            raise ValueError(
                "Authorization должен добавляться credential-aware transport, а не request."
            )

    def __repr__(self) -> str:
        if self.body is not None:
            body_description = f"<{len(self.body)} bytes>"
        elif self.body_stream is not None:
            body_description = f"<stream {self.body_stream.content_length} bytes>"
        else:
            body_description = "None"
        return (
            "HttpRequest("
            f"method={self.method!r}, url={redact_url(self.url)!r}, "
            f"headers={redact_headers(self.headers)!r}, body={body_description}, "
            f"idempotency_key={self.idempotency_key!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class HttpResponse:
    """Raw provider response.  Callers never persist ``body`` verbatim."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def __repr__(self) -> str:
        return (
            "HttpResponse("
            f"status_code={self.status_code!r}, headers={redact_headers(self.headers)!r}, "
            f"body=<{len(self.body)} bytes>)"
        )

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


class HttpTransportError(RuntimeError):
    """A socket/protocol failure whose public representation contains no secret."""

    def __init__(self, message: str = "HTTPS transport не завершил запрос.") -> None:
        super().__init__(message)


class HttpCredentialError(HttpTransportError):
    """Safe typed signal that OAuth credentials need human intervention."""

    def __init__(
        self, message: str = "Provider credential недоступен или требует обновления."
    ) -> None:
        super().__init__(message)


class RedactingHttpsTransport:
    """Concrete stdlib HTTPS transport with injected, non-loggable credentials.

    Provider adapters construct requests without credentials.  Immediately
    before I/O this transport gets the current OAuth access token and injects
    it as the only Authorization header.  Neither requests, response errors,
    nor raised transport exceptions retain that value.  The class deliberately
    uses only the Python standard library so it can be installed on the
    Windows laptop without an additional HTTP dependency.
    """

    def __init__(
        self,
        *,
        credential: AuthorizationCredential,
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("HTTPS timeout должен быть положительным.")
        self._credential = credential
        self._timeout_seconds = timeout_seconds

    def send(self, request: HttpRequest) -> HttpResponse:
        parsed = urlsplit(request.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise HttpTransportError("Provider transport принимает только абсолютные HTTPS URLs.")
        hostname = parsed.hostname
        assert hostname is not None
        headers = {str(key): str(value) for key, value in request.headers.items()}
        # HttpRequest rejects this too; keep the invariant at the I/O edge if a
        # custom Mapping changes between construction and dispatch.
        if any(key.lower() == "authorization" for key in headers):
            raise HttpTransportError("Authorization нельзя передавать в provider request.")
        try:
            authorization = self._credential.authorization_header()
        except Exception:
            raise HttpCredentialError() from None
        if not authorization:
            raise HttpCredentialError("Provider credential не вернул Authorization header.")
        headers["Authorization"] = authorization
        return self._send_with_headers(
            request=request, parsed=parsed, headers=headers, hostname=hostname
        )

    def _send_with_headers(
        self,
        *,
        request: HttpRequest,
        parsed: SplitResult,
        headers: dict[str, str],
        hostname: str,
    ) -> HttpResponse:
        body_length = self._body_length(request)
        supplied_length = next(
            (value for key, value in headers.items() if key.lower() == "content-length"), None
        )
        if supplied_length is None:
            headers["Content-Length"] = str(body_length)
        elif supplied_length != str(body_length):
            raise HttpTransportError("Content-Length не совпадает с request body.")

        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection = http.client.HTTPSConnection(
            hostname,
            port=parsed.port,
            timeout=self._timeout_seconds,
        )
        try:
            connection.putrequest(request.method, path, skip_accept_encoding=True)
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            if request.body is not None:
                connection.send(request.body)
            elif request.body_stream is not None:
                self._send_stream(connection, request.body_stream)
            raw_response = connection.getresponse()
            return HttpResponse(
                status_code=raw_response.status,
                headers={key: value for key, value in raw_response.getheaders()},
                body=raw_response.read(),
            )
        except TimeoutError:
            raise TimeoutError("HTTPS provider request превысил timeout.") from None
        except (OSError, http.client.HTTPException):
            raise HttpTransportError() from None
        finally:
            connection.close()

    @staticmethod
    def _body_length(request: HttpRequest) -> int:
        if request.body is not None:
            return len(request.body)
        if request.body_stream is not None:
            return request.body_stream.content_length
        return 0

    @staticmethod
    def _send_stream(connection: http.client.HTTPSConnection, stream: HttpBodyStream) -> None:
        sent = 0
        for chunk in stream.iter_chunks():
            if not isinstance(chunk, bytes) or not chunk:
                raise HttpTransportError("HttpBodyStream должен выдавать непустые bytes chunks.")
            sent += len(chunk)
            if sent > stream.content_length:
                raise HttpTransportError("HttpBodyStream превысил declared content_length.")
            connection.send(chunk)
        if sent != stream.content_length:
            raise HttpTransportError("HttpBodyStream не достиг declared content_length.")


class TelegramBotHttpsTransport(RedactingHttpsTransport):
    """HTTPS transport that injects ``/bot<TOKEN>/`` only while sending.

    Telegram adapters use literal safe URLs containing ``/bot/``.  The token
    never enters ``HttpRequest``, its repr, an exception, or a provider log.
    """

    def __init__(
        self,
        *,
        credential: TelegramBotCredential,
        timeout_seconds: float = 30.0,
    ) -> None:
        # RedactingHttpsTransport normally needs a Bearer credential. This
        # value is never used because send is overridden; it merely shares the
        # carefully bounded stdlib I/O implementation.
        class _UnusedCredential:
            def authorization_header(self) -> str:
                raise AssertionError("Telegram transport does not use Bearer auth.")

        super().__init__(credential=_UnusedCredential(), timeout_seconds=timeout_seconds)
        self._telegram_credential = credential

    def send(self, request: HttpRequest) -> HttpResponse:
        parsed = urlsplit(request.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise HttpTransportError("Provider transport принимает только абсолютные HTTPS URLs.")
        hostname = parsed.hostname
        assert hostname is not None
        marker = "/bot/"
        if marker not in parsed.path or parsed.path.count(marker) != 1:
            raise HttpTransportError("Telegram request должен содержать безопасный path /bot/.")
        headers = {str(key): str(value) for key, value in request.headers.items()}
        if any(key.lower() == "authorization" for key in headers):
            raise HttpTransportError("Telegram request не должен содержать Authorization.")
        try:
            token = self._telegram_credential.bot_token()
        except Exception:
            raise HttpCredentialError() from None
        if not token or any(character.isspace() for character in token) or "/" in token:
            raise HttpCredentialError("Telegram credential не вернул корректный Bot token.")
        wire_path = parsed.path.replace(marker, f"/bot{token}/")
        wire_parsed = urlsplit(
            urlunsplit((parsed.scheme, parsed.netloc, wire_path, parsed.query, ""))
        )
        return self._send_with_headers(
            request=request, parsed=wire_parsed, headers=headers, hostname=hostname
        )


def json_body(value: Mapping[str, object]) -> bytes:
    """Canonical JSON bytes so retries repeat the same provider payload."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def with_query(url: str, parameters: Mapping[str, str]) -> str:
    """Append deterministic query parameters to an endpoint URL."""

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(sorted(parameters.items()))}"


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a diagnostics-safe header mapping without credential material."""

    sensitive = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
    return {
        str(key): "<redacted>" if str(key).lower() in sensitive else str(value)
        for key, value in headers.items()
    }


def redact_url(url: str) -> str:
    """Remove bearer-like query/path values before a URL can reach diagnostics."""

    parsed = urlsplit(url)
    safe_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        safe_query.append(
            (key, "<redacted>")
            if any(marker in lowered for marker in ("token", "secret", "authorization", "key"))
            else (key, value)
        )
    path_parts = parsed.path.split("/")
    safe_parts = [
        "<redacted>" if part.lower().startswith("bot") and len(part) > 3 else part
        for part in path_parts
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, "/".join(safe_parts), urlencode(safe_query), "")
    )


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
    elif isinstance(exception, HttpCredentialError):
        code = "PROVIDER_AUTH_REQUIRED"
    else:
        code = "UNKNOWN_PROVIDER_OUTCOME"
    return ProviderOperationError(
        code=code,
        sanitized_detail=f"Не удалось подтвердить результат операции {provider}.",
    )
