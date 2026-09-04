"""Pure Windows integration specifications and adapters."""

from smm_agent.adapters.windows.task_scheduler import (
    TaskRegistrationPlan,
    TaskScheduleSpec,
    render_task_xml,
    task_name_for_release,
)

__all__ = [
    "TaskRegistrationPlan",
    "TaskScheduleSpec",
    "render_task_xml",
    "task_name_for_release",
]
