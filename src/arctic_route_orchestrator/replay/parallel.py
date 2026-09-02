"""Replay-local objective-level parallelism (Strategy B, controlled).

The only parallel unit is *one C planning request's three objectives*
(fastest / low_risk / recommended).  Replay ticks, layers and B builds stay
strictly serial because they carry causal dependencies.

Implementation: ``install()`` swaps one private C ingress method in the
running process so that the existing ``PlanningService`` /
``FourLayerPlanningService`` orchestration (coordinator, publication,
replan policy, switch gate) remains byte-for-byte the production path while
the CPU-heavy searches run in worker processes that rebuild the exact
committed risk window from the immutable store.  Frozen production paths in
other processes are untouched; the patch is restored on exit.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctic_route_planning.config import load_configuration
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain import ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import (
    PlanningRequest,
    PlanningResult,
    RouteStep,
    SearchMetrics,
    TimeDependentAStar,
)
from arctic_route_planning.risk import RiskSampler
from arctic_route_risk import PersistentRiskStore

_WORKER_COMPONENT_CACHE: dict[str, dict[str, Any]] = {}


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _child_result(
    paths: dict[str, str],
    commit_id: str,
    request: dict[str, Any],
    objective: str,
) -> dict[str, Any]:
    """Rebuild one exact committed window and solve one objective."""

    commit = json.loads(
        (
            Path(paths["risk_store_root"])
            / "commits"
            / f"{commit_id}.json"
        ).read_text(encoding="utf-8")
    )
    cache_key = (
        f"{commit_id}:{commit.get('config_digest', '')}:"
        f"{commit.get('model_config_digest', '')}:"
        f"{paths['c_config_root']}:{paths['contracts_config_root']}"
    )
    cached = _WORKER_COMPONENT_CACHE.get(cache_key)
    if cached is None:
        configuration = load_configuration(
            paths["c_config_root"],
            commit["scenario_id"],
            shared_config_root=Path(paths["contracts_config_root"]),
        )
        from arctic_route_planning.contracts import RiskWindowQuery

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
        sampler = RiskSampler(frames, max_frame_gap=timedelta(hours=1))
        grid = RegularGrid.from_risk_frame(
            frames[0],
            allow_diagonal=configuration.planner.connectivity == 8,
        )
        vessel_model = VesselPerformanceModel.from_configuration(
            configuration.vessel_model
        )
        cached = {
            "configuration": configuration,
            "query": query,
            "frames": frames,
            "sampler": sampler,
            "grid": grid,
            "vessel_model": vessel_model,
        }
        if len(_WORKER_COMPONENT_CACHE) >= 2:
            _WORKER_COMPONENT_CACHE.clear()
        _WORKER_COMPONENT_CACHE[cache_key] = cached
    configuration = cached["configuration"]
    frames = cached["frames"]
    sampler = cached["sampler"]
    grid = cached["grid"]
    planner = TimeDependentAStar(
        grid,
        sampler,
        cached["vessel_model"],
        planner_config=configuration.planner,
    )
    plan_request = PlanningRequest(
        start=tuple(request["start"]),
        goal=tuple(request["goal"]),
        departure_time=_parse_utc(request["departure_time"]),
        objective=ObjectiveMode(objective),
        time_bucket_size=timedelta(minutes=request["time_bucket_minutes"]),
        edge_sample_count=int(request["edge_sample_count"]),
        maximum_elapsed=(
            timedelta(seconds=request["maximum_elapsed_seconds"])
            if request.get("maximum_elapsed_seconds") is not None
            else None
        ),
        maximum_risk=request.get("maximum_risk"),
        max_expansions=int(request.get("max_expansions", 250_000)),
    )
    result = planner.plan_candidates(plan_request, (ObjectiveMode(objective),))[
        ObjectiveMode(objective)
    ]
    return {
        "objective": objective,
        "worker_pid": os.getpid(),
        "steps": [
            {
                "node": list(step.node),
                "longitude": step.longitude,
                "latitude": step.latitude,
                "eta": step.eta.isoformat(),
                "incoming_heading_degrees": step.incoming_heading_degrees,
                "recommended_speed_knots": step.recommended_speed_knots,
                "edge_distance_km": step.edge_distance_km,
                "edge_risk_score": step.edge_risk_score,
                "edge_maximum_risk": step.edge_maximum_risk,
                "edge_confidence": step.edge_confidence,
                "source_risk_ids": list(step.source_risk_ids),
            }
            for step in result.steps
        ],
        "total_cost_hours": result.total_cost_hours,
        "distance_km": result.distance_km,
        "travel_hours": result.travel_hours,
        "average_risk": result.average_risk,
        "maximum_risk": result.maximum_risk,
        "minimum_confidence": result.minimum_confidence,
        "source_risk_ids": list(result.source_risk_ids),
        "metrics": {
            "expanded_states": result.metrics.expanded_states,
            "generated_states": result.metrics.generated_states,
            "rejected_hard_edges": result.metrics.rejected_hard_edges,
            "rejected_risk_edges": result.metrics.rejected_risk_edges,
            "rejected_speed_edges": result.metrics.rejected_speed_edges,
            "rejected_coverage_edges": result.metrics.rejected_coverage_edges,
            "queue_peak": result.metrics.queue_peak,
            "compute_ms": result.metrics.compute_ms,
            "unique_states": result.metrics.unique_states,
            "heap_pushes": result.metrics.heap_pushes,
            "heap_pops": result.metrics.heap_pops,
            "stale_pops": result.metrics.stale_pops,
            "reopened_states": result.metrics.reopened_states,
            "max_time_index": result.metrics.max_time_index,
        },
    }


def _result_from_dict(document: dict[str, Any]) -> PlanningResult:
    steps = tuple(
        RouteStep(
            node=tuple(step["node"]),
            longitude=step["longitude"],
            latitude=step["latitude"],
            eta=_parse_utc(step["eta"]),
            incoming_heading_degrees=step["incoming_heading_degrees"],
            recommended_speed_knots=step["recommended_speed_knots"],
            edge_distance_km=step["edge_distance_km"],
            edge_risk_score=step["edge_risk_score"],
            edge_maximum_risk=step["edge_maximum_risk"],
            edge_confidence=step["edge_confidence"],
            edge_cost=None,
            source_risk_ids=tuple(step["source_risk_ids"]),
        )
        for step in document["steps"]
    )
    metrics = document["metrics"]
    return PlanningResult(
        objective=ObjectiveMode(document["objective"]),
        steps=steps,
        total_cost_hours=document["total_cost_hours"],
        distance_km=document["distance_km"],
        travel_hours=document["travel_hours"],
        average_risk=document["average_risk"],
        maximum_risk=document["maximum_risk"],
        minimum_confidence=document["minimum_confidence"],
        source_risk_ids=tuple(document["source_risk_ids"]),
        metrics=SearchMetrics(
            expanded_states=metrics["expanded_states"],
            generated_states=metrics["generated_states"],
            rejected_hard_edges=metrics["rejected_hard_edges"],
            rejected_risk_edges=metrics["rejected_risk_edges"],
            rejected_speed_edges=metrics["rejected_speed_edges"],
            rejected_coverage_edges=metrics["rejected_coverage_edges"],
            queue_peak=metrics["queue_peak"],
            compute_ms=metrics["compute_ms"],
            unique_states=metrics["unique_states"],
            heap_pushes=metrics["heap_pushes"],
            heap_pops=metrics["heap_pops"],
            stale_pops=metrics["stale_pops"],
            reopened_states=metrics["reopened_states"],
            max_time_index=metrics["max_time_index"],
        ),
    )


class _ParallelObjectivePlanner:
    """C-compatible planner facade whose searches run in worker processes."""

    def __init__(
        self,
        serial: TimeDependentAStar,
        *,
        commit_id: str,
        paths: dict[str, str],
        workers: int,
        timeout_seconds: int,
        pool_mode: str = "persistent",
    ) -> None:
        self.serial = serial
        self.commit_id = commit_id
        self.paths = paths
        self.workers = workers
        self.timeout_seconds = timeout_seconds
        self.pool_mode = pool_mode

    @property
    def risk_identity(self):
        return self.serial.risk_identity

    @property
    def risk_as_of_times(self):
        return self.serial.risk_as_of_times

    def plan_candidates(self, request: PlanningRequest, objectives: tuple):
        if self.workers <= 1 or len(objectives) <= 1:
            return self.serial.plan_candidates(request, objectives)
        request_fields = {
            "start": list(request.start),
            "goal": list(request.goal),
            "departure_time": request.departure_time.isoformat(),
            "time_bucket_minutes": int(request.time_bucket_size.total_seconds() / 60),
            "edge_sample_count": request.edge_sample_count,
            "maximum_elapsed_seconds": (
                int(request.maximum_elapsed.total_seconds())
                if request.maximum_elapsed is not None
                else None
            ),
            "maximum_risk": request.maximum_risk,
            "max_expansions": request.max_expansions,
        }
        ordered_objectives = tuple(objectives)
        telemetry = _active_telemetry
        telemetry["planning_calls"] = telemetry.get("planning_calls", 0) + 1
        telemetry["tasks_submitted"] = (
            telemetry.get("tasks_submitted", 0) + len(ordered_objectives)
        )
        telemetry["max_parallel_tasks"] = max(
            telemetry.get("max_parallel_tasks", 0),
            min(self.workers, len(ordered_objectives)),
        )
        active_executor = _active_executor
        if active_executor is not None and self.pool_mode == "persistent":
            executor = active_executor
            futures = [
                executor.submit(
                    _child_result,
                    self.paths,
                    self.commit_id,
                    request_fields,
                    objective.value,
                )
                for objective in ordered_objectives
            ]
            documents = [
                future.result(timeout=self.timeout_seconds)
                for future in futures
            ]
        else:
            with ProcessPoolExecutor(
                max_workers=min(self.workers, len(ordered_objectives))
            ) as executor:
                futures = [
                    executor.submit(
                        _child_result,
                        self.paths,
                        self.commit_id,
                        request_fields,
                        objective.value,
                    )
                    for objective in ordered_objectives
                ]
                documents = [
                    future.result(timeout=self.timeout_seconds)
                    for future in futures
                ]
        telemetry["worker_pids"].update(
            int(document["worker_pid"])
            for document in documents
            if document.get("worker_pid") is not None
        )
        results = {
            ObjectiveMode(document["objective"]): _result_from_dict(document)
            for document in documents
        }
        if set(results) != set(ordered_objectives):
            raise RuntimeError("parallel planner returned a mismatched objective set")
        return {objective: results[objective] for objective in ordered_objectives}


_active_paths: dict[str, str] | None = None
_active_workers = 1
_active_timeout = 900
_active_executor: ProcessPoolExecutor | None = None
_active_pool_mode = "persistent"
_active_telemetry: dict[str, Any] = {}


def _reset_telemetry(*, workers: int, pool_mode: str) -> None:
    global _active_telemetry
    _active_telemetry = {
        "enabled": workers > 1,
        "requested_workers": workers,
        "pool_mode": pool_mode,
        "tasks_submitted": 0,
        "planning_calls": 0,
        "max_parallel_tasks": 0,
        "worker_pids": set(),
    }


def snapshot_telemetry() -> dict[str, Any]:
    """Return JSON-safe parent-process telemetry for the active install."""

    result = dict(_active_telemetry)
    result["worker_pids"] = sorted(result.get("worker_pids", set()))
    result["effective_workers"] = len(result["worker_pids"])
    result["parallel_active"] = bool(
        result.get("enabled") and result["effective_workers"] > 0
    )
    return result


def _validate_install_options(workers: int, pool_mode: str) -> int:
    if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
        raise ValueError("planning workers must be a positive integer")
    if pool_mode not in {"persistent", "percall"}:
        raise ValueError("parallel pool mode must be persistent or percall")
    available = os.cpu_count() or 1
    if workers > available:
        raise RuntimeError(
            f"requested {workers} planning workers but only {available} CPUs are available"
        )
    return workers


@contextlib.contextmanager
def install(
    *,
    workers: int,
    risk_store_root: str | Path,
    c_config_root: str | Path,
    contracts_config_root: str | Path,
    max_snap_km: float = 30.0,
    timeout_seconds: int = 900,
    pool_mode: str = "persistent",
) -> Iterator[None]:
    """Temporarily route C objective searches through worker processes."""

    global _active_paths, _active_workers, _active_timeout
    global _active_executor, _active_pool_mode, _active_telemetry
    from arctic_route_planning.ingress import PreparedRiskPlanning

    worker_count = _validate_install_options(workers, pool_mode)

    paths = {
        "risk_store_root": str(risk_store_root),
        "c_config_root": str(c_config_root),
        "contracts_config_root": str(contracts_config_root),
        "max_snap_km": str(max_snap_km),
    }
    previous_private_planner = PreparedRiskPlanning._private_planner
    previous_paths = _active_paths
    previous_workers = _active_workers
    previous_timeout = _active_timeout
    previous_executor = _active_executor
    previous_pool_mode = _active_pool_mode
    previous_telemetry = _active_telemetry

    def _parallel_private_planner(self, current):
        serial = previous_private_planner(self, current)
        return _ParallelObjectivePlanner(
            serial,
            commit_id=current.commit_id,
            paths=dict(_active_paths or paths),
            workers=_active_workers,
            timeout_seconds=_active_timeout,
            pool_mode=_active_pool_mode,
        )

    _active_paths = paths
    _active_workers = worker_count
    _active_timeout = timeout_seconds
    _active_pool_mode = pool_mode
    _reset_telemetry(workers=worker_count, pool_mode=pool_mode)
    PreparedRiskPlanning._private_planner = _parallel_private_planner
    executor = None
    try:
        if pool_mode == "persistent":
            executor = ProcessPoolExecutor(max_workers=worker_count)
            _active_executor = executor
        yield
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
        PreparedRiskPlanning._private_planner = previous_private_planner
        _active_paths = previous_paths
        _active_workers = previous_workers
        _active_timeout = previous_timeout
        _active_executor = previous_executor
        _active_pool_mode = previous_pool_mode
        # Keep the just-completed telemetry available to the caller.  Nested
        # installs restore their parent's snapshot only after the caller has
        # had a chance to read it.
        if previous_telemetry:
            _active_telemetry = previous_telemetry
