"""Pure Task Scheduler XML generation.

Task creation is deliberately outside this module.  Rendering a plan lets
tests inspect the Windows contract without pretending that a task was live
registered or that a sleeping laptop was successfully woken.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Literal
from xml.etree import ElementTree

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TASK_FOLDER_DEFAULT = "\\VeselkovSmm"
TaskPurpose = Literal["preflight", "telegram", "reconciliation", "worker"]
_SAFE_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SAFE_FOLDER = re.compile(r"\\[A-Za-z0-9_-]+(?:\\[A-Za-z0-9_-]+)*")


@dataclass(frozen=True, slots=True)
class TaskScheduleSpec:
    """The complete, non-live definition of one scheduled local command."""

    task_name: str
    target_at: datetime
    executable: str
    arguments: tuple[str, ...]
    working_directory: str
    run_as_user: str
    description: str
    purpose: TaskPurpose

    def __post_init__(self) -> None:
        if not _SAFE_FOLDER.fullmatch(self.task_name.rsplit("\\", 1)[0]):
            raise ValueError("task_name должен находиться в безопасной папке Task Scheduler.")
        leaf = self.task_name.rsplit("\\", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", leaf):
            raise ValueError("Имя Task Scheduler допускает только буквы, цифры, _ и -.")
        if self.target_at.tzinfo is None or self.target_at.utcoffset() is None:
            raise ValueError("target_at должен быть timezone-aware.")
        for label, value in (
            ("executable", self.executable),
            ("working_directory", self.working_directory),
        ):
            if not value or "\x00" in value or "\r" in value or "\n" in value:
                raise ValueError(f"{label} содержит недопустимый символ.")
            if not Path(value).is_absolute() and not PureWindowsPath(value).is_absolute():
                raise ValueError(f"{label} должен быть абсолютным путём.")
        if not self.description or any(char in self.description for char in "\x00\r\n"):
            raise ValueError("description пустое или содержит недопустимый символ.")
        if not self.run_as_user or any(char in self.run_as_user for char in "\x00\r\n"):
            raise ValueError("run_as_user пустой или содержит недопустимый символ.")
        if any(
            "\x00" in argument or "\r" in argument or "\n" in argument
            for argument in self.arguments
        ):
            raise ValueError("Task Scheduler arguments содержат недопустимый символ.")

    @property
    def target_at_utc(self) -> datetime:
        return self.target_at.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TaskRegistrationPlan:
    """An inspectable hand-off that expressly does not register anything."""

    task_name: str
    xml: str
    live_registration: Literal[False] = False
    next_action: str = "Передайте XML Windows installer; этот модуль не вызывает schtasks.exe."


def task_name_for_release(
    release_id: str,
    purpose: TaskPurpose,
    *,
    task_folder: str = TASK_FOLDER_DEFAULT,
) -> str:
    """Return the same safe name for the same release and purpose every time."""
    if not _SAFE_RELEASE_ID.fullmatch(release_id):
        raise ValueError("release_id небезопасен для Task Scheduler task name.")
    if not _SAFE_FOLDER.fullmatch(task_folder):
        raise ValueError("task_folder небезопасен для Task Scheduler.")
    return f"{task_folder}\\release-{release_id}-{purpose}"


def render_task_xml(spec: TaskScheduleSpec) -> TaskRegistrationPlan:
    """Render Task Scheduler XML with UTC trigger, WakeToRun and logged-off mode."""
    ElementTree.register_namespace("", TASK_NAMESPACE)
    task = ElementTree.Element(_tag("Task"), {"version": "1.4"})
    registration = ElementTree.SubElement(task, _tag("RegistrationInfo"))
    ElementTree.SubElement(registration, _tag("Description")).text = spec.description

    triggers = ElementTree.SubElement(task, _tag("Triggers"))
    trigger = ElementTree.SubElement(triggers, _tag("TimeTrigger"))
    ElementTree.SubElement(trigger, _tag("StartBoundary")).text = _utc_timestamp(spec.target_at_utc)
    ElementTree.SubElement(trigger, _tag("Enabled")).text = "true"

    principals = ElementTree.SubElement(task, _tag("Principals"))
    principal = ElementTree.SubElement(principals, _tag("Principal"), {"id": "Operator"})
    ElementTree.SubElement(principal, _tag("UserId")).text = spec.run_as_user
    ElementTree.SubElement(principal, _tag("LogonType")).text = "Password"
    ElementTree.SubElement(principal, _tag("RunLevel")).text = "LeastPrivilege"

    settings = ElementTree.SubElement(task, _tag("Settings"))
    ElementTree.SubElement(settings, _tag("MultipleInstancesPolicy")).text = "IgnoreNew"
    ElementTree.SubElement(settings, _tag("StartWhenAvailable")).text = "true"
    ElementTree.SubElement(settings, _tag("WakeToRun")).text = "true"
    ElementTree.SubElement(settings, _tag("AllowStartOnDemand")).text = "false"
    ElementTree.SubElement(settings, _tag("ExecutionTimeLimit")).text = "PT15M"

    actions = ElementTree.SubElement(task, _tag("Actions"), {"Context": "Operator"})
    execute = ElementTree.SubElement(actions, _tag("Exec"))
    ElementTree.SubElement(execute, _tag("Command")).text = spec.executable
    ElementTree.SubElement(execute, _tag("Arguments")).text = quote_windows_arguments(
        spec.arguments
    )
    ElementTree.SubElement(execute, _tag("WorkingDirectory")).text = spec.working_directory

    xml = ElementTree.tostring(task, encoding="unicode", xml_declaration=False)
    return TaskRegistrationPlan(task_name=spec.task_name, xml=xml)


def quote_windows_arguments(arguments: tuple[str, ...]) -> str:
    """Quote argv with the documented MS C runtime escaping rules, never a shell."""
    return subprocess.list2cmdline(list(arguments))


def _tag(name: str) -> str:
    return f"{{{TASK_NAMESPACE}}}{name}"


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
