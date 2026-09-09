"""UC-06/07 orchestration for accepting and rendering one local recording."""

import hashlib
import mimetypes
import os
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from smm_agent.adapters.files.recording_watcher import FileObservation, recording_creation_ns
from smm_agent.application.editorial_artifacts import (
    artifact_set_hash,
    prepare_file_artifact,
    register_prepared_file,
    store_artifact,
)
from smm_agent.application.editorial_support import canonical_bytes, now_utc
from smm_agent.application.release_service import IdempotencyConflict, StateConflict
from smm_agent.application.setup_service import require_accepted_media_profile
from smm_agent.contracts.cli import ReleaseResult
from smm_agent.contracts.video import AlignmentProfile
from smm_agent.domain.release.model import Release
from smm_agent.domain.video.ports import MediaTool, SpeechRecognizer
from smm_agent.domain.video.service import build_timeline, validate_recording
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7
from smm_agent.platform.video_store import VideoStore, decode_file_identity

TELEGRAM_MAX_BYTES = 49_000_000
DISK_RESERVE_BYTES = 2 * 1024**3


class RecordingSourceInvalid(ValueError):
    pass


def _verify_recording_source(path: Path, expected_sha256: str) -> None:
    try:
        matches = _hash_file(path) == expected_sha256
    except OSError as error:
        raise RecordingSourceInvalid("Принятая запись отсутствует или недоступна.") from error
    if not matches:
        raise RecordingSourceInvalid("Hash принятой записи не совпадает с Манифестом.")


