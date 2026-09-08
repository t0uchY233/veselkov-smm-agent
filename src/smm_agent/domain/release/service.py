"""Pure release creation rules."""

from datetime import UTC, datetime

from smm_agent.domain.release.model import Release


class ReleaseConflict(Exception):
    """A requested release transition violates an invariant."""


def start_release(*, release_id: str, topic: str, now: datetime | None = None) -> Release:
    normalized_topic = " ".join(topic.split())
    if not normalized_topic:
        raise ValueError("Тема выпуска не может быть пустой.")

    timestamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    return Release(
        release_id=release_id,
        topic=normalized_topic,
        state="topic_received",
        revision=1,
        active=True,
        created_at=timestamp,
        updated_at=timestamp,
    )
