from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import arctic_route_planning
import pytest
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import ProvenanceKind
from arctic_route_planning.development import create_development_run_context
from arctic_route_planning.domain import ObjectiveMode
from arctic_route_planning.layered import FourLayerPlanningService
from arctic_route_planning.planners import PlanningResult, RouteStep, SearchMetrics
from arctic_route_planning.publishing import LayeredRoutePlanLatestStore
from arctic_route_planning.replanning import (
    PlanningCoordinator,
    ReplanningPolicy,
    ReplanTriggerEvaluator,
    RouteSwitchGate,
)
from arctic_route_planning.service import ServicePlanningRequest

from arctic_route_orchestrator.route_presentation import (
    load_route_candidates,
    project_route_candidates,
    validate_route_candidates,
)

CONFIG_ROOT = Path(arctic_route_planning.__file__).resolve().parents[2] / "configs"


class _FixturePlanner:
    def plan_candidates(self, request, objectives):
        goal_column = request.goal[1]
        hours = (
            (0, 3, 12, 30, 60, 90)
            if goal_column == 5
            else (0, 3, 12, 30, 60)[: goal_column + 1]
        )
        return {
            objective: _result(request.departure_time, hours, objective)
            for objective in objectives
        }


def _plan_set():
    configuration = load_configuration(
        CONFIG_ROOT,
        "tromso_isfjorden_july_2026_retrospective_v1",
    )
    run_context = create_development_run_context(configuration, source_kind="synthetic")
    policy = ReplanningPolicy.from_config(configuration.replanning)
    service = FourLayerPlanningService(
        _FixturePlanner(),
        planner_config=configuration.planner,
        coordinator=PlanningCoordinator(request_id_factory=lambda: "projection-request"),
        store=LayeredRoutePlanLatestStore(),
        switch_gate=RouteSwitchGate(policy),
        trigger_evaluator=ReplanTriggerEvaluator(policy),
        clock=lambda: configuration.scenario.simulation_start,
    )
    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest="1" * 64,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.SYNTHETIC,
        generation_id=0,
        input_revision=0,
        as_of_time=configuration.scenario.simulation_start,
        start_time=configuration.scenario.simulation_start,
        start=(0, 0),
        goal=(0, 5),
        maximum_elapsed=timedelta(hours=96),
    )
    return service.execute(request).plan_set


def _result(start_time, hours, objective: ObjectiveMode) -> PlanningResult:
    steps = tuple(
        RouteStep(
            node=(0, index),
            longitude=float(index),
            latitude=70.0,
            eta=start_time + timedelta(hours=hour),
            incoming_heading_degrees=None if index == 0 else 90.0,
            recommended_speed_knots=None if index == 0 else 10.0,
            edge_distance_km=0.0 if index == 0 else 10.0,
            edge_risk_score=0.0 if index == 0 else 0.2,
            edge_maximum_risk=0.0 if index == 0 else 0.3,
            edge_confidence=1.0,
            edge_cost=None,
            source_risk_ids=(f"risk-{index}",),
        )
        for index, hour in enumerate(hours)
    )
    return PlanningResult(
        objective=objective,
        steps=steps,
        total_cost_hours=float(hours[-1]) + list(ObjectiveMode).index(objective),
        distance_km=10.0 * (len(steps) - 1),
        travel_hours=float(hours[-1]),
        average_risk=0.2,
        maximum_risk=0.3,
        minimum_confidence=1.0,
        source_risk_ids=tuple(f"risk-{index}" for index in range(len(steps))),
        metrics=SearchMetrics(
            expanded_states=10,
            generated_states=12,
            rejected_hard_edges=0,
            rejected_risk_edges=0,
            rejected_speed_edges=0,
            rejected_coverage_edges=0,
            queue_peak=4,
            compute_ms=1.0,
        ),
    )


def test_projection_preserves_all_c_owned_routes_and_selected_identity(tmp_path: Path) -> None:
    plan_set = _plan_set()

    document = project_route_candidates(plan_set)

    assert document["status"] == "PUBLISHED"
    assert document["layer_set_id"] == plan_set.layer_set_id
    assert document["selected_candidate_id"] == plan_set.recommended.plan_id
    assert len(document["candidates"]) == 12
    source_plans = {
        plan.plan_id: plan
        for bundle in plan_set.layers
        for plan in bundle.plans.values()
    }
    for candidate in document["candidates"]:
        source = source_plans[candidate["candidate_id"]]
        assert candidate["distance_km"] == source.metrics.distance_km
        assert candidate["risk_metrics"]["average_risk"] == source.metrics.avg_risk
        assert candidate["geometry"]["coordinates"] == [
            [waypoint.longitude, waypoint.latitude] for waypoint in source.waypoints
        ]

    path = tmp_path / "route-candidates.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_route_candidates(path) == document


def test_published_package_fails_closed_on_partial_candidate_set() -> None:
    document = project_route_candidates(_plan_set())
    document["candidates"].pop()

    with pytest.raises(ValueError, match="exactly 12"):
        validate_route_candidates(document)