def _require_disk_space(root: Path, *, source_bytes: int, multiplier: int) -> None:
    required = source_bytes * multiplier + DISK_RESERVE_BYTES
    available = shutil.disk_usage(root).free
    if available < required:
        raise ValueError(
            f"Недостаточно места для безопасной обработки: нужно {required} байт, "
            f"свободно {available}."
        )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_source(
    database: Database,
    release_id: str,
    source: Path,
    expected: FileObservation,
) -> tuple[Path, str]:
    staging = database.data_root / "releases" / release_id / "work" / "ingest"
    staging.mkdir(parents=True, exist_ok=True)
    _require_disk_space(staging, source_bytes=expected.size, multiplier=2)
    target = staging / f"{uuid7()}{source.suffix.lower()}"
    digest = hashlib.sha256()
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as input_file, target.open("xb") as output_file:
            before = os.fstat(input_file.fileno())
            identity = (
                before.st_size,
                before.st_mtime_ns,
                recording_creation_ns(before),
                before.st_dev,
                before.st_ino,
            )
            expected_identity = (
                expected.size,
                expected.mtime_ns,
                expected.ctime_ns,
                expected.device,
                expected.inode,
            )
            if identity != expected_identity:
                raise ValueError("Запись была заменена после проверки стабильности.")
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
                output_file.write(chunk)
            after = os.fstat(input_file.fileno())
            output_file.flush()
            os.fsync(output_file.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Запись изменилась во время безопасного копирования.")
        return target, digest.hexdigest()
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _active(
    database: Database, connection: sqlite3.Connection, expected_revision: int, state: str
) -> Release:
    release = database.active_release(connection)
    if release is None or release.revision != expected_revision or release.state != state:
        actual = "none" if release is None else f"{release.state}@{release.revision}"
        raise StateConflict(f"expected {state}@{expected_revision}; actual {actual}")
    return release


def _active_for_render(
    database: Database, connection: sqlite3.Connection, expected_revision: int
) -> Release:
    release = database.active_release(connection)
    allowed = release is not None and (
        release.state == "video_processing"
        or (release.state == "revision_requested" and release.revision_target == "video")
    )
    if not allowed or release is None or release.revision != expected_revision:
        actual = "none" if release is None else f"{release.state}@{release.revision}"
        raise StateConflict(f"expected renderable@{expected_revision}; actual {actual}")
    return release


def _renew_render_lease(
    database: Database, *, release_id: str, revision: int, owner_id: str
) -> None:
    now = datetime.now(UTC)
    until = now + timedelta(minutes=40)
    with database.transaction() as connection:
        renewed = VideoStore.renew_media_lease(
            connection,
            release_id=release_id,
            release_revision=revision,
            owner_id=owner_id,
            now=now.isoformat().replace("+00:00", "Z"),
            lease_until=until.isoformat().replace("+00:00", "Z"),
        )
    if not renewed:
        raise StateConflict("Lease монтажа истёк или передан другой попытке.")


def _replay(
    database: Database, connection: sqlite3.Connection, command_id: str, request_hash: str
) -> ReleaseResult | None:
    if not command_id.strip():
        raise ValueError("command_id не может быть пустым.")
    previous = database.find_command(connection, command_id)
    if previous is None:
        return None
    if previous["request_hash"] != request_hash:
        raise IdempotencyConflict(command_id)
    return ReleaseResult.model_validate_json(previous["outcome_json"])


def _result(database: Database, connection: sqlite3.Connection, release: Release) -> ReleaseResult:
    return ReleaseResult(
        release_id=release.release_id,
        revision=release.revision,
        state=release.state,
        topic=release.topic,
        next_action=release.next_action,
        pending_gate="final" if release.state == "final_pending" else None,
        target_at_utc=release.target_at_utc,
        target_timezone=release.target_timezone,
        artifacts=database.latest_artifact_paths(connection, release.release_id),
    )


def _save(
    database: Database,
    connection: sqlite3.Connection,
    *,
    command_id: str,
    command: str,
    request_hash: str,
    result: ReleaseResult,
    now: str,
) -> None:
    database.save_command(
        connection,
        command_id=command_id,
        actor="worker",
        command=command,
        request_hash=request_hash,
        outcome=result.model_dump(mode="json", by_alias=True),
        occurred_at=now,
    )


def record_video_issue(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    code: str,
    details: dict[str, object],
    action: str,
    remediation_target: str,
) -> ReleaseResult:
    if remediation_target not in {"recording", "video"}:
        raise ValueError("Неизвестная цель исправления media issue.")
    request_hash = hashlib.sha256(
        canonical_bytes(
            {
                "command": "worker.record-video-issue",
                "expected_revision": expected_revision,
                "code": code,
                "details": details,
                "action": action,
                "remediation_target": remediation_target,
            }
        )
    ).hexdigest()
    now = now_utc()
    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = database.active_release(connection)
        if release is None or release.revision != expected_revision:
            raise StateConflict("Версия Выпуска изменилась до записи media issue.")
        if release.state not in {"awaiting_recording", "video_processing", "revision_requested"}:
            raise StateConflict("Media issue не соответствует текущему этапу Выпуска.")
        store_artifact(
            database,
            connection,
            release_id=release.release_id,
            kind="video_issue",
            filename="issue.json",
            payload=canonical_bytes(
                {
                    "schema_version": "1.0",
                    "code": code,
                    "details": details,
                    "safe_action": action,
                    "remediation_target": remediation_target,
                    "at": now,
                }
            ),
            media_type="application/json",
            created_at=now,
        )
        revision = database.update_release(
            connection,
            release_id=release.release_id,
            expected_revision=release.revision,
            state="needs_attention",
            updated_at=now,
            revision_target=remediation_target,
        )
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=release.release_id,
            from_state=release.state,
            to_state="needs_attention",
            actor="worker",
            reason=f"UC-06/07 {code}",
            occurred_at=now,
        )
        database.add_event(
            connection,
            event_id=uuid7(),
            name="VideoAttentionRequired.v1",
            occurred_at=now,
            release_id=release.release_id,
            actor="worker",
            payload={
                "code": code,
                "safe_action": action,
                "remediation_target": remediation_target,
            },
        )
        updated = replace(
            release,
            state="needs_attention",
            revision=revision,
            updated_at=now,
            revision_target=remediation_target,
        )
        result = _result(database, connection, updated)
        _save(
            database,
            connection,
            command_id=command_id,
            command="worker.record-video-issue",
            request_hash=request_hash,
            result=result,
            now=now,
        )
        return result


