from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).parents[1]


def _schema(name: str) -> dict[str, object]:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def test_schemas_are_valid_draft_2020_12() -> None:
    for name in (
        "execution-spec-v1.schema.json",
        "execution-spec-v2.schema.json",
        "presentation-route-candidates-v1.schema.json",
        "run-report-v1.schema.json",
    ):
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


def test_v2_execution_spec_matches_schema() -> None:
    document = {
        "schema_version": "orchestrator.execution-spec.v2",
        "run_id": "run-00000000-0000-4000-8000-000000000002",
        "scenario_id": "murmansk_dikson_july_2026_retrospective_v1",
        "generation_id": 0,
        "input_revision": 0,
        "generated_at": "2026-08-13T00:00:00Z",
        "planning_contract": "cd.four-layer-route-plan-set.v3",
        "max_snap_km": 150.0,
        "replan_after_hours": 6,
        "per_stage_timeout_seconds": 900.0,
        "planning_workers": 3,
        "parallel_pool_mode": "persistent",
    }

    Draft202012Validator(
        _schema("execution-spec-v2.schema.json"),
        format_checker=FormatChecker(),
    ).validate(document)


def test_route_candidate_schema_accepts_fail_closed_not_published_sentinel() -> None:
    document = {
        "schema_version": "presentation.route-candidates.v1",
        "status": "NOT_PUBLISHED",
        "candidates": [],
        "reason": "candidate_geometry_and_metrics_not_published",
    }

    Draft202012Validator(
        _schema("presentation-route-candidates-v1.schema.json"),
        format_checker=FormatChecker(),
    ).validate(document)
