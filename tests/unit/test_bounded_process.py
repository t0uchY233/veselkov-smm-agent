import sys

from smm_agent.adapters.media.process import CommandTimedOut, run_bounded


def test_bounded_process_times_out() -> None:
    try:
        run_bounded(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout_seconds=0.05,
        )
    except CommandTimedOut as error:
        assert "0.05 seconds" in str(error)
    else:
        raise AssertionError("sleeping process did not time out")


def test_bounded_timeout_terminates_descendants(tmp_path) -> None:
    import time

    marker = tmp_path / "survived.txt"
    ready = tmp_path / "ready.txt"
    child = (
        "import time; from pathlib import Path; "
        f"Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(3); "
        f"Path({str(marker)!r}).write_text('survived')"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    )
    try:
        run_bounded([sys.executable, "-c", parent], timeout_seconds=1)
    except CommandTimedOut:
        pass
    else:
        raise AssertionError("parent did not time out")
    assert ready.exists(), "child did not start; cleanup was not exercised"
    time.sleep(3)
    assert not marker.exists(), "descendant survived process-tree cleanup"


def test_guard_rejects_second_command_on_same_host(tmp_path) -> None:
    from pathlib import Path

    marker = tmp_path / "second-test.txt"
    guard = Path(__file__).resolve().parents[2] / "tools" / "test_guard.py"
    result = run_bounded(
        [sys.executable, str(guard), "--timeout", "10s", "--", sys.executable,
         "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        timeout_seconds=15,
    )
    # The surrounding guarded pytest command owns the host lock.
    assert result.returncode == 2
    assert not marker.exists()