def accept_recording(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    inbox: Path,
    source: Path,
    observation: FileObservation,
    media_tool: MediaTool,
    selected_candidate_id: str | None = None,
) -> ReleaseResult:
    inbox_root = inbox.resolve()
    if inbox.is_symlink() or not inbox_root.is_dir() or source.is_symlink():
        raise ValueError("Inbox и запись не должны быть symlink/reparse-point.")
    source_path = source.resolve()
    if not source_path.is_relative_to(inbox_root) or not source_path.is_file():
        raise ValueError("Запись должна находиться внутри настроенного inbox.")
    request_hash = hashlib.sha256(
        canonical_bytes(
            {
                "command": "worker.accept-recording",
                "expected_revision": expected_revision,
                "source": str(source_path),
                "observation": {
                    "size": observation.size,
                    "mtime_ns": observation.mtime_ns,
                    "ctime_ns": observation.ctime_ns,
                    "device": observation.device,
                    "inode": observation.inode,
                },
                "selected_candidate_id": selected_candidate_id,
            }
        )
    ).hexdigest()
    with database.connect() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release_before_copy = database.active_release(connection)
        if release_before_copy is None:
            raise StateConflict("нет активного Выпуска")
        allowed = release_before_copy.state == "awaiting_recording" or (
            release_before_copy.state == "revision_requested"
            and release_before_copy.revision_target == "recording"
        ) or (
            release_before_copy.state == "needs_attention"
            and release_before_copy.revision_target == "recording"
            and selected_candidate_id is not None
        )
        if release_before_copy.revision != expected_revision or not allowed:
            raise StateConflict("Выпуск не ожидает новую запись.")
    staged, fingerprint = _stage_source(
        database, release_before_copy.release_id, source_path, observation
    )
    prepared_source = None
    committed = False
    try:
        probe = media_tool.probe(staged)
        validate_recording(staged.with_suffix(source_path.suffix), probe)
        now = now_utc()
        prepared_source = prepare_file_artifact(
            database,
            release_id=release_before_copy.release_id,
            kind="recording_source",
            filename=source_path.name,
            source=staged,
            media_type=mimetypes.guess_type(source_path.name)[0] or "video/mp4",
            created_at=now,
        )
        prepared_source.path.chmod(prepared_source.path.stat().st_mode & ~0o222)
        with database.transaction() as connection:
            replay = _replay(database, connection, command_id, request_hash)
            if replay:
                return replay
            release = database.active_release(connection)
            if release is None or release.revision != expected_revision:
                raise StateConflict("Версия Выпуска изменилась во время приёма записи.")
            allowed = release.state == "awaiting_recording" or (
                release.state == "revision_requested" and release.revision_target == "recording"
            ) or (
                release.state == "needs_attention"
                and release.revision_target == "recording"
                and selected_candidate_id is not None
            )
            if not allowed:
                raise StateConflict("Выпуск больше не ожидает запись.")
            if release.state == "revision_requested":
                VideoStore.invalidate_recording(connection, release.release_id)
                database.invalidate_artifacts(connection, release.release_id, "recording")
            source_artifact = register_prepared_file(database, connection, prepared_source)
            VideoStore.insert_accepted_recording(
                connection,
                candidate_id=selected_candidate_id or uuid7(),
                recording_id=uuid7(),
                release_id=release.release_id,
                observed_path=str(source_path),
                size=staged.stat().st_size,
                mtime_ns=observation.mtime_ns,
                ctime_ns=observation.ctime_ns,
                device=observation.device,
                inode=observation.inode,
                fingerprint=fingerprint,
                source_artifact_id=str(source_artifact["artifact_id"]),
                duration_seconds=probe.duration_seconds,
                width=probe.width,
                height=probe.height,
                accepted_at=now,
            )
            revision = database.update_release(
                connection,
                release_id=release.release_id,
                expected_revision=release.revision,
                state="video_processing",
                updated_at=now,
            )
            database.add_transition(
                connection,
                transition_id=uuid7(),
                release_id=release.release_id,
                from_state=release.state,
                to_state="video_processing",
                actor="worker",
                reason="UC-06 stable recording accepted",
                occurred_at=now,
            )
            database.add_event(
                connection,
                event_id=uuid7(),
                name="RecordingAccepted.v1",
                occurred_at=now,
                release_id=release.release_id,
                actor="worker",
                payload={
                    "artifact_id": str(source_artifact["artifact_id"]),
                    "duration_seconds": probe.duration_seconds,
                    "fingerprint": fingerprint,
                },
            )
            updated = replace(
                release,
                state="video_processing",
                revision=revision,
                updated_at=now,
                revision_target=None,
            )
            result = _result(database, connection, updated)
            _save(
                database,
                connection,
                command_id=command_id,
                command="worker.accept-recording",
                request_hash=request_hash,
                result=result,
                now=now,
            )
            committed = True
            return result
    finally:
        staged.unlink(missing_ok=True)
        if prepared_source is not None and not committed:
            if prepared_source.path.exists():
                prepared_source.path.chmod(prepared_source.path.stat().st_mode | 0o200)
            prepared_source.path.unlink(missing_ok=True)


