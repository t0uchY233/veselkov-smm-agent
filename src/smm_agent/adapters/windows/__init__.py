"""Pure Windows integration specifications and adapters."""

from smm_agent.adapters.windows.security import WindowsAclInspector, WindowsSecurityInspector
from smm_agent.adapters.windows.task_scheduler import (
    CommandResult,
    RegisteredTask,
    ReleaseTaskPlans,
    TaskRegistrationPlan,
    TaskScheduleSpec,
    WindowsTaskScheduler,
    build_release_task_plans,
    render_task_xml,
    task_name_for_release,
)

__all__ = [
    "CommandResult",
    "RegisteredTask",
    "ReleaseTaskPlans",
    "TaskRegistrationPlan",
    "TaskScheduleSpec",
    "WindowsTaskScheduler",
    "WindowsAclInspector",
    "WindowsSecurityInspector",
    "build_release_task_plans",
    "render_task_xml",
    "task_name_for_release",
]
