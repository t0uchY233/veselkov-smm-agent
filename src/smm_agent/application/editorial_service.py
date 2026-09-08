"""UC-03 through UC-05: editorial imports, approvals and revisions."""

import json
import mimetypes
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from smm_agent.application.editorial_artifacts import (
    artifact_set_hash,
    load_bundle_assets,
    lock_artifact_records,
    store_artifact,
    validate_image_dimensions,
    verify_artifact_records,
)
from smm_agent.application.editorial_support import (
    canonical_bytes,
    invalidated_gates,
    now_utc,
    require_actor,
    sha256,
)
from smm_agent.application.release_service import (
    IdempotencyConflict,
    StateConflict,
    canonical_hash,
    release_result,
)
from smm_agent.contracts.cli import ReleaseResult
from smm_agent.contracts.editorial import EditorialBundle, PlanDocument
from smm_agent.domain.editorial.ports import DocxBuilder, ImageInspector
from smm_agent.domain.publication.service import validate_future_target
from smm_agent.domain.release.model import Release, ReleaseState
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7
from smm_agent.platform.video_store import VideoStore

Gate = Literal["plan", "editorial", "final"]
Decision = Literal["approved", "rejected"]


def set_publication_target(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    actor: str,
    target_at: str,
    timezone: str,
) -> ReleaseResult:
    require_actor(actor, {"author"})
    if timezone != "Europe/Moscow":
        raise ValueError("Для Выпуска поддерживается timezone Europe/Moscow.")
    try:
        parsed = datetime.fromisoformat(target_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Время публикации должно быть ISO 8601.") from error
    if parsed.tzinfo is None:
        raise ValueError("Время публикации должно содержать часовой пояс.")
    target_utc = parsed.astimezone(UTC)
    if target_utc - datetime.now(UTC) < timedelta(minutes=35):
        raise ValueError("До времени публикации должно оставаться не менее 35 минут.")
    target_value = target_utc.isoformat().replace("+00:00", "Z")
    request_hash = canonical_hash(
        {
            "actor": actor,
            "command": "release.set-target",
            "expected_revision": expected_revision,
            "target_at_utc": target_value,
            "timezone": timezone,
        }
    )
    now = now_utc()
    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = _active_for_mutation(database, connection, expected_revision)
        if release.state != "final_pending":
            raise StateConflict("Время публикации задаётся перед финальным утверждением.")
        approval = database.pending_approval(connection, release.release_id)
        if approval is None or approval["gate"] != "final":
            raise StateConflict("Нет финального комплекта для назначения времени.")
        records = database.approval_artifact_records(
            connection, str(approval["approval_id"])
        )
        verify_artifact_records(records)
        if artifact_set_hash(records) != approval["artifact_set_hash"]:
            raise StateConflict("Финальный комплект изменился до назначения времени.")
        database.invalidate_approvals(connection, release.release_id, ("final",), now)
        revision = database.set_release_target(
            connection,
            release_id=release.release_id,
            expected_revision=release.revision,
            target_at_utc=target_value,
            target_timezone=timezone,
            updated_at=now,
        )
        database.insert_approval(
            connection,
            approval_id=uuid7(),
            release_id=release.release_id,
            gate="final",
            release_revision=revision,
            artifact_set_hash=str(approval["artifact_set_hash"]),
            artifacts=records,
            created_at=now,
        )
        database.add_event(
            connection,
            event_id=uuid7(),
            name="PublicationTargetSet.v1",
            occurred_at=now,
            release_id=release.release_id,
            actor=actor,
            payload={"target_at_utc": target_value, "target_timezone": timezone},
        )
        updated = replace(
            release,
            revision=revision,
            updated_at=now,
            target_at_utc=target_value,
            target_timezone=timezone,
        )
        result = release_result(
            updated,
            pending_gate="final",
            artifacts=database.latest_artifact_paths(connection, release.release_id),
        )
        _save_result(
            database,
            connection,
            command_id,
            actor,
            "release.set-target",
            request_hash,
            result,
            now,
        )
        return result


def _validate_narrow_revision(
    database: Database,
    connection: sqlite3.Connection,
    *,
    release_id: str,
    target: str,
    bundle: EditorialBundle,
    assets: dict[str, bytes],
) -> None:
    allowed_fields = {
        "cover": {"cover_path"},
        "metadata": {"youtube", "dzen"},
        "telegram": {"telegram"},
    }
    allowed = allowed_fields.get(target)
    if allowed is None:
        return
    old_record = database.latest_artifact_record(
        connection, release_id, "editorial_manifest", valid_only=False
    )
    if old_record is None:
        raise ValueError("Предыдущий редакционный Манифест не найден.")
    old_payload = Path(str(old_record["path"])).read_bytes()
    if sha256(old_payload) != old_record["sha256"]:
        raise ValueError("Предыдущий редакционный Манифест повреждён.")
    old_bundle = json.loads(old_payload)
    new_bundle = bundle.model_dump(mode="json")
    for field in set(old_bundle) | set(new_bundle):
        if field not in allowed and old_bundle.get(field) != new_bundle.get(field):
            raise ValueError(
                f"Узкая правка {target} изменила защищённое поле {field}; "
                "запросите соответствующую редакционную правку."
            )
    for visual in bundle.visuals:
        record = database.latest_artifact_record(
            connection, release_id, f"visual_{visual.visual_id}"
        )
        if record is None or sha256(assets[visual.asset_path]) != record["sha256"]:
            raise ValueError("Узкая правка не может заменять утверждённые визуалы.")
    if target != "cover":
        cover = database.latest_artifact_record(connection, release_id, "cover")
        if cover is None or sha256(assets[bundle.cover_path]) != cover["sha256"]:
            raise ValueError("Эта правка не может заменять утверждённую обложку.")


def import_plan(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    actor: str,
    plan: PlanDocument,
) -> ReleaseResult:
    require_actor(actor, {"codex"})
    request = {
        "actor": actor,
        "command": "release.import-plan",
        "expected_revision": expected_revision,
        "plan": plan.model_dump(mode="json"),
    }
    request_hash = canonical_hash(request)
    now = now_utc()

    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = _active_for_mutation(database, connection, expected_revision)
        if release.state == "revision_requested" and release.revision_target != "plan":
            raise StateConflict("ожидается новая версия редакционного пакета")
        if release.state not in {"topic_received", "revision_requested"}:
            raise StateConflict(f"нельзя импортировать план из состояния {release.state}")

        payload = canonical_bytes(plan.model_dump(mode="json"))
        artifact = store_artifact(
            database,
            connection,
            release_id=release.release_id,
            kind="plan",
            filename="plan.json",
            payload=payload,
            media_type="application/json",
            created_at=now,
        )
        new_revision = database.update_release(
            connection,
            release_id=release.release_id,
            expected_revision=release.revision,
            state="plan_pending",
            updated_at=now,
        )
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=release.release_id,
            from_state=release.state,
            to_state="plan_pending",
            actor=actor,
            reason="UC-02/03 plan imported",
            occurred_at=now,
        )
        database.insert_approval(
            connection,
            approval_id=uuid7(),
            release_id=release.release_id,
            gate="plan",
            release_revision=new_revision,
            artifact_set_hash=artifact_set_hash([artifact]),
            artifacts=[artifact],
            created_at=now,
        )
        updated = replace(
            release,
            state="plan_pending",
            revision=new_revision,
            updated_at=now,
            revision_target=None,
        )
        result = release_result(updated, pending_gate="plan", artifacts={"plan": artifact["path"]})
        _save_result(
            database,
            connection,
            command_id,
            actor,
            "release.import-plan",
            request_hash,
            result,
            now,
        )
        return result


