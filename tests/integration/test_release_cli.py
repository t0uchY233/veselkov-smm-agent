import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def run_cli(project_root: Path, data_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    return subprocess.run(
        [sys.executable, "-m", "smm_agent.cli.main", *args, "--data-root", str(data_root)],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_survives_process_restart_and_command_is_idempotent(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    topic_file = tmp_path / "topic.txt"
    topic_file.write_text("Как контракт замораживает оборотку", encoding="utf-8")

    first = run_cli(
        project_root,
        tmp_path / "data",
        "release",
        "start",
        "--command-id",
        "cmd-001",
        "--topic-file",
        str(topic_file),
    )
    repeated = run_cli(
        project_root,
        tmp_path / "data",
        "release",
        "start",
        "--command-id",
        "cmd-001",
        "--topic-file",
        str(topic_file),
    )
    status = run_cli(project_root, tmp_path / "data", "release", "status")

    assert first.returncode == repeated.returncode == status.returncode == 0
    assert first.stderr == repeated.stderr == status.stderr == ""
    first_json = json.loads(first.stdout)
    assert json.loads(repeated.stdout) == first_json
    assert json.loads(status.stdout) == first_json
    assert first_json["state"] == "topic_received"

    connection = sqlite3.connect(tmp_path / "data" / "state" / "smm.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM transitions").fetchone() == (1,)
        assert connection.execute(
            "SELECT name FROM domain_events"
        ).fetchone() == ("ReleaseStarted.v1",)
    finally:
        connection.close()


def test_reused_command_id_with_changed_topic_is_rejected(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    topic_file = tmp_path / "topic.txt"
    topic_file.write_text("Первая тема", encoding="utf-8")
    data_root = tmp_path / "data"
    first = run_cli(
        project_root,
        data_root,
        "release",
        "start",
        "--command-id",
        "cmd-001",
        "--topic-file",
        str(topic_file),
    )
    topic_file.write_text("Другая тема", encoding="utf-8")
    conflict = run_cli(
        project_root,
        data_root,
        "release",
        "start",
        "--command-id",
        "cmd-001",
        "--topic-file",
        str(topic_file),
    )

    assert first.returncode == 0
    assert conflict.returncode == 2
    assert json.loads(conflict.stdout)["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_second_active_release_is_rejected(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    topic_file = tmp_path / "topic.txt"
    topic_file.write_text("Первая тема", encoding="utf-8")
    data_root = tmp_path / "data"
    first = run_cli(
        project_root,
        data_root,
        "release",
        "start",
        "--command-id",
        "cmd-001",
        "--topic-file",
        str(topic_file),
    )
    conflict = run_cli(
        project_root,
        data_root,
        "release",
        "start",
        "--command-id",
        "cmd-002",
        "--topic-file",
        str(topic_file),
    )

    assert first.returncode == 0
    assert conflict.returncode == 2
    assert json.loads(conflict.stdout)["error"]["code"] == "STATE_CONFLICT"


def test_start_rejects_nonzero_expected_revision(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    topic_file = tmp_path / "topic.txt"
    topic_file.write_text("Тема", encoding="utf-8")

    conflict = run_cli(
        project_root,
        tmp_path / "data",
        "release",
        "start",
        "--command-id",
        "cmd-stale",
        "--topic-file",
        str(topic_file),
        "--expected-revision",
        "1",
    )

    assert conflict.returncode == 2
    assert json.loads(conflict.stdout)["error"]["code"] == "STATE_CONFLICT"
