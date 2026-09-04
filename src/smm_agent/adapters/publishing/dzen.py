"""Dzen Studio publication adapter with an injected, headful browser session.

This adapter is intentionally not wired into the worker until Slice 6's live
capability check has mapped ``DzenPage`` selectors to a real Dzen account.
It nevertheless implements the provider contract now, so its identity and
receipt checks can be tested without a network call or a browser dependency.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from smm_agent.adapters.publishing.dzen_page import (
    DzenArticleReceipt,
    DzenPage,
    DzenSession,
)
from smm_agent.contracts.publication import (
    PreparedPublication,
    PublicationRequest,
    PublicationSnapshot,
    PublicationState,
)
from smm_agent.domain.publication.ports import DzenDomMismatchError, ProviderOperationError

__all__ = [
    "DzenDomMismatchError",
    "DzenKnownDraft",
    "DzenPublisher",
    "DzenSession",
    "dzen_request",
]


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
class DzenKnownDraft:
    """Durable-safe binding required to resume a Dzen receipt after restart.

    The caller restores these values from the local publication receipt.  The
    binding contains no browser storage or article body.  ``title`` is only
    available in the preparing process; later reconciliation still verifies
    article ID and canonical URL when it is absent.
    """

    remote_id: str
    article_url: str
    payload_sha256: str
    title: str | None = None


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


def _draft_input(payload: dict[str, object]) -> tuple[str, Path, Path, list[Path]]:
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
    return title.strip(), article_path, cover_path, visual_paths


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


class DzenPublisher:
    """Native-Dzen schedule adapter guarded by page-object receipts.

    It holds idempotency keys only for its process lifetime.  Durable receipt
    persistence remains the application service's responsibility.  A new
    process receives ``known_drafts`` reconstructed from those receipts before
    it may call ``status`` or perform a recovery action.
    """

    platform = "dzen"

    def __init__(
        self, page: DzenPage, *, known_drafts: Iterable[DzenKnownDraft] = ()
    ) -> None:
        self._page = page
        self._drafts_by_request_key: dict[str, DzenKnownDraft] = {}
        self._drafts_by_remote_id = {item.remote_id: item for item in known_drafts}
        self._operations: set[str] = set()
        self.last_diagnostic_screenshot_path: str | None = None

    def _remember(
        self, receipt: DzenArticleReceipt, *, payload_sha256: str
    ) -> DzenKnownDraft:
        known = DzenKnownDraft(
            remote_id=receipt.article_id,
            article_url=receipt.article_url,
            title=receipt.title,
            payload_sha256=payload_sha256,
        )
        self._drafts_by_remote_id[known.remote_id] = known
        self.last_diagnostic_screenshot_path = receipt.diagnostic_screenshot_path
        return known

    def _known(self, remote_id: str) -> DzenKnownDraft | None:
        return self._drafts_by_remote_id.get(remote_id)

    def _receipt_for(self, remote_id: str) -> tuple[DzenArticleReceipt, DzenKnownDraft]:
        known = self._known(remote_id)
        if known is None:
            raise DzenDomMismatchError()
        receipt = self._page.read_article(known.article_url)
        if receipt.article_id != known.remote_id:
            raise DzenDomMismatchError()
        if known.title is not None:
            self._page.assert_identity(
                receipt, expected_article_id=known.remote_id, expected_title=known.title
            )
        self.last_diagnostic_screenshot_path = receipt.diagnostic_screenshot_path
        return receipt, known

    def prepare(self, request: PublicationRequest) -> PreparedPublication:
        if request.platform != self.platform:
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="DzenPublisher принимает только dzen requests.",
            )
        previous = self._drafts_by_request_key.get(request.idempotency_key)
        if previous is not None:
            if previous.payload_sha256 != request.payload_sha256:
                raise ProviderOperationError(
                    code="INVALID_PAYLOAD",
                    sanitized_detail="Dzen idempotency key повторно использован с другим payload.",
                )
            return PreparedPublication(
                platform="dzen",
                state="prepared",
                remote_id=previous.remote_id,
                known_url=previous.article_url,
                payload_sha256=previous.payload_sha256,
            )
        title, article_path, cover_path, visual_paths = _draft_input(request.payload)
        receipt = self._page.prepare_draft(
            title=title,
            article_markdown=article_path.read_text(encoding="utf-8"),
            cover_path=cover_path,
            visual_paths=visual_paths,
        )
        known = self._remember(receipt, payload_sha256=request.payload_sha256)
        self._drafts_by_request_key[request.idempotency_key] = known
        snapshot = _snapshot(receipt, payload_sha256=request.payload_sha256)
        return PreparedPublication(
            platform="dzen",
            state="prepared",
            remote_id=snapshot.remote_id,
            known_url=snapshot.known_url,
            payload_sha256=snapshot.payload_sha256,
        )

    def preflight(self, remote_id: str, *, payload_sha256: str) -> None:
        receipt, known = self._receipt_for(remote_id)
        if known.payload_sha256 != payload_sha256 or receipt.state != "draft":
            raise DzenDomMismatchError()

    def arm(
        self, remote_id: str, *, target_at_utc: datetime, operation_key: str
    ) -> PublicationSnapshot:
        receipt, known = self._receipt_for(remote_id)
        target = target_at_utc.astimezone(UTC)
        if operation_key in self._operations:
            return _snapshot(receipt, payload_sha256=known.payload_sha256)
        if receipt.state == "scheduled":
            self._page.assert_scheduled_target(receipt, target_at_utc=target)
            self._operations.add(operation_key)
            return _snapshot(receipt, payload_sha256=known.payload_sha256)
        if receipt.state != "draft":
            raise DzenDomMismatchError()
        scheduled = self._page.schedule_article(known.article_url, target_at_utc=target)
        if scheduled.article_id != known.remote_id:
            raise DzenDomMismatchError()
        if known.title is not None:
            self._page.assert_identity(
                scheduled, expected_article_id=known.remote_id, expected_title=known.title
            )
        self._operations.add(operation_key)
        self.last_diagnostic_screenshot_path = scheduled.diagnostic_screenshot_path
        return _snapshot(scheduled, payload_sha256=known.payload_sha256)

    def status(
        self, remote_id: str, *, now: datetime | None = None
    ) -> PublicationSnapshot:
        del now  # Dzen's native schedule is authoritative; never infer public locally.
        receipt, known = self._receipt_for(remote_id)
        return _snapshot(receipt, payload_sha256=known.payload_sha256)

    def cancel(self, remote_id: str, *, operation_key: str) -> PublicationSnapshot:
        receipt, known = self._receipt_for(remote_id)
        if operation_key in self._operations:
            return _snapshot(receipt, payload_sha256=known.payload_sha256)
        cancelled = self._page.cancel_scheduled_article(known.article_url)
        self._operations.add(operation_key)
        self.last_diagnostic_screenshot_path = cancelled.diagnostic_screenshot_path
        return _snapshot(cancelled, payload_sha256=known.payload_sha256)

    def execute(
        self, remote_id: str, *, operation_key: str, now: datetime
    ) -> PublicationSnapshot:
        """Reconcile Dzen's native schedule without issuing a manual publish."""

        del operation_key, now
        return self.status(remote_id)
