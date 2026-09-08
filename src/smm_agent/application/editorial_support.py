"""Small shared rules for editorial application commands."""

import hashlib
import json
from datetime import UTC, datetime


def invalidated_gates(target: str) -> tuple[str, ...]:
    if target == "plan":
        return ("plan", "editorial", "final")
    if target in {"main_text", "visuals"}:
        return ("editorial", "final")
    if target in {"cover", "metadata", "telegram", "video", "recording"}:
        return ("final",)
    raise ValueError(f"Неизвестная цель правки: {target}")


def require_actor(actor: str, allowed: set[str]) -> None:
    if actor not in allowed:
        raise ValueError(f"Actor {actor!r} не имеет права на эту команду")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def now_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
