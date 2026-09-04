from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from smm_agent.adapters.publishing.dzen import DzenPublisher, dzen_request
from smm_agent.adapters.publishing.dzen_page import DzenDiagnosticPaths, DzenPage, DzenSelectors
from smm_agent.adapters.publishing.dzen_playwright import PlaywrightDzenSession
from smm_agent.adapters.publishing.dzen_receipts import (
    DzenContentFingerprint,
    DzenReceipt,
    JsonDzenReceiptStore,
)
from smm_agent.domain.publication.ports import DzenDomMismatchError, ProviderOperationError


class FakeDzenSession:
    """Synthetic browser session; it deliberately has no network capability."""

    def __init__(
        self,
        *,
        challenge: str | None = None,
        honor_schedule: bool = True,
        identity_visible: bool = True,
    ) -> None:
        self.challenge = challenge
        self.honor_schedule = honor_schedule
        self.identity_visible = identity_visible
        self.current_url: str | None = None
        self.clicks: list[str] = []
        self.fills: dict[str, str] = {}
        self.uploads: dict[str, list[Path]] = {}
        self.screenshots: list[Path] = []
        self.article_url = "https://dzen.ru/a/draft-42"
        self.title = "Контракт и деньги"
        self.state = "draft"
        self.target_at_utc: str | None = None
        self.public_at_utc: str | None = None
        self.channel_url = "https://dzen.ru/ekonomikadliavseh"
        self.author_identity = "veselkov-economy"

    def goto(self, url: str) -> None:
        self.current_url = url

    def is_visible(self, selector: str) -> bool:
        if selector == "[data-testid='dzen-captcha']":
            return self.challenge == "captcha"
        if selector == "[data-testid='dzen-mfa']":
            return self.challenge == "mfa"
        if selector == "[data-testid='dzen-editor-authenticated']":
            return self.challenge is None
        if selector == "[data-testid='dzen-session-identity']":
            return self.challenge is None and self.identity_visible
        return self.challenge is None

    def text_content(self, selector: str) -> str | None:
        if selector == "[data-testid='dzen-article-title']":
            return self.title
        return None

    def get_attribute(self, selector: str, name: str) -> str | None:
        if selector == "[data-testid='dzen-session-identity']":
            return {
                "data-channel-url": self.channel_url,
                "data-author-identity": self.author_identity,
            }.get(name)
        if selector != "[data-testid='dzen-article']":
            return None
        return {
            "data-article-url": self.article_url,
            "data-publication-state": self.state,
            "data-target-at-utc": self.target_at_utc,
            "data-public-at-utc": self.public_at_utc,
        }.get(name)

    def fill(self, selector: str, value: str) -> None:
        self.fills[selector] = value
        if selector == "[data-testid='dzen-title-input']":
            self.title = value

    def click(self, selector: str) -> None:
        self.clicks.append(selector)
        if selector == "[data-testid='dzen-schedule']" and self.honor_schedule:
            self.state = "scheduled"
            self.target_at_utc = self.fills["[data-testid='dzen-schedule-at-utc']"]
        if selector == "[data-testid='dzen-cancel-schedule']":
            self.state = "cancelled"
            self.target_at_utc = None

    def set_input_files(self, selector: str, paths: list[Path]) -> None:
        self.uploads[selector] = paths

    def screenshot(self, path: Path) -> None:
        path.write_bytes(b"synthetic-dzen-screenshot")
        self.screenshots.append(path)


def _payload(tmp_path: Path) -> dict[str, object]:
    article = tmp_path / "dzen.md"
    cover = tmp_path / "cover.png"
    visual = tmp_path / "visual.png"
    article.write_text("Текст статьи", encoding="utf-8")
    cover.write_bytes(b"cover")
    visual.write_bytes(b"visual")
    return {
        "article": {"path": str(article)},
        "cover": {"path": str(cover)},
        "visuals": [{"asset_path": str(visual)}],
        "metadata": {"title": "Контракт и деньги"},
    }


def _page(tmp_path: Path, session: FakeDzenSession | None = None) -> DzenPage:
    return DzenPage(
        session or FakeDzenSession(),
        expected_channel_url="https://dzen.ru/ekonomikadliavseh",
        expected_author_identity="veselkov-economy",
        diagnostics=DzenDiagnosticPaths(tmp_path / "diagnostics"),
    )


def _publisher(
    tmp_path: Path,
    session: FakeDzenSession | None = None,
    *,
    receipt_store: JsonDzenReceiptStore | None = None,
) -> DzenPublisher:
    return DzenPublisher(
        _page(tmp_path, session),
        receipt_store=receipt_store,
    )


