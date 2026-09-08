"""Narrow production composition contracts for publishing providers.

Provider adapters deliberately do not discover credentials or open network
connections at import time.  A Windows deployment supplies one factory after
local capability validation; tests supply fakes.  The default fails closed so
``smm-worker --config`` can never quietly fall back to replay adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from smm_agent.adapters.publishing.dzen import DzenPublisher, DzenSession
from smm_agent.adapters.publishing.dzen_page import DzenPage
from smm_agent.adapters.publishing.dzen_receipts import JsonDzenReceiptStore
from smm_agent.adapters.publishing.http import (
    HttpTransport,
    TelegramBotHttpsTransport,
)
from smm_agent.adapters.publishing.telegram import TelegramPublisher
from smm_agent.adapters.publishing.youtube import JsonYouTubeUploadReceiptStore, YouTubePublisher
from smm_agent.adapters.secrets.secret_reader import SecretValue
from smm_agent.contracts.publication import Platform
from smm_agent.domain.notification.ports import AlertTransport
from smm_agent.domain.publication.ports import Publisher
from smm_agent.platform.config import SmmAgentConfig
from smm_agent.platform.db import Database
from smm_agent.platform.telegram_tasks import SQLiteTelegramTaskStore


class RuntimeCompositionError(RuntimeError):
    """A safe, actionable reason why a live worker must not start."""


class SecretReader(Protocol):
    """Least-privilege secret reader, injectable only at a composition edge."""

    def read(self, reference: str) -> SecretValue:
        """Return one secret value without logging or persisting it."""


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    """The only provider objects a publication worker may receive."""

    publishers: Mapping[Platform, Publisher]
    alert_transport: AlertTransport
    capability_label: str

    def __post_init__(self) -> None:
        expected = {"youtube", "dzen", "telegram"}
        actual = set(self.publishers)
        if actual != expected:
            raise RuntimeCompositionError(
                "Live provider factory должна явно предоставить YouTube, Dzen и Telegram."
            )
        if not self.capability_label or len(self.capability_label) > 128:
            raise RuntimeCompositionError(
                "Live provider factory вернула неверный capability label."
            )


class ProviderFactory(Protocol):
    """Composable live provider boundary; concrete implementations stay replaceable."""

    def create(
        self,
        *,
        config: SmmAgentConfig,
        database: Database,
        secrets: SecretReader,
    ) -> ProviderRuntime: ...


class UnavailableProviderFactory:
    """Fail closed until the Windows installer explicitly binds live adapters."""

    def create(
        self,
        *,
        config: SmmAgentConfig,
        database: Database,
        secrets: SecretReader,
    ) -> ProviderRuntime:
        del config, database, secrets
        raise RuntimeCompositionError(
            "Live provider factory не подключена; replay не будет использован "
            "без --publication-replay."
        )


class LiveProviderBindings(Protocol):
    """Windows-only integrations that require verified external capabilities.

    Dzen's visible browser session, YouTube OAuth client wiring and incident
    alert transport are intentionally injected rather than guessed from the
    machine.  This lets the composition root use concrete adapters without
    creating a second, conflicting transport implementation.
    """

    def youtube_transport(
        self, *, config: SmmAgentConfig, secrets: SecretReader
    ) -> HttpTransport: ...

    def dzen_session(self, *, config: SmmAgentConfig) -> DzenSession: ...

    def alert_transport(
        self, *, config: SmmAgentConfig, secrets: SecretReader
    ) -> AlertTransport: ...


class _TelegramCredential:
    """Read a Bot token only where ``TelegramBotHttpsTransport`` performs I/O."""

    def __init__(self, *, secrets: SecretReader, reference: str) -> None:
        self._secrets = secrets
        self._reference = reference

    def bot_token(self) -> str:
        return self._secrets.read(self._reference).reveal()


class WindowsProviderFactory:
    """Build concrete provider adapters from explicit, verified bindings.

    The class wires Telegram's token-safe transport directly: adapters see the
    stable safe ``/bot/`` path, while the transport substitutes the token only
    at socket I/O.  Other bindings remain explicit until their real capability
    smoke is accepted on the Windows laptop.
    """

    def __init__(self, *, bindings: LiveProviderBindings, production_ready: bool = False) -> None:
        self._bindings = bindings
        # A completed smoke report is evidence, not auto-approval. The Windows
        # installer must pass this explicit post-review gate; default remains
        # fail-closed for every ordinary ``smm-worker --config`` invocation.
        self._production_ready = production_ready

    def create(
        self,
        *,
        config: SmmAgentConfig,
        database: Database,
        secrets: SecretReader,
    ) -> ProviderRuntime:
        if not self._production_ready:
            raise RuntimeCompositionError(
                "Live provider composition заблокирована до явного принятия non-production smoke."
            )
        youtube = YouTubePublisher(
            transport=self._bindings.youtube_transport(config=config, secrets=secrets),
            channel_id=config.youtube.channel_id,
            receipt_store=JsonYouTubeUploadReceiptStore(
                database.data_root / "state/youtube-upload-receipts.json"
            ),
        )
        dzen = DzenPublisher(
            DzenPage(
                self._bindings.dzen_session(config=config),
                expected_channel_url=config.dzen.channel_url,
                expected_author_identity=config.dzen.author_identity,
            ),
            expected_channel_url=config.dzen.channel_url,
            expected_author_identity=config.dzen.author_identity,
            receipt_store=JsonDzenReceiptStore(
                database.data_root / "state/dzen-receipts.json"
            ),
        )
        telegram = TelegramPublisher(
            transport=TelegramBotHttpsTransport(
                credential=_TelegramCredential(
                    secrets=secrets,
                    reference=config.telegram.bot_credential_ref,
                )
            ),
            chat_id=config.telegram.channel_id,
            api_base="https://api.telegram.org",
            task_store=SQLiteTelegramTaskStore(database),
        )
        publishers: dict[Platform, Publisher] = {
            "youtube": cast(Publisher, youtube),
            "dzen": cast(Publisher, dzen),
            "telegram": cast(Publisher, telegram),
        }
        return ProviderRuntime(
            publishers=publishers,
            alert_transport=self._bindings.alert_transport(config=config, secrets=secrets),
            capability_label="windows-live-provider-bindings",
        )
