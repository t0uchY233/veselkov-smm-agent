"""OAuth refresh boundary for credential-aware HTTPS transports.

The YouTube adapter never sees an access or refresh token.  It receives a
transport whose credential resolves an ephemeral Authorization header only at
the last possible moment.
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit

from smm_agent.adapters.secrets.secret_reader import (
    SecretReader,
    SecretUnavailableError,
    SecretValue,
)


class OAuthRefreshError(RuntimeError):
    """OAuth failure safe for provider error classification and logs."""

    def __init__(self, message: str = "Не удалось обновить OAuth credential.") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True, repr=False)
class OAuthAccessToken:
    """A short-lived token which can only render as redacted diagnostics."""

    token: SecretValue
    expires_at: datetime

    def valid_at(self, now: datetime, *, skew_seconds: int = 30) -> bool:
        return self.expires_at > now.astimezone(UTC) + timedelta(seconds=skew_seconds)

    def __repr__(self) -> str:
        return f"OAuthAccessToken(token=<redacted>, expires_at={self.expires_at!r})"


class RefreshingOAuthCredential:
    """Lazily refreshes a secret-stored refresh token and yields Bearer auth."""

    def __init__(
        self,
        *,
        secret_reader: SecretReader,
        credential_reference: str,
        refresher: Callable[[SecretValue], OAuthAccessToken],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not credential_reference:
            raise ValueError("OAuth credential reference не может быть пустым.")
        self._secret_reader = secret_reader
        self._credential_reference = credential_reference
        self._refresher = refresher
        self._now = now or (lambda: datetime.now(UTC))
        self._access_token: OAuthAccessToken | None = None

    def authorization_header(self) -> str:
        now = self._now().astimezone(UTC)
        token = self._access_token
        if token is None or not token.valid_at(now):
            try:
                refresh_token = self._secret_reader.read(self._credential_reference)
                token = self._refresher(refresh_token)
            except (SecretUnavailableError, OAuthRefreshError):
                raise
            except Exception:
                raise OAuthRefreshError() from None
            if not token.valid_at(now, skew_seconds=0):
                raise OAuthRefreshError("OAuth server вернул уже истёкший access token.")
            self._access_token = token
        return f"Bearer {token.token.reveal()}"


class StdlibOAuthTokenRefresher:
    """Minimal HTTPS refresh-token client with no response-body diagnostics."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: SecretValue | None = None,
        timeout_seconds: float = 30.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = urlsplit(token_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("OAuth token URL должен быть абсолютным HTTPS URL.")
        if not client_id or timeout_seconds <= 0:
            raise ValueError("OAuth client_id и timeout обязательны.")
        self._url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout_seconds = timeout_seconds
        self._now = now or (lambda: datetime.now(UTC))

    def __call__(self, refresh_token: SecretValue) -> OAuthAccessToken:
        parsed = urlsplit(self._url)
        hostname = parsed.hostname
        assert hostname is not None
        fields = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token.reveal(),
            "client_id": self._client_id,
        }
        if self._client_secret is not None:
            fields["client_secret"] = self._client_secret.reveal()
        body = urlencode(fields).encode("ascii")
        connection = http.client.HTTPSConnection(
            hostname, port=parsed.port, timeout=self._timeout_seconds
        )
        try:
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            raw = response.read()
        except TimeoutError:
            raise OAuthRefreshError("OAuth refresh превысил timeout.") from None
        except (OSError, http.client.HTTPException):
            raise OAuthRefreshError() from None
        finally:
            connection.close()
        if response.status != 200:
            raise OAuthRefreshError(f"OAuth server вернул HTTP {response.status}.")
        return self._parse_response(raw)

    def _parse_response(self, raw: bytes) -> OAuthAccessToken:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OAuthRefreshError("OAuth server вернул некорректный JSON.") from None
        if not isinstance(value, Mapping):
            raise OAuthRefreshError("OAuth server вернул некорректный JSON.")
        token = value.get("access_token")
        expires_in = value.get("expires_in")
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(expires_in, int)
            or expires_in <= 0
        ):
            raise OAuthRefreshError("OAuth server не вернул access token и срок действия.")
        return OAuthAccessToken(
            token=SecretValue(token),
            expires_at=self._now().astimezone(UTC) + timedelta(seconds=expires_in),
        )
