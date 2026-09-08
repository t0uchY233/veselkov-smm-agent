"""UC-02 and UC-13 application services."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from smm_agent.contracts.cli import ReleaseResult
from smm_agent.domain.release import Release, ReleaseConflict, start_release
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7
from smm_agent.platform.publications import PublicationStore


class IdempotencyConflict(Exception):
    """A command id was reused with different input."""


class StateConflict(Exception):
    """Stored release state does not match command expectations."""


class ReleaseNotFound(Exception):
    """No requested or active release exists."""


def next_default_target(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("now must include timezone")
    moscow = ZoneInfo("Europe/Moscow")
    local = now.astimezone(moscow)
    days_until_thursday = (3 - local.weekday()) % 7
    candidate_date = local.date() + timedelta(days=days_until_thursday)
    candidate = datetime.combine(candidate_date, time(14, 0), tzinfo=moscow)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
    request_hash = canonical_hash(request)

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

        release = start_release(release_id=uuid7(), topic=topic)
        release = replace(
            release,
            target_at_utc=next_default_target(
                datetime.fromisoformat(release.created_at.replace("Z", "+00:00"))
            ),
        )
        database.insert_release(connection, release, actor=actor)
        result = release_result(release)
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
        approval = database.pending_approval(connection, release.release_id)
        artifacts = database.latest_artifact_paths(connection, release.release_id)
        publication_states = {
            str(row["platform"]): str(row["state"])
            for row in PublicationStore.rows(connection, release.release_id)
        }
    return release_result(
        release,
        pending_gate=str(approval["gate"]) if approval else None,
        artifacts=artifacts,
        publication_states=publication_states,
    )


def release_result(
    release: Release,
    *,
    pending_gate: str | None = None,
    artifacts: dict[str, str] | None = None,
    publication_states: dict[str, str] | None = None,
) -> ReleaseResult:
    return ReleaseResult(
        release_id=release.release_id,
        revision=release.revision,
        state=release.state,
        topic=release.topic,
        next_action=release.next_action,
        pending_gate=pending_gate,
        target_at_utc=release.target_at_utc,
        target_timezone=release.target_timezone,
        publication_states=publication_states or {},
        artifacts=artifacts or {},
    )


def canonical_hash(value: dict[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def utc_request_id() -> str:
    return f"req-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
