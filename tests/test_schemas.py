from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).parents[1]


def _schema(name: str) -> dict[str, object]:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def test_schemas_are_valid_draft_2020_12() -> None:
    for name in ("execution-spec-v1.schema.json", "run-report-v1.schema.json"):
        Draft202012Validator.check_schema(_schema(name))


def test_example_execution_spec_matches_schema() -> None:
    document = json.loads(
        (ROOT / "examples" / "murmansk-v3.execution-spec.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator(
        _schema("execution-spec-v1.schema.json"),
        format_checker=FormatChecker(),
    ).validate(document)
