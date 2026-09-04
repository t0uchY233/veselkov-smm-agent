import json
from pathlib import Path

from smm_agent.contracts.cli import ErrorResponse, ReleaseResult
from smm_agent.contracts.editorial import EditorialBundle, PlanDocument
from smm_agent.contracts.publication import PublicationRequest, PublicationResult
from smm_agent.contracts.video import AlignmentProfile, AlignmentResult, Transcript, VideoQc


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
    }

    for filename, schema in expected.items():
        assert json.loads((root / filename).read_text(encoding="utf-8")) == schema