def import_editorial(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    actor: str,
    bundle_root: Path,
    bundle: EditorialBundle,
    docx_builder: DocxBuilder,
    image_inspector: ImageInspector,
    tone_of_voice_sha256: str,
    humanizer_skill_version: str,
) -> ReleaseResult:
    require_actor(actor, {"codex"})
    if bundle.humanizer.tone_of_voice_sha256 != tone_of_voice_sha256:
        raise ValueError("Редакционный пакет собран с другой версией tone-of-voice.md")
    if bundle.humanizer.skill_version != humanizer_skill_version:
        raise ValueError("Редакционный пакет собран с другой версией humanizer-ru")
    assets = load_bundle_assets(bundle_root, bundle)
    validate_image_dimensions(bundle, assets, image_inspector)
    request = {
        "actor": actor,
        "command": "release.import-editorial",
        "expected_revision": expected_revision,
        "bundle": bundle.model_dump(mode="json"),
        "asset_hashes": {name: sha256(payload) for name, payload in assets.items()},
    }
    request_hash = canonical_hash(request)
    now = now_utc()

    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = _active_for_mutation(database, connection, expected_revision)
        if release.state == "revision_requested" and release.revision_target == "plan":
            raise StateConflict("сначала нужна новая версия плана")
        if release.state not in {"editorial_building", "revision_requested"}:
            raise StateConflict(f"нельзя импортировать пакет из состояния {release.state}")
        needs_editorial_gate = release.state == "editorial_building" or release.revision_target in {
            "editorial",
            "main_text",
            "visuals",
        }
        if release.state == "revision_requested" and release.revision_target:
            _validate_narrow_revision(
                database,
                connection,
                release_id=release.release_id,
                target=release.revision_target,
                bundle=bundle,
                assets=assets,
            )
        new_state: ReleaseState = (
            "editorial_pending" if needs_editorial_gate else "awaiting_recording"
        )

        records: list[dict[str, Any]] = []
        paths: dict[str, str] = {}
        manifest = store_artifact(
            database,
            connection,
            release_id=release.release_id,
            kind="editorial_manifest",
            filename="manifest.json",
            payload=canonical_bytes(bundle.model_dump(mode="json")),
            media_type="application/json",
            created_at=now,
        )
        records.append(manifest)
        paths["editorial_manifest"] = manifest["path"]

        derived = {
            "main_text": ("main-text.txt", bundle.main_text),
            "teleprompter": ("teleprompter.txt", f"Здравствуйте, друзья.\n\n{bundle.main_text}"),
            "dzen": ("dzen.md", bundle.main_text),
            "telegram_template": ("telegram.md", bundle.telegram.render_template()),
        }
        for kind, (filename, text) in derived.items():
            record = store_artifact(
                database,
                connection,
                release_id=release.release_id,
                kind=kind,
                filename=filename,
                payload=text.encode("utf-8"),
                media_type="text/markdown" if filename.endswith(".md") else "text/plain",
                created_at=now,
            )
            records.append(record)
            paths[kind] = record["path"]

        cover_name = Path(bundle.cover_path).name
        cover_record = store_artifact(
            database,
            connection,
            release_id=release.release_id,
            kind="cover",
            filename=cover_name,
            payload=assets[bundle.cover_path],
            media_type=mimetypes.guess_type(cover_name)[0] or "application/octet-stream",
            created_at=now,
        )
        records.append(cover_record)
        paths["cover"] = cover_record["path"]

        database.clear_editorial_records(connection, release.release_id)
        for source in bundle.sources:
            database.insert_source(
                connection,
                release_id=release.release_id,
                source_id=source.source_id,
                url=str(source.url),
                title=source.title,
                publisher=source.publisher,
                checked_at=source.checked_at.isoformat(),
                evidence_excerpt_hash=source.evidence_excerpt_hash,
            )
        for claim in bundle.claims:
            database.insert_claim(
                connection,
                release_id=release.release_id,
                claim_id=claim.claim_id,
                exact_text=claim.exact_text,
                materiality=claim.materiality,
                status=claim.status,
                source_ids=claim.source_ids,
            )
        for position, visual in enumerate(bundle.visuals, start=1):
            visual_name = Path(visual.asset_path).name
            record = store_artifact(
                database,
                connection,
                release_id=release.release_id,
                kind=f"visual_{visual.visual_id}",
                filename=visual_name,
                payload=assets[visual.asset_path],
                media_type=mimetypes.guess_type(visual_name)[0] or "application/octet-stream",
                created_at=now,
            )
            records.append(record)
            paths[f"visual_{position}"] = record["path"]
            database.insert_visual_spec(
                connection,
                release_id=release.release_id,
                visual_id=visual.visual_id,
                position=position,
                kind=visual.kind,
                anchor_text=visual.anchor_text,
                purpose=visual.purpose,
                artifact_id=record["artifact_id"],
                claim_ids=visual.claim_ids,
            )

        docx_record = store_artifact(
            database,
            connection,
            release_id=release.release_id,
            kind="docx",
            filename="editorial-preview.docx",
            payload=docx_builder.build(bundle, assets),
            media_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            created_at=now,
        )
        records.append(docx_record)
        paths["docx"] = docx_record["path"]

        new_revision = database.update_release(
            connection,
            release_id=release.release_id,
            expected_revision=release.revision,
            state=new_state,
            updated_at=now,
        )
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=release.release_id,
            from_state=release.state,
            to_state=new_state,
            actor=actor,
            reason="UC-04 editorial package validated",
            occurred_at=now,
        )
        artifact_hash = artifact_set_hash(records)
        if needs_editorial_gate:
            database.insert_approval(
                connection,
                approval_id=uuid7(),
                release_id=release.release_id,
                gate="editorial",
                release_revision=new_revision,
                artifact_set_hash=artifact_hash,
                artifacts=records,
                created_at=now,
            )
        database.add_event(
            connection,
            event_id=uuid7(),
            name="EditorialPackageValidated.v1",
            occurred_at=now,
            release_id=release.release_id,
            actor=actor,
            payload={"artifact_set_hash": artifact_hash, "visual_count": len(bundle.visuals)},
        )
        updated = replace(
            release,
            state=new_state,
            revision=new_revision,
            updated_at=now,
            revision_target=None,
        )
        result = release_result(
            updated,
            pending_gate="editorial" if needs_editorial_gate else None,
            artifacts=paths,
        )
        _save_result(
            database,
            connection,
            command_id,
            actor,
            "release.import-editorial",
            request_hash,
            result,
            now,
        )
        return result


