"""Release aggregate state used by the first vertical slice."""

from dataclasses import dataclass
from typing import Literal

ReleaseState = Literal[
    "topic_received",
    "plan_pending",
    "editorial_building",
    "editorial_pending",
    "awaiting_recording",
    "video_processing",
    "final_pending",
    "publication_preparing",
    "scheduled",
    "publishing",
    "published",
    "revision_requested",
    "recovering",
    "delayed",
    "needs_attention",
]


@dataclass(frozen=True, slots=True)
class Release:
    release_id: str
    topic: str
    state: ReleaseState
    revision: int
    active: bool
    created_at: str
    updated_at: str
    revision_target: str | None = None
    target_at_utc: str | None = None
    target_timezone: str = "Europe/Moscow"

    @property
    def next_action(self) -> str:
        if self.state == "revision_requested" and self.revision_target == "recording":
            return "Сохранить новую запись в настроенную папку для повторного монтажа."
        actions = {
            "topic_received": "Подготовить и показать Сергею Николаевичу план выпуска.",
            "plan_pending": "Попросить Сергея Николаевича утвердить план или дать правки.",
            "editorial_building": "Собрать и проверить весь редакционный пакет.",
            "editorial_pending": (
                "Попросить Сергея Николаевича утвердить материалы или дать правки."
            ),
            "awaiting_recording": "Сохранить записанное видео в настроенную папку.",
            "video_processing": "Дождаться окончания монтажа и автоматической проверки.",
            "final_pending": "Попросить Сергея Николаевича утвердить финальный комплект.",
            "publication_preparing": "Подготовить площадки и поставить выпуск в расписание.",
            "scheduled": "Дождаться согласованного времени публикации.",
            "publishing": "Проверить результаты публикации на трёх площадках.",
            "published": "Выпуск завершён. Можно начать новую тему.",
            "revision_requested": "Исправить указанный материал и импортировать новую версию.",
            "recovering": "Дождаться восстановления отсутствующей публикации.",
            "delayed": "Устранить причину задержки и выбрать новое время.",
            "needs_attention": (
                "Открыть последний отчёт об ошибке и выполнить безопасное действие."
            ),
        }
        return actions[self.state]
