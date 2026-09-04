"""Durable, non-secret Dzen publication receipt bindings.

Dzen does not expose a general idempotency API for draft creation.  The local
receipt is therefore the safety boundary: an intent is written before a browser
side effect, and every observed article receipt is tied to the configured
author, canonical channel and immutable approved assets.  A restart that sees
an unfinished intent deliberately refuses to create a second article.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urlparse

from smm_agent.domain.publication.ports import ProviderOperationError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DZEN_ARTICLE_PATH = re.compile(r"^/a/[A-Za-z0-9_-]+$")
DzenReceiptState = Literal["preparing", "prepared", "armed", "public", "cancelled"]


def canonical_dzen_channel_url(value: str) -> str:
    """Return one stable representation of an explicitly configured channel."""

    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if (
        parsed.scheme != "https"
        or host not in {"dzen.ru", "www.dzen.ru"}
        or parsed.query
        or parsed.fragment
        or parsed.params
        or not parsed.path.strip("/")
    ):
        raise ValueError("Dzen channel URL должен быть каноническим HTTPS URL канала.")
    return f"https://dzen.ru/{parsed.path.strip('/')}"


def canonical_dzen_article_url(value: str) -> tuple[str, str]:
    """Validate a stable article URL and return its ID plus canonical URL."""

    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() not in {"dzen.ru", "www.dzen.ru"}
        or parsed.query
        or parsed.fragment
        or parsed.params
        or not _DZEN_ARTICLE_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("Dzen receipt должен содержать канонический URL статьи.")
    article_id = parsed.path.removeprefix("/a/")
    return article_id, f"https://dzen.ru/a/{article_id}"


@dataclass(frozen=True, slots=True)
class DzenContentFingerprint:
    """Hashes of exact local bytes approved for one Dzen article transfer."""

    article_sha256: str
    cover_sha256: str
    visual_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.article_sha256):
            raise ValueError("article_sha256 должен быть SHA-256.")
        if not _SHA256.fullmatch(self.cover_sha256):
            raise ValueError("cover_sha256 должен быть SHA-256.")
        if not self.visual_sha256s or any(
            not _SHA256.fullmatch(item) for item in self.visual_sha256s
        ):
            raise ValueError("visual_sha256s должен содержать SHA-256 каждого visual.")


@dataclass(frozen=True, slots=True)
class DzenReceipt:
    """Durable local evidence for one Dzen prepare/arm/status lifecycle.

    ``preparing`` is an intent with no remote ID.  It exists solely to prevent
    an uncertain browser-side effect from being retried as a second draft.
    """

    idempotency_key: str
    payload_sha256: str
    title: str
    channel_url: str
    author_identity: str
    content: DzenContentFingerprint
    state: DzenReceiptState
    remote_id: str | None = None
    article_url: str | None = None
    target_at_utc: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"preparing", "prepared", "armed", "public", "cancelled"}:
            raise ValueError("Dzen receipt содержит неизвестное состояние.")
        if not self.idempotency_key:
            raise ValueError("Dzen receipt должен содержать idempotency key.")
        if not _SHA256.fullmatch(self.payload_sha256):
            raise ValueError("Dzen receipt payload_sha256 должен быть SHA-256.")
        if not self.title.strip():
            raise ValueError("Dzen receipt должен содержать title.")
        object.__setattr__(self, "channel_url", canonical_dzen_channel_url(self.channel_url))
        if not self.author_identity.strip() or len(self.author_identity) > 256:
            raise ValueError("Dzen receipt должен содержать author identity.")
        if self.state == "preparing":
            if self.remote_id is not None or self.article_url is not None:
                raise ValueError("Dzen preparing intent не должен содержать remote article.")
            return
        if self.remote_id is None or self.article_url is None:
            raise ValueError("Dzen receipt должен содержать article ID и URL.")
        article_id, canonical_url = canonical_dzen_article_url(self.article_url)
        if article_id != self.remote_id or canonical_url != self.article_url:
            raise ValueError("Dzen receipt article ID и URL не совпадают.")

    @property
    def canonical_channel_url(self) -> str:
        return canonical_dzen_channel_url(self.channel_url)


class DzenReceiptStore(Protocol):
    """Persistence contract; production must inject a durable implementation."""

    def load(self, idempotency_key: str) -> DzenReceipt | None: ...

    def load_by_remote_id(self, remote_id: str) -> DzenReceipt | None: ...

    def save(self, receipt: DzenReceipt) -> None: ...


class InMemoryDzenReceiptStore:
    """Test-only store.  It intentionally has no production durability claim."""

    def __init__(self) -> None:
        self._receipts: dict[str, DzenReceipt] = {}

    def load(self, idempotency_key: str) -> DzenReceipt | None:
        return self._receipts.get(idempotency_key)

    def load_by_remote_id(self, remote_id: str) -> DzenReceipt | None:
        return next(
            (
                receipt
                for receipt in self._receipts.values()
                if receipt.remote_id == remote_id
            ),
            None,
        )

    def save(self, receipt: DzenReceipt) -> None:
        _validate_replacement(self._receipts.get(receipt.idempotency_key), receipt)
        self._receipts[receipt.idempotency_key] = receipt


class JsonDzenReceiptStore:
    """Atomic file store for the non-secret Dzen receipt contract.

    The file contains IDs, hashes and configured account binding only.  It
    never includes browser profile data, cookies, article text or screenshots.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, idempotency_key: str) -> DzenReceipt | None:
        raw = self._all().get(idempotency_key)
        return _receipt_from_json(raw) if raw is not None else None

    def load_by_remote_id(self, remote_id: str) -> DzenReceipt | None:
        return next(
            (
                receipt
                for raw in self._all().values()
                if (receipt := _receipt_from_json(raw)).remote_id == remote_id
            ),
            None,
        )

    def save(self, receipt: DzenReceipt) -> None:
        values = self._all()
        existing_raw = values.get(receipt.idempotency_key)
        existing = _receipt_from_json(existing_raw) if existing_raw is not None else None
        _validate_replacement(existing, receipt)
        values[receipt.idempotency_key] = _receipt_to_json(receipt)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self._path)

    def _all(self) -> dict[str, dict[str, object]]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Локальный Dzen receipt store повреждён.",
            ) from None
        if not isinstance(raw, dict) or any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in raw.items()
        ):
            raise ProviderOperationError(
                code="INVALID_PAYLOAD",
                sanitized_detail="Локальный Dzen receipt store имеет неверный формат.",
            )
        return {key: dict(value) for key, value in raw.items()}


