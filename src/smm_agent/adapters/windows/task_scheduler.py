"""Windows Task Scheduler plans and a deliberately narrow ``schtasks`` adapter.

The plan is deterministic and inspectable before any operating-system mutation.
Registration is an explicit second action, with a caller-provided password from
an injected secret reader. Nothing in this module discovers accounts, secrets
or tasks on import.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Literal, Protocol
from xml.etree import ElementTree

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TASK_FOLDER_DEFAULT = "\\VeselkovSmm"
TaskPurpose = Literal["preflight", "telegram", "reconciliation", "worker"]
_SAFE_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SAFE_FOLDER = re.compile(r"\\[A-Za-z0-9_-]+(?:\\[A-Za-z0-9_-]+)*")


class TaskSchedulerError(RuntimeError):
    """A scheduler failure with no command output or secret text."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    """Subprocess seam; tests use a fake and never run schtasks."""

    def run(self, arguments: Sequence[str]) -> CommandResult: ...


class SubprocessCommandRunner:
    """Windows command runner that does not use a shell or log a secret."""

    def run(self, arguments: Sequence[str]) -> CommandResult:
        completed = subprocess.run(  # noqa: S603 -- fixed executable, list argv, no shell
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True, slots=True)
class TaskScheduleSpec:
    """The complete definition of one Windows task action."""

    task_name: str
    target_at: datetime
    executable: str
    arguments: tuple[str, ...]
    working_directory: str
    run_as_user: str
    description: str
    purpose: TaskPurpose

    def __post_init__(self) -> None:
        folder, separator, leaf = self.task_name.rpartition("\\")
        if not separator or not _SAFE_FOLDER.fullmatch(folder):
            raise ValueError("task_name должен находиться в безопасной папке Task Scheduler.")
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

    @property
    def command_argv(self) -> tuple[str, ...]:
        """The exact worker argv committed to the task XML."""

        return (self.executable, *self.arguments)


@dataclass(frozen=True, slots=True)
class TaskRegistrationPlan:
    """An inspectable task definition, not proof of a live registration."""

    task_name: str
    xml: str
    run_as_user: str
    command_argv: tuple[str, ...]
    live_registration: Literal[False] = False
    next_action: str = "Явно зарегистрируйте XML на Windows и подтвердите wake smoke."


@dataclass(frozen=True, slots=True)
class RegisteredTask:
    task_name: str
    action: Literal["registered", "deleted"]


@dataclass(frozen=True, slots=True)
class ReleaseTaskPlans:
    """The two task identities that implement T−30 and T for one release."""

    release_id: str
    preflight: TaskRegistrationPlan
    target: TaskRegistrationPlan

    @property
    def task_names(self) -> tuple[str, str]:
        return (self.preflight.task_name, self.target.task_name)


def task_name_for_release(
    release_id: str,
    purpose: TaskPurpose,
    *,
    task_folder: str = TASK_FOLDER_DEFAULT,
) -> str:
    """Return the same safe task identity for the same release and purpose."""

    if not _SAFE_RELEASE_ID.fullmatch(release_id):
        raise ValueError("release_id небезопасен для Task Scheduler task name.")
    if not _SAFE_FOLDER.fullmatch(task_folder):
        raise ValueError("task_folder небезопасен для Task Scheduler.")
    return f"{task_folder}\\release-{release_id}-{purpose}"


def build_release_task_plans(
    *,
    release_id: str,
    target_at: datetime,
    preflight_offset_minutes: int,
    task_folder: str,
    worker_executable: str,
    config_path: str,
    run_as_user: str,
    working_directory: str,
) -> ReleaseTaskPlans:
    """Bind T−30 and T plans to one release and exact worker argv.

    A due durable SQLite job remains authoritative. ``--scheduled-task`` is an
    auditable identity, so a task from a different release cannot masquerade as
    this release's T−30 or T invocation.
    """

    if preflight_offset_minutes < 1:
        raise ValueError("preflight_offset_minutes должен быть положительным.")
    target = target_at.astimezone(UTC)
    preflight_name = task_name_for_release(release_id, "preflight", task_folder=task_folder)
    target_name = task_name_for_release(release_id, "telegram", task_folder=task_folder)
    preflight = TaskScheduleSpec(
        task_name=preflight_name,
        target_at=target - timedelta(minutes=preflight_offset_minutes),
        executable=worker_executable,
        arguments=("--config", config_path, "--once", "--scheduled-task", preflight_name),
        working_directory=working_directory,
        run_as_user=run_as_user,
        description=f"UC-10 T-30 preflight for release {release_id}",
        purpose="preflight",
    )
    target_task = TaskScheduleSpec(
        task_name=target_name,
        target_at=target,
        executable=worker_executable,
        arguments=("--config", config_path, "--once", "--scheduled-task", target_name),
        working_directory=working_directory,
        run_as_user=run_as_user,
        description=f"UC-10 Telegram/reconciliation target for release {release_id}",
        purpose="telegram",
    )
    return ReleaseTaskPlans(
        release_id=release_id,
        preflight=render_task_xml(preflight),
        target=render_task_xml(target_task),
    )


def render_task_xml(spec: TaskScheduleSpec) -> TaskRegistrationPlan:
    """Render XML with UTC trigger, WakeToRun and logged-off task semantics."""

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
    return TaskRegistrationPlan(
        task_name=spec.task_name,
        xml=xml,
        run_as_user=spec.run_as_user,
        command_argv=spec.command_argv,
    )


class WindowsTaskScheduler:
    """Explicit Windows mutation adapter around ``schtasks.exe``.

    The caller must supply the task password after reading it through a
    dedicated secret port. The adapter never includes it in exceptions,
    results or logs. Unit tests inject ``CommandRunner`` and exercise exact
    argv construction only.
    """

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        is_windows: bool | None = None,
    ) -> None:
        self._runner = runner or SubprocessCommandRunner()
        self._is_windows = os.name == "nt" if is_windows is None else is_windows

    def register(self, plan: TaskRegistrationPlan, *, task_password: str) -> RegisteredTask:
        self._require_windows()
        if not task_password or any(character in task_password for character in "\x00\r\n"):
            raise TaskSchedulerError(
                "Task Scheduler password недоступен или имеет неверный формат."
            )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".xml", delete=False
            ) as stream:
                stream.write(plan.xml)
                temporary_path = Path(stream.name)
            result = self._runner.run(
                (
                    "schtasks.exe",
                    "/Create",
                    "/TN",
                    plan.task_name,
                    "/XML",
                    str(temporary_path),
                    "/RU",
                    plan.run_as_user,
                    "/RP",
                    task_password,
                    "/F",
                )
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise TaskSchedulerError("Не удалось зарегистрировать Windows Task Scheduler task.")
        return RegisteredTask(task_name=plan.task_name, action="registered")

    def delete(self, task_name: str) -> RegisteredTask:
        self._require_windows()
        _validate_task_name(task_name)
        result = self._runner.run(("schtasks.exe", "/Delete", "/TN", task_name, "/F"))
        if result.returncode != 0:
            raise TaskSchedulerError("Не удалось удалить Windows Task Scheduler task.")
        return RegisteredTask(task_name=task_name, action="deleted")

    def registered_run_as_user(self, task_name: str) -> str | None:
        """Read task XML to compare its account without locale-dependent parsing."""

        self._require_windows()
        _validate_task_name(task_name)
        result = self._runner.run(("schtasks.exe", "/Query", "/TN", task_name, "/XML"))
        if result.returncode != 0:
            return None
        try:
            root = ElementTree.fromstring(result.stdout)
            user_id = root.find(f".//{{{TASK_NAMESPACE}}}UserId")
        except ElementTree.ParseError:
            return None
        if user_id is None or not user_id.text:
            return None
        return user_id.text.strip() or None

    def _require_windows(self) -> None:
        if not self._is_windows:
            raise TaskSchedulerError("Windows Task Scheduler недоступен вне Windows.")


def quote_windows_arguments(arguments: tuple[str, ...]) -> str:
    """Quote argv using MS C runtime escaping rules; never invoke a shell."""

    return subprocess.list2cmdline(list(arguments))


def _validate_task_name(task_name: str) -> None:
    folder, separator, leaf = task_name.rpartition("\\")
    if not separator or not _SAFE_FOLDER.fullmatch(folder) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", leaf
    ):
        raise ValueError("task_name небезопасен для Task Scheduler.")


def _tag(name: str) -> str:
    return f"{{{TASK_NAMESPACE}}}{name}"


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
