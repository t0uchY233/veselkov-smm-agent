"""Status-first recovery of a partially public release.

This module deliberately owns no alert outbox.  A terminal outcome is returned
as structured data and, when supplied, handed to an integration-owned callback
inside the same SQLite transaction as the release/job state change.  That keeps
the recovery rule testable without coupling it to Telegram alert delivery.
"""

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from smm_agent.contracts.publication import (
    Platform,
    PublicationSnapshot,
    RecoveryError,
    RecoveryJobPayload,
    RetryDecision,
)
from smm_agent.domain.publication.ports import ProviderOperationError, Publisher
from smm_agent.domain.publication.service import retry_decision, validate_public_snapshot
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7
from smm_agent.platform.jobs import JobClaim, JobStore
from smm_agent.platform.publications import PublicationStore
from smm_agent.platform.redaction import redact_persisted_detail

RecoveryState = Literal["published", "recovering", "retry_wait", "needs_attention"]
TerminalRecoveryRecorder = Callable[[sqlite3.Connection, "RecoveryOutcome"], None]


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """The durable result of one fenced recovery attempt.

    ``requires_attention`` is the seam for the alert/incident integration.
    The caller must install ``terminal_recorder`` in production so incident and
    notification records are committed atomically with this terminal state.
    """

    release_id: str
    job_id: str
    state: RecoveryState
    missing_platforms: tuple[Platform, ...]
    platform: Platform | None = None
    error: RecoveryError | None = None
    decision: RetryDecision | None = None
    requires_attention: bool = False


class RecoveryStateConflict(RuntimeError):
    """The job no longer authorizes a recovery mutation."""


