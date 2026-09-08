"""Bounded subprocess execution with descendant cleanup on timeout."""

import os
import signal
import subprocess

from smm_agent.adapters.media.windows_job import close_job, create_kill_on_close_job


class CommandTimedOut(RuntimeError):
    pass


def _kill_posix_process_group(pid: int) -> None:
    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if killpg is None or sigkill is None:
        raise OSError("POSIX process-group cleanup is unavailable")
    killpg(pid, sigkill)


def run_bounded(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if os.name == "nt":
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        job = create_kill_on_close_job(int(process._handle))  # type: ignore[attr-defined]
    else:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        job = None
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        if os.name == "nt":
            if job is not None:
                close_job(job)
                job = None
            else:
                try:
                    cleanup = subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                    if cleanup.returncode != 0:
                        process.kill()
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
        else:
            _kill_posix_process_group(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired as cleanup_error:
            process.kill()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", "process tree did not terminate"
            raise CommandTimedOut("timed-out process tree could not be reaped") from cleanup_error
        raise CommandTimedOut(
            f"command exceeded {timeout_seconds:g} seconds"
        ) from error
    finally:
        if job is not None:
            close_job(job)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
