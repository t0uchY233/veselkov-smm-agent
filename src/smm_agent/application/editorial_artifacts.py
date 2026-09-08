"""Safe storage and validation of editorial artifact bytes."""

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smm_agent.application.editorial_support import canonical_bytes, sha256
from smm_agent.contracts.editorial import EditorialBundle
from smm_agent.domain.editorial.ports import ImageInspector
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7


@dataclass(frozen=True, slots=True)
class PreparedFileArtifact:
    artifact_id: str
    release_id: str
    kind: str
    filename: str
    path: Path
    sha256: str
    size: int
    media_type: str
    created_at: str


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
    temporary = path.with_name(f".{path.name}.{artifact_id}.tmp")
    try:
        with temporary.open("xb") as output_file:
            output_file.write(payload)
            output_file.flush()
            os.fsync(output_file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
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


def store_file_artifact(
    database: Database,
    connection: sqlite3.Connection,
    *,
    release_id: str,
    kind: str,
    filename: str,
    source: Path,
    media_type: str,
    created_at: str,
) -> dict[str, Any]:
    """Copy a potentially large file without loading it entirely into memory."""
    version = database.next_artifact_version(connection, release_id, kind)
    artifact_id = uuid7()
    target = database.data_root / "releases" / release_id / "artifacts" / kind
    target.mkdir(parents=True, exist_ok=True)
    path = (target / f"v{version:04d}-{Path(filename).name}").resolve()
    temporary = path.with_name(f".{path.name}.{artifact_id}.tmp")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    database.insert_artifact(
        connection,
        artifact_id=artifact_id,
        release_id=release_id,
        kind=kind,
        version=version,
        path=str(path),
        sha256=digest.hexdigest(),
        size=path.stat().st_size,
        media_type=media_type,
        created_at=created_at,
    )
    return {"artifact_id": artifact_id, "sha256": digest.hexdigest(), "path": str(path)}


def prepare_file_artifact(
    database: Database,
    *,
    release_id: str,
    kind: str,
    filename: str,
    source: Path,
    media_type: str,
    created_at: str,
) -> PreparedFileArtifact:
    """Copy and fsync large bytes before acquiring the SQLite writer lock."""
    artifact_id = uuid7()
    target = database.data_root / "releases" / release_id / "artifacts" / kind
    target.mkdir(parents=True, exist_ok=True)
    path = (target / f"{artifact_id}-{Path(filename).name}").resolve()
    temporary = path.with_name(f".{path.name}.tmp")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return PreparedFileArtifact(
        artifact_id=artifact_id,
        release_id=release_id,
        kind=kind,
        filename=Path(filename).name,
        path=path,
        sha256=digest.hexdigest(),
        size=path.stat().st_size,
        media_type=media_type,
        created_at=created_at,
    )


def register_prepared_file(
    database: Database,
    connection: sqlite3.Connection,
    prepared: PreparedFileArtifact,
) -> dict[str, Any]:
    version = database.next_artifact_version(
        connection, prepared.release_id, prepared.kind
    )
    database.insert_artifact(
        connection,
        artifact_id=prepared.artifact_id,
        release_id=prepared.release_id,
        kind=prepared.kind,
        version=version,
        path=str(prepared.path),
        sha256=prepared.sha256,
        size=prepared.size,
        media_type=prepared.media_type,
        created_at=prepared.created_at,
    )
    return {
        "artifact_id": prepared.artifact_id,
        "sha256": prepared.sha256,
        "path": str(prepared.path),
    }


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
    if inspector.contrast_score(assets[bundle.cover_path]) < 3:
        raise ValueError("Обложка выглядит пустой или однотонной.")
    for visual in bundle.visuals:
        size = inspector.dimensions(assets[visual.asset_path])
        if size != (1080, 1080):
            raise ValueError(
                f"Визуал {visual.visual_id} должен быть 1080x1080, "
                f"получено {size[0]}x{size[1]}"
            )
        if inspector.contrast_score(assets[visual.asset_path]) < 3:
            raise ValueError(f"Визуал {visual.visual_id} выглядит пустым или однотонным.")


def artifact_set_hash(records: list[dict[str, Any]]) -> str:
    pairs = [
        {"artifact_id": record["artifact_id"], "sha256": record["sha256"]}
        for record in records
    ]
    pairs.sort(key=lambda item: item["artifact_id"])
    return sha256(canonical_bytes(pairs))


def verify_artifact_records(records: list[dict[str, Any]]) -> None:
    for record in records:
        path = Path(str(record["path"]))
        digest = hashlib.sha256()
        if path.is_file():
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        if not path.is_file() or digest.hexdigest() != record["sha256"]:
            raise ValueError(f"Artifact bytes не совпадают с Манифестом: {record['kind']}")


def lock_artifact_records(records: list[dict[str, Any]]) -> None:
    for record in records:
        path = Path(str(record["path"]))
        path.chmod(path.stat().st_mode & ~0o222)
