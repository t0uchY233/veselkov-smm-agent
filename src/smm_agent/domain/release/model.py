"""Release aggregate state used by the first vertical slice."""

from dataclasses import dataclass
from typing import Literal

ReleaseState = Literal["topic_received"]


@dataclass(frozen=True, slots=True)
class Release:
    release_id: str
    topic: str
    state: ReleaseState
    revision: int
    active: bool
    created_at: str
    updated_at: str

    @property
    def next_action(self) -> str:
        return "Подготовить и показать Сергею Николаевичу план выпуска."

