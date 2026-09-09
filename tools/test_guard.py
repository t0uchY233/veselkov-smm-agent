"""Serialize host tests and bound their lifetime using the existing process runner."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from smm_agent.adapters.media.process import CommandTimedOut, run_bounded


@contextmanager
def host_lock() -> Iterator[None]:
    # Keep the inode: unlinking a lock file can let a third process bypass it.
    with (Path(tempfile.gettempdir()) / "veselkov-smm-test-guard.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    match = re.fullmatch(r"([1-9][0-9]*)([sm])", args.timeout)
    if match is None:
        parser.error("timeout must be a positive duration such as 10m or 30s")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    seconds = int(match.group(1)) * (60 if match.group(2) == "m" else 1)
    try:
        with host_lock():
            result = run_bounded(command, timeout_seconds=seconds)
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            return result.returncode
    except CommandTimedOut:
        print("Test deadline exceeded; check process-tree cleanup before retry.", file=sys.stderr)
        return 124
    except OSError:
        print("Test guard unavailable or another guarded command is active.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
