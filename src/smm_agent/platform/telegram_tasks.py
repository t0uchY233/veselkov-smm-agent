"""SQLite persistence for Telegram's local, fenced send intent.

Telegram Bot API does not offer a publication idempotency key.  This store is
deliberately small and provider-specific: it persists enough identity to
reconcile an interrupted send, but never credentials or raw Bot API payloads.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

from smm_agent.domain.publication.ports import ProviderOperationError
from smm_agent.platform.db import Database

if TYPE_CHECKING:
    from smm_agent.adapters.publishing.telegram import TelegramTask


class TelegramTaskConflict(RuntimeError):
    """A task changed after the caller read its explicit version fence."""


class SQLiteTelegramTaskStore:
    """SQLite task store with compare-and-swap state transitions.

    ``Database.transaction`` starts ``BEGIN IMMEDIATE``; the additional
    ``version`` predicate protects callers that did their provider I/O between
    separate transactions.  A stale sender must reload/reconcile, never issue
    another side effect based on its outdated local task.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def load(self, remote_id: str) -> TelegramTask | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM telegram_tasks WHERE remote_id = ?", (remote_id,)
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    def create(self, task: TelegramTask) -> TelegramTask:
        if task.version != 0:
            raise ValueError("Новый Telegram task должен иметь version 0.")
        now = _now()
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO telegram_tasks (
                        remote_id, idempotency_key, payload_sha256, video_path,
                        video_sha256, video_size, caption, state, target_at_utc,
                        operation_key, message_id, public_at, lookup_cursor,
                        send_cursor, send_intent_at, confirmed_update_id, version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    _values(task) + (now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise TelegramTaskConflict("Telegram task already exists.") from exc
        return task

    def compare_and_swap(
        self, *, expected: TelegramTask, replacement: TelegramTask
    ) -> TelegramTask:
        if expected.remote_id != replacement.remote_id:
            raise ValueError("CAS Telegram task не может менять remote_id.")
        if replacement.version != expected.version:
            raise ValueError("CAS replacement должен нести версию прочитанного task.")
        now = _now()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE telegram_tasks
                SET idempotency_key = ?, payload_sha256 = ?, video_path = ?,
                    video_sha256 = ?, video_size = ?, caption = ?, state = ?,
                    target_at_utc = ?, operation_key = ?, message_id = ?, public_at = ?,
                    lookup_cursor = ?, send_cursor = ?, send_intent_at = ?,
                    confirmed_update_id = ?, version = version + 1, updated_at = ?
                WHERE remote_id = ? AND version = ?
                """,
                _values(replacement)[1:] + (now, expected.remote_id, expected.version),
            )
        if cursor.rowcount != 1:
            raise TelegramTaskConflict("Telegram task version fence lost.")
        return replace(replacement, version=expected.version + 1)


def _values(task: TelegramTask) -> tuple[object, ...]:
    return (
        task.remote_id,
        task.idempotency_key,
        task.payload_sha256,
        task.video_path,
        task.video_sha256,
        task.video_size,
        task.caption,
        task.state,
        task.target_at_utc,
        task.operation_key,
        task.message_id,
        task.public_at,
        task.lookup_cursor,
        task.send_cursor,
        task.send_intent_at,
        task.confirmed_update_id,
    )


def _task_from_row(row: sqlite3.Row) -> TelegramTask:
    from smm_agent.adapters.publishing.telegram import TelegramTask

    required_text = (
        "remote_id",
        "idempotency_key",
        "payload_sha256",
        "video_path",
        "video_sha256",
        "caption",
        "state",
    )
    if any(not isinstance(row[field], str) or not row[field] for field in required_text):
        _invalid_store()
    for field in ("video_size", "lookup_cursor", "version"):
        if not isinstance(row[field], int) or row[field] < 0:
            _invalid_store()
    optional_text = ("target_at_utc", "operation_key", "message_id", "public_at", "send_intent_at")
    if any(row[field] is not None and not isinstance(row[field], str) for field in optional_text):
        _invalid_store()
    optional_int = ("send_cursor", "confirmed_update_id")
    if any(
        row[field] is not None and (not isinstance(row[field], int) or row[field] < 0)
        for field in optional_int
    ):
        _invalid_store()
    task = TelegramTask(
        remote_id=row["remote_id"],
        idempotency_key=row["idempotency_key"],
        payload_sha256=row["payload_sha256"],
        video_path=row["video_path"],
        video_sha256=row["video_sha256"],
        video_size=row["video_size"],
        caption=row["caption"],
        state=row["state"],
        target_at_utc=row["target_at_utc"],
        operation_key=row["operation_key"],
        message_id=row["message_id"],
        public_at=row["public_at"],
        lookup_cursor=row["lookup_cursor"],
        send_cursor=row["send_cursor"],
        send_intent_at=row["send_intent_at"],
        confirmed_update_id=row["confirmed_update_id"],
        version=row["version"],
    )
    # Validate persisted timestamps/state using the adapter's own strict
    # snapshot conversion, without leaking a malformed database value.
    try:
        task.target()
        task.published()
    except ProviderOperationError:
        _invalid_store()
    return task


def _invalid_store() -> NoReturn:
    raise ProviderOperationError(
        code="RECEIPT_MISMATCH",
        sanitized_detail="SQLite Telegram task store содержит неверный receipt.",
    )


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
