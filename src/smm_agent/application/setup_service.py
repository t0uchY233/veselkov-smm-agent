"""Explicit operator acceptance of machine-specific media calibration."""

import hashlib
from datetime import datetime

from smm_agent.application.editorial_support import canonical_bytes, now_utc
from smm_agent.contracts.video import AlignmentProfile, MediaProfileAcceptance
from smm_agent.platform.db import Database

MEDIA_PROFILE_CONFIRMATION = "ПРИНИМАЮ ПРОФИЛЬ МОНТАЖА"


def media_profile_hash(profile: AlignmentProfile) -> str:
    return hashlib.sha256(canonical_bytes(profile.model_dump(mode="json"))).hexdigest()


def accept_media_profile(
    database: Database,
    *,
    profile: AlignmentProfile,
    actor: str,
    confirmation: str,
) -> MediaProfileAcceptance:
    if actor != "operator":
        raise ValueError("Профиль монтажа принимает только operator.")
    if confirmation != MEDIA_PROFILE_CONFIRMATION:
        raise ValueError("Нужно явное подтверждение принятия media profile.")
    digest = media_profile_hash(profile)
    now = now_utc()
    profile_json = canonical_bytes(profile.model_dump(mode="json")).decode("utf-8")
    with database.transaction() as connection:
        database.register_media_profile(
            connection,
            profile_sha256=digest,
            profile_json=profile_json,
            accepted_by=actor,
            accepted_at=now,
        )
    return MediaProfileAcceptance(
        profile_sha256=digest,
        accepted_by="operator",
        accepted_at=datetime.fromisoformat(now.replace("Z", "+00:00")),
    )


def require_accepted_media_profile(database: Database, profile: AlignmentProfile) -> None:
    digest = media_profile_hash(profile)
    expected_json = canonical_bytes(profile.model_dump(mode="json")).decode("utf-8")
    with database.connect() as connection:
        row = database.accepted_media_profile(connection, digest)
    if row is None or row["profile_json"] != expected_json:
        raise ValueError(
            "Media profile не принят оператором в trusted setup state; production заблокирован."
        )
