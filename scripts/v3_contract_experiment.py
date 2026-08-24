"""Replay-local v3 main_corridor contract-edge experiment (no C changes).

Uses one real committed Scenario B risk window and C's own planner/grid/
sampler/vessel code.  Reproduces the current production cap
``layer_elapsed = min(request, 72h, full_end - start)`` and evaluates the
candidate ``layer_elapsed = min(request, 72h)`` for the main corridor when
the recommended full-voyage ETA is below 72h (anchor == destination).

Usage:
    python v3_contract_experiment.py --commit <full-window-commit-id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import RiskWindowQuery
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain import ObjectiveMode
from arctic_route_planning.endpoints import map_corridor_endpoints
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.risk import RiskSampler
from arctic_route_risk import PersistentRiskStore

from arctic_route_orchestrator.replay.route_integrity import audit_route


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _build(paths: dict[str, str], commit_id: str):
    commit_dir = Path(paths["risk_store_root"]) / "commits"
    document = json.loads(
        (commit_dir / f"{commit_id}.json").read_text(encoding="utf-8")
    )
    configuration = load_configuration(
        paths["c_config_root"],
        document["scenario_id"],
        shared_config_root=Path(paths["contracts_config_root"]),
    )
    query = RiskWindowQuery(
        start=_parse_utc(document["start"]),
        end=_parse_utc(document["end"]),
        interval=timedelta(hours=1),
        run_id=document["run_id"],
        scenario_id=document["scenario_id"],
        corridor_id=document["corridor_id"],
        generation_id=document["generation_id"],
        vessel_profile_id=document["vessel_profile_id"],
        config_digest=document["config_digest"],
        model_config_digest=document["model_config_digest"],
        as_of=_parse_utc(document["as_of"]),
    )
    store = PersistentRiskStore(paths["risk_store_root"])
    window = store.get_committed_window(query)
    frames = tuple(window.frames)
    sampler = RiskSampler(frames, max_frame_gap=timedelta(hours=1))
    grid = RegularGrid.from_risk_frame(
        frames[0],
        allow_diagonal=configuration.planner.connectivity == 8,
    )
    endpoints = map_corridor_endpoints(
        configuration,
        frames[0],
        max_adjustment_km=float(paths.get("max_snap_km", "30.0")),
    )
    planner = TimeDependentAStar(
        grid,
        sampler,
        VesselPerformanceModel.from_configuration(configuration.vessel_model),
        planner_config=configuration.planner,
    )
    return configuration, query, frames, planner, endpoints


def _plan_layer(
    planner: TimeDependentAStar,
    configuration,
    query: RiskWindowQuery,
    endpoints,
    *,
    goal,
    layer_elapsed: timedelta,
    departure_time: datetime,
):
    request = PlanningRequest(
        start=endpoints.start.node,
        goal=goal,
        departure_time=departure_time,
        time_bucket_size=timedelta(minutes=configuration.planner.time_bucket_minutes),
        edge_sample_count=configuration.planner.edge_sample_count,
        maximum_elapsed=layer_elapsed,
    )
    started = time.monotonic()
    results = planner.plan_candidates(request, tuple(ObjectiveMode))
    wall = time.monotonic() - started
    return results, wall


def _summary(results, wall: float) -> dict[str, object]:
    return {
        objective.value: {
            "eta_hours": result.travel_hours,
            "distance_km": result.distance_km,
            "expanded_states": result.metrics.expanded_states,
            "status": "PASS",
        }
        for objective, result in results.items()
    } | {"wall_seconds": round(wall, 1)}


def _route_namespace(result, start_time: datetime):
    waypoints = tuple(
        SimpleNamespace(
            longitude=step.longitude,
            latitude=step.latitude,
            eta=step.eta,
        )
        for step in result.steps
    )
    return SimpleNamespace(
        plan_id="experiment",
        waypoints=waypoints,
        metrics=SimpleNamespace(
            distance_km=result.distance_km,
            eta_hours=result.travel_hours,
            avg_risk=result.average_risk,
            max_risk=result.maximum_risk,
            integrated_risk_hours=result.average_risk * result.travel_hours,
            minimum_confidence=result.minimum_confidence,
            hard_constraint_violations=0,
            turn_count=0,
            expanded_nodes=result.metrics.expanded_states,
            objective_cost=result.total_cost_hours,
        ),
        start_time=start_time,
    )


def _integrity(results, frames, start_time) -> dict[str, object]:
    output: dict[str, object] = {}
    for objective, result in results.items():
        route = _route_namespace(result, start_time)
        audit = audit_route(route, frames)
        output[objective.value] = {
            "status": audit["status"],
            "land_intersections": audit["land_intersections"],
            "data_unavailable_violations": audit["data_unavailable_violations"],
            "hard_edge_violations": audit["edge_hard_violations"],
            "corner_cutting_violations": audit["corner_cutting_violations"],
        }
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="v3-contract-experiment")
    parser.add_argument(
        "--commit",
        default=(
            "risk-window-sha256-7b89a6e32621cc920e96f1f18e993739a2e2ff67"
            "cc723379a33b9a425e91af3f"
        ),
    )
    parser.add_argument(
        "--risk-store-root",
        default=str(
            _workspace_root() / "work_package_a" / "data" / "output" / "rc2-smoke"
            / "causal-replay-mvp" / "sb-c-12h4" / "risk-store"
        ),
    )
    parser.add_argument(
        "--c-config-root",
        default=str(_workspace_root() / "work_package_c" / "configs"),
    )
    parser.add_argument(
        "--contracts-config-root",
        default=str(_workspace_root() / "arctic_route_contracts" / "configs"),
    )
    parser.add_argument("--max-snap-km", type=float, default=30.0)
    args = parser.parse_args(argv)

    paths = {
        "risk_store_root": args.risk_store_root,
        "c_config_root": args.c_config_root,
        "contracts_config_root": args.contracts_config_root,
        "max_snap_km": str(args.max_snap_km),
    }
    configuration, query, frames, planner, endpoints = _build(paths, args.commit)
    report: dict[str, object] = {
        "commit_id": args.commit,
        "window_start": query.start.isoformat(),
        "window_end": query.end.isoformat(),
        "request_maximum_elapsed_hours": (query.end - query.start).total_seconds()
        / 3600.0,
    }
    departure = query.start

    print("[experiment] full voyage (3 objectives)", flush=True)
    full, full_wall = _plan_layer(
        planner,
        configuration,
        query,
        endpoints,
        goal=endpoints.goal.node,
        layer_elapsed=query.end - query.start,
        departure_time=departure,
    )
    full_end = full[ObjectiveMode.RECOMMENDED].steps[-1].eta
    report["full_voyage"] = _summary(full, full_wall)
    report["full_recommended_eta_hours"] = (
        full_end - departure
    ).total_seconds() / 3600.0
    report["full_voyage_integrity"] = _integrity(full, frames, departure)

    anchor_72 = full[ObjectiveMode.RECOMMENDED].steps[-1].node
    current_cap = min(
        query.end - query.start,
        timedelta(hours=72),
        full_end - departure,
    )
    candidate_cap = min(query.end - query.start, timedelta(hours=72))
    print("[experiment] main corridor current cap", flush=True)
    old_results = None
    try:
        old_results, old_wall = _plan_layer(
            planner,
            configuration,
            query,
            endpoints,
            goal=anchor_72,
            layer_elapsed=current_cap,
            departure_time=departure,
        )
        old_status = _summary(old_results, old_wall)
        old_error = None
    except Exception as exc:  # expected failure reproduction
        old_status = None
        old_error = f"{type(exc).__name__}: {exc}"
    print("[experiment] main corridor candidate cap (72h)", flush=True)
    new_results, new_wall = _plan_layer(
        planner,
        configuration,
        query,
        endpoints,
        goal=anchor_72,
        layer_elapsed=candidate_cap,
        departure_time=departure,
    )
    report["main_corridor"] = {
        "anchor_node": list(anchor_72),
        "anchor_is_destination": anchor_72 == endpoints.goal.node,
        "current_cap_hours": current_cap.total_seconds() / 3600.0,
        "candidate_cap_hours": candidate_cap.total_seconds() / 3600.0,
        "current_cap_status": old_status,
        "current_cap_error": old_error,
        "candidate_cap_status": _summary(new_results, new_wall),
        "candidate_cap_integrity": _integrity(new_results, frames, departure),
    }

    rolling_anchor = _anchor_at_or_before(
        full[ObjectiveMode.RECOMMENDED],
        departure + timedelta(hours=24),
    )
    rolling_elapsed = min(
        query.end - query.start,
        timedelta(hours=24),
        full_end - departure,
    )
    print("[experiment] rolling 0-24h", flush=True)
    rolling, rolling_wall = _plan_layer(
        planner,
        configuration,
        query,
        endpoints,
        goal=rolling_anchor,
        layer_elapsed=rolling_elapsed,
        departure_time=departure,
    )
    report["rolling_0_24h"] = _summary(rolling, rolling_wall)
    report["rolling_0_24h_integrity"] = _integrity(rolling, frames, departure)

    executable_anchor = _anchor_at_or_before(
        full[ObjectiveMode.RECOMMENDED],
        departure + timedelta(hours=6),
    )
    executable_elapsed = min(
        query.end - query.start,
        timedelta(hours=6),
        full_end - departure,
    )
    print("[experiment] executable 0-6h", flush=True)
    executable, executable_wall = _plan_layer(
        planner,
        configuration,
        query,
        endpoints,
        goal=executable_anchor,
        layer_elapsed=executable_elapsed,
        departure_time=departure,
    )
    report["executable_0_6h"] = _summary(executable, executable_wall)
    report["executable_0_6h_integrity"] = _integrity(executable, frames, departure)

    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _anchor_at_or_before(result, cutoff: datetime):
    non_start = tuple(
        step for step in result.steps[1:] if step.node != result.steps[0].node
    )
    if result.steps[-1].eta <= cutoff:
        return result.steps[-1].node
    eligible = tuple(step for step in non_start if step.eta <= cutoff)
    if not eligible:
        raise ValueError("no non-start waypoint at or before cutoff")
    return eligible[-1].node


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