def _validate_replacement(existing: DzenReceipt | None, incoming: DzenReceipt) -> None:
    """Allow state observations, never change the bound article or content."""

    if existing is None:
        return
    immutable = (
        "idempotency_key",
        "payload_sha256",
        "title",
        "channel_url",
        "author_identity",
        "content",
    )
    if any(getattr(existing, name) != getattr(incoming, name) for name in immutable):
        raise ProviderOperationError(
            code="RECEIPT_MISMATCH",
            sanitized_detail="Dzen receipt уже привязан к другому каналу или контенту.",
        )
    if existing.remote_id is not None and existing.remote_id != incoming.remote_id:
        raise ProviderOperationError(
            code="RECEIPT_MISMATCH",
            sanitized_detail="Dzen receipt уже привязан к другой статье.",
        )
    if existing.article_url is not None and existing.article_url != incoming.article_url:
        raise ProviderOperationError(
            code="RECEIPT_MISMATCH",
            sanitized_detail="Dzen receipt уже привязан к другому URL статьи.",
        )
    if existing.target_at_utc is not None and existing.target_at_utc != incoming.target_at_utc:
        raise ProviderOperationError(
            code="RECEIPT_MISMATCH",
            sanitized_detail="Dzen receipt уже привязан к другому времени публикации.",
        )


def _receipt_to_json(receipt: DzenReceipt) -> dict[str, object]:
    payload = asdict(receipt)
    content = payload["content"]
    assert isinstance(content, dict)
    content["visual_sha256s"] = list(receipt.content.visual_sha256s)
    return payload


def _receipt_from_json(value: dict[str, object]) -> DzenReceipt:
    try:
        content_raw = value["content"]
        if not isinstance(content_raw, dict):
            raise ValueError("content")
        hashes_raw = content_raw["visual_sha256s"]
        if not isinstance(hashes_raw, list) or not all(
            isinstance(item, str) for item in hashes_raw
        ):
            raise ValueError("visual_sha256s")
        state = value["state"]
        if state not in {"preparing", "prepared", "armed", "public", "cancelled"}:
            raise ValueError("state")
        return DzenReceipt(
            idempotency_key=_string(value, "idempotency_key"),
            payload_sha256=_string(value, "payload_sha256"),
            title=_string(value, "title"),
            channel_url=_string(value, "channel_url"),
            author_identity=_string(value, "author_identity"),
            content=DzenContentFingerprint(
                article_sha256=_string(content_raw, "article_sha256"),
                cover_sha256=_string(content_raw, "cover_sha256"),
                visual_sha256s=tuple(hashes_raw),
            ),
            state=cast(DzenReceiptState, state),
            remote_id=_optional_string(value, "remote_id"),
            article_url=_optional_string(value, "article_url"),
            target_at_utc=_optional_string(value, "target_at_utc"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderOperationError(
            code="INVALID_PAYLOAD",
            sanitized_detail="Локальный Dzen receipt store содержит неверный receipt.",
        ) from error


def _string(value: dict[str, object], key: str) -> str:
    result = value[key]
    if not isinstance(result, str):
        raise ValueError(key)
    return result


def _optional_string(value: dict[str, object], key: str) -> str | None:
    result = value.get(key)
    if result is not None and not isinstance(result, str):
        raise ValueError(key)
    return result
