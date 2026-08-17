"""Equal-work serial vs two-worker objective benchmark (RC2 performance).

Uses one committed real risk window and two independent planner objectives
(fastest, low_risk).  ``serial`` runs both objectives in one process;
``parallel`` runs the same two objectives in two child processes.

Usage:
    python bench_equal_work_objectives.py serial <paths.json>
    python bench_equal_work_objectives.py parallel <paths.json>
    python bench_equal_work_objectives.py child <paths.json> <objective>
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
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


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _paths(argv: list[str]) -> dict[str, str]:
    raw = argv[0]
    try:
        return json.loads(raw)
    except ValueError:
        pass
    return json.loads(Path(raw).read_text(encoding="utf-8"))


def _business_summary(result) -> dict[str, object]:
    nodes = result.nodes
    return {
        "objective": result.objective.value,
        "distance_km": result.distance_km,
        "eta_hours": result.travel_hours,
        "avg_risk": result.average_risk,
        "max_risk": result.maximum_risk,
        "objective_cost": result.total_cost_hours,
        "expanded_states": result.metrics.expanded_states,
        "node_count": len(nodes),
        "first_node": list(nodes[0]),
        "last_node": list(nodes[-1]),
    }


def _run_objective(paths: dict[str, str], objective: ObjectiveMode) -> dict[str, object]:
    commit = json.loads(
        (Path(paths["commit_dir"]) / "risk" / "full-window-commit.json").read_text(
            encoding="utf-8"
        )
    )
    configuration = load_configuration(
        paths["c_config_root"],
        commit["scenario_id"],
        shared_config_root=Path(paths["contracts_config_root"]),
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
    private_frames = tuple(window.frames)
    sampler = RiskSampler(private_frames, max_frame_gap=timedelta(hours=1))
    grid = RegularGrid.from_risk_frame(
        private_frames[0],
        allow_diagonal=configuration.planner.connectivity == 8,
    )
    endpoint_mapping = map_corridor_endpoints(
        configuration,
        private_frames[0],
        max_adjustment_km=150.0,
    )
    planner = TimeDependentAStar(
        grid,
        sampler,
        VesselPerformanceModel.from_configuration(configuration.vessel_model),
        planner_config=configuration.planner,
    )
    request = PlanningRequest(
        start=endpoint_mapping.start.node,
        goal=endpoint_mapping.goal.node,
        departure_time=query.start,
        time_bucket_size=timedelta(minutes=configuration.planner.time_bucket_minutes),
        edge_sample_count=configuration.planner.edge_sample_count,
        maximum_elapsed=query.end - query.start,
    )
    started = time.monotonic()
    results = planner.plan_candidates(request, (objective,))
    wall = time.monotonic() - started
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "objective": objective.value,
        "wall_seconds": wall,
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss * 1024,
        "business": _business_summary(results[objective]),
    }


def _serial(paths: dict[str, str]) -> dict[str, object]:
    started = time.monotonic()
    fastest = _run_objective(paths, ObjectiveMode.FASTEST)
    low_risk = _run_objective(paths, ObjectiveMode.LOW_RISK)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "mode": "serial",
        "wall_seconds": time.monotonic() - started,
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss * 1024,
        "results": [fastest, low_risk],
    }


def _parallel(paths: dict[str, str]) -> dict[str, object]:
    script = Path(__file__).resolve()
    commands = []
    for objective in ("fastest", "low_risk"):
        commands.append(
            [
                sys.executable,
                str(script),
                "child",
                json.dumps(paths, sort_keys=True),
                objective,
            ]
        )
    started = time.monotonic()
    procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) for cmd in commands]
    outputs: list[dict[str, object]] = []
    for proc in procs:
        stdout, _ = proc.communicate()
        outputs.append(json.loads(stdout))
    wall = time.monotonic() - started
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "mode": "parallel",
        "wall_seconds": wall,
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss * 1024,
        "children": outputs,
    }


def _prototype(paths: dict[str, str]) -> dict[str, object]:
    """Explicit bounded two-worker prototype inside one parent process."""

    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_run_objective, paths, ObjectiveMode.FASTEST),
            executor.submit(_run_objective, paths, ObjectiveMode.LOW_RISK),
        ]
        outputs = [future.result() for future in futures]
    wall = time.monotonic() - started
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "mode": "prototype",
        "wall_seconds": wall,
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss * 1024,
        "children": outputs,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) < 2:
        print(
            "usage: bench_equal_work_objectives "
            "<serial|parallel|prototype|child> <paths.json> [objective]"
        )
        return 2
    mode, paths_json = args[0], args[1]
    paths = _paths([paths_json])
    if mode == "serial":
        print(json.dumps(_serial(paths), sort_keys=True))
        return 0
    if mode == "parallel":
        print(json.dumps(_parallel(paths), sort_keys=True))
        return 0
    if mode == "prototype":
        print(json.dumps(_prototype(paths), sort_keys=True))
        return 0
    if mode == "child":
        objective = ObjectiveMode(args[2])
        print(json.dumps(_run_objective(paths, objective), sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
