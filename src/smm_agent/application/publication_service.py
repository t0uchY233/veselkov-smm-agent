"""UC-09/10 orchestration for preparing and arming one coordinated release."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from smm_agent.adapters.publishing.dzen import dzen_request
from smm_agent.adapters.publishing.telegram import telegram_request
from smm_agent.adapters.publishing.youtube import youtube_request
from smm_agent.application.editorial_artifacts import store_artifact, verify_artifact_records
from smm_agent.application.editorial_support import canonical_bytes, now_utc
from smm_agent.application.release_service import IdempotencyConflict, StateConflict
from smm_agent.contracts.publication import (
    Platform,
    PreparedPublication,
    PublicationResult,
    PublicationSnapshot,
    PublicationState,
    RecoveryJobPayload,
    TelegramJobPayload,
)
from smm_agent.domain.publication.ports import Publisher
from smm_agent.domain.publication.service import (
    validate_armed_snapshot,
    validate_future_target,
    validate_link_sources,
    validate_prepared_snapshot,
    validate_public_snapshot,
    validate_telegram_caption,
)
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7
from smm_agent.platform.jobs import JobClaim, JobStore
from smm_agent.platform.publications import PublicationStore


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _replay(
    database: Database, connection: sqlite3.Connection, command_id: str, request_hash: str
) -> PublicationResult | None:
    previous = database.find_command(connection, command_id)
    if previous is None:
        return None
    if previous["request_hash"] != request_hash:
        raise IdempotencyConflict(command_id)
    return PublicationResult.model_validate_json(previous["outcome_json"])


def _save(
    database: Database,
    connection: sqlite3.Connection,
    *,
    command_id: str,
    request_hash: str,
    result: PublicationResult,
    now: str,
    command: str,
) -> None:
    database.save_command(
        connection,
        command_id=command_id,
        actor="worker",
        command=command,
        request_hash=request_hash,
        outcome=result.model_dump(mode="json"),
        occurred_at=now,
    )


def _approved_artifacts(
    database: Database, connection: sqlite3.Connection, release_id: str
) -> dict[str, dict[str, object]]:
    approval = database.latest_decided_approval(
        connection, release_id=release_id, gate="final", decision="approved"
    )
    if approval is None:
        raise StateConflict("Финальный комплект не утверждён Автором.")
    records = database.approval_artifact_records(connection, str(approval["approval_id"]))
    verify_artifact_records(records)
    by_kind = {str(record["kind"]): record for record in records}
    required = {
        "master",
        "telegram_video",
        "cover",
        "dzen",
        "telegram_template",
        "editorial_manifest",
    }
    missing = sorted(required - set(by_kind))
    if missing:
        raise StateConflict(f"В финальном комплекте отсутствуют artifacts: {missing}")
    return by_kind


def _payload_hash(payload: dict[str, object]) -> str:
    return _hash(payload)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(prepared: PreparedPublication, target: datetime | None = None) -> PublicationSnapshot:
    return PublicationSnapshot(
        **prepared.model_dump(),
        target_at_utc=target,
    )


def _restore_snapshot(
    publisher: Publisher,
    row: sqlite3.Row,
    *,
    platform: Platform,
    payload_sha256: str,
    target_at_utc: datetime,
) -> PublicationSnapshot:
    snapshot = publisher.status(str(row["remote_id"]))
    if snapshot.state == "prepared":
        validate_prepared_snapshot(
            snapshot, platform=platform, payload_sha256=payload_sha256
        )
    elif snapshot.state == "armed":
        validate_armed_snapshot(
            snapshot,
            platform=platform,
            payload_sha256=payload_sha256,
            target_at_utc=target_at_utc,
        )
    else:
        raise ValueError(f"{platform}: сохранённый receipt нельзя возобновить")
    return snapshot


def _result(
    *,
    release_id: str,
    revision: int,
    state: str,
    target: str,
    snapshots: dict[Platform, PublicationSnapshot],
    caption_path: str | None,
    next_action: str,
) -> PublicationResult:
    return PublicationResult(
        release_id=release_id,
        revision=revision,
        state=state,
        target_at_utc=target,
        publications=snapshots,
        telegram_caption_path=caption_path,
        next_action=next_action,
    )


def _cancel_available(
    publishers: dict[Platform, Publisher],
    snapshots: dict[Platform, PublicationSnapshot],
    *,
    release_id: str,
    target_at_utc: datetime,
) -> dict[Platform, PublicationSnapshot]:
    cancelled: dict[Platform, PublicationSnapshot] = {}
    for platform, snapshot in snapshots.items():
        if snapshot.state == "public":
            cancelled[platform] = snapshot
            continue
        try:
            operation_key = f"cancel:{release_id}:{platform}:{target_at_utc.isoformat()}"
            publishers[platform].cancel(snapshot.remote_id, operation_key=operation_key)
            confirmed = publishers[platform].status(snapshot.remote_id)
            if confirmed.state != "cancelled":
                raise RuntimeError(f"{platform} не подтвердил отмену")
            cancelled[platform] = confirmed
        except (RuntimeError, ValueError, KeyError):
            cancelled[platform] = snapshot.model_copy(update={"state": "failed"})
    return cancelled


def _persist_cancellation_outcome(
    database: Database,
    *,
    command_id: str,
    request_hash: str,
    expected_revision: int,
    snapshots: dict[Platform, PublicationSnapshot],
    caption_path: str | None,
    reason: str,
    lease_owner_id: str | None = None,
) -> PublicationResult:
    now = now_utc()
    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = database.active_release(connection)
        if release is None or release.revision != expected_revision:
            raise StateConflict("Версия Выпуска изменилась во время отмены отложек.")
        if release.target_at_utc is None:
            raise StateConflict("У Выпуска отсутствует target.")
        for snapshot in snapshots.values():
            PublicationStore.upsert(
                connection,
                release_id=release.release_id,
                snapshot=snapshot,
                target_at_utc=release.target_at_utc,
                updated_at=now,
            )
        JobStore.cancel_release_jobs(connection, release_id=release.release_id, now=now)
        safely_cancelled = all(
            snapshot.state == "cancelled" for snapshot in snapshots.values()
        )
        destination = "delayed" if safely_cancelled else "needs_attention"
        if not safely_cancelled:
            JobStore.enqueue(
                connection,
                release_id=release.release_id,
                kind="publication_cancel_reconcile",
                due_at=now,
                retry_policy_id="provider-30s-2m-5m-15m",
                idempotency_key=f"cancel-reconcile:{release.release_id}:{expected_revision}",
                payload={
                    "schema_version": "1.0",
                    "platforms": [
                        platform
                        for platform, snapshot in snapshots.items()
                        if snapshot.state != "cancelled"
                    ],
                },
                now=now,
            )
        revision = database.update_release(
            connection,
            release_id=release.release_id,
            expected_revision=release.revision,
            state=destination,
            updated_at=now,
        )
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=release.release_id,
            from_state=release.state,
            to_state=destination,
            actor="worker",
            reason=reason,
            occurred_at=now,
        )
        database.add_event(
            connection,
            event_id=uuid7(),
            name=(
                "PublicationSchedulesCancelled.v1"
                if safely_cancelled
                else "PublicationCancellationNeedsAttention.v1"
            ),
            occurred_at=now,
            release_id=release.release_id,
            actor="worker",
            payload={
                "destination": destination,
                "states": {
                    platform: snapshot.state
                    for platform, snapshot in snapshots.items()
                },
            },
        )
        result = _result(
            release_id=release.release_id,
            revision=revision,
            state=destination,
            target=release.target_at_utc,
            snapshots=snapshots,
            caption_path=caption_path,
            next_action=(
                "Выбрать новое время публикации."
                if safely_cancelled
                else "Проверить и завершить отмену отложенных публикаций."
            ),
        )
        _save(
            database,
            connection,
            command_id=command_id,
            request_hash=request_hash,
            result=result,
            now=now,
            command="publication.cancel-after-failure",
        )
        if lease_owner_id is not None:
            PublicationStore.release_lease(
                connection, release_id=release.release_id, owner_id=lease_owner_id
            )
        return result


def _persist_receipt(
    database: Database,
    *,
    release_id: str,
    expected_revision: int,
    owner_id: str,
    snapshot: PublicationSnapshot,
    target_at_utc: datetime,
    idempotency_key: str | None = None,
    operation_key: str | None = None,
) -> None:
    at = datetime.now(UTC)
    with database.transaction() as connection:
        release = database.active_release(connection)
        if (
            release is None
            or release.release_id != release_id
            or release.revision != expected_revision
            or release.state != "publication_preparing"
            or not PublicationStore.lease_owned(
                connection,
                release_id=release_id,
                release_revision=expected_revision,
                owner_id=owner_id,
                now=at,
            )
        ):
            raise StateConflict("Потеряно право на подготовку площадок.")
        PublicationStore.upsert(
            connection,
            release_id=release_id,
            snapshot=snapshot,
            target_at_utc=target_at_utc.isoformat().replace("+00:00", "Z"),
            updated_at=at.isoformat().replace("+00:00", "Z"),
            idempotency_key=idempotency_key,
            operation_key=operation_key,
        )


def schedule_release(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    publishers: dict[Platform, Publisher],
    now: datetime | None = None,
) -> PublicationResult:
    if set(publishers) != {"youtube", "dzen", "telegram"}:
        raise ValueError("Нужны adapters YouTube, Дзен и Telegram.")
    request_hash = _hash(
        {"command": "publication.schedule", "expected_revision": expected_revision}
    )
    owner_id = uuid7()
    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = database.active_release(connection)
        if (
            release is None
            or release.revision != expected_revision
            or release.state != "publication_preparing"
            or release.target_at_utc is None
        ):
            raise StateConflict("Выпуск не готов к постановке в расписание.")
        artifacts = _approved_artifacts(database, connection, release.release_id)
        persisted_rows = {
            cast(Platform, str(row["platform"])): row
            for row in PublicationStore.rows(connection, release.release_id)
        }
        target = validate_future_target(
            datetime.fromisoformat(release.target_at_utc.replace("Z", "+00:00")),
            now or datetime.now(UTC),
        )
        manifest = json.loads(
            Path(str(artifacts["editorial_manifest"]["path"])).read_text()
        )
        if not PublicationStore.acquire_lease(
            connection,
            release_id=release.release_id,
            release_revision=release.revision,
            owner_id=owner_id,
            now=datetime.now(UTC),
        ):
            raise StateConflict("Подготовку площадок уже выполняет другой worker.")
    youtube_payload: dict[str, object] = {
        "master": artifacts["master"],
        "cover": artifacts["cover"],
        "metadata": manifest["youtube"],
    }
    dzen_payload: dict[str, object] = {
        "article": artifacts["dzen"],
        "cover": artifacts["cover"],
        "visuals": manifest["visuals"],
        "sources": manifest["sources"],
        "metadata": manifest["dzen"],
    }
    snapshots: dict[Platform, PublicationSnapshot] = {}
    caption_path: str | None = None
    try:
        youtube_hash = _payload_hash(youtube_payload)
        schedule_key = target.isoformat()
        youtube_restored = (
            _restore_snapshot(
                publishers["youtube"],
                persisted_rows["youtube"],
                platform="youtube",
                payload_sha256=youtube_hash,
                target_at_utc=target,
            )
            if "youtube" in persisted_rows
            else None
        )
        if youtube_restored is None:
            youtube_prepared = publishers["youtube"].prepare(
                youtube_request(
                    release_id=release.release_id,
                    schedule_key=schedule_key,
                    payload_sha256=youtube_hash,
                    payload=youtube_payload,
                )
            )
            validate_prepared_snapshot(
                youtube_prepared, platform="youtube", payload_sha256=youtube_hash
            )
            snapshots["youtube"] = _snapshot(youtube_prepared)
        else:
            snapshots["youtube"] = youtube_restored
            youtube_prepared = PreparedPublication(
                **youtube_restored.model_dump(
                    exclude={"target_at_utc", "public_at"}
                )
            )
        _persist_receipt(
            database,
            release_id=release.release_id,
            expected_revision=expected_revision,
            owner_id=owner_id,
            snapshot=snapshots["youtube"],
            target_at_utc=target,
        )
        dzen_hash = _payload_hash(dzen_payload)
        dzen_restored = (
            _restore_snapshot(
                publishers["dzen"],
                persisted_rows["dzen"],
                platform="dzen",
                payload_sha256=dzen_hash,
                target_at_utc=target,
            )
            if "dzen" in persisted_rows
            else None
        )
        if dzen_restored is None:
            dzen_prepared = publishers["dzen"].prepare(
                dzen_request(
                    release_id=release.release_id,
                    schedule_key=schedule_key,
                    payload_sha256=dzen_hash,
                    payload=dzen_payload,
                )
            )
            validate_prepared_snapshot(
                dzen_prepared, platform="dzen", payload_sha256=dzen_hash
            )
            snapshots["dzen"] = _snapshot(dzen_prepared)
        else:
            snapshots["dzen"] = dzen_restored
        _persist_receipt(
            database,
            release_id=release.release_id,
            expected_revision=expected_revision,
            owner_id=owner_id,
            snapshot=snapshots["dzen"],
            target_at_utc=target,
        )
        for platform in ("youtube", "dzen"):
            snapshot = snapshots[platform]
            publishers[platform].preflight(
                snapshot.remote_id, payload_sha256=snapshot.payload_sha256
            )
        validate_future_target(target, now or datetime.now(UTC))
        dzen_armed = snapshots["dzen"]
        if dzen_armed.state != "armed":
            dzen_armed = publishers["dzen"].arm(
                snapshots["dzen"].remote_id,
                target_at_utc=target,
                operation_key=f"arm:{release.release_id}:dzen:{target.isoformat()}",
            )
        validate_armed_snapshot(
            dzen_armed,
            platform="dzen",
            payload_sha256=dzen_hash,
            target_at_utc=target,
        )
        snapshots["dzen"] = dzen_armed
        _persist_receipt(
            database,
            release_id=release.release_id,
            expected_revision=expected_revision,
            owner_id=owner_id,
            snapshot=dzen_armed,
            target_at_utc=target,
        )
        youtube_url, dzen_url = validate_link_sources(
            youtube_prepared, dzen_armed, target_at_utc=target
        )
        template = Path(str(artifacts["telegram_template"]["path"])).read_text(
            encoding="utf-8"
        )
        caption = template.replace("{{youtube_url}}", youtube_url).replace(
            "{{dzen_url}}", dzen_url
        )
        validate_telegram_caption(caption)
        with database.transaction() as connection:
            caption_record = store_artifact(
                database,
                connection,
                release_id=release.release_id,
                kind="telegram_caption",
                filename="telegram-caption.md",
                payload=caption.encode("utf-8"),
                media_type="text/markdown",
                created_at=now_utc(),
            )
        caption_path = str(caption_record["path"])
        telegram_payload: dict[str, object] = {
            "video": artifacts["telegram_video"],
            "caption": caption,
            "caption_sha256": caption_record["sha256"],
            "youtube_url": youtube_url,
            "dzen_url": dzen_url,
        }
        telegram_hash = _payload_hash(telegram_payload)
        telegram_restored = (
            _restore_snapshot(
                publishers["telegram"],
                persisted_rows["telegram"],
                platform="telegram",
                payload_sha256=telegram_hash,
                target_at_utc=target,
            )
            if "telegram" in persisted_rows
            else None
        )
        if telegram_restored is None:
            telegram_prepared = publishers["telegram"].prepare(
                telegram_request(
                    release_id=release.release_id,
                    schedule_key=schedule_key,
                    payload_sha256=telegram_hash,
                    payload=telegram_payload,
                )
            )
            validate_prepared_snapshot(
                telegram_prepared,
                platform="telegram",
                payload_sha256=telegram_hash,
            )
            snapshots["telegram"] = _snapshot(telegram_prepared)
        else:
            snapshots["telegram"] = telegram_restored
            telegram_prepared = PreparedPublication(
                **telegram_restored.model_dump(
                    exclude={"target_at_utc", "public_at"}
                )
            )
        _persist_receipt(
            database,
            release_id=release.release_id,
            expected_revision=expected_revision,
            owner_id=owner_id,
            snapshot=snapshots["telegram"],
            target_at_utc=target,
        )
        publishers["telegram"].preflight(
            telegram_prepared.remote_id,
            payload_sha256=telegram_prepared.payload_sha256,
        )
        arm_platforms: tuple[Platform, ...] = ("youtube", "telegram")
        for platform in arm_platforms:
            if snapshots[platform].state == "armed":
                continue
            validate_future_target(target, now or datetime.now(UTC))
            armed = publishers[platform].arm(
                snapshots[platform].remote_id,
                target_at_utc=target,
                operation_key=(
                    f"arm:{release.release_id}:{platform}:{target.isoformat()}"
                ),
            )
            validate_armed_snapshot(
                armed,
                platform=platform,
                payload_sha256=snapshots[platform].payload_sha256,
                target_at_utc=target,
            )
            snapshots[platform] = armed
            _persist_receipt(
                database,
                release_id=release.release_id,
                expected_revision=expected_revision,
                owner_id=owner_id,
                snapshot=armed,
                target_at_utc=target,
            )
    except (RuntimeError, ValueError, KeyError, StateConflict) as error:
        with database.connect() as connection:
            still_owner = PublicationStore.lease_owned(
                connection,
                release_id=release.release_id,
                release_revision=expected_revision,
                owner_id=owner_id,
                now=datetime.now(UTC),
            )
        if not still_owner:
            raise StateConflict(
                "Подготовка остановлена после потери publication lease."
            ) from error
        cancelled = _cancel_available(
            publishers,
            snapshots,
            release_id=release.release_id,
            target_at_utc=target,
        )
        return _persist_cancellation_outcome(
            database,
            command_id=command_id,
            request_hash=request_hash,
            expected_revision=expected_revision,
            snapshots=cancelled,
            caption_path=caption_path,
            reason=f"UC-09/10 preflight failed: {type(error).__name__}",
            lease_owner_id=owner_id,
        )

    at = now_utc()
    target_text = target.isoformat().replace("+00:00", "Z")
    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        current = database.active_release(connection)
        if (
            current is None
            or current.revision != expected_revision
            or current.state != "publication_preparing"
            or not PublicationStore.lease_owned(
                connection,
                release_id=release.release_id,
                release_revision=expected_revision,
                owner_id=owner_id,
                now=datetime.now(UTC),
            )
        ):
            raise StateConflict("Версия Выпуска изменилась во время подготовки площадок.")
        for platform, snapshot in snapshots.items():
            publication_idempotency_key = (
                f"publication:{current.release_id}:{platform}:{target.isoformat()}"
            )
            operation_key = (
                f"execute:{current.release_id}:telegram:{target.isoformat()}"
                if platform == "telegram"
                else f"arm:{current.release_id}:{platform}:{target.isoformat()}"
            )
            PublicationStore.upsert(
                connection,
                release_id=current.release_id,
                snapshot=snapshot,
                target_at_utc=target_text,
                updated_at=at,
                idempotency_key=publication_idempotency_key,
                operation_key=operation_key,
            )
            database.add_event(
                connection,
                event_id=uuid7(),
                name="PublicationPrepared.v1",
                occurred_at=at,
                release_id=current.release_id,
                actor="worker",
                payload={
                    "platform": snapshot.platform,
                    "remote_id": snapshot.remote_id,
                    "known_url": snapshot.known_url,
                },
            )
        JobStore.enqueue(
            connection,
            release_id=current.release_id,
            kind="publication_preflight",
            due_at=(target - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            retry_policy_id="provider-30s-2m-5m-15m",
            idempotency_key=f"preflight:{current.release_id}:{target_text}",
            payload={"schema_version": "1.0", "target_at_utc": target_text},
            now=at,
        )
        JobStore.enqueue(
            connection,
            release_id=current.release_id,
            kind="telegram_publish_reconcile",
            due_at=target_text,
            retry_policy_id="provider-30s-2m-5m-15m",
            idempotency_key=f"telegram:{current.release_id}:{target_text}",
            payload={
                "schema_version": "1.0",
                "task_id": snapshots["telegram"].remote_id,
                "target_at_utc": target_text,
                "payload_sha256": snapshots["telegram"].payload_sha256,
                "video_path": str(artifacts["telegram_video"]["path"]),
                "video_sha256": str(artifacts["telegram_video"]["sha256"]),
                "caption_path": caption_path,
                "caption_sha256": str(caption_record["sha256"]),
            },
            now=at,
        )
        revision = database.update_release(
            connection,
            release_id=current.release_id,
            expected_revision=current.revision,
            state="scheduled",
            updated_at=at,
        )
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=current.release_id,
            from_state=current.state,
            to_state="scheduled",
            actor="worker",
            reason="UC-09/10 all platforms armed",
            occurred_at=at,
        )
        database.add_event(
            connection,
            event_id=uuid7(),
            name="PublicationScheduled.v1",
            occurred_at=at,
            release_id=current.release_id,
            actor="worker",
            payload={"target_at_utc": target_text},
        )
        result = _result(
            release_id=current.release_id,
            revision=revision,
            state="scheduled",
            target=target_text,
            snapshots=snapshots,
            caption_path=caption_path,
            next_action="Дождаться согласованного времени публикации.",
        )
        _save(
            database,
            connection,
            command_id=command_id,
            request_hash=request_hash,
            result=result,
            now=at,
            command="publication.schedule",
        )
        PublicationStore.release_lease(
            connection, release_id=current.release_id, owner_id=owner_id
        )
        return result


def execute_telegram_task(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    publishers: dict[Platform, Publisher],
    claim: JobClaim,
    now: datetime,
) -> PublicationResult:
    """Run the target-time Telegram job and durably hand partial work to recovery.

    The target-time job has exactly one authority: its fenced ``JobClaim``.  If
    any platform cannot be confirmed public, it records every known receipt,
    enqueues the single recovery job using the original Telegram task identity,
    and only then completes itself.  A crash before that transaction is safe:
    the next claim checks remote status before any repeated Telegram execute.
    """

    if claim.kind != "telegram_publish_reconcile":
        raise ValueError("Telegram target handler получил job другого вида.")
    payload = TelegramJobPayload.model_validate_json(claim.payload_json)
    request_hash = _hash(
        {
            "command": "publication.telegram.execute",
            "expected_revision": expected_revision,
            "task_id": payload.task_id,
            "target_at_utc": payload.target_at_utc.isoformat(),
        }
    )
    with database.connect() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = database.active_release(connection)
        if (
            release is None
            or release.revision != expected_revision
            or release.state != "scheduled"
            or release.target_at_utc is None
            or release.release_id != claim.release_id
        ):
            raise StateConflict("Telegram task относится к другому состоянию Выпуска.")
        publication_rows = PublicationStore.rows(connection, release.release_id)
        stored = {cast(Platform, str(item["platform"])): item for item in publication_rows}
    if set(stored) != {"youtube", "dzen", "telegram"}:
        raise StateConflict("Telegram task требует receipts всех площадок.")
    if str(stored["telegram"]["remote_id"]) != payload.task_id:
        raise StateConflict("Telegram task не совпадает с сохранённым receipt.")
    target = datetime.fromisoformat(release.target_at_utc.replace("Z", "+00:00"))
    if payload.target_at_utc.astimezone(UTC) != target or now.astimezone(UTC) < target:
        raise StateConflict("Telegram task запущена не в согласованный target.")
    video_path = Path(payload.video_path)
    caption_path = Path(payload.caption_path)
    if (
        not video_path.is_file()
        or _file_sha256(video_path) != payload.video_sha256
        or not caption_path.is_file()
        or _file_sha256(caption_path) != payload.caption_sha256
    ):
        raise StateConflict("Telegram task artifacts изменились или отсутствуют.")

    def stored_snapshot(platform: Platform) -> PublicationSnapshot:
        row = stored[platform]
        public_at = row["public_at"]
        return PublicationSnapshot(
            platform=platform,
            state=cast(PublicationState, str(row["state"])),
            remote_id=str(row["remote_id"]),
            known_url=str(row["known_url"]) if row["known_url"] is not None else None,
            payload_sha256=str(row["payload_sha256"]),
            target_at_utc=datetime.fromisoformat(str(row["target_at_utc"]).replace("Z", "+00:00")),
            public_at=(
                datetime.fromisoformat(str(public_at).replace("Z", "+00:00"))
                if public_at is not None
                else None
            ),
        )

    # Every target-time attempt performs a complete status pass before a
    # Telegram side effect.  A provider exception becomes a recovery handoff,
    # but KeyboardInterrupt/SystemExit remain crash points by design.
    snapshots: dict[Platform, PublicationSnapshot] = {}
    status_failed = False
    for platform in ("youtube", "dzen", "telegram"):
        row = stored[platform]
        try:
            snapshot = publishers[platform].status(str(row["remote_id"]), now=now)
        except Exception:
            snapshots[platform] = stored_snapshot(platform)
            status_failed = True
            continue
        if snapshot.remote_id != str(row["remote_id"]):
            raise StateConflict(f"{platform}: remote receipt не совпадает")
        if snapshot.payload_sha256 != str(row["payload_sha256"]):
            raise StateConflict(f"{platform}: payload receipt не совпадает")
        if snapshot.target_at_utc is None or snapshot.target_at_utc.astimezone(UTC) != target:
            raise StateConflict(f"{platform}: target receipt не совпадает")
        snapshots[platform] = snapshot

    telegram = snapshots["telegram"]
    if not status_failed and telegram.state != "public":
        validate_armed_snapshot(
            telegram,
            platform="telegram",
            payload_sha256=payload.payload_sha256,
            target_at_utc=target,
        )
        try:
            telegram = publishers["telegram"].execute(
                telegram.remote_id,
                operation_key=(
                    str(stored["telegram"]["operation_key"])
                    if stored["telegram"]["operation_key"] is not None
                    else f"execute:{release.release_id}:telegram:{target.isoformat()}"
                ),
                now=now,
            )
        except Exception:
            # The provider may have accepted the side effect before timing
            # out.  Do not guess: recovery will look up the original task.
            status_failed = True
        else:
            if telegram.remote_id != payload.task_id:
                raise StateConflict("Telegram execute вернул другой remote receipt.")
            snapshots["telegram"] = telegram

    if not status_failed:
        for platform, snapshot in snapshots.items():
            if snapshot.state == "public":
                validate_public_snapshot(
                    snapshot,
                    platform=platform,
                    payload_sha256=str(stored[platform]["payload_sha256"]),
                    target_at_utc=target,
                )
    destination = (
        "published"
        if not status_failed and all(snapshot.state == "public" for snapshot in snapshots.values())
        else "recovering"
    )
    at = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        current = database.active_release(connection)
        if (
            current is None
            or current.revision != expected_revision
            or current.state != "scheduled"
            or current.release_id != claim.release_id
        ):
            raise StateConflict("Версия Выпуска изменилась во время Telegram send.")
        current_stored = {
            cast(Platform, str(item["platform"])): item
            for item in PublicationStore.rows(connection, current.release_id)
        }
        for platform, snapshot in snapshots.items():
            if str(current_stored[platform]["remote_id"]) != snapshot.remote_id:
                raise StateConflict(f"{platform}: remote receipt не совпадает")
            expected_hash = str(current_stored[platform]["payload_sha256"])
            if snapshot.state == "public":
                validate_public_snapshot(
                    snapshot,
                    platform=platform,
                    payload_sha256=expected_hash,
                    target_at_utc=target,
                )
            PublicationStore.upsert(
                connection,
                release_id=current.release_id,
                snapshot=snapshot,
                target_at_utc=release.target_at_utc,
                updated_at=at,
                idempotency_key=(
                    str(current_stored[platform]["idempotency_key"])
                    if current_stored[platform]["idempotency_key"] is not None
                    else f"publication:{current.release_id}:{platform}:{target.isoformat()}"
                ),
                operation_key=(
                    str(current_stored[platform]["operation_key"])
                    if current_stored[platform]["operation_key"] is not None
                    else f"execute:{current.release_id}:telegram:{target.isoformat()}"
                    if platform == "telegram"
                    else f"arm:{current.release_id}:{platform}:{target.isoformat()}"
                ),
            )
        if destination == "recovering":
            telegram_row = current_stored["telegram"]
            publication_key = (
                str(telegram_row["idempotency_key"])
                if telegram_row["idempotency_key"] is not None
                else f"publication:{current.release_id}:telegram:{target.isoformat()}"
            )
            operation_key = (
                str(telegram_row["operation_key"])
                if telegram_row["operation_key"] is not None
                else f"execute:{current.release_id}:telegram:{target.isoformat()}"
            )
            JobStore.enqueue(
                connection,
                release_id=current.release_id,
                kind="publication_recovery",
                due_at=at,
                retry_policy_id="provider-30s-2m-5m-15m",
                idempotency_key=(
                    f"recovery:{current.release_id}:telegram:{target.isoformat()}"
                ),
                payload=RecoveryJobPayload(
                    release_id=current.release_id,
                    platform="telegram",
                    target_at_utc=target,
                    payload_sha256=str(telegram_row["payload_sha256"]),
                    publication_idempotency_key=publication_key,
                    operation_key=operation_key,
                    remote_id=str(telegram_row["remote_id"]),
                ).model_dump(mode="json"),
                now=at,
            )
        revision = (
            database.complete_release(
                connection,
                release_id=current.release_id,
                expected_revision=current.revision,
                updated_at=at,
            )
            if destination == "published"
            else database.update_release(
                connection,
                release_id=current.release_id,
                expected_revision=current.revision,
                state="recovering",
                updated_at=at,
            )
        )
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=current.release_id,
            from_state="scheduled",
            to_state=destination,
            actor="worker",
            reason="Target-time publications reconciled",
            occurred_at=at,
        )
        database.add_event(
            connection,
            event_id=uuid7(),
            name="PublicationStarted.v1",
            occurred_at=at,
            release_id=current.release_id,
            actor="worker",
            payload={"target_at_utc": release.target_at_utc, "trigger": "target-job"},
        )
        for platform, snapshot in snapshots.items():
            if snapshot.state == "public":
                database.add_event(
                    connection,
                    event_id=uuid7(),
                    name="PlatformPublished.v1",
                    occurred_at=at,
                    release_id=current.release_id,
                    actor="worker",
                    payload={
                        "platform": platform,
                        "remote_id": snapshot.remote_id,
                        "known_url": snapshot.known_url,
                        "public_at": snapshot.public_at.isoformat()
                        if snapshot.public_at
                        else at,
                    },
                )
        if not JobStore.mark_succeeded(connection, claim=claim, now=at):
            raise StateConflict("Потеряна lease Telegram job.")
        result = _result(
            release_id=current.release_id,
            revision=revision,
            state=destination,
            target=release.target_at_utc,
            snapshots=snapshots,
            caption_path=payload.caption_path,
            next_action=(
                "Выпуск опубликован на всех площадках."
                if destination == "published"
                else "Восстановить отсутствующие публикации."
            ),
        )
        _save(
            database,
            connection,
            command_id=command_id,
            request_hash=request_hash,
            result=result,
            now=at,
            command="publication.telegram.execute",
        )
        return result


def reschedule_release(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    actor: str,
    target_at: datetime,
    publishers: dict[Platform, Publisher],
    now: datetime | None = None,
) -> PublicationResult:
    if actor != "author":
        raise ValueError("Изменить target может только Автор.")
    if set(publishers) != {"youtube", "dzen", "telegram"}:
        raise ValueError("Нужны adapters YouTube, Дзен и Telegram.")
    target = validate_future_target(target_at, now or datetime.now(UTC))
    target_text = target.isoformat().replace("+00:00", "Z")
    request_hash = _hash(
        {
            "actor": actor,
            "command": "publication.reschedule",
            "expected_revision": expected_revision,
            "target_at_utc": target_text,
        }
    )
    with database.connect() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = database.active_release(connection)
        if (
            release is None
            or release.revision != expected_revision
            or release.state not in {"scheduled", "delayed"}
            or release.target_at_utc is None
        ):
            raise StateConflict("Изменить target можно только до начала публикации.")
        rows = PublicationStore.rows(connection, release.release_id)
        caption = database.latest_artifact_record(
            connection, release.release_id, "telegram_caption"
        )
    old_state = release.state
    old_target = datetime.fromisoformat(release.target_at_utc.replace("Z", "+00:00"))
    snapshots: dict[Platform, PublicationSnapshot] = {}
    for row in rows:
        platform = cast(Platform, str(row["platform"]))
        if old_state == "delayed":
            snapshot = PublicationSnapshot(
                platform=platform,
                state=cast(PublicationState, str(row["state"])),
                remote_id=str(row["remote_id"]),
                known_url=row["known_url"],
                payload_sha256=str(row["payload_sha256"]),
                target_at_utc=old_target,
            )
            if snapshot.state != "cancelled":
                raise StateConflict("Delayed Выпуск содержит неподтверждённую отмену.")
            snapshots[platform] = snapshot
            continue
        snapshot = publishers[platform].status(str(row["remote_id"]))
        if snapshot.state == "public":
            raise StateConflict("Публикация уже началась; target менять нельзя.")
        validate_armed_snapshot(
            snapshot,
            platform=platform,
            payload_sha256=str(row["payload_sha256"]),
            target_at_utc=old_target,
        )
        snapshots[platform] = snapshot
    cancelled = (
        snapshots
        if old_state == "delayed"
        else _cancel_available(
            publishers,
            snapshots,
            release_id=release.release_id,
            target_at_utc=old_target,
        )
    )
    if not all(snapshot.state == "cancelled" for snapshot in cancelled.values()):
        return _persist_cancellation_outcome(
            database,
            command_id=command_id,
            request_hash=request_hash,
            expected_revision=expected_revision,
            snapshots=cancelled,
            caption_path=str(caption["path"]) if caption else None,
            reason="UC-14 не удалось подтвердить отмену старого target",
        )
    at = now_utc()
    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        current = database.active_release(connection)
        if (
            current is None
            or current.revision != expected_revision
            or current.state != old_state
        ):
            raise StateConflict("Версия Выпуска изменилась во время переноса target.")
        JobStore.cancel_release_jobs(connection, release_id=current.release_id, now=at)
        for snapshot in cancelled.values():
            PublicationStore.upsert(
                connection,
                release_id=current.release_id,
                snapshot=snapshot,
                target_at_utc=release.target_at_utc,
                updated_at=at,
            )
        PublicationStore.delete_release(connection, current.release_id)
        revision = database.reschedule_release(
            connection,
            release_id=current.release_id,
            expected_revision=current.revision,
            target_at_utc=target_text,
            target_timezone="Europe/Moscow",
            updated_at=at,
        )
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=current.release_id,
            from_state=old_state,
            to_state="publication_preparing",
            actor=actor,
            reason="UC-14 target changed after confirmed cancellation",
            occurred_at=at,
        )
        database.add_event(
            connection,
            event_id=uuid7(),
            name="PublicationRescheduled.v1",
            occurred_at=at,
            release_id=current.release_id,
            actor=actor,
            payload={
                "old_target_at_utc": release.target_at_utc,
                "target_at_utc": target_text,
            },
        )
        result = _result(
            release_id=current.release_id,
            revision=revision,
            state="publication_preparing",
            target=target_text,
            snapshots=cancelled,
            caption_path=str(caption["path"]) if caption else None,
            next_action="Подготовить площадки для нового времени публикации.",
        )
        _save(
            database,
            connection,
            command_id=command_id,
            request_hash=request_hash,
            result=result,
            now=at,
            command="publication.reschedule",
        )
        return result


def preflight_scheduled_release(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    publishers: dict[Platform, Publisher],
    now: datetime | None = None,
) -> PublicationResult:
    request_hash = _hash(
        {"command": "publication.preflight", "expected_revision": expected_revision}
    )
    with database.connect() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = database.active_release(connection)
        if (
            release is None
            or release.revision != expected_revision
            or release.state != "scheduled"
            or release.target_at_utc is None
        ):
            raise StateConflict("Выпуск не находится в состоянии scheduled.")
        rows = PublicationStore.rows(connection, release.release_id)
        caption = database.latest_artifact_record(
            connection, release.release_id, "telegram_caption"
        )
    snapshots: dict[Platform, PublicationSnapshot] = {}
    failed: Exception | None = None
    expected_target = datetime.fromisoformat(
        release.target_at_utc.replace("Z", "+00:00")
    )
    overdue = (now or datetime.now(UTC)).astimezone(UTC) >= expected_target
    for row in rows:
        platform = cast(Platform, str(row["platform"]))
        remote_snapshot: PublicationSnapshot | None = None
        try:
            remote_snapshot = publishers[platform].status(str(row["remote_id"]))
            snapshots[platform] = remote_snapshot
            if remote_snapshot.state == "public":
                failed = RuntimeError("target уже наступил; обнаружена public площадка")
                continue
            validate_armed_snapshot(
                remote_snapshot,
                platform=platform,
                payload_sha256=str(row["payload_sha256"]),
                target_at_utc=expected_target,
            )
            if overdue:
                raise RuntimeError("T-30 job выполнен после target")
            publishers[platform].preflight(
                remote_snapshot.remote_id, payload_sha256=str(row["payload_sha256"])
            )
        except (RuntimeError, ValueError, KeyError) as error:
            failed = error
            if remote_snapshot is None:
                snapshots[platform] = PublicationSnapshot(
                    platform=platform,
                    state=cast(PublicationState, str(row["state"])),
                    remote_id=str(row["remote_id"]),
                    known_url=row["known_url"],
                    payload_sha256=str(row["payload_sha256"]),
                    target_at_utc=datetime.fromisoformat(
                        str(row["target_at_utc"]).replace("Z", "+00:00")
                    ),
                )
    if failed is not None:
        if any(snapshot.state == "public" for snapshot in snapshots.values()):
            return _persist_cancellation_outcome(
                database,
                command_id=command_id,
                request_hash=request_hash,
                expected_revision=expected_revision,
                snapshots=snapshots,
                caption_path=str(caption["path"]) if caption else None,
                reason="UC-10 обнаружена начавшаяся публикация; нужна reconciliation",
            )
        cancelled = _cancel_available(
            publishers,
            snapshots,
            release_id=release.release_id,
            target_at_utc=expected_target,
        )
        return _persist_cancellation_outcome(
            database,
            command_id=command_id,
            request_hash=request_hash,
            expected_revision=expected_revision,
            snapshots=cancelled,
            caption_path=str(caption["path"]) if caption else None,
            reason=f"UC-10 T-30 preflight failed: {type(failed).__name__}",
        )
    result = _result(
        release_id=release.release_id,
        revision=release.revision,
        state=release.state,
        target=release.target_at_utc,
        snapshots=snapshots,
        caption_path=str(caption["path"]) if caption else None,
        next_action=release.next_action,
    )
    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        current = database.active_release(connection)
        if current is None or current.revision != expected_revision:
            raise StateConflict("Версия Выпуска изменилась во время preflight.")
        _save(
            database,
            connection,
            command_id=command_id,
            request_hash=request_hash,
            result=result,
            now=now_utc(),
            command="publication.preflight",
        )
    return result