def test_fixture_exposes_the_complete_semantic_selector_contract() -> None:
    draft = (Path(__file__).parents[1] / "fixtures" / "dzen" / "draft.html").read_text(
        encoding="utf-8"
    )
    scheduled = (
        Path(__file__).parents[1] / "fixtures" / "dzen" / "scheduled.html"
    ).read_text(encoding="utf-8")
    selectors = DzenSelectors()
    for selector in (
        selectors.authenticated_marker,
        selectors.session_identity,
        selectors.new_article,
        selectors.title_input,
        selectors.article_input,
        selectors.cover_input,
        selectors.visual_input,
        selectors.save_draft,
        selectors.article_root,
        selectors.article_title,
    ):
        assert selector.split("'")[1] in draft
    for selector in (
        selectors.schedule_input,
        selectors.schedule_button,
        selectors.cancel_schedule,
    ):
        assert selector.split("'")[1] in scheduled


def test_prepare_and_native_schedule_return_a_verified_stable_dzen_receipt(tmp_path: Path) -> None:
    session = FakeDzenSession()
    publisher = _publisher(tmp_path, session)
    request = dzen_request(
        release_id="release-42",
        schedule_key="2099-09-10T11:00:00Z",
        payload_sha256="a" * 64,
        payload=_payload(tmp_path),
    )

    prepared = publisher.prepare(request)
    target = datetime(2099, 9, 10, 11, tzinfo=UTC)
    armed = publisher.arm(prepared.remote_id, target_at_utc=target, operation_key="arm-42")

    assert prepared.known_url == "https://dzen.ru/a/draft-42"
    assert armed.state == "armed"
    assert armed.target_at_utc == target
    assert session.uploads["[data-testid='dzen-cover-input']"]
    assert session.uploads["[data-testid='dzen-visual-input']"]
    assert publisher.last_diagnostic_screenshot_path is not None
    assert "secret" not in publisher.last_diagnostic_screenshot_path.lower()


def test_bad_schedule_receipt_is_a_terminal_dom_mismatch(tmp_path: Path) -> None:
    session = FakeDzenSession(honor_schedule=False)
    publisher = _publisher(tmp_path, session)
    prepared = publisher.prepare(
        dzen_request(
            release_id="release-42",
            schedule_key="2099-09-10T11:00:00Z",
            payload_sha256="a" * 64,
            payload=_payload(tmp_path),
        )
    )

    with pytest.raises(DzenDomMismatchError):
        publisher.arm(
            prepared.remote_id,
            target_at_utc=datetime(2099, 9, 10, 11, tzinfo=UTC),
            operation_key="arm-42",
        )


