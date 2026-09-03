"""Generate checked-in JSON Schemas from canonical Pydantic DTOs."""

import json
from pathlib import Path

from smm_agent.contracts.cli import ErrorResponse, ReleaseResult

SCHEMAS = {
    "cli-error.v1.json": ErrorResponse.model_json_schema(by_alias=True),
    "release-result.v1.json": ReleaseResult.model_json_schema(by_alias=True),
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
