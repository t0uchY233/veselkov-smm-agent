"""Safe Dzen Studio publication adapter.

The adapter is intentionally still behind the Windows live-capability gate.
When composed, it binds all browser actions to exactly one configured Dzen
channel/author pair and a durable receipt store. It never searches for a
"similar" article or assumes a browser profile is the right account.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from smm_agent.adapters.publishing.dzen_page import (
    DzenAccountIdentity,
    DzenArticleReceipt,
    DzenPage,
    DzenSession,
)
from smm_agent.adapters.publishing.dzen_receipts import (
    DzenContentFingerprint,
    DzenReceipt,
    DzenReceiptState,
    DzenReceiptStore,
    InMemoryDzenReceiptStore,
    canonical_dzen_article_url,
)
from smm_agent.contracts.publication import (
    PreparedPublication,
    PublicationRequest,
    PublicationSnapshot,
    PublicationState,
)
from smm_agent.domain.publication.ports import DzenDomMismatchError, ProviderOperationError

__all__ = [
    "DzenContentFingerprint",
    "DzenDomMismatchError",
    "DzenKnownDraft",
    "DzenPublisher",
    "DzenReceipt",
    "DzenReceiptStore",
    "DzenSession",
    "InMemoryDzenReceiptStore",
    "dzen_request",
]

# Kept as the public recovery name used by existing callers. It now contains
# the complete durable channel/content binding rather than only article ID.
DzenKnownDraft = DzenReceipt


def dzen_request(
    *,
    release_id: str,
    schedule_key: str,
    payload_sha256: str,
    payload: dict[str, object],
) -> PublicationRequest:
    return PublicationRequest(
        release_id=release_id,
        platform="dzen",
        idempotency_key=f"publication:{release_id}:dzen:{schedule_key}",
        payload_sha256=payload_sha256,
        payload=payload,
    )


@dataclass(frozen=True, slots=True)
class DzenDraftInput:
    """Validated artifact paths and their exact byte fingerprints."""

    title: str
    article_path: Path
    cover_path: Path
    visual_paths: tuple[Path, ...]
    content: DzenContentFingerprint

    def assert_unchanged(self) -> None:
        """Check immediately before browser file/text transfer, not eventually."""

        current = DzenContentFingerprint(
            article_sha256=_file_sha256(self.article_path),
            cover_sha256=_file_sha256(self.cover_path),
            visual_sha256s=tuple(_file_sha256(path) for path in self.visual_paths),
        )
        if current != self.content:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail=(
                    "Утверждённые Dzen article, cover или visuals изменились до передачи в браузер."
                ),
            )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail=f"Dzen artifact недоступен: {path.name}.",
        ) from None
    return digest.hexdigest()


def _payload_artifact_path(payload: dict[str, object], name: str) -> Path:
    raw = payload.get(name)
    if not isinstance(raw, dict):
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail=f"Dzen payload должен содержать artifact {name}.",
        )
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail=f"Dzen artifact {name} должен содержать path.",
        )
    resolved = Path(path)
    if not resolved.is_file():
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail=f"Dzen artifact {name} недоступен: {resolved.name}.",
        )
    return resolved


def _draft_input(payload: dict[str, object]) -> DzenDraftInput:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="Dzen payload должен содержать metadata.",
        )
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="Dzen metadata должен содержать title.",
        )
    article_path = _payload_artifact_path(payload, "article")
    cover_path = _payload_artifact_path(payload, "cover")
    visuals = payload.get("visuals")
    if not isinstance(visuals, list):
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="Dzen payload должен содержать список visuals.",
        )
    visual_paths: list[Path] = []
    for visual in visuals:
        if not isinstance(visual, dict):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Каждый Dzen visual должен быть объектом.",
            )
        raw_path = visual.get("asset_path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Каждый Dzen visual должен содержать asset_path.",
            )
        path = Path(raw_path)
        if not path.is_file():
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail=f"Dzen visual недоступен: {path.name}.",
            )
        visual_paths.append(path)
    if not visual_paths:
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="Dzen payload должен содержать хотя бы один visual.",
        )
    return DzenDraftInput(
        title=title.strip(),
        article_path=article_path,
        cover_path=cover_path,
        visual_paths=tuple(visual_paths),
        content=DzenContentFingerprint(
            article_sha256=_file_sha256(article_path),
            cover_sha256=_file_sha256(cover_path),
            visual_sha256s=tuple(_file_sha256(path) for path in visual_paths),
        ),
    )


def _snapshot(receipt: DzenArticleReceipt, *, payload_sha256: str) -> PublicationSnapshot:
    if receipt.state == "draft":
        state: PublicationState = "prepared"
    elif receipt.state == "scheduled":
        state = "armed"
    elif receipt.state == "public":
        state = "public"
    else:
        state = "cancelled"
    return PublicationSnapshot(
        platform="dzen",
        state=state,
        remote_id=receipt.article_id,
        known_url=receipt.article_url,
        payload_sha256=payload_sha256,
        target_at_utc=receipt.target_at_utc,
        public_at=receipt.public_at_utc,
    )


def _receipt_state(article_state: str) -> DzenReceiptState:
    if article_state == "draft":
        return "prepared"
    if article_state == "scheduled":
        return "armed"
    if article_state == "public":
        return "public"
    return "cancelled"


class DzenPublisher:
    """Dzen adapter with identity, hash and durable-receipt checks.

    Production composition must pass ``JsonDzenReceiptStore`` (or an equivalent
    durable store). The in-memory default is preserved exclusively for tests.
    """

    platform = "dzen"

    def __init__(
        self,
        page: DzenPage,
        *,
        expected_channel_url: str | None = None,
        expected_author_identity: str | None = None,
        receipt_store: DzenReceiptStore | None = None,
        known_drafts: Iterable[DzenKnownDraft] = (),
    ) -> None:
        if (expected_channel_url is None) != (expected_author_identity is None):
            raise ValueError("Dzen publisher требует channel URL и author identity вместе.")
        supplied = (
            DzenAccountIdentity(expected_channel_url, expected_author_identity)
            if expected_channel_url is not None and expected_author_identity is not None
            else None
        )
        if supplied is not None and page.expected_identity not in {None, supplied}:
            raise ValueError("Dzen page и publisher ожидают разные учётные записи.")
        self._page = page
        self._identity = supplied or page.expected_identity
        self._receipt_store = receipt_store or InMemoryDzenReceiptStore()
        self._drafts_by_request_key: dict[str, DzenKnownDraft] = {}
        self._drafts_by_remote_id: dict[str, DzenKnownDraft] = {}
        for known in known_drafts:
            self._validate_known_receipt(known)
            self._remember(known, persist=False)
        self._operations: set[str] = set()
        self.last_diagnostic_screenshot_path: str | None = None

    def _required_identity(self) -> DzenAccountIdentity:
        if self._identity is None:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail=(
                    "Dzen publisher должен быть создан с ожидаемыми channel URL и author identity."
                ),
            )
        return self._identity

    def _validate_known_receipt(self, known: DzenKnownDraft) -> None:
        identity = self._required_identity()
        if (
            known.canonical_channel_url != identity.channel_url
            or known.author_identity != identity.author_identity
        ):
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Dzen receipt сохранён для другой учётной записи или канала.",
            )
        if known.state == "preparing":
            return
        assert known.remote_id is not None
        assert known.article_url is not None
        article_id, article_url = canonical_dzen_article_url(known.article_url)
        if article_id != known.remote_id or article_url != known.article_url:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Dzen receipt содержит несогласованные article ID и URL.",
            )

    def _remember(self, known: DzenKnownDraft, *, persist: bool = True) -> DzenKnownDraft:
        self._validate_known_receipt(known)
        if persist:
            self._receipt_store.save(known)
        self._drafts_by_request_key[known.idempotency_key] = known
        if known.remote_id is not None:
            self._drafts_by_remote_id[known.remote_id] = known
        return known

    def _known(self, remote_id: str) -> DzenKnownDraft | None:
        known = self._drafts_by_remote_id.get(remote_id)
        if known is not None:
            return known
        persisted = self._receipt_store.load_by_remote_id(remote_id)
        if persisted is not None:
            self._validate_known_receipt(persisted)
            self._remember(persisted, persist=False)
        return persisted

    def _receipt_for(self, remote_id: str) -> tuple[DzenArticleReceipt, DzenKnownDraft]:
        known = self._known(remote_id)
        if known is None or known.state == "preparing" or known.article_url is None:
            raise DzenDomMismatchError()
        receipt = self._page.read_article(known.article_url)
        identity = self._required_identity()
        if (
            receipt.article_id != known.remote_id
            or receipt.article_url != known.article_url
            or receipt.title != known.title
            or receipt.channel_url != identity.channel_url
            or receipt.author_identity != identity.author_identity
        ):
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Dzen browser не подтвердил сохранённый article receipt.",
            )
        self.last_diagnostic_screenshot_path = receipt.diagnostic_screenshot_path
        return receipt, known

    @staticmethod
    def _prepared(known: DzenKnownDraft) -> PreparedPublication:
        if known.remote_id is None or known.article_url is None:
            raise ProviderOperationError(
                code="UNKNOWN_PROVIDER_OUTCOME",
                sanitized_detail="Dzen draft intent не содержит подтверждённой статьи.",
            )
        return PreparedPublication(
            platform="dzen",
            state="prepared",
            remote_id=known.remote_id,
            known_url=known.article_url,
            payload_sha256=known.payload_sha256,
        )

    def _from_article_receipt(
        self, article: DzenArticleReceipt, known: DzenKnownDraft
    ) -> DzenKnownDraft:
        if article.article_id != known.remote_id or article.article_url != known.article_url:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Dzen вернул статью, не совпадающую с durable receipt.",
            )
        target = (
            article.target_at_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if article.target_at_utc is not None
            else None
        )
        observed = replace(known, state=_receipt_state(article.state), target_at_utc=target)
        return self._remember(observed)

    def prepare(self, request: PublicationRequest) -> PreparedPublication:
        if request.platform != self.platform:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="DzenPublisher принимает только dzen requests.",
            )
        identity = self._required_identity()
        draft = _draft_input(request.payload)
        previous = self._drafts_by_request_key.get(request.idempotency_key)
        if previous is None:
            previous = self._receipt_store.load(request.idempotency_key)
        if previous is not None:
            self._validate_known_receipt(previous)
            self._remember(previous, persist=False)
            if (
                previous.payload_sha256 != request.payload_sha256
                or previous.title != draft.title
                or previous.content != draft.content
            ):
                raise ProviderOperationError(
                    code="RECEIPT_MISMATCH",
                    sanitized_detail=(
                        "Dzen idempotency key повторно использован с другим контентом."
                    ),
                )
            if previous.state == "preparing":
                raise ProviderOperationError(
                    code="UNKNOWN_PROVIDER_OUTCOME",
                    sanitized_detail=(
                        "Dzen draft creation была прервана до подтверждённого receipt; "
                        "новая статья автоматически не создаётся."
                    ),
                )
            return self._prepared(previous)

        # Persist intent before any browser-side mutation. A later run stops for
        # reconciliation instead of creating a duplicate draft after a crash.
        intent = DzenKnownDraft(
            idempotency_key=request.idempotency_key,
            payload_sha256=request.payload_sha256,
            title=draft.title,
            channel_url=identity.channel_url,
            author_identity=identity.author_identity,
            content=draft.content,
            state="preparing",
        )
        self._remember(intent)
        draft.assert_unchanged()
        article = self._page.prepare_draft(
            title=draft.title,
            article_markdown=draft.article_path.read_text(encoding="utf-8"),
            cover_path=draft.cover_path,
            visual_paths=list(draft.visual_paths),
        )
        if (
            article.title != draft.title
            or article.channel_url != identity.channel_url
            or article.author_identity != identity.author_identity
        ):
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Dzen не подтвердил ожидаемый канал, автора или title draft.",
            )
        known = replace(
            intent,
            state="prepared",
            remote_id=article.article_id,
            article_url=article.article_url,
        )
        self._remember(known)
        self.last_diagnostic_screenshot_path = article.diagnostic_screenshot_path
        return self._prepared(known)

    def preflight(self, remote_id: str, *, payload_sha256: str) -> None:
        article, known = self._receipt_for(remote_id)
        if known.payload_sha256 != payload_sha256 or article.state != "draft":
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Dzen preflight не подтвердил исходный draft receipt.",
            )
        self._from_article_receipt(article, known)

    def arm(
        self, remote_id: str, *, target_at_utc: datetime, operation_key: str
    ) -> PublicationSnapshot:
        article, known = self._receipt_for(remote_id)
        target = target_at_utc.astimezone(UTC)
        if operation_key in self._operations:
            return _snapshot(article, payload_sha256=known.payload_sha256)
        if article.state == "scheduled":
            self._page.assert_scheduled_target(article, target_at_utc=target)
            self._operations.add(operation_key)
            persisted = self._from_article_receipt(article, known)
            return _snapshot(article, payload_sha256=persisted.payload_sha256)
        if article.state != "draft":
            raise DzenDomMismatchError()
        scheduled = self._page.schedule_article(known.article_url or "", target_at_utc=target)
        if scheduled.title != known.title:
            raise ProviderOperationError(
                code="RECEIPT_MISMATCH",
                sanitized_detail="Dzen schedule вернул другой title статьи.",
            )
        self._page.assert_scheduled_target(scheduled, target_at_utc=target)
        self._operations.add(operation_key)
        persisted = self._from_article_receipt(scheduled, known)
        self.last_diagnostic_screenshot_path = scheduled.diagnostic_screenshot_path
        return _snapshot(scheduled, payload_sha256=persisted.payload_sha256)

    def status(
        self, remote_id: str, *, now: datetime | None = None
    ) -> PublicationSnapshot:
        del now  # Dzen's native schedule is authoritative; never infer public locally.
        article, known = self._receipt_for(remote_id)
        persisted = self._from_article_receipt(article, known)
        return _snapshot(article, payload_sha256=persisted.payload_sha256)

    def cancel(self, remote_id: str, *, operation_key: str) -> PublicationSnapshot:
        article, known = self._receipt_for(remote_id)
        if operation_key in self._operations:
            return _snapshot(article, payload_sha256=known.payload_sha256)
        cancelled = self._page.cancel_scheduled_article(known.article_url or "")
        self._operations.add(operation_key)
        persisted = self._from_article_receipt(cancelled, known)
        self.last_diagnostic_screenshot_path = cancelled.diagnostic_screenshot_path
        return _snapshot(cancelled, payload_sha256=persisted.payload_sha256)

    def execute(
        self, remote_id: str, *, operation_key: str, now: datetime
    ) -> PublicationSnapshot:
        """Reconcile Dzen's native schedule without issuing a manual publish."""

        del operation_key, now
        return self.status(remote_id)
