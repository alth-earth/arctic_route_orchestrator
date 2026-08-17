"""Real small-window replanning worker for Live Demo (one recommended route).

Reads a committed frozen risk window and runs the real C planner for the
recommended objective starting six hours into the window.  Output is a
``d.live-result.v1`` document marked LIVE_COMPUTED.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

from arctic_route_planning import map_corridor_endpoints
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import RiskWindowQuery
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain import ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.risk import RiskSampler
from arctic_route_risk import PersistentRiskStore


def _parse_utc(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 2:
        print("usage: demo_live_worker <paths.json> <result.json>", file=sys.stderr)
        return 2
    paths = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    result_path = Path(args[1])

    commit = json.loads(
        (Path(paths["commit_dir"]) / "risk" / "full-window-commit.json").read_text(
            encoding="utf-8"
        )
    )
    query = RiskWindowQuery(
        start=_parse_utc(commit["start"]),
        end=_parse_utc(commit["end"]),
        interval=timedelta(hours=1),
        run_id=commit["run_id"],
        scenario_id=commit["scenario_id"],
        corridor_id=commit["corridor_id"],
        generation_id=commit["generation_id"],
        vessel_profile_id=commit["vessel_profile_id"],
        config_digest=commit["config_digest"],
        model_config_digest=commit["model_config_digest"],
        as_of=_parse_utc(commit["as_of"]),
    )
    store = PersistentRiskStore(paths["risk_store_root"])
    window = store.get_committed_window(query)
    frames = tuple(window.frames)
    configuration = load_configuration(
        paths["c_config_root"],
        commit["scenario_id"],
        shared_config_root=Path(paths["contracts_config_root"]),
    )
    sampler = RiskSampler(frames, max_frame_gap=timedelta(hours=1))
    grid = RegularGrid.from_risk_frame(
        frames[0],
        allow_diagonal=configuration.planner.connectivity == 8,
    )
    endpoint_mapping = map_corridor_endpoints(
        configuration,
        frames[0],
        max_adjustment_km=150.0,
    )
    planner = TimeDependentAStar(
        grid,
        sampler,
        VesselPerformanceModel.from_configuration(configuration.vessel_model),
        planner_config=configuration.planner,
    )
    departure = query.start + timedelta(hours=6)
    request = PlanningRequest(
        start=endpoint_mapping.start.node,
        goal=endpoint_mapping.goal.node,
        departure_time=departure,
        time_bucket_size=timedelta(minutes=configuration.planner.time_bucket_minutes),
        edge_sample_count=configuration.planner.edge_sample_count,
        maximum_elapsed=query.end - departure,
    )
    print(
        json.dumps({"event": "stage_start", "stage": "live_replan"}, sort_keys=True),
        flush=True,
    )
    started = time.monotonic()
    result = planner.plan(request)
    wall = time.monotonic() - started

    steps = result.steps
    waypoints = [
        {
            "longitude": step.longitude,
            "latitude": step.latitude,
            "eta": step.eta.isoformat().replace("+00:00", "Z"),
        }
        for step in steps
    ]
    edge_hours = [
        step.edge_distance_km / max(step.recommended_speed_knots * 1.852, 1e-9)
        for step in steps[1:]
    ]
    integrated_risk = sum(
        step.edge_risk_score * hours
        for step, hours in zip(steps[1:], edge_hours, strict=True)
    )
    turn_count = sum(
        1
        for lower, upper in pairwise(steps)
        if lower.incoming_heading_degrees is not None
        and upper.incoming_heading_degrees is not None
        and abs(upper.incoming_heading_degrees - lower.incoming_heading_degrees) > 1e-6
    )
    document = {
        "schema_version": "d.live-result.v1",
        "result_origin": "LIVE_COMPUTED",
        "scenario_id": commit["scenario_id"],
        "corridor_id": commit["corridor_id"],
        "corridor_version": paths.get("corridor_version", "1.2.0"),
        "run_id": commit["run_id"],
        "departure_time": departure.isoformat().replace("+00:00", "Z"),
        "wall_seconds": wall,
        "objective": ObjectiveMode.RECOMMENDED.value,
        "plan_kind": "replanned",
        "waypoints": waypoints,
        "metrics": {
            "distance_km": result.distance_km,
            "eta_hours": result.travel_hours,
            "avg_risk": result.average_risk,
            "max_risk": result.maximum_risk,
            "integrated_risk_hours": integrated_risk,
            "minimum_confidence": min(
                (step.edge_confidence for step in steps[1:]), default=0.0
            ),
            "hard_constraint_violations": 0,
            "turn_count": turn_count,
            "objective_cost": result.total_cost_hours,
            "expanded_states": result.metrics.expanded_states,
        },
        "notes": [
            "LIVE small-window replanning: recommended objective, +6h departure, "
            "real C planner on frozen committed risk window.",
            "Integrated risk is an edge-sum approximation for display.",
        ],
    }
    print(
        json.dumps(
            {"event": "stage_done", "stage": "live_replan", "duration_seconds": wall},
            sort_keys=True,
        ),
        flush=True,
    )
    result_path.write_text(
        json.dumps({"ok": True, "live_result": document}, sort_keys=True),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
