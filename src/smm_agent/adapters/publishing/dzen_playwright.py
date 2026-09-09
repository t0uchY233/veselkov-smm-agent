"""Headful persistent-profile adapter for the narrow :mod:`dzen_page` port.

This module intentionally imports Playwright lazily. Installing the optional
browser package is a Windows live-capability concern, not an import-time
requirement for the worker, tests or replay mode. The adapter never types a
password, solves CAPTCHA or answers MFA: DzenPage detects those states and
returns a terminal manual-login error.
"""

from __future__ import annotations

import importlib
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from smm_agent.domain.publication.ports import DzenDomMismatchError, ProviderOperationError


class PlaywrightDzenSession:
    """Visible Chromium session backed by an operator-owned profile directory."""

    def __init__(self, *, page: Any, context: Any, playwright: Any, timeout_ms: int) -> None:
        self._page = page
        self._context = context
        self._playwright = playwright
        self._timeout_ms = timeout_ms

    @classmethod
    def open(
        cls,
        *,
        profile_path: Path,
        timeout_ms: int = 15_000,
        headless: bool = False,
        browser_channel: str | None = "chrome",
    ) -> PlaywrightDzenSession:
        """Open a visible persistent browser without attempting authentication."""

        if headless:
            raise ValueError("Dzen session разрешён только в видимом headful браузере.")
        if timeout_ms < 1:
            raise ValueError("Dzen Playwright timeout должен быть положительным.")
        try:
            api = importlib.import_module("playwright.sync_api")
            sync_playwright = cast(Any, api.sync_playwright)
        except (ImportError, AttributeError):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail=(
                    "Playwright для Dzen не установлен; нужен ручной setup на Windows."
                ),
            ) from None
        try:
            playwright = sync_playwright().start()
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=False,
                channel=browser_channel,
            )
            pages = context.pages
            page = pages[0] if pages else context.new_page()
            page.set_default_timeout(timeout_ms)
        except Exception:
            with suppress(Exception):
                playwright.stop()
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail=(
                    "Не удалось открыть видимый Dzen browser profile; проверьте Windows setup."
                ),
            ) from None
        return cls(page=page, context=context, playwright=playwright, timeout_ms=timeout_ms)

    def close(self) -> None:
        """Close only browser resources; never delete the persistent profile."""

        try:
            self._context.close()
        finally:
            self._playwright.stop()

    def goto(self, url: str) -> None:
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        except Exception:
            raise ProviderOperationError(
                code="PROVIDER_TIMEOUT",
                sanitized_detail="Dzen browser не открыл страницу в отведённое время.",
            ) from None

    def is_visible(self, selector: str) -> bool:
        try:
            return bool(self._page.locator(selector).is_visible(timeout=self._timeout_ms))
        except Exception:
            # The page object classifies a missing supported selector as the
            # explicit DZEN_DOM_MISMATCH terminal error.
            return False

    def text_content(self, selector: str) -> str | None:
        try:
            return cast(
                str | None,
                self._page.locator(selector).text_content(timeout=self._timeout_ms),
            )
        except Exception:
            raise DzenDomMismatchError() from None

    def get_attribute(self, selector: str, name: str) -> str | None:
        try:
            return cast(
                str | None,
                self._page.locator(selector).get_attribute(name, timeout=self._timeout_ms),
            )
        except Exception:
            raise DzenDomMismatchError() from None

    def fill(self, selector: str, value: str) -> None:
        try:
            self._page.locator(selector).fill(value, timeout=self._timeout_ms)
        except Exception:
            raise DzenDomMismatchError() from None

    def click(self, selector: str) -> None:
        try:
            self._page.locator(selector).click(timeout=self._timeout_ms)
        except Exception:
            raise DzenDomMismatchError() from None

    def set_input_files(self, selector: str, paths: list[Path]) -> None:
        try:
            self._page.locator(selector).set_input_files(
                [str(path) for path in paths], timeout=self._timeout_ms
            )
        except Exception:
            raise DzenDomMismatchError() from None

    def screenshot(self, path: Path) -> None:
        """Write a local failure diagnostic without returning browser data to logs."""

        try:
            self._page.screenshot(path=str(path), full_page=False)
        except Exception:
            # Screenshot evidence is best-effort and must not supersede the
            # original classified provider failure.
            return
