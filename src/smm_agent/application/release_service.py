"""UC-02 and UC-13 application services."""

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime

from smm_agent.contracts.cli import ReleaseResult
from smm_agent.domain.release import Release, ReleaseConflict, start_release
from smm_agent.platform.db import Database


class IdempotencyConflict(Exception):
    """A command id was reused with different input."""


class StateConflict(Exception):
    """Stored release state does not match command expectations."""


class ReleaseNotFound(Exception):
    """No requested or active release exists."""


def create_release(
    database: Database,
    *,
    command_id: str,
    topic: str,
    actor: str,
    expected_revision: int,
) -> ReleaseResult:
    if actor not in {"author", "codex"}:
        raise ValueError("Начать выпуск может только Автор через Codex.")
    if not command_id.strip():
        raise ValueError("command_id не может быть пустым.")

    request = {
        "actor": actor,
        "command": "release.start",
        "expected_revision": expected_revision,
        "topic": " ".join(topic.split()),
    }
    request_hash = _canonical_hash(request)

    with database.transaction() as connection:
        previous = database.find_command(connection, command_id)
        if previous:
            if previous["request_hash"] != request_hash:
                raise IdempotencyConflict(command_id)
            return ReleaseResult.model_validate_json(previous["outcome_json"])

        active = database.active_release(connection)
        if expected_revision != 0:
            raise StateConflict(f"expected revision {expected_revision}; start requires 0")
        if active:
            raise ReleaseConflict(active.release_id)

        release = start_release(release_id=_uuid7(), topic=topic)
        database.insert_release(connection, release, actor=actor)
        result = _to_result(release)
        database.save_command(
            connection,
            command_id=command_id,
            actor=actor,
            command="release.start",
            request_hash=request_hash,
            outcome=result.model_dump(mode="json", by_alias=True),
            occurred_at=release.created_at,
        )
        return result


def get_release_status(database: Database, release_id: str | None = None) -> ReleaseResult:
    with database.connect() as connection:
        release = (
            database.release_by_id(connection, release_id)
            if release_id
            else database.active_release(connection)
        )
    if not release:
        raise ReleaseNotFound(release_id or "active")
    return _to_result(release)


def _to_result(release: Release) -> ReleaseResult:
    return ReleaseResult(
        release_id=release.release_id,
        revision=release.revision,
        state=release.state,
        topic=release.topic,
        next_action=release.next_action,
    )


def _canonical_hash(value: dict[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _uuid7() -> str:
    """Generate an RFC 9562 UUIDv7 without a third-party dependency."""
    timestamp_ms = int(time.time() * 1000)
    random_bits = secrets.randbits(74)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((random_bits >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= random_bits & ((1 << 62) - 1)
    hex_value = f"{value:032x}"
    return (
        f"{hex_value[:8]}-{hex_value[8:12]}-{hex_value[12:16]}-"
        f"{hex_value[16:20]}-{hex_value[20:]}"
    )


def utc_request_id() -> str:
    return f"req-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