def _as_utc_text(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def classify_provider_error(exception: Exception) -> RecoveryError:
    """Classify only sanitized provider outcomes; never persist provider bodies."""

    if isinstance(exception, ProviderOperationError):
        return RecoveryError(
            code=exception.code,
            sanitized_detail=redact_persisted_detail(exception.sanitized_detail),
        )
    if isinstance(exception, TimeoutError):
        return RecoveryError(
            code="PROVIDER_TIMEOUT", sanitized_detail="Таймаут обращения к площадке."
        )
    if isinstance(exception, PermissionError):
        return RecoveryError(
            code="PROVIDER_PERMISSION_DENIED",
            sanitized_detail="Площадка отклонила действие из-за прав доступа.",
        )
    if isinstance(exception, KeyError):
        return RecoveryError(
            code="RECEIPT_MISMATCH",
            sanitized_detail="Сохранённый remote receipt не найден на площадке.",
        )
    text = str(exception).lower()
    if "429" in text:
        return RecoveryError(
            code="PROVIDER_HTTP_429",
            sanitized_detail="Площадка временно ограничила запросы (HTTP 429).",
        )
    if any(marker in text for marker in ("401", "unauthor", "auth")):
        return RecoveryError(
            code="PROVIDER_AUTH_REQUIRED",
            sanitized_detail="Для площадки требуется повторная авторизация.",
        )
    if any(marker in text for marker in ("403", "permission", "forbidden")):
        return RecoveryError(
            code="PROVIDER_PERMISSION_DENIED",
            sanitized_detail="Площадка отклонила действие из-за прав доступа.",
        )
    if any(marker in text for marker in ("500", "501", "502", "503", "504", "5xx")):
        return RecoveryError(
            code="PROVIDER_HTTP_5XX",
            sanitized_detail="Площадка временно вернула серверную ошибку.",
        )
    return RecoveryError(
        code="UNKNOWN_PROVIDER_OUTCOME",
        sanitized_detail="Площадка не подтвердила результат операции.",
    )


def _receipt_error(detail: str) -> RecoveryError:
    return RecoveryError(code="RECEIPT_MISMATCH", sanitized_detail=detail)


def _payload_error(detail: str) -> RecoveryError:
    return RecoveryError(code="INVALID_PAYLOAD", sanitized_detail=detail)


def _validate_status(
    snapshot_platform: Platform,
    snapshot_remote_id: str,
    snapshot_payload_sha256: str,
    snapshot_target_at_utc: datetime | None,
    *,
    row: sqlite3.Row,
    target_at_utc: datetime,
) -> RecoveryError | None:
    """Validate a status response without assuming it is already public."""

    platform = cast(Platform, str(row["platform"]))
    if snapshot_platform != platform:
        return _receipt_error("Площадка вернула receipt другого канала публикации.")
    persisted_remote_id = row["remote_id"]
    if persisted_remote_id is not None and snapshot_remote_id != str(persisted_remote_id):
        return _receipt_error("Площадка вернула другой remote receipt.")
    if snapshot_payload_sha256 != str(row["payload_sha256"]):
        return _receipt_error("Площадка вернула receipt с другим payload hash.")
    if snapshot_target_at_utc is None or (
        snapshot_target_at_utc.astimezone(UTC) != target_at_utc.astimezone(UTC)
    ):
        return _receipt_error("Площадка вернула receipt с другим target публикации.")
    return None


def _persist_snapshots(
    connection: sqlite3.Connection,
    *,
    release_id: str,
    snapshots: Mapping[Platform, PublicationSnapshot],
    rows: Mapping[Platform, sqlite3.Row],
    payload: RecoveryJobPayload,
    target_at_utc: str,
    updated_at: str,
) -> None:
    """Persist only receipts confirmed by status/execute in this attempt."""

    for platform, snapshot in snapshots.items():
        row = rows[platform]
        PublicationStore.upsert(
            connection,
            release_id=release_id,
            snapshot=snapshot,
            target_at_utc=target_at_utc,
            updated_at=updated_at,
            idempotency_key=(
                str(row["idempotency_key"])
                if row["idempotency_key"] is not None
                else payload.publication_idempotency_key
                if platform == payload.platform
                else None
            ),
            operation_key=(
                str(row["operation_key"])
                if row["operation_key"] is not None
                else payload.operation_key
                if platform == payload.platform
                else None
            ),
        )


def _move_to_recovering(
    database: Database,
    connection: sqlite3.Connection,
    *,
    release_id: str,
    state: str,
    revision: int,
    updated_at: str,
) -> int:
    if state == "recovering":
        return revision
    if state != "scheduled":
        raise RecoveryStateConflict("Recovery job относится к уже изменённому Выпуску.")
    next_revision = database.update_release(
        connection,
        release_id=release_id,
        expected_revision=revision,
        state="recovering",
        updated_at=updated_at,
    )
    database.add_transition(
        connection,
        transition_id=uuid7(),
        release_id=release_id,
        from_state="scheduled",
        to_state="recovering",
        actor="worker",
        reason="Partial publication requires status-first recovery",
        occurred_at=updated_at,
    )
    return next_revision


def _retry_or_terminal(
    database: Database,
    *,
    claim: JobClaim,
    payload: RecoveryJobPayload,
    rows: Mapping[Platform, sqlite3.Row],
    snapshots: Mapping[Platform, PublicationSnapshot],
    target_at_utc: str,
    now: datetime,
    error: RecoveryError,
    error_platform: Platform | None,
    missing_platforms: tuple[Platform, ...],
    terminal_recorder: TerminalRecoveryRecorder | None,
) -> RecoveryOutcome:
    """Persist confirmed status, then finish this fenced job exactly once."""

    at = _as_utc_text(now)
    with database.transaction() as connection:
        release = database.release_by_id(connection, claim.release_id)
        job = connection.execute(
            "SELECT attempts FROM jobs WHERE job_id = ?", (claim.job_id,)
        ).fetchone()
        if (
            release is None
            or not release.active
            or release.state not in {"scheduled", "recovering"}
            or release.target_at_utc != target_at_utc
            or job is None
        ):
            raise RecoveryStateConflict("Recovery job относится к уже изменённому Выпуску.")
        _persist_snapshots(
            connection,
            release_id=claim.release_id,
            snapshots=snapshots,
            rows=rows,
            payload=payload,
            target_at_utc=target_at_utc,
            updated_at=at,
        )
        current_revision = _move_to_recovering(
            database,
            connection,
            release_id=claim.release_id,
            state=release.state,
            revision=release.revision,
            updated_at=at,
        )
        decision = retry_decision(
            error,
            operation="provider",
            attempt_number=int(job["attempts"]),
        )
        if decision.disposition == "retry":
            assert decision.retry_after_seconds is not None
            due_at = _as_utc_text(now + timedelta(seconds=decision.retry_after_seconds))
            if not JobStore.mark_retry_wait(
                connection,
                claim=claim,
                error=error,
                due_at=due_at,
                now=at,
            ):
                raise RecoveryStateConflict("Recovery job потерял fenced lease.")
            return RecoveryOutcome(
                release_id=claim.release_id,
                job_id=claim.job_id,
                state="retry_wait",
                missing_platforms=missing_platforms,
                platform=error_platform,
                error=error,
                decision=decision,
            )

        if not JobStore.mark_failed(connection, claim=claim, error=error, now=at):
            raise RecoveryStateConflict("Recovery job потерял fenced lease.")
        next_revision = database.update_release(
            connection,
            release_id=claim.release_id,
            expected_revision=current_revision,
            state="needs_attention",
            updated_at=at,
        )
        _ = next_revision
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=claim.release_id,
            from_state="recovering",
            to_state="needs_attention",
            actor="worker",
            reason="Recovery retry exhausted or provider returned terminal error",
            occurred_at=at,
        )
        outcome = RecoveryOutcome(
            release_id=claim.release_id,
            job_id=claim.job_id,
            state="needs_attention",
            missing_platforms=missing_platforms,
            platform=error_platform,
            error=error,
            decision=decision,
            requires_attention=True,
        )
        database.add_event(
            connection,
            event_id=uuid7(),
            name="RecoveryExhausted.v1",
            occurred_at=at,
            release_id=claim.release_id,
            actor="worker",
            payload={
                "job_id": claim.job_id,
                "platform": error_platform,
                "error_code": error.code,
                "missing_platforms": list(missing_platforms),
            },
        )
        if terminal_recorder is not None:
            terminal_recorder(connection, outcome)
        return outcome