def decide_gate(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    actor: str,
    gate: Gate,
    decision: Decision,
    reason: str | None,
) -> ReleaseResult:
    require_actor(actor, {"author"})
    if decision == "rejected" and not (reason and reason.strip()):
        raise ValueError("Для отклонения нужна причина.")
    request = {
        "actor": actor,
        "command": "release.decide",
        "expected_revision": expected_revision,
        "gate": gate,
        "decision": decision,
        "reason": reason,
    }
    request_hash = canonical_hash(request)
    now = now_utc()
    final_records: list[dict[str, Any]] = []
    approved_records: list[dict[str, Any]] = []

    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = _active_for_mutation(database, connection, expected_revision)
        approval = database.pending_approval(connection, release.release_id)
        if not approval or approval["gate"] != gate:
            raise StateConflict(f"нет ожидающего gate {gate}")
        if int(approval["release_revision"]) != release.revision:
            raise StateConflict("gate относится к другой версии Выпуска")
        if decision == "approved":
            if gate == "final" and release.target_at_utc is None:
                raise StateConflict("До финального утверждения задайте время публикации.")
            if gate == "final" and release.target_at_utc is not None:
                validate_future_target(
                    datetime.fromisoformat(
                        release.target_at_utc.replace("Z", "+00:00")
                    ),
                    datetime.now(UTC),
                )
            final_records = database.latest_artifact_records(connection, release.release_id)
            approved_records = database.approval_artifact_records(
                connection, str(approval["approval_id"])
            )
            verify_artifact_records(approved_records)
            if artifact_set_hash(approved_records) != approval["artifact_set_hash"]:
                raise StateConflict("утверждаемый комплект изменился после открытия gate")
            final_hash_changed = (
                gate == "final"
                and artifact_set_hash(final_records) != approval["artifact_set_hash"]
            )
            if final_hash_changed:
                raise StateConflict("финальный комплект изменился после открытия gate")

        approved_states: dict[Gate, ReleaseState] = {
            "plan": "editorial_building",
            "editorial": "awaiting_recording",
            "final": "publication_preparing",
        }
        expected_states = {
            "plan": "plan_pending",
            "editorial": "editorial_pending",
            "final": "final_pending",
        }
        if release.state != expected_states[gate]:
            raise StateConflict(f"gate {gate} нельзя решить из состояния {release.state}")
        new_state: ReleaseState = (
            approved_states[gate] if decision == "approved" else "revision_requested"
        )
        revision_target = (
            "recording" if gate == "final" and decision == "rejected" else gate
        )

        database.decide_approval(
            connection,
            approval_id=str(approval["approval_id"]),
            decision=decision,
            actor=actor,
            reason=reason,
            decided_at=now,
        )
        if decision == "approved":
            lock_artifact_records(approved_records)
        if gate == "final" and decision == "rejected":
            VideoStore.invalidate_recording(connection, release.release_id)
            database.invalidate_artifacts(connection, release.release_id, "recording")
            VideoStore.reset_recording_window(connection, release.release_id, now)
        new_revision = database.update_release(
            connection,
            release_id=release.release_id,
            expected_revision=release.revision,
            state=new_state,
            updated_at=now,
            revision_target=revision_target if decision == "rejected" else None,
        )
        if gate == "editorial" and decision == "approved":
            VideoStore.open_recording_window(connection, release.release_id, now)
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=release.release_id,
            from_state=release.state,
            to_state=new_state,
            actor=actor,
            reason=reason or f"UC gate {gate} approved",
            occurred_at=now,
        )
        database.add_event(
            connection,
            event_id=uuid7(),
            name="ApprovalRecorded.v1",
            occurred_at=now,
            release_id=release.release_id,
            actor=actor,
            payload={
                "gate": gate,
                "decision": decision,
                "artifact_set_hash": str(approval["artifact_set_hash"]),
            },
        )
        updated = replace(
            release,
            state=new_state,
            revision=new_revision,
            updated_at=now,
            revision_target=revision_target if decision == "rejected" else None,
        )
        result = release_result(
            updated,
            artifacts=database.latest_artifact_paths(connection, release.release_id),
        )
        _save_result(
            database,
            connection,
            command_id,
            actor,
            "release.decide",
            request_hash,
            result,
            now,
        )
        return result


