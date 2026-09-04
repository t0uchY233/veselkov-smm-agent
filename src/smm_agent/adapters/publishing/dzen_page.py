"""Narrow Dzen Studio page-object contract.

This module deliberately knows no Playwright types.  A Windows-only bootstrap
will later adapt a persistent, headful Playwright profile to ``DzenSession``.
Keeping browser calls behind this small protocol makes the selector contract
testable against synthetic DOM fixtures and prevents a UI change from leaking
through the publication application service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

from smm_agent.domain.publication.ports import DzenDomMismatchError, ProviderOperationError

DZEN_STUDIO_URL = "https://dzen.ru/profile/editor"
_DZEN_ARTICLE_PATH = re.compile(r"^/a/[A-Za-z0-9_-]+$")
DzenPageState = Literal["draft", "scheduled", "public", "cancelled"]


class DzenSession(Protocol):
    """The smallest browser surface the page object needs.

    The future implementation is expected to use a persistent Chromium
    profile and a visible browser for login.  It must not automate CAPTCHA or
    MFA; those states are classified before normal editor interactions.
    """

    def goto(self, url: str) -> None: ...

    def is_visible(self, selector: str) -> bool: ...

    def text_content(self, selector: str) -> str | None: ...

    def get_attribute(self, selector: str, name: str) -> str | None: ...

    def fill(self, selector: str, value: str) -> None: ...

    def click(self, selector: str) -> None: ...

    def set_input_files(self, selector: str, paths: list[Path]) -> None: ...

    def screenshot(self, path: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class DzenSelectors:
    """Semantic selectors asserted by the staging smoke.

    These selectors are intentionally isolated in one place.  They currently
    describe the adapter's supported DOM contract, not a promise that an
    unverified live Dzen layout has these attributes.  The live capability
    check maps them to verified selectors before production wiring is enabled.
    """

    authenticated_marker: str = "[data-testid='dzen-editor-authenticated']"
    captcha_marker: str = "[data-testid='dzen-captcha']"
    mfa_marker: str = "[data-testid='dzen-mfa']"
    new_article: str = "[data-testid='dzen-new-article']"
    title_input: str = "[data-testid='dzen-title-input']"
    article_input: str = "[data-testid='dzen-article-input']"
    cover_input: str = "[data-testid='dzen-cover-input']"
    visual_input: str = "[data-testid='dzen-visual-input']"
    save_draft: str = "[data-testid='dzen-save-draft']"
    article_root: str = "[data-testid='dzen-article']"
    article_title: str = "[data-testid='dzen-article-title']"
    schedule_input: str = "[data-testid='dzen-schedule-at-utc']"
    schedule_button: str = "[data-testid='dzen-schedule']"
    cancel_schedule: str = "[data-testid='dzen-cancel-schedule']"


@dataclass(frozen=True, slots=True)
class DzenArticleReceipt:
    """Safe, observable state extracted from an article page.

    ``diagnostic_screenshot_path`` is a local path only.  Browser storage,
    cookies, article text and raw DOM are intentionally absent from this value.
    """

    article_id: str
    article_url: str
    title: str
    state: DzenPageState
    target_at_utc: datetime | None
    public_at_utc: datetime | None
    diagnostic_screenshot_path: str | None = None


@dataclass(frozen=True, slots=True)
class DzenDiagnosticPaths:
    """Constrain browser screenshots to an operator-provided diagnostic root."""

    root: Path

    def for_stage(
        self, stage: Literal["prepared", "scheduled", "status", "failure"]
    ) -> Path:
        root = self.root.resolve()
        path = (root / f"dzen-{stage}.png").resolve()
        try:
            path.relative_to(root)
        except ValueError as error:  # pragma: no cover - defensive custom Path handling.
            raise ValueError("Путь screenshot должен оставаться в diagnostics root.") from error
        return path


def _manual_login_required() -> ProviderOperationError:
    return ProviderOperationError(
        code="PROVIDER_AUTH_REQUIRED",
        sanitized_detail=(
            "Требуется ручной вход в Дзен в видимом браузере: CAPTCHA и MFA не обходятся."
        ),
    )


def _parse_utc(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DzenDomMismatchError() from error
    if parsed.tzinfo is None:
        raise DzenDomMismatchError()
    return parsed.astimezone(UTC)


def _article_identity(article_url: str | None) -> tuple[str, str]:
    if article_url is None:
        raise DzenDomMismatchError()
    parsed = urlparse(article_url)
    if parsed.scheme != "https" or parsed.netloc not in {"dzen.ru", "www.dzen.ru"}:
        raise DzenDomMismatchError()
    if parsed.query or parsed.fragment or not _DZEN_ARTICLE_PATH.fullmatch(parsed.path):
        raise DzenDomMismatchError()
    article_id = parsed.path.removeprefix("/a/")
    return article_id, f"https://dzen.ru/a/{article_id}"


class DzenPage:
    """One verified interaction surface of an existing Dzen Studio session."""

    def __init__(
        self,
        session: DzenSession,
        *,
        selectors: DzenSelectors | None = None,
        diagnostics: DzenDiagnosticPaths | None = None,
    ) -> None:
        self._session = session
        self._selectors = selectors or DzenSelectors()
        self._diagnostics = diagnostics

    def _capture(
        self, stage: Literal["prepared", "scheduled", "status", "failure"]
    ) -> str | None:
        if self._diagnostics is None:
            return None
        path = self._diagnostics.for_stage(stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._session.screenshot(path)
        return str(path)

    def _require_authenticated(self) -> None:
        if self._session.is_visible(self._selectors.captcha_marker) or self._session.is_visible(
            self._selectors.mfa_marker
        ):
            self._capture("failure")
            raise _manual_login_required()
        if not self._session.is_visible(self._selectors.authenticated_marker):
            self._capture("failure")
            raise _manual_login_required()

    def _require_visible(self, selector: str) -> None:
        if not self._session.is_visible(selector):
            self._capture("failure")
            raise DzenDomMismatchError()

    def _receipt(
        self, *, stage: Literal["prepared", "scheduled", "status", "failure"]
    ) -> DzenArticleReceipt:
        self._require_visible(self._selectors.article_root)
        article_id, article_url = _article_identity(
            self._session.get_attribute(self._selectors.article_root, "data-article-url")
        )
        title = self._session.text_content(self._selectors.article_title)
        raw_state = self._session.get_attribute(
            self._selectors.article_root, "data-publication-state"
        )
        if title is None or not title.strip():
            self._capture("failure")
            raise DzenDomMismatchError()
        if raw_state == "draft":
            state: DzenPageState = "draft"
        elif raw_state == "scheduled":
            state = "scheduled"
        elif raw_state == "public":
            state = "public"
        elif raw_state == "cancelled":
            state = "cancelled"
        else:
            self._capture("failure")
            raise DzenDomMismatchError()
        target_at_utc = _parse_utc(
            self._session.get_attribute(self._selectors.article_root, "data-target-at-utc")
        )
        public_at_utc = _parse_utc(
            self._session.get_attribute(self._selectors.article_root, "data-public-at-utc")
        )
        if state == "scheduled" and target_at_utc is None:
            self._capture("failure")
            raise DzenDomMismatchError()
        if state == "public" and public_at_utc is None:
            self._capture("failure")
            raise DzenDomMismatchError()
        return DzenArticleReceipt(
            article_id=article_id,
            article_url=article_url,
            title=title.strip(),
            state=state,
            target_at_utc=target_at_utc,
            public_at_utc=public_at_utc,
            diagnostic_screenshot_path=self._capture(stage),
        )

    @staticmethod
    def assert_identity(
        receipt: DzenArticleReceipt,
        *,
        expected_article_id: str,
        expected_title: str,
    ) -> None:
        if receipt.article_id != expected_article_id or receipt.title != expected_title:
            raise DzenDomMismatchError()

    @staticmethod
    def assert_scheduled_target(
        receipt: DzenArticleReceipt, *, target_at_utc: datetime
    ) -> None:
        expected = target_at_utc.astimezone(UTC)
        if receipt.state != "scheduled" or receipt.target_at_utc != expected:
            raise DzenDomMismatchError()

    def prepare_draft(
        self,
        *,
        title: str,
        article_markdown: str,
        cover_path: Path,
        visual_paths: list[Path],
    ) -> DzenArticleReceipt:
        """Create and save one draft; never attempts an interactive login."""

        self._session.goto(DZEN_STUDIO_URL)
        self._require_authenticated()
        for selector in (
            self._selectors.new_article,
            self._selectors.title_input,
            self._selectors.article_input,
            self._selectors.cover_input,
            self._selectors.visual_input,
            self._selectors.save_draft,
        ):
            self._require_visible(selector)
        self._session.click(self._selectors.new_article)
        self._session.fill(self._selectors.title_input, title)
        self._session.fill(self._selectors.article_input, article_markdown)
        self._session.set_input_files(self._selectors.cover_input, [cover_path])
        self._session.set_input_files(self._selectors.visual_input, visual_paths)
        self._session.click(self._selectors.save_draft)
        receipt = self._receipt(stage="prepared")
        self.assert_identity(
            receipt, expected_article_id=receipt.article_id, expected_title=title
        )
        if receipt.state != "draft":
            self._capture("failure")
            raise DzenDomMismatchError()
        return receipt

    def read_article(self, article_url: str) -> DzenArticleReceipt:
        """Open a known stable article URL and return only its verified receipt."""

        expected_article_id, expected_url = _article_identity(article_url)
        self._session.goto(expected_url)
        self._require_authenticated()
        receipt = self._receipt(stage="status")
        self.assert_identity(
            receipt, expected_article_id=expected_article_id, expected_title=receipt.title
        )
        return receipt

    def schedule_article(
        self, article_url: str, *, target_at_utc: datetime
    ) -> DzenArticleReceipt:
        expected_article_id, expected_url = _article_identity(article_url)
        self._session.goto(expected_url)
        self._require_authenticated()
        self._require_visible(self._selectors.schedule_input)
        self._require_visible(self._selectors.schedule_button)
        self._session.fill(
            self._selectors.schedule_input,
            target_at_utc.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )
        self._session.click(self._selectors.schedule_button)
        receipt = self._receipt(stage="scheduled")
        self.assert_identity(
            receipt, expected_article_id=expected_article_id, expected_title=receipt.title
        )
        self.assert_scheduled_target(receipt, target_at_utc=target_at_utc)
        return receipt

    def cancel_scheduled_article(self, article_url: str) -> DzenArticleReceipt:
        expected_article_id, expected_url = _article_identity(article_url)
        self._session.goto(expected_url)
        self._require_authenticated()
        before = self._receipt(stage="status")
        self.assert_identity(
            before, expected_article_id=expected_article_id, expected_title=before.title
        )
        if before.state == "public":
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Публичную статью Дзена нельзя отменить автоматически.",
            )
        self._require_visible(self._selectors.cancel_schedule)
        self._session.click(self._selectors.cancel_schedule)
        receipt = self._receipt(stage="status")
        self.assert_identity(
            receipt, expected_article_id=expected_article_id, expected_title=before.title
        )
        if receipt.state != "cancelled":
            self._capture("failure")
            raise DzenDomMismatchError()
        return receipt
