"""Concrete Windows bindings; constructors never authorize production work."""

from smm_agent.adapters.notification.telegram import TelegramAlertTransport
from smm_agent.adapters.publishing.dzen_page import DzenSession
from smm_agent.adapters.publishing.dzen_playwright import PlaywrightDzenSession
from smm_agent.adapters.publishing.http import (
    HttpTransport,
    RedactingHttpsTransport,
    TelegramBotHttpsTransport,
)
from smm_agent.adapters.publishing.oauth import RefreshingOAuthCredential, StdlibOAuthTokenRefresher
from smm_agent.adapters.secrets.secret_reader import SecretReader
from smm_agent.domain.notification.ports import AlertTransport
from smm_agent.platform.config import SmmAgentConfig, configured_path
from smm_agent.platform.db import Database


class _BotCredential:
    def __init__(self, secrets: SecretReader, reference: str) -> None:
        self._secrets = secrets
        self._reference = reference

    def bot_token(self) -> str:
        return self._secrets.read(self._reference).reveal()


class WindowsLiveBindings:
    def youtube_transport(
        self, *, config: SmmAgentConfig, secrets: SecretReader
    ) -> HttpTransport:
        refresher = StdlibOAuthTokenRefresher(
            token_url="https://oauth2.googleapis.com/token",
            client_id=config.youtube.oauth_client_id,
            client_secret=secrets.read(config.youtube.client_secret_credential_ref),
        )
        credential = RefreshingOAuthCredential(
            secret_reader=secrets,
            credential_reference=config.youtube.credential_ref,
            refresher=refresher,
        )
        return RedactingHttpsTransport(credential=credential, timeout_seconds=60)

    def dzen_session(self, *, config: SmmAgentConfig) -> DzenSession:
        return PlaywrightDzenSession.open(
            profile_path=configured_path(config.dzen.browser_profile), browser_channel="chrome"
        )

    def alert_transport(
        self, *, config: SmmAgentConfig, secrets: SecretReader
    ) -> AlertTransport:
        return TelegramAlertTransport(
            database=Database(configured_path(config.runtime.data_root)),
            transport=TelegramBotHttpsTransport(
                credential=_BotCredential(secrets, config.telegram.bot_credential_ref)
            ),
        )