def request_revision(
    database: Database,
    *,
    command_id: str,
    expected_revision: int,
    actor: str,
    target: str,
    reason: str,
) -> ReleaseResult:
    require_actor(actor, {"author"})
    if not reason.strip():
        raise ValueError("Для правки нужна причина.")
    gates = invalidated_gates(target)
    request = {
        "actor": actor,
        "command": "release.revise",
        "expected_revision": expected_revision,
        "target": target,
        "reason": reason,
    }
    request_hash = canonical_hash(request)
    now = now_utc()

    with database.transaction() as connection:
        replay = _replay(database, connection, command_id, request_hash)
        if replay:
            return replay
        release = _active_for_mutation(database, connection, expected_revision)
        if release.state not in {
            "plan_pending",
            "editorial_pending",
            "awaiting_recording",
            "video_processing",
            "final_pending",
            "needs_attention",
        }:
            raise StateConflict(f"нельзя запросить правку из состояния {release.state}")
        if release.state == "plan_pending" and target != "plan":
            raise StateConflict("до утверждения плана можно править только план")
        if release.state == "final_pending" and target not in {"recording", "video"}:
            raise StateConflict("на финальном gate можно исправить запись или монтаж")
        if release.state == "video_processing" and target not in {"recording", "video"}:
            raise StateConflict("во время монтажа можно отозвать запись или результат монтажа")
        if release.state == "needs_attention" and target != release.revision_target:
            raise StateConflict("исправление должно соответствовать отчёту video_issue")
        database.invalidate_approvals(connection, release.release_id, gates, now)
        database.invalidate_artifacts(connection, release.release_id, target)
        if target == "recording":
            VideoStore.invalidate_recording(connection, release.release_id)
            VideoStore.reset_recording_window(connection, release.release_id, now)
        new_revision = database.update_release(
            connection,
            release_id=release.release_id,
            expected_revision=release.revision,
            state="revision_requested",
            updated_at=now,
            revision_target=target,
        )
        database.add_transition(
            connection,
            transition_id=uuid7(),
            release_id=release.release_id,
            from_state=release.state,
            to_state="revision_requested",
            actor=actor,
            reason=f"UC-03/05 revision {target}: {reason}",
            occurred_at=now,
        )
        updated = replace(
            release,
            state="revision_requested",
            revision=new_revision,
            updated_at=now,
            revision_target=target,
        )
        result = release_result(
            updated,
            artifacts=database.latest_artifact_paths(connection, release.release_id),
        )
        _save_result(
            database,
            connection,
            command_id,
            actor,
            "release.revise",
            request_hash,
            result,
            now,
        )
        return result


def _active_for_mutation(
    database: Database, connection: sqlite3.Connection, expected_revision: int
) -> Release:
    release = database.active_release(connection)
    if not release:
        raise StateConflict("нет активного Выпуска")
    if release.revision != expected_revision:
        raise StateConflict(
            f"expected revision {expected_revision}; actual revision {release.revision}"
        )
    return release


def _replay(
    database: Database, connection: sqlite3.Connection, command_id: str, request_hash: str
) -> ReleaseResult | None:
    if not command_id.strip():
        raise ValueError("command_id не может быть пустым.")
    previous = database.find_command(connection, command_id)
    if not previous:
        return None
    if previous["request_hash"] != request_hash:
        raise IdempotencyConflict(command_id)
    return ReleaseResult.model_validate_json(previous["outcome_json"])


def _save_result(
    database: Database,
    connection: sqlite3.Connection,
    command_id: str,
    actor: str,
    command: str,
    request_hash: str,
    result: ReleaseResult,
    now: str,
) -> None:
    database.save_command(
        connection,
        command_id=command_id,
        actor=actor,
        command=command,
        request_hash=request_hash,
        outcome=result.model_dump(mode="json", by_alias=True),
        occurred_at=now,
    )
