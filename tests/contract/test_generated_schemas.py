import json
from pathlib import Path

from smm_agent.contracts.cli import ErrorResponse, ReleaseResult


def test_checked_in_contract_schemas_match_models() -> None:
    root = Path(__file__).parents[2] / "docs" / "generated" / "contracts"
    expected = {
        "cli-error.v1.json": ErrorResponse.model_json_schema(by_alias=True),
        "release-result.v1.json": ReleaseResult.model_json_schema(by_alias=True),
    }

    for filename, schema in expected.items():
        assert json.loads((root / filename).read_text(encoding="utf-8")) == schema