def test_invalid_local_payload_is_classified_as_terminal(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["cover"] = {"path": str(tmp_path / "missing-cover.png")}

    with pytest.raises(ProviderOperationError) as raised:
        _publisher(tmp_path).prepare(
            dzen_request(
                release_id="release-42",
                schedule_key="2099-09-10T11:00:00Z",
                payload_sha256="a" * 64,
                payload=payload,
            )
        )

    assert raised.value.code == "INVALID_PAYLOAD"


@pytest.mark.parametrize("challenge", ("captcha", "mfa"))
def test_captcha_or_mfa_requires_headful_manual_login(
    tmp_path: Path, challenge: str
) -> None:
    session = FakeDzenSession(challenge=challenge)
    publisher = _publisher(tmp_path, session)

    with pytest.raises(ProviderOperationError) as raised:
        publisher.prepare(
            dzen_request(
                release_id="release-42",
                schedule_key="2099-09-10T11:00:00Z",
                payload_sha256="a" * 64,
                payload=_payload(tmp_path),
            )
        )

    assert raised.value.code == "PROVIDER_AUTH_REQUIRED"
    assert not session.clicks
    assert session.screenshots == [tmp_path / "diagnostics" / "dzen-failure.png"]


def test_restarted_adapter_requires_durable_binding_and_rechecks_article_identity(
    tmp_path: Path,
) -> None:
    session = FakeDzenSession()
    publisher = _publisher(tmp_path, session)
    prepared = publisher.prepare(
        dzen_request(
            release_id="release-42",
            schedule_key="2099-09-10T11:00:00Z",
            payload_sha256="a" * 64,
            payload=_payload(tmp_path),
        )
    )
    session.state = "scheduled"
    session.target_at_utc = "2099-09-10T11:00:00Z"

    snapshot = publisher.status(prepared.remote_id)

    assert snapshot.state == "armed"
    assert snapshot.payload_sha256 == "a" * 64
    session.article_url = "https://dzen.ru/a/another-article"
    with pytest.raises(DzenDomMismatchError):
        publisher.status(prepared.remote_id)


def test_wrong_live_channel_or_author_is_terminal_before_browser_mutation(tmp_path: Path) -> None:
    session = FakeDzenSession()
    session.channel_url = "https://dzen.ru/another-author"

    with pytest.raises(ProviderOperationError) as raised:
        _publisher(tmp_path, session).prepare(
            dzen_request(
                release_id="release-42",
                schedule_key="2099-09-10T11:00:00Z",
                payload_sha256="a" * 64,
                payload=_payload(tmp_path),
            )
        )

    assert raised.value.code == "RECEIPT_MISMATCH"
    assert not session.clicks


def test_missing_identity_selector_is_a_terminal_dom_mismatch(tmp_path: Path) -> None:
    session = FakeDzenSession(identity_visible=False)

    with pytest.raises(DzenDomMismatchError):
        _publisher(tmp_path, session).prepare(
            dzen_request(
                release_id="release-42",
                schedule_key="2099-09-10T11:00:00Z",
                payload_sha256="a" * 64,
                payload=_payload(tmp_path),
            )
        )

    assert not session.clicks


def test_source_content_mutation_between_retries_never_reuses_dzen_receipt(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    payload = _payload(tmp_path)
    request = dzen_request(
        release_id="release-42",
        schedule_key="2099-09-10T11:00:00Z",
        payload_sha256="a" * 64,
        payload=payload,
    )
    publisher.prepare(request)
    Path(str(payload["article"]["path"])).write_text("Изменённый текст", encoding="utf-8")  # type: ignore[index]

    with pytest.raises(ProviderOperationError) as raised:
        publisher.prepare(request)

    assert raised.value.code == "RECEIPT_MISMATCH"


def test_json_store_restores_complete_account_article_and_content_binding(tmp_path: Path) -> None:
    path = tmp_path / "state" / "dzen-receipts.json"
    store = JsonDzenReceiptStore(path)
    payload = _payload(tmp_path)
    request = dzen_request(
        release_id="release-42",
        schedule_key="2099-09-10T11:00:00Z",
        payload_sha256="a" * 64,
        payload=payload,
    )
    first = _publisher(tmp_path, receipt_store=store)
    prepared = first.prepare(request)
    second = _publisher(tmp_path, receipt_store=store)

    restored = second.status(prepared.remote_id)
    receipt = store.load(request.idempotency_key)

    assert restored.remote_id == prepared.remote_id
    assert receipt is not None
    assert receipt.channel_url == "https://dzen.ru/ekonomikadliavseh"
    assert receipt.author_identity == "veselkov-economy"
    assert receipt.title == "Контракт и деньги"
    assert receipt.article_url == "https://dzen.ru/a/draft-42"
    assert receipt.content.article_sha256
    assert receipt.content.cover_sha256
    assert len(receipt.content.visual_sha256s) == 1


def test_restart_rejects_receipt_bound_to_a_different_account(tmp_path: Path) -> None:
    receipt = DzenReceipt(
        idempotency_key="publication:release-42:dzen:2099-09-10T11:00:00Z",
        payload_sha256="a" * 64,
        title="Контракт и деньги",
        channel_url="https://dzen.ru/another-author",
        author_identity="another-author",
        content=DzenContentFingerprint("a" * 64, "b" * 64, ("c" * 64,)),
        state="prepared",
        remote_id="draft-42",
        article_url="https://dzen.ru/a/draft-42",
    )

    with pytest.raises(ProviderOperationError) as raised:
        DzenPublisher(_page(tmp_path), known_drafts=(receipt,))

    assert raised.value.code == "RECEIPT_MISMATCH"


def test_playwright_adapter_rejects_headless_and_missing_optional_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="headful"):
        PlaywrightDzenSession.open(profile_path=tmp_path / "profile", headless=True)

    import smm_agent.adapters.publishing.dzen_playwright as browser_adapter

    def missing_playwright(_: str) -> object:
        raise ImportError("not installed")

    monkeypatch.setattr(browser_adapter.importlib, "import_module", missing_playwright)
    with pytest.raises(ProviderOperationError) as raised:
        PlaywrightDzenSession.open(profile_path=tmp_path / "profile")

    assert raised.value.code == "INVALID_PAYLOAD"
