import json
from pathlib import Path

from smm_agent.contracts.cli import ErrorResponse, ReleaseResult
from smm_agent.contracts.editorial import EditorialBundle, PlanDocument
from smm_agent.contracts.publication import (
    IncidentRecord,
    NotificationJobPayload,
    NotificationReceipt,
    NotificationRequest,
    PublicationRequest,
    PublicationResult,
    RecoveryError,
    RecoveryJobPayload,
    RetryDecision,
)
from smm_agent.contracts.video import AlignmentProfile, AlignmentResult, Transcript, VideoQc
from smm_agent.domain.publication.service import retry_decision


def test_checked_in_contract_schemas_match_models() -> None:
    root = Path(__file__).parents[2] / "docs" / "generated" / "contracts"
    expected = {
        "cli-error.v1.json": ErrorResponse.model_json_schema(by_alias=True),
        "release-result.v1.json": ReleaseResult.model_json_schema(by_alias=True),
        "editorial-bundle.v1.json": EditorialBundle.model_json_schema(),
        "plan-document.v1.json": PlanDocument.model_json_schema(),
        "video-transcript.v1.json": Transcript.model_json_schema(),
        "video-alignment.v1.json": AlignmentResult.model_json_schema(),
        "video-qc.v1.json": VideoQc.model_json_schema(),
        "video-alignment-profile.v1.json": AlignmentProfile.model_json_schema(),
        "publication-request.v1.json": PublicationRequest.model_json_schema(),
        "publication-result.v1.json": PublicationResult.model_json_schema(),
        "recovery-job-payload.v1.json": RecoveryJobPayload.model_json_schema(),
        "recovery-error.v1.json": RecoveryError.model_json_schema(),
        "retry-decision.v1.json": RetryDecision.model_json_schema(),
        "incident-record.v1.json": IncidentRecord.model_json_schema(),
        "notification-job-payload.v1.json": NotificationJobPayload.model_json_schema(),
        "notification-request.v1.json": NotificationRequest.model_json_schema(),
        "notification-receipt.v1.json": NotificationReceipt.model_json_schema(),
    }

    for filename, schema in expected.items():
        assert json.loads((root / filename).read_text(encoding="utf-8")) == schema


def test_retry_taxonomy_has_exact_recovery_schedules_and_terminal_errors() -> None:
    temporary = RecoveryError(code="PROVIDER_HTTP_429", sanitized_detail="HTTP 429")
    assert [
        retry_decision(temporary, operation="provider", attempt_number=attempt).retry_after_seconds
        for attempt in range(1, 5)
    ] == [30, 120, 300, 900]
    assert (
        retry_decision(temporary, operation="provider", attempt_number=5).disposition
        == "exhausted"
    )
    assert [
        retry_decision(
            temporary, operation="notification", attempt_number=attempt
        ).retry_after_seconds
        for attempt in range(1, 4)
    ] == [60, 300, 900]
    assert (
        retry_decision(temporary, operation="notification", attempt_number=4).disposition
        == "exhausted"
    )

    for code in (
        "PROVIDER_TIMEOUT",
        "PROVIDER_HTTP_429",
        "PROVIDER_HTTP_5XX",
        "UNKNOWN_PROVIDER_OUTCOME",
    ):
        decision = retry_decision(
            RecoveryError(code=code, sanitized_detail="Temporary replay failure"),
            operation="provider",
            attempt_number=1,
        )
        assert decision.classification == "retryable"
        assert decision.disposition == "retry"

    for code in (
        "PROVIDER_AUTH_REQUIRED",
        "PROVIDER_PERMISSION_DENIED",
        "INVALID_PAYLOAD",
        "RECEIPT_MISMATCH",
    ):
        decision = retry_decision(
            RecoveryError(code=code, sanitized_detail="Terminal replay failure"),
            operation="provider",
            attempt_number=1,
        )
        assert decision.classification == "terminal"
        assert decision.disposition == "terminal"
        assert decision.retry_after_seconds is None
