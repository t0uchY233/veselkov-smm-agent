from datetime import UTC, datetime

import pytest

from smm_agent.domain.release.service import start_release


def test_start_release_normalizes_topic_and_sets_next_action() -> None:
    release = start_release(
        release_id="019-test",
        topic="  Контракт   и деньги \n",
        now=datetime(2026, 9, 3, tzinfo=UTC),
    )

    assert release.topic == "Контракт и деньги"
    assert release.state == "topic_received"
    assert release.revision == 1
    assert "план выпуска" in release.next_action


def test_start_release_rejects_blank_topic() -> None:
    with pytest.raises(ValueError, match="не может быть пустой"):
        start_release(release_id="019-test", topic=" \n ")

