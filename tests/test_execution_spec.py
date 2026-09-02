from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.models import ExecutionSpec


def test_execution_spec_strict_round_trip(tmp_path) -> None:
    expected = ExecutionSpec(
        schema_version="orchestrator.execution-spec.v1",
        run_id="run-00000000-0000-4000-8000-000000000001",
        scenario_id="murmansk_dikson_july_2026_retrospective_v1",
        generation_id=2,
        input_revision=0,
        generated_at=datetime(2026, 8, 13, tzinfo=UTC),
        planning_contract="cd.route-plan.v2",
        max_snap_km=150.0,
        replan_after_hours=6,
        per_stage_timeout_seconds=900.0,
    )
    path = tmp_path / "execution-spec.json"
    path.write_text(json.dumps(expected.to_document()), encoding="utf-8")

    assert ExecutionSpec.from_path(path) == expected


def test_execution_spec_rejects_extra_fields(tmp_path) -> None:
    path = tmp_path / "execution-spec.json"
    document = {
        "schema_version": "orchestrator.execution-spec.v1",
        "run_id": "run-00000000-0000-4000-8000-000000000001",
        "scenario_id": "murmansk_dikson_july_2026_retrospective_v1",
        "generation_id": 0,
        "input_revision": 0,
        "generated_at": "2026-08-13T00:00:00Z",
        "planning_contract": "cd.route-plan.v2",
        "max_snap_km": 150.0,
        "replan_after_hours": 6,
        "per_stage_timeout_seconds": 900.0,
        "latest": True,
    }
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(OrchestrationError, match="fields differ"):
        ExecutionSpec.from_path(path)


def test_execution_spec_v2_persists_rc2_parallel_profile(tmp_path) -> None:
    expected = ExecutionSpec(
        schema_version="orchestrator.execution-spec.v2",
        run_id="run-00000000-0000-4000-8000-000000000002",
        scenario_id="murmansk_dikson_july_2026_retrospective_v1",
        generation_id=2,
        input_revision=0,
        generated_at=datetime(2026, 8, 13, tzinfo=UTC),
        planning_contract="cd.four-layer-route-plan-set.v3",
        max_snap_km=150.0,
        replan_after_hours=6,
        per_stage_timeout_seconds=900.0,
        planning_workers=3,
        parallel_pool_mode="persistent",
    )
    path = tmp_path / "execution-spec-v2.json"
    path.write_text(json.dumps(expected.to_document()), encoding="utf-8")

    assert ExecutionSpec.from_path(path) == expected


def test_execution_spec_v1_uses_fixed_rc2_profile() -> None:
    spec = ExecutionSpec(
        schema_version="orchestrator.execution-spec.v1",
        run_id="run-00000000-0000-4000-8000-000000000003",
        scenario_id="murmansk_dikson_july_2026_retrospective_v1",
        generation_id=0,
        input_revision=0,
        generated_at=datetime(2026, 8, 13, tzinfo=UTC),
        planning_contract="cd.route-plan.v2",
    )
    assert spec.planning_workers == 3
    assert spec.parallel_pool_mode == "persistent"
