"""Safe storage and validation of editorial artifact bytes."""

import sqlite3
from pathlib import Path
from typing import Any

from smm_agent.application.editorial_support import canonical_bytes, sha256
from smm_agent.contracts.editorial import EditorialBundle
from smm_agent.domain.editorial.ports import ImageInspector
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7


def store_artifact(
    database: Database,
    connection: sqlite3.Connection,
    *,
    release_id: str,
    kind: str,
    filename: str,
    payload: bytes,
    media_type: str,
    created_at: str,
) -> dict[str, Any]:
    version = database.next_artifact_version(connection, release_id, kind)
    artifact_id = uuid7()
    safe_kind = kind.replace(":", "_")
    target = database.data_root / "releases" / release_id / "artifacts" / safe_kind
    target.mkdir(parents=True, exist_ok=True)
    path = (target / f"v{version:04d}-{Path(filename).name}").resolve()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    digest = sha256(payload)
    database.insert_artifact(
        connection,
        artifact_id=artifact_id,
        release_id=release_id,
        kind=kind,
        version=version,
        path=str(path),
        sha256=digest,
        size=len(payload),
        media_type=media_type,
        created_at=created_at,
    )
    return {"artifact_id": artifact_id, "sha256": digest, "path": str(path)}


def load_bundle_assets(bundle_root: Path, bundle: EditorialBundle) -> dict[str, bytes]:
    root = bundle_root.resolve()
    requested = [bundle.cover_path, *(visual.asset_path for visual in bundle.visuals)]
    assets: dict[str, bytes] = {}
    for relative in requested:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"Небезопасный путь asset: {relative}")
        path = (root / candidate).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"Asset не найден внутри bundle: {relative}")
        assets[relative] = path.read_bytes()
    return assets


def validate_image_dimensions(
    bundle: EditorialBundle,
    assets: dict[str, bytes],
    inspector: ImageInspector,
) -> None:
    cover_size = inspector.dimensions(assets[bundle.cover_path])
    if cover_size != (1280, 720):
        raise ValueError(f"Обложка должна быть 1280x720, получено {cover_size[0]}x{cover_size[1]}")
    for visual in bundle.visuals:
        size = inspector.dimensions(assets[visual.asset_path])
        if size != (1080, 1080):
            raise ValueError(
                f"Визуал {visual.visual_id} должен быть 1080x1080, "
                f"получено {size[0]}x{size[1]}"
            )


def artifact_set_hash(records: list[dict[str, Any]]) -> str:
    pairs = [
        {"artifact_id": record["artifact_id"], "sha256": record["sha256"]}
        for record in records
    ]
    pairs.sort(key=lambda item: item["artifact_id"])
    return sha256(canonical_bytes(pairs))