def select_recording_candidate(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    actor: str,
    candidate_id: str,
    inbox: Path,
    media_tool: MediaTool,
) -> ReleaseResult:
    if actor != "author":
        raise ValueError("Только Автор может выбрать запись.")
    with database.connect() as connection:
        release = database.active_release(connection)
        if (
            release is None
            or release.revision != expected_revision
            or release.state != "needs_attention"
            or release.revision_target != "recording"
        ):
            raise StateConflict("Выпуск не ожидает выбора неоднозначной записи.")
        candidate = VideoStore.candidate_for_selection(
            connection, release.release_id, candidate_id
        )
    if candidate is None:
        raise ValueError("Указанный ambiguous candidate не найден.")
    observation = FileObservation(
        size=int(candidate["size"]),
        mtime_ns=int(candidate["mtime_ns"]),
        ctime_ns=int(candidate["ctime_ns"]),
        device=decode_file_identity(candidate["device"]),
        inode=decode_file_identity(candidate["inode"]),
        unchanged_since=datetime.fromisoformat(
            str(candidate["first_seen_at"]).replace("Z", "+00:00")
        ),
    )
    return accept_recording(
        database,
        command_id=command_id,
        expected_revision=expected_revision,
        inbox=inbox,
        source=Path(str(candidate["observed_path"])),
        observation=observation,
        media_tool=media_tool,
        selected_candidate_id=candidate_id,
    )


