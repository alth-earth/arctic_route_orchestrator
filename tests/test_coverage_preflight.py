"""Planning coverage preflight: gate semantics and reason accounting."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from arctic_route_planning.adapters import FixtureRiskSource
from arctic_route_planning.config import load_configuration
from arctic_route_planning.development import create_development_run_context

from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.service import _coverage_preflight

ORCHESTRATOR_ROOT = Path(__file__).parents[1]
WORKSPACE = ORCHESTRATOR_ROOT.parent
C_CONFIG_ROOT = WORKSPACE / "work_package_c" / "configs"


def _spec() -> ExecutionSpec:
    return ExecutionSpec(
        schema_version="orchestrator.execution-spec.v1",
        run_id="run-00000000-0000-4000-8000-000000000042",
        scenario_id="tromso_isfjorden_july_2026_retrospective_v1",
        generation_id=0,
        input_revision=0,
        generated_at=datetime(2026, 8, 17, 6, 0, tzinfo=UTC),
        planning_contract="cd.four-layer-route-plan-set.v3",
    )


def _frames():
    configuration = load_configuration(
        C_CONFIG_ROOT,
        "tromso_isfjorden_july_2026_retrospective_v1",
    )
    run_context = create_development_run_context(
        configuration,
        source_kind="synthetic",
    )
    source = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        frame_count=2,
        shape=(3, 4),
    )
    frames = list(source.frames)
    payload = frames[0].payload.copy(deep=True)
    hard = np.asarray(payload["hard_mask"].values)
    reasons = np.where(hard, "LAND", "NONE").astype("U32")
    payload["hard_reason"] = (("latitude", "longitude"), reasons)
    payload.attrs["missing_input_variable_counts"] = {
        "ocean_current_u": int(np.count_nonzero(hard))
    }
    from dataclasses import replace

    frames = [replace(frames[0], payload=payload), frames[1]]
    return configuration, run_context, frames


def test_preflight_passes_and_reports_reason_accounting() -> None:
    configuration, run_context, frames = _frames()

    document = _coverage_preflight(
        spec=_spec(),
        frames=frames,
        expected_count=2,
        run_context=run_context,
    )

    assert document["schema_version"] == "orchestrator.planning-coverage-preflight.v1"
    assert document["frames_expected"] == 2
    assert document["frames_checked"] == 2
    assert document["gate_passed"] is True
    assert document["corridor_id"] == configuration.corridor.corridor_id
    first = document["frames"][0]
    assert first["unknown_navigable_nodes"] == 0
    assert first["land_nodes"] == first["hard_nodes"]
    assert first["data_unavailable_nodes"] == 0
    assert first["missing_input_variable_counts"]["ocean_current_u"] == first["hard_nodes"]


def test_preflight_fails_when_navigable_node_has_unknown_risk() -> None:
    _, run_context, frames = _frames()
    payload = frames[0].payload.copy(deep=True)
    risk = np.asarray(payload["risk_score"].values).copy()
    level = np.asarray(payload["risk_level"].values).copy()
    hard = np.asarray(payload["hard_mask"].values).copy()
    confidence = np.asarray(payload["confidence"].values).copy()
    reason = np.asarray(payload["hard_reason"].values).copy()
    risk[0, 0] = np.nan
    level[0, 0] = 5
    hard[0, 0] = False
    confidence[0, 0] = 0.0
    reason[0, 0] = "NONE"
    payload["risk_score"] = (("latitude", "longitude"), risk)
    payload["risk_level"] = (("latitude", "longitude"), level)
    payload["hard_mask"] = (("latitude", "longitude"), hard)
    payload["confidence"] = (("latitude", "longitude"), confidence)
    payload["hard_reason"] = (("latitude", "longitude"), reason)
    from dataclasses import replace

    frames = [replace(frames[0], payload=payload), frames[1]]

    with pytest.raises(OrchestrationError, match="coverage_preflight_failed"):
        _coverage_preflight(
            spec=_spec(),
            frames=frames,
            expected_count=2,
            run_context=run_context,
        )
