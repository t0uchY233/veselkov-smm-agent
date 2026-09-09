from datetime import UTC, datetime

import pytest

from smm_agent.adapters.notification.telegram import TelegramAlertTransport
from smm_agent.adapters.publishing.http import HttpResponse
from smm_agent.contracts.publication import NotificationRequest
from smm_agent.domain.publication.ports import ProviderOperationError
from smm_agent.platform.db import Database


class FakeTransport:
    def __init__(self, *, ambiguous=False, wrong_recipient=False):
        self.calls = 0
        self.ambiguous = ambiguous
        self.wrong_recipient = wrong_recipient

    def send(self, request):
        self.calls += 1
        assert request.url == "https://api.telegram.org/bot/sendMessage"
        if self.ambiguous:
            raise TimeoutError("DO_NOT_EXPOSE_SECRET_RESPONSE")
        recipient = 999 if self.wrong_recipient else 276042853
        return HttpResponse(
            status_code=200,
            body=(
                f'{{"ok":true,"result":{{"message_id":7,"date":1788940000,'
                f'"chat":{{"id":{recipient}}}}}}}'
            ).encode(),
        )


def request():
    return NotificationRequest(
        notification_id="notice-1",
        incident_id="incident-1",
        release_id="release-1",
        error_code="PROVIDER_TIMEOUT",
        occurred_at=datetime(2026, 9, 9, tzinfo=UTC),
        safe_next_action="Inspect the local incident.",
        suppression_key="incident-1",
    )


def test_alert_receipt_survives_restart_without_second_send(tmp_path):
    database = Database(tmp_path)
    database.initialize()
    http = FakeTransport()
    transport = TelegramAlertTransport(database=database, transport=http)
    expected = transport.send(request=request(), body="Technical alert")
    restarted = TelegramAlertTransport(database=database, transport=http)
    assert restarted.lookup(notification_id="notice-1", recipient="276042853") == expected
    assert restarted.send(request=request(), body="Technical alert") == expected
    assert http.calls == 1
    with pytest.raises(ProviderOperationError):
        restarted.send(request=request(), body="Changed content")
    assert http.calls == 1


@pytest.mark.parametrize("failure", ["ambiguous", "wrong_recipient"])
def test_alert_unconfirmed_send_never_repeats_or_leaks_response(tmp_path, failure):
    database = Database(tmp_path)
    database.initialize()
    http = FakeTransport(**{failure: True})
    transport = TelegramAlertTransport(database=database, transport=http)
    with pytest.raises(ProviderOperationError) as error:
        transport.send(request=request(), body="Technical alert")
    assert error.value.code == "RECEIPT_MISMATCH"
    assert "DO_NOT_EXPOSE" not in error.value.sanitized_detail
    restarted = TelegramAlertTransport(database=database, transport=http)
    with pytest.raises(ProviderOperationError):
        restarted.lookup(notification_id="notice-1", recipient="276042853")
    with pytest.raises(ProviderOperationError):
        restarted.send(request=request(), body="Technical alert")
    assert http.calls == 1
