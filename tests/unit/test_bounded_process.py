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
