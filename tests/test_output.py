from __future__ import annotations

import json

import pytest

from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.output import (
    publish_output_directory,
    semantic_route_plan_digest,
)


def test_semantic_route_digest_ignores_execution_bookkeeping() -> None:
    first = {
        "schema_version": "cd.route-plan.v2",
        "plan_id": "random-one",
        "planning_request_id": "request-one",
        "generated_at": "2026-08-14T00:00:00Z",
        "objective_mode": "recommended",
        "metrics": {"objective_cost": 12.0, "compute_ms": 1.0},
    }
    second = {
        **first,
        "plan_id": "random-two",
        "planning_request_id": "request-two",
        "generated_at": "2026-08-14T00:01:00Z",
        "metrics": {"objective_cost": 12.0, "compute_ms": 99.0},
    }

    assert semantic_route_plan_digest(first) == semantic_route_plan_digest(second)


def test_output_directory_is_atomic_immutable_and_hashed(tmp_path) -> None:
    target = tmp_path / "run-output"
    published, checksums = publish_output_directory(
        target,
        {
            "nested/evidence.json": {
                "schema_version": "test.evidence.v1",
                "value": 1,
            },
            "run-report.json": {
                "schema_version": "orchestrator.run-report.v1",
                "status": "success",
            },
        },
    )

    assert published == target.resolve()
    assert set(checksums) == {"nested/evidence.json", "run-report.json"}
    manifest = json.loads((target / "checksums.json").read_text(encoding="utf-8"))
    assert manifest["files"] == checksums
    with pytest.raises(OrchestrationError, match="already exists"):
        publish_output_directory(target, {"new.json": {"value": 2}})


def test_output_directory_rejects_path_traversal(tmp_path) -> None:
    with pytest.raises(OrchestrationError, match="unsafe output path"):
        publish_output_directory(tmp_path / "run", {"../escape.json": {"value": 1}})