def reconcile_publication_recovery(
    database: Database,
    *,
    claim: JobClaim,
    publishers: Mapping[Platform, Publisher],
    now: datetime,
    terminal_recorder: TerminalRecoveryRecorder | None = None,
) -> RecoveryOutcome:
    """Reconcile receipts first and execute only the missing Telegram task.

    Native YouTube and Dzen publications are never prepared, armed or executed
    here: they have their own remote schedules.  Telegram may be executed only
    after every persisted receipt has been checked, only with the original
    remote task and operation key, and only when its status remains non-public.
    """

    if claim.kind != "publication_recovery":
        raise ValueError("Recovery handler получил job другого вида.")
    payload = RecoveryJobPayload.model_validate_json(claim.payload_json)
    if payload.release_id != claim.release_id:
        raise ValueError("Recovery payload относится к другому Выпуску.")
    if set(publishers) != {"youtube", "dzen", "telegram"}:
        raise ValueError("Для recovery нужны adapters YouTube, Дзен и Telegram.")

    target = payload.target_at_utc.astimezone(UTC)
    target_text = _as_utc_text(target)
    with database.connect() as connection:
        release = database.release_by_id(connection, claim.release_id)
        if (
            release is None
            or not release.active
            or release.state not in {"scheduled", "recovering"}
            or release.target_at_utc != target_text
        ):
            raise RecoveryStateConflict("Recovery job относится к уже изменённому Выпуску.")
        rows = {
            cast(Platform, str(row["platform"])): row
            for row in PublicationStore.rows(connection, claim.release_id)
        }

    expected_platforms: tuple[Platform, ...] = ("youtube", "dzen", "telegram")
    if set(rows) != set(expected_platforms):
        error = _payload_error("В recovery отсутствует сохранённый receipt одной из площадок.")
        return _retry_or_terminal(
            database,
            claim=claim,
            payload=payload,
            rows=rows,
            snapshots={},
            target_at_utc=target_text,
            now=now,
            error=error,
            error_platform=None,
            missing_platforms=expected_platforms,
            terminal_recorder=terminal_recorder,
        )

    snapshots: dict[Platform, PublicationSnapshot] = {}
    payload_row = rows[payload.platform]
    failures: list[tuple[Platform, RecoveryError]] = []
    if str(payload_row["payload_sha256"]) != payload.payload_sha256:
        failures.append(
            (payload.platform, _payload_error("Recovery payload содержит другой payload hash."))
        )
    if (
        payload.remote_id is not None
        and payload_row["remote_id"] is not None
        and payload.remote_id != str(payload_row["remote_id"])
    ):
        failures.append(
            (payload.platform, _payload_error("Recovery payload содержит другой remote receipt."))
        )
    if (
        payload_row["idempotency_key"] is not None
        and payload.publication_idempotency_key != str(payload_row["idempotency_key"])
    ):
        failures.append(
            (payload.platform, _payload_error("Recovery payload содержит другой idempotency key."))
        )
    if (
        payload_row["operation_key"] is not None
        and payload.operation_key != str(payload_row["operation_key"])
    ):
        failures.append(
            (payload.platform, _payload_error("Recovery payload содержит другой operation key."))
        )
    # This is deliberately a complete status pass before a single execute call.
    for platform in expected_platforms:
        row = rows[platform]
        persisted_remote_id = row["remote_id"]
        remote_id = (
            str(persisted_remote_id)
            if persisted_remote_id is not None
            else payload.remote_id
            if platform == payload.platform
            else None
        )
        if not remote_id:
            failures.append(
                (platform, _receipt_error("У сохранённого receipt нет remote identity для сверки."))
            )
            continue
        try:
            snapshot = publishers[platform].status(remote_id, now=now)
        except Exception as exception:
            failures.append((platform, classify_provider_error(exception)))
            continue
        mismatch = _validate_status(
            snapshot.platform,
            snapshot.remote_id,
            snapshot.payload_sha256,
            snapshot.target_at_utc,
            row=row,
            target_at_utc=target,
        )
        if mismatch is not None:
            failures.append((platform, mismatch))
            continue
        snapshots[platform] = snapshot

    missing = tuple(
        platform
        for platform in expected_platforms
        if platform not in snapshots or snapshots[platform].state != "public"
    )
    if failures:
        return _retry_or_terminal(
            database,
            claim=claim,
            payload=payload,
            rows=rows,
            snapshots=snapshots,
            target_at_utc=target_text,
            now=now,
            error=failures[0][1],
            error_platform=failures[0][0],
            missing_platforms=missing,
            terminal_recorder=terminal_recorder,
        )

    telegram = snapshots["telegram"]
    if payload.platform == "telegram" and telegram.state != "public":
        try:
            telegram = publishers["telegram"].execute(
                telegram.remote_id,
                operation_key=payload.operation_key,
                now=now,
            )
        except Exception as exception:
            return _retry_or_terminal(
                database,
                claim=claim,
                payload=payload,
                rows=rows,
                snapshots=snapshots,
                target_at_utc=target_text,
                now=now,
                error=classify_provider_error(exception),
                error_platform="telegram",
                missing_platforms=missing,
                terminal_recorder=terminal_recorder,
            )
        mismatch = _validate_status(
            telegram.platform,
            telegram.remote_id,
            telegram.payload_sha256,
            telegram.target_at_utc,
            row=rows["telegram"],
            target_at_utc=target,
        )
        if mismatch is not None:
            return _retry_or_terminal(
                database,
                claim=claim,
                payload=payload,
                rows=rows,
                snapshots=snapshots,
                target_at_utc=target_text,
                now=now,
                error=mismatch,
                error_platform="telegram",
                missing_platforms=missing,
                terminal_recorder=terminal_recorder,
            )
        snapshots["telegram"] = telegram

    missing = tuple(
        platform for platform in expected_platforms if snapshots[platform].state != "public"
    )
    if missing:
        return _retry_or_terminal(
            database,
            claim=claim,
            payload=payload,
            rows=rows,
            snapshots=snapshots,
            target_at_utc=target_text,
            now=now,
            error=RecoveryError(
                code="UNKNOWN_PROVIDER_OUTCOME",
                sanitized_detail="Площадка ещё не подтвердила публичное состояние.",
            ),
            error_platform=missing[0] if len(missing) == 1 else None,
            missing_platforms=missing,
            terminal_recorder=terminal_recorder,
        )

    at = _as_utc_text(now)
    with database.transaction() as connection:
        release = database.release_by_id(connection, claim.release_id)
        if (
            release is None
            or not release.active
            or release.state not in {"scheduled", "recovering"}
            or release.target_at_utc != target_text
        ):
            raise RecoveryStateConflict("Recovery job относится к уже изменённому Выпуску.")
        for platform, snapshot in snapshots.items():
            validate_public_snapshot(
                snapshot,
                platform=platform,
                payload_sha256=str(rows[platform]["payload_sha256"]),
                target_at_utc=target,
            )
        _persist_snapshots(
            connection,
            release_id=claim.release_id,
            snapshots=snapshots,
            rows=rows,
            payload=payload,
            target_at_utc=target_text,
            updated_at=at,
        )
        revision = database.complete_release(
            connection,
            release_id=claim.release_id,
            expected_revision=release.revision,
            updated_at=at,
        )
        if not JobStore.mark_succeeded(connection, claim=claim, now=at):
            raise RecoveryStateConflict("Recovery job потерял fenced lease.")
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=claim.release_id,
            from_state=release.state,
            to_state="published",
            actor="worker",
            reason="All persisted platform receipts reconciled as public",
            occurred_at=at,
        )
        database.add_event(
            connection,
            event_id=uuid7(),
            name="PublicationRecoveryCompleted.v1",
            occurred_at=at,
            release_id=claim.release_id,
            actor="worker",
            payload={"job_id": claim.job_id, "release_revision": revision},
        )
    return RecoveryOutcome(
        release_id=claim.release_id,
        job_id=claim.job_id,
        state="published",
        missing_platforms=(),
    )