def render_video(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    alignment_profile: AlignmentProfile,
    recognizer: SpeechRecognizer,
    media_tool: MediaTool,
) -> ReleaseResult:
    require_accepted_media_profile(database, alignment_profile)
    if (
        recognizer.runtime_id != alignment_profile.asr_runtime
        or recognizer.model_sha256 != alignment_profile.model_sha256
    ):
        raise ValueError("Offline ASR не соответствует принятому calibration profile.")
    request_hash = hashlib.sha256(
        canonical_bytes(
            {
                "command": "worker.render-video",
                "expected_revision": expected_revision,
                "alignment_profile": alignment_profile.model_dump(mode="json"),
            }
        )
    ).hexdigest()
    lease_owner = f"render:{uuid7()}"
    lease_now = datetime.now(UTC)
    lease_until = lease_now + timedelta(minutes=40)
    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = _active_for_render(database, connection, expected_revision)
        acquired = VideoStore.acquire_media_lease(
            connection,
            release_id=release.release_id,
            release_revision=release.revision,
            owner_id=lease_owner,
            now=lease_now.isoformat().replace("+00:00", "Z"),
            lease_until=lease_until.isoformat().replace("+00:00", "Z"),
        )
        if not acquired:
            raise StateConflict("Монтаж этой версии уже выполняет другой worker.")
        recording, visual_rows = VideoStore.video_inputs(connection, release.release_id)
    source = Path(str(recording["source_path"]))
    _require_disk_space(
        database.data_root,
        source_bytes=int(recording["source_size"]),
        multiplier=3,
    )
    _verify_recording_source(source, str(recording["source_sha256"]))
    visuals = [Path(str(row["path"])) for row in visual_rows]
    if not 3 <= len(visuals) <= 5:
        raise ValueError("Для монтажа требуется от трёх до пяти валидных визуалов.")
    for row, path in zip(visual_rows, visuals, strict=True):
        if _hash_file(path) != row["sha256"]:
            raise ValueError(f"Hash визуала {row['visual_id']} не совпадает с Манифестом.")

    transcript = recognizer.transcribe(source)
    alignment = build_timeline(
        anchors=[(str(row["visual_id"]), str(row["anchor_text"])) for row in visual_rows],
        transcript=transcript,
        duration_seconds=float(recording["duration_seconds"]),
        confidence_threshold=alignment_profile.confidence_threshold,
    )
    _renew_render_lease(
        database,
        release_id=release.release_id,
        revision=release.revision,
        owner_id=lease_owner,
    )
    work = (
        database.data_root
        / "releases"
        / release.release_id
        / "work"
        / f"render-r{release.revision}-{lease_owner.removeprefix('render:')}"
    )
    work.mkdir(parents=True, exist_ok=True)
    master = work / "master-1080p.mp4"
    telegram = work / "telegram-video.mp4"
    media_tool.render_master(
        source=source, visuals=visuals, timeline=alignment.timeline, output=master
    )
    _renew_render_lease(
        database,
        release_id=release.release_id,
        revision=release.revision,
        owner_id=lease_owner,
    )
    master_qc = media_tool.validate_master(
        master,
        float(recording["duration_seconds"]),
        source=source,
        visuals=visuals,
        timeline=alignment.timeline,
    )
    if not master_qc.passed:
        raise ValueError("Master не прошёл автоматический QC.")
    _renew_render_lease(
        database,
        release_id=release.release_id,
        revision=release.revision,
        owner_id=lease_owner,
    )
    media_tool.render_telegram(master=master, output=telegram, max_bytes=TELEGRAM_MAX_BYTES)
    _renew_render_lease(
        database,
        release_id=release.release_id,
        revision=release.revision,
        owner_id=lease_owner,
    )
    telegram_qc = media_tool.validate_telegram(
        telegram,
        float(recording["duration_seconds"]),
        TELEGRAM_MAX_BYTES,
        master=master,
        timeline=alignment.timeline,
    )
    if not telegram_qc.passed:
        raise ValueError("Telegram-копия не прошла автоматический QC.")
    _renew_render_lease(
        database,
        release_id=release.release_id,
        revision=release.revision,
        owner_id=lease_owner,
    )

    _verify_recording_source(source, str(recording["source_sha256"]))
    for row, path in zip(visual_rows, visuals, strict=True):
        if _hash_file(path) != row["sha256"]:
            raise ValueError(f"Визуал {row['visual_id']} изменился во время монтажа.")

    now = now_utc()
    prepared_media = {
        kind: prepare_file_artifact(
            database,
            release_id=release.release_id,
            kind=kind,
            filename=path.name,
            source=path,
            media_type="video/mp4",
            created_at=now,
        )
        for kind, path in (("master", master), ("telegram_video", telegram))
    }
    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        current = _active_for_render(database, connection, expected_revision)
        if not VideoStore.media_lease_owned(
            connection,
            release_id=current.release_id,
            release_revision=current.revision,
            owner_id=lease_owner,
            now=now,
        ):
            raise StateConflict("Lease монтажа потерян; результат отброшен.")
        records: list[dict[str, object]] = []
        json_artifacts = {
            "transcript": transcript.model_dump(mode="json"),
            "alignment": alignment.model_dump(mode="json"),
            "timeline": [item.model_dump(mode="json") for item in alignment.timeline],
            "render_plan": {
                "schema_version": "1.0",
                "alignment_profile": alignment_profile.model_dump(mode="json"),
                "alignment_profile_sha256": hashlib.sha256(
                    canonical_bytes(alignment_profile.model_dump(mode="json"))
                ).hexdigest(),
                "canvas": [1920, 1080],
                "author_panel": [0, 0, 960, 1080],
                "visual_panel": [960, 0, 960, 1080],
                "timeline": [item.model_dump(mode="json") for item in alignment.timeline],
            },
        }
        for kind, payload in json_artifacts.items():
            records.append(
                store_artifact(
                    database,
                    connection,
                    release_id=current.release_id,
                    kind=kind,
                    filename=f"{kind}.json",
                    payload=canonical_bytes(payload),
                    media_type="application/json",
                    created_at=now,
                )
            )
        media_records: dict[str, dict[str, object]] = {}
        for kind, prepared in prepared_media.items():
            media_record = register_prepared_file(database, connection, prepared)
            records.append(media_record)
            media_records[kind] = media_record
        qc_payload = {
            "schema_version": "1.0",
            "master": master_qc.model_dump(mode="json"),
            "telegram": telegram_qc.model_dump(mode="json"),
        }
        qc_record = store_artifact(
                database,
                connection,
                release_id=current.release_id,
                kind="video_qc",
                filename="qc.json",
                payload=canonical_bytes(qc_payload),
                media_type="application/json",
                created_at=now,
            )
        records.append(qc_record)
        package_records = database.latest_artifact_records(connection, current.release_id)
        package_pairs = [
            {
                "artifact_id": record["artifact_id"],
                "kind": record["kind"],
                "sha256": record["sha256"],
            }
            for record in package_records
        ]
        final_package = store_artifact(
            database,
            connection,
            release_id=current.release_id,
            kind="final_package",
            filename="manifest.json",
            payload=canonical_bytes(
                {"schema_version": "1.0", "artifacts": package_pairs}
            ),
            media_type="application/json",
            created_at=now,
        )
        package_records = database.latest_artifact_records(connection, current.release_id)
        artifact_hash = artifact_set_hash(package_records)
        revision = database.update_release(
            connection,
            release_id=current.release_id,
            expected_revision=current.revision,
            state="final_pending",
            updated_at=now,
        )
        database.insert_approval(
            connection,
            approval_id=uuid7(),
            release_id=current.release_id,
            gate="final",
            release_revision=revision,
            artifact_set_hash=artifact_hash,
            artifacts=package_records,
            created_at=now,
        )
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=current.release_id,
            from_state=current.state,
            to_state="final_pending",
            actor="worker",
            reason="UC-07 render and QC succeeded",
            occurred_at=now,
        )
        database.add_event(
            connection,
            event_id=uuid7(),
            name="VideoValidated.v1",
            occurred_at=now,
            release_id=current.release_id,
            actor="worker",
            payload={
                "artifact_set_hash": artifact_hash,
                "minimum_alignment_confidence": alignment.minimum_confidence,
                "master_id": media_records["master"]["artifact_id"],
                "telegram_copy_id": media_records["telegram_video"]["artifact_id"],
                "qc_id": qc_record["artifact_id"],
                "final_package_artifact_id": final_package["artifact_id"],
            },
        )
        updated = replace(current, state="final_pending", revision=revision, updated_at=now)
        result = _result(database, connection, updated)
        _save(
            database,
            connection,
            command_id=command_id,
            command="worker.render-video",
            request_hash=request_hash,
            result=result,
            now=now,
        )
    shutil.rmtree(work, ignore_errors=True)
    return result
