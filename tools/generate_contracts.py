"""Generate checked-in JSON Schemas from canonical Pydantic DTOs."""

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

SCHEMAS = {
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


def main() -> None:
    output = Path("docs/generated/contracts")
    output.mkdir(parents=True, exist_ok=True)
    for filename, schema in SCHEMAS.items():
        (output / filename).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
