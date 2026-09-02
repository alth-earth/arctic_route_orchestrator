"""Formal A -> B -> C execution through public package boundaries only."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import arctic_route_contracts
import arctic_route_data
import arctic_route_planning
import arctic_route_risk
import numpy as np

try:
    import resource
except ImportError:  # pragma: no cover - resource is unavailable on Windows.
    resource = None

from arctic_route_contracts import run_context_to_dict
from arctic_route_planning import (
    RiskSourcePlanningIngress,
    ServicePlanningRequest,
    map_corridor_endpoints,
)
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import ProvenanceKind, risk_frame_content_digest
from arctic_route_planning.publishing import (
    four_layer_route_plan_set_semantic_digest,
    four_layer_route_plan_set_to_dict,
    four_layer_route_plan_set_to_geojson,
    route_plan_to_dict,
    route_plan_to_geojson,
)
from arctic_route_planning.replanning import ReplanObservation
from arctic_route_risk import (
    BInputEnvelope,
    PersistentRiskStore,
    RiskBuildRequest,
    RiskBuildService,
    load_risk_build_configuration,
)

from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.intake import ArtifactIntake
from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.output import (
    publish_output_directory,
    semantic_route_plan_digest,
)


@dataclass(frozen=True, slots=True)
class RunPaths:
    bundle_path: Path
    a_data_root: Path
    b_config_path: Path
    c_config_root: Path
    risk_store_root: Path
    output_dir: Path
    run_context_path: Path | None = None
    contracts_config_root: Path | None = None


@dataclass(frozen=True, slots=True)
class FormalRunResult:
    output_dir: Path
    report: dict[str, Any]
    checksums: dict[str, str]


def execute_formal_run(
    spec: ExecutionSpec,
    paths: RunPaths,
    *,
    heartbeat: Callable[[dict[str, Any]], None] | None = None,
) -> FormalRunResult:
    """Execute one immutable v2 or v3 run and its six-hour suffix replan."""

    stage = "initialization"
    if heartbeat is not None:
        heartbeat({"event": "stage_start", "stage": stage})
    started = time.perf_counter()
    timings: dict[str, float] = {}
    stage_records: list[dict[str, Any]] = []
    stage_started_at = datetime.now(UTC)
    stage_report_path = paths.output_dir / "run-stage-report.json"
    unsubscribe = None
    try:
        stage_started = time.perf_counter()
        stage_started_at = datetime.now(UTC)
        configuration = load_configuration(
            paths.c_config_root,
            spec.scenario_id,
            shared_config_root=paths.contracts_config_root,
        )
        intake = ArtifactIntake.validate(
            bundle_path=paths.bundle_path,
            run_context_path=paths.run_context_path,
            a_data_root=paths.a_data_root,
            generation_id=spec.generation_id,
            scenario_id=spec.scenario_id,
            run_id=spec.run_id,
            created_at=(spec.generated_at if paths.run_context_path is None else None),
            contracts_config_root=paths.contracts_config_root,
        )
        _require_configuration_identity(configuration, intake.run_context)
        prepared_as_of_time = intake.prepared_window.as_of_time
        dataset_bundle_document = intake.prepared_window.dataset_bundle.to_dict()
        timings["configuration_and_a_intake_seconds"] = time.perf_counter() - stage_started
        _append_stage_record(
            stage_records=stage_records,
            spec=spec,
            stage=stage,
            started_at=stage_started_at,
            duration_seconds=timings["configuration_and_a_intake_seconds"],
            status="completed",
        )
        if heartbeat is not None:
            heartbeat(
                {
                    "event": "stage_done",
                    "stage": stage,
                    "duration_seconds": timings["configuration_and_a_intake_seconds"],
                }
            )
        _check_stage_timeout(
            spec, timings["configuration_and_a_intake_seconds"], stage,
        )

        stage = "b_build"
        if heartbeat is not None:
            heartbeat({"event": "stage_start", "stage": stage})
        stage_started = time.perf_counter()
        stage_started_at = datetime.now(UTC)
        risk_configuration = load_risk_build_configuration(paths.b_config_path)
        envelope = BInputEnvelope.from_prepared_window(
            run_context=intake.run_context,
            prepared_window=intake.prepared_window,
            generation_id=spec.generation_id,
            knowledge_as_of=prepared_as_of_time,
        )
        bbox = configuration.corridor.data_bbox
        build_request = RiskBuildRequest(
            envelope=envelope,
            target_bbox=(bbox.west, bbox.south, bbox.east, bbox.north),
            grid_config=risk_configuration.grid_config,
            model_config=risk_configuration.model_config,
        )
        model_config_digest = build_request.model_config_digest
        frames = RiskBuildService(utc_now=lambda: spec.generated_at).build_window(
            build_request
        )
        expected_count = configuration.scenario.horizon_hours + 1
        if len(frames) != expected_count:
            raise OrchestrationError(
                "b_frame_count_mismatch",
                f"expected {expected_count} inclusive hourly frames, got {len(frames)}",
            )
        store = PersistentRiskStore(paths.risk_store_root)
        unsubscribe = store.bind_generation_authority(spec.run_id, intake.clock)
        full_commit = store.publish_window(frames)
        timings["b_build_and_full_commit_seconds"] = time.perf_counter() - stage_started
        _append_stage_record(
            stage_records=stage_records,
            spec=spec,
            stage=stage,
            started_at=stage_started_at,
            duration_seconds=timings["b_build_and_full_commit_seconds"],
            status="completed",
        )
        if heartbeat is not None:
            heartbeat(
                {
                    "event": "stage_done",
                    "stage": stage,
                    "duration_seconds": timings["b_build_and_full_commit_seconds"],
                }
            )
        _check_stage_timeout(
            spec, timings["b_build_and_full_commit_seconds"], stage,
        )
        del envelope, build_request

        stage = "coverage_preflight"
        if heartbeat is not None:
            heartbeat({"event": "stage_start", "stage": stage})
        stage_started = time.perf_counter()
        stage_started_at = datetime.now(UTC)
        coverage_preflight = _coverage_preflight(
            spec=spec,
            frames=frames,
            expected_count=expected_count,
            run_context=intake.run_context,
        )
        timings["coverage_preflight_seconds"] = time.perf_counter() - stage_started
        _append_stage_record(
            stage_records=stage_records,
            spec=spec,
            stage=stage,
            started_at=stage_started_at,
            duration_seconds=timings["coverage_preflight_seconds"],
            status="completed",
        )
        if heartbeat is not None:
            heartbeat(
                {
                    "event": "stage_done",
                    "stage": stage,
                    "duration_seconds": timings["coverage_preflight_seconds"],
                }
            )
        _check_stage_timeout(spec, timings["coverage_preflight_seconds"], stage)

        stage = "endpoint_mapping"
        if heartbeat is not None:
            heartbeat({"event": "stage_start", "stage": stage})
        stage_started = time.perf_counter()
        stage_started_at = datetime.now(UTC)
        endpoint_mapping = map_corridor_endpoints(
            configuration,
            frames[0],
            max_adjustment_km=spec.max_snap_km,
        )
        timings["endpoint_mapping_seconds"] = time.perf_counter() - stage_started
        _append_stage_record(
            stage_records=stage_records,
            spec=spec,
            stage=stage,
            started_at=stage_started_at,
            duration_seconds=timings["endpoint_mapping_seconds"],
            status="completed",
        )
        if heartbeat is not None:
            heartbeat(
                {
                    "event": "stage_done",
                    "stage": stage,
                    "duration_seconds": timings["endpoint_mapping_seconds"],
                }
            )
        _check_stage_timeout(spec, timings["endpoint_mapping_seconds"], stage)
        # B is complete and only the compact risk store is needed downstream;
        # release the large A-frame references (PreparedWindow + envelope)
        # before C planning to avoid retaining gigabytes during the search.
        intake = replace(intake, prepared_window=None)

        initial_request = _planning_request(
            configuration=configuration,
            run_context=intake.run_context,
            model_config_digest=model_config_digest,
            generation_id=spec.generation_id,
            input_revision=spec.input_revision,
            as_of_time=prepared_as_of_time,
            start_time=intake.run_context.simulation_start,
            start=endpoint_mapping.start.node,
            goal=endpoint_mapping.goal.node,
        )
        ingress = RiskSourcePlanningIngress(store, configuration=configuration)

        stage = "c_initial_planning"
        if heartbeat is not None:
            heartbeat({"event": "stage_start", "stage": stage})
        stage_started = time.perf_counter()
        initial, initial_plan, plan_documents, initial_parallel = _execute_with_parallel(
            spec=spec,
            paths=paths,
            operation=lambda: _execute_initial(
                ingress=ingress,
                request=initial_request,
                planning_contract=spec.planning_contract,
            ),
        )
        timings["c_initial_planning_seconds"] = time.perf_counter() - stage_started
        _append_stage_record(
            stage_records=stage_records,
            spec=spec,
            stage=stage,
            started_at=stage_started_at,
            duration_seconds=timings["c_initial_planning_seconds"],
            status="completed",
        )
        if heartbeat is not None:
            heartbeat(
                {
                    "event": "stage_done",
                    "stage": stage,
                    "duration_seconds": timings["c_initial_planning_seconds"],
                }
            )
        _check_stage_timeout(
            spec, timings["c_initial_planning_seconds"], stage,
        )
        _require_planning_traceability(
            spec.planning_contract,
            initial,
            full_commit,
        )

        stage = "b_suffix_commit"
        if heartbeat is not None:
            heartbeat({"event": "stage_start", "stage": stage})
        stage_started = time.perf_counter()
        replan_time = intake.run_context.simulation_start + timedelta(
            hours=spec.replan_after_hours
        )
        if replan_time >= intake.run_context.simulation_end:
            raise OrchestrationError(
                "replan_time_invalid", "replan time must remain inside the RunContext"
            )
        _advance_clock_without_seek(intake.clock, replan_time)
        suffix_commit = store.publish_suffix_window(frames, start=replan_time)
        current_node, current_waypoint = _current_route_node(
            initial_plan,
            replan_time=replan_time,
            frame=frames[0],
        )
        if current_node == endpoint_mapping.goal.node:
            raise OrchestrationError(
                "replan_not_materializable", "voyage reached the goal before the 6 h trigger"
        )
        timings["b_suffix_commit_seconds"] = time.perf_counter() - stage_started
        _append_stage_record(
            stage_records=stage_records,
            spec=spec,
            stage=stage,
            started_at=stage_started_at,
            duration_seconds=timings["b_suffix_commit_seconds"],
            status="completed",
        )
        if heartbeat is not None:
            heartbeat(
                {
                    "event": "stage_done",
                    "stage": stage,
                    "duration_seconds": timings["b_suffix_commit_seconds"],
                }
            )
        _check_stage_timeout(
            spec, timings["b_suffix_commit_seconds"], stage,
        )

        replan_request = _planning_request(
            configuration=configuration,
            run_context=intake.run_context,
            model_config_digest=model_config_digest,
            generation_id=spec.generation_id,
            input_revision=spec.input_revision + 1,
            as_of_time=prepared_as_of_time,
            start_time=replan_time,
            start=current_node,
            goal=endpoint_mapping.goal.node,
        )
        observation = ReplanObservation(
            observed_at=replan_time,
            risk_valid_time=replan_time,
            data_revision=spec.input_revision + 1,
            risk_revision=suffix_commit.commit_id,
            route_avg_risk=initial_plan.metrics.avg_risk,
            route_max_risk=initial_plan.metrics.max_risk,
        )

        stage = "c_replanning"
        if heartbeat is not None:
            heartbeat({"event": "stage_start", "stage": stage})
        stage_started = time.perf_counter()
        replanning, _, replan_documents, replanning_parallel = _execute_with_parallel(
            spec=spec,
            paths=paths,
            operation=lambda: _execute_replan(
                ingress=ingress,
                request=replan_request,
                observation=observation,
                planning_contract=spec.planning_contract,
            ),
        )
        timings["c_replanning_seconds"] = time.perf_counter() - stage_started
        _append_stage_record(
            stage_records=stage_records,
            spec=spec,
            stage=stage,
            started_at=stage_started_at,
            duration_seconds=timings["c_replanning_seconds"],
            status="completed",
        )
        if heartbeat is not None:
            heartbeat(
                {
                    "event": "stage_done",
                    "stage": stage,
                    "duration_seconds": timings["c_replanning_seconds"],
                }
            )
        _check_stage_timeout(spec, timings["c_replanning_seconds"], stage)
        _require_planning_traceability(
            spec.planning_contract,
            replanning.batch
            if spec.planning_contract == "cd.route-plan.v2"
            else replanning.outcome,
            suffix_commit,
        )

        stage = "output_publication"
        stage_started_at = datetime.now(UTC)
        timings["total_execution_seconds"] = time.perf_counter() - started
        documents: dict[str, dict[str, Any]] = {
            "dataset-bundle.json": dataset_bundle_document,
            "endpoint-mapping.json": endpoint_mapping.to_document(),
            "execution-spec.json": spec.to_document(),
            "risk/full-window-commit.json": _window_document(full_commit),
            "risk/suffix-window-commit.json": _window_document(suffix_commit),
            "planning-coverage-preflight.json": coverage_preflight,
            "run-context.json": run_context_to_dict(intake.run_context),
            "source-records.json": {
                "schema_version": "orchestrator.source-records.v1",
                "records": [asdict(record) for record in intake.source_records],
            },
            **plan_documents,
            **replan_documents,
        }
        report = _run_report(
            spec=spec,
            intake=intake,
            configuration=configuration,
            model_config_digest=model_config_digest,
            prepared_as_of_time=prepared_as_of_time,
            full_commit=full_commit,
            suffix_commit=suffix_commit,
            endpoint_mapping=endpoint_mapping,
            initial=initial,
            replanning=replanning,
            current_waypoint=current_waypoint,
            timings=timings,
            parallel_telemetry=_merge_parallel_telemetry(
                initial_parallel,
                replanning_parallel,
            ),
            artifact_paths=tuple(
                sorted((*documents, "run-report.json", "checksums.json"))
            ),
        )
        documents["run-report.json"] = report
        output_dir, checksums = publish_output_directory(paths.output_dir, documents)
        stage_records.append(
            {
                "schema_version": "orchestrator.stage-record.v1",
                "run_id": spec.run_id,
                "stage": stage,
                "started_at": stage_started_at.isoformat(),
                "duration_seconds": round(
                    max(0.0, timings["total_execution_seconds"] - sum(timings.values())),
                    3,
                ),
                "status": "completed",
            }
        )
        _write_stage_report(stage_report_path, spec, stage_records, "completed")
        return FormalRunResult(output_dir=output_dir, report=report, checksums=checksums)
    except OrchestrationError as exc:
        _write_stage_report(
            stage_report_path, spec, stage_records, "failed", exc.code, str(exc),
        )
        raise
    except Exception as exc:
        error = OrchestrationError(
            f"{stage}_failed", f"{type(exc).__name__}: {exc}"
        )
        _write_stage_report(
            stage_report_path, spec, stage_records, "failed", error.code, str(error),
        )
        raise error from exc
    finally:
        if unsubscribe is not None:
            unsubscribe()


def _execute_with_parallel(*, spec: ExecutionSpec, paths: RunPaths, operation):
    """Run one C planning call with the RC2 objective-level worker profile."""

    if spec.planning_workers <= 1:
        result = operation()
        return (*result, _serial_parallel_telemetry(spec))

    from arctic_route_orchestrator.replay import parallel as replay_parallel

    contracts_root = paths.contracts_config_root
    if contracts_root is None:
        contracts_root = arctic_route_contracts.default_config_root()
    with replay_parallel.install(
        workers=spec.planning_workers,
        risk_store_root=paths.risk_store_root,
        c_config_root=paths.c_config_root,
        contracts_config_root=contracts_root,
        max_snap_km=spec.max_snap_km,
        timeout_seconds=max(1, int(spec.per_stage_timeout_seconds)),
        pool_mode=spec.parallel_pool_mode,
    ):
        result = operation()
        telemetry = replay_parallel.snapshot_telemetry()
    return (*result, telemetry)


def _serial_parallel_telemetry(spec: ExecutionSpec) -> dict[str, Any]:
    return {
        "enabled": False,
        "parallel_active": False,
        "requested_workers": spec.planning_workers,
        "effective_workers": 1,
        "pool_mode": spec.parallel_pool_mode,
        "planning_calls": 0,
        "tasks_submitted": 0,
        "max_parallel_tasks": 1,
        "worker_pids": [],
    }


def _merge_parallel_telemetry(
    initial: dict[str, Any], replanning: dict[str, Any]
) -> dict[str, Any]:
    worker_pids = sorted(
        set(initial.get("worker_pids", ())) | set(replanning.get("worker_pids", ()))
    )
    requested = max(
        int(initial.get("requested_workers", 1)),
        int(replanning.get("requested_workers", 1)),
    )
    return {
        "enabled": bool(initial.get("enabled") or replanning.get("enabled")),
        "parallel_active": bool(
            initial.get("parallel_active") or replanning.get("parallel_active")
        ),
        "requested_workers": requested,
        # PIDs are retained as cumulative provenance across the initial and
        # replan calls; effective capacity is the largest simultaneous pool,
        # not the number of distinct processes seen over the whole run.
        "effective_workers": max(
            int(initial.get("effective_workers", 1)),
            int(replanning.get("effective_workers", 1)),
        ),
        "pool_mode": initial.get("pool_mode", replanning.get("pool_mode", "persistent")),
        "planning_calls": int(initial.get("planning_calls", 0))
        + int(replanning.get("planning_calls", 0)),
        "tasks_submitted": int(initial.get("tasks_submitted", 0))
        + int(replanning.get("tasks_submitted", 0)),
        "max_parallel_tasks": max(
            int(initial.get("max_parallel_tasks", 1)),
            int(replanning.get("max_parallel_tasks", 1)),
        ),
        "worker_pids": worker_pids,
    }


def _execute_initial(*, ingress, request, planning_contract: str):
    if planning_contract == "cd.route-plan.v2":
        batch = ingress.execute(request)
        if not batch.published:
            raise OrchestrationError("c_initial_not_published", "v2 initial plan was not published")
        documents = _v2_documents("routes/v2/initial", batch.plans)
        return batch, batch.selected, documents
    outcome = ingress.execute_four_layer(request)
    if not outcome.published:
        raise OrchestrationError("c_initial_not_published", "v3 initial set was not published")
    plan_set = outcome.plan_set
    documents = {
        "routes/v3/initial.json": four_layer_route_plan_set_to_dict(plan_set),
        "routes/v3/initial.geojson": four_layer_route_plan_set_to_geojson(plan_set),
    }
    return outcome, plan_set.recommended, documents


def _append_stage_record(
    *,
    stage_records: list[dict[str, Any]],
    spec: ExecutionSpec,
    stage: str,
    started_at: datetime,
    duration_seconds: float,
    status: str,
) -> None:
    stage_records.append(
        {
            "schema_version": "orchestrator.stage-record.v1",
            "run_id": spec.run_id,
            "stage": stage,
            "started_at": started_at.isoformat(),
            "duration_seconds": round(float(duration_seconds), 3),
            "status": status,
        }
    )


def _check_stage_timeout(spec: ExecutionSpec, duration_seconds: float, stage: str) -> None:
    if duration_seconds > spec.per_stage_timeout_seconds:
        raise OrchestrationError(
            "stage_timeout",
            f"stage '{stage}' exceeded {spec.per_stage_timeout_seconds:.0f}s timeout",
        )


def _write_stage_report(
    path: Path,
    spec: ExecutionSpec,
    stage_records: list[dict[str, Any]],
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if path.exists():
        return
    document = {
        "schema_version": "orchestrator.stage-report.v1",
        "run_id": spec.run_id,
        "status": status,
        "per_stage_timeout_seconds": spec.per_stage_timeout_seconds,
        "error_code": error_code,
        "error_message": error_message,
        "stages": stage_records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".tmp")
    staging.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    os.replace(staging, path)


def _execute_replan(
    *,
    ingress,
    request,
    observation: ReplanObservation,
    planning_contract: str,
):
    if planning_contract == "cd.route-plan.v2":
        result = ingress.replan_if_needed(request, observation)
        if not result.decision.triggered:
            raise OrchestrationError(
                "replan_not_triggered", "six-hour formal observation did not trigger replanning"
            )
        if result.batch is None or not result.batch.published:
            raise OrchestrationError(
                "replan_not_published", "v2 replan did not atomically replace the current route"
            )
        documents = _v2_documents("routes/v2/replanned", result.batch.plans)
        return result, result.batch.selected, documents
    result = ingress.replan_four_layer_if_needed(request, observation)
    if not result.decision.triggered:
        raise OrchestrationError(
            "replan_not_triggered", "six-hour formal observation did not trigger replanning"
        )
    if result.outcome is None or not result.outcome.published:
        raise OrchestrationError(
            "replan_not_published", "v3 replan did not atomically replace the four-layer set"
        )
    plan_set = result.outcome.plan_set
    documents = {
        "routes/v3/replanned.json": four_layer_route_plan_set_to_dict(plan_set),
        "routes/v3/replanned.geojson": four_layer_route_plan_set_to_geojson(plan_set),
    }
    return result, plan_set.recommended, documents


def _v2_documents(prefix: str, plans) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for objective, plan in plans.items():
        documents[f"{prefix}/{objective.value}.json"] = route_plan_to_dict(plan)
        documents[f"{prefix}/{objective.value}.geojson"] = route_plan_to_geojson(plan)
    return documents


def _planning_request(
    *,
    configuration,
    run_context,
    model_config_digest: str,
    generation_id: int,
    input_revision: int,
    as_of_time: datetime,
    start_time: datetime,
    start: tuple[int, int],
    goal: tuple[int, int],
) -> ServicePlanningRequest:
    return ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=generation_id,
        input_revision=input_revision,
        as_of_time=as_of_time,
        start_time=start_time,
        start=start,
        goal=goal,
        maximum_elapsed=run_context.simulation_end - start_time,
    )


def _current_route_node(plan, *, replan_time: datetime, frame) -> tuple[tuple[int, int], Any]:
    eligible = tuple(waypoint for waypoint in plan.waypoints if waypoint.eta <= replan_time)
    if not eligible:
        raise OrchestrationError(
            "replan_not_materializable", "initial route has no waypoint at or before trigger"
        )
    waypoint = eligible[-1]
    latitude = np.asarray(frame.payload.coords["latitude"].values, dtype=np.float64)
    longitude = np.asarray(frame.payload.coords["longitude"].values, dtype=np.float64)
    y = np.flatnonzero(np.isclose(latitude, waypoint.latitude, rtol=0.0, atol=1e-10))
    x = np.flatnonzero(np.isclose(longitude, waypoint.longitude, rtol=0.0, atol=1e-10))
    if y.size != 1 or x.size != 1:
        raise OrchestrationError(
            "replan_position_not_on_grid", "selected route waypoint does not map to one risk node"
        )
    return (int(y[0]), int(x[0])), waypoint


def _advance_clock_without_seek(clock, target: datetime) -> None:
    before = clock.snapshot()
    delta = target - before.current_time
    if delta <= timedelta(0):
        raise OrchestrationError("replan_time_invalid", "clock target must move forward")
    clock.play()
    try:
        after = clock.tick(delta)
    finally:
        clock.pause()
    if after.current_time != target or after.generation_id != before.generation_id:
        raise OrchestrationError(
            "generation_changed", "time-trigger advance must not change simulation generation"
        )


def _require_configuration_identity(configuration, run_context) -> None:
    expected = {
        "scenario_id": configuration.scenario.scenario_id,
        "scenario_version": configuration.scenario.version,
        "corridor_id": configuration.corridor.corridor_id,
        "corridor_version": configuration.corridor.version,
        "vessel_profile_id": configuration.vessel.vessel_profile_id,
        "vessel_profile_version": configuration.vessel.version,
    }
    mismatched = [name for name, value in expected.items() if getattr(run_context, name) != value]
    if mismatched:
        raise OrchestrationError(
            "configuration_identity_mismatch", ", ".join(sorted(mismatched))
        )


def _require_plan_traceability(plan, committed) -> None:
    committed_ids = {frame.risk_id for frame in committed.frames}
    if not set(plan.source_risk_ids) <= committed_ids:
        raise OrchestrationError(
            "route_risk_traceability_failed", "route references risk IDs outside its commit"
        )
    if plan.metrics.hard_constraint_violations != 0:
        raise OrchestrationError(
            "hard_constraint_violation", "formal route contains a hard-constraint violation"
        )
    if hasattr(plan, "layer_goal_reached") and not plan.layer_goal_reached:
        raise OrchestrationError(
            "layer_goal_not_reached", "formal v3 route did not reach its layer goal"
        )
    if any(
        first.eta >= second.eta
        for first, second in zip(plan.waypoints, plan.waypoints[1:], strict=False)
    ):
        raise OrchestrationError("eta_not_monotonic", "formal route ETA is not strictly increasing")


def _require_planning_traceability(planning_contract: str, result, committed) -> None:
    if planning_contract == "cd.route-plan.v2":
        plans = tuple(result.plans.values())
    else:
        plans = tuple(
            plan
            for bundle in result.plan_set.layers
            for plan in bundle.plans.values()
        )
    expected_count = 3 if planning_contract == "cd.route-plan.v2" else 12
    if len(plans) != expected_count:
        raise OrchestrationError(
            "route_set_incomplete",
            f"expected {expected_count} routes, got {len(plans)}",
        )
    for plan in plans:
        _require_plan_traceability(plan, committed)


def _window_document(window) -> dict[str, Any]:
    return {
        "schema_version": window.schema_version,
        "commit_id": window.commit_id,
        "content_digest": window.content_digest,
        "start": _iso(window.start),
        "end": _iso(window.end),
        "interval_seconds": int(window.interval.total_seconds()),
        "count": window.count,
        "run_id": window.run_id,
        "scenario_id": window.scenario_id,
        "corridor_id": window.corridor_id,
        "generation_id": window.generation_id,
        "vessel_profile_id": window.vessel_profile_id,
        "config_digest": window.config_digest,
        "model_config_digest": window.model_config_digest,
        "as_of": _iso(window.as_of),
        "frames": [
            {
                "risk_id": frame.risk_id,
                "content_digest": risk_frame_content_digest(frame),
            }
            for frame in window.frames
        ],
    }


def _coverage_preflight(
    *,
    spec: ExecutionSpec,
    frames,
    expected_count: int,
    run_context,
) -> dict[str, Any]:
    """Fail-closed planning coverage gate computed from committed risk frames.

    The gate requires zero ``unknown_navigable_nodes`` on every frame.  This
    only makes the problem visible earlier; C's own ``RiskSamplingError``
    fail-closed behaviour is intentionally unchanged.
    """

    frame_summaries: list[dict[str, Any]] = []
    worst: dict[str, Any] | None = None
    gate_passed = True
    for index, frame in enumerate(frames):
        payload = frame.payload
        latitude = np.asarray(payload.coords["latitude"].values, dtype=float)
        longitude = np.asarray(payload.coords["longitude"].values, dtype=float)
        hard = np.asarray(payload["hard_mask"].values, dtype=bool)
        risk = np.asarray(payload["risk_score"].values, dtype=float)
        finite = np.isfinite(risk)
        navigable = ~hard
        land_nodes = 0
        data_unavailable_nodes = 0
        other_hard_nodes = 0
        if "hard_reason" in payload.data_vars:
            reasons = np.asarray(payload["hard_reason"].values)
            land_nodes = int(np.count_nonzero(reasons == "LAND"))
            data_unavailable_nodes = int(np.count_nonzero(reasons == "DATA_UNAVAILABLE"))
            other_hard_nodes = int(np.count_nonzero(reasons == "OTHER"))
        unknown_navigable = int(np.count_nonzero(navigable & ~finite))
        ice_free_counts = payload.attrs.get("ice_free_neutralized_input_counts", {})
        ice_free_neutralized_nodes = int(
            max(ice_free_counts.values(), default=0)
        )
        if unknown_navigable:
            gate_passed = False
        summary = {
            "frame_index": index,
            "valid_time": _iso(frame.valid_time),
            "total_nodes": int(latitude.size * longitude.size),
            "hard_nodes": int(np.count_nonzero(hard)),
            "land_nodes": land_nodes,
            "data_unavailable_nodes": data_unavailable_nodes,
            "other_hard_nodes": other_hard_nodes,
            "navigable_nodes": int(np.count_nonzero(navigable)),
            "unknown_navigable_nodes": unknown_navigable,
            "ice_free_neutralized_nodes": ice_free_neutralized_nodes,
            "finite_coverage_percent": round(
                100.0
                * np.count_nonzero(navigable & finite)
                / max(1, int(np.count_nonzero(navigable))),
                4,
            ),
            "missing_input_variable_counts": dict(
                payload.attrs.get("missing_input_variable_counts", {})
            ),
        }
        frame_summaries.append(summary)
        if worst is None or unknown_navigable > worst["unknown_navigable_nodes"]:
            worst = summary
    document = {
        "schema_version": "orchestrator.planning-coverage-preflight.v1",
        "run_id": spec.run_id,
        "scenario_id": spec.scenario_id,
        "corridor_id": run_context.corridor_id,
        "generation_id": spec.generation_id,
        "input_revision": spec.input_revision,
        "frames_expected": expected_count,
        "frames_checked": len(frame_summaries),
        "gate_passed": gate_passed,
        "gate_semantics": (
            "unknown_navigable_nodes must equal 0 on every frame; "
            "C fail-closed RiskSamplingError remains active"
        ),
        "worst_frame": worst,
        "frames": frame_summaries,
    }
    if not gate_passed:
        raise OrchestrationError(
            "coverage_preflight_failed",
            "unknown-navigable nodes remain on the risk window; refusing to plan",
        )
    return document


def _run_report(
    *,
    spec: ExecutionSpec,
    intake: ArtifactIntake,
    configuration,
    model_config_digest: str,
    prepared_as_of_time: datetime,
    full_commit,
    suffix_commit,
    endpoint_mapping,
    initial,
    replanning,
    current_waypoint,
    timings: dict[str, float],
    parallel_telemetry: dict[str, Any],
    artifact_paths: tuple[str, ...],
) -> dict[str, Any]:
    initial_identity, initial_routes = _planning_summary(spec.planning_contract, initial)
    replanned_identity, replanned_routes = _planning_summary(
        spec.planning_contract,
        replanning.batch if spec.planning_contract == "cd.route-plan.v2" else replanning.outcome,
    )
    a_input = asdict(intake.report)
    a_input["requested_data_types"] = list(a_input["requested_data_types"])
    return {
        "schema_version": "orchestrator.run-report.v1",
        "status": "success",
        "error_code": None,
        "scientific_status": "demo_unvalidated",
        "navigation_use": "prohibited",
        "planning_contract": spec.planning_contract,
        "identity": {
            "run_id": intake.run_context.run_id,
            "scenario_id": intake.run_context.scenario_id,
            "corridor_id": intake.run_context.corridor_id,
            "vessel_profile_id": intake.run_context.vessel_profile_id,
            "generation_id": spec.generation_id,
            "initial_input_revision": spec.input_revision,
            "replanned_input_revision": spec.input_revision + 1,
            "as_of_time": _iso(prepared_as_of_time),
            "simulation_start": _iso(intake.run_context.simulation_start),
            "simulation_end": _iso(intake.run_context.simulation_end),
            "orchestrator_generated_at": _iso(spec.generated_at),
        },
        "digests": {
            "dataset_bundle_id": intake.report.bundle_id,
            "dataset_bundle_digest": intake.report.bundle_digest,
            "config_digest": intake.run_context.config_digest,
            "model_config_digest": model_config_digest,
            "planner_config_digest": configuration.planner_config_digest,
            "full_risk_commit_id": full_commit.commit_id,
            "full_risk_content_digest": full_commit.content_digest,
            "suffix_risk_commit_id": suffix_commit.commit_id,
            "suffix_risk_content_digest": suffix_commit.content_digest,
            "initial_route_semantic_identity": initial_identity,
            "replanned_route_semantic_identity": replanned_identity,
        },
        "a_input": {
            **a_input,
            "source_records": [asdict(record) for record in intake.source_records],
        },
        "b_output": {
            "full_frame_count": full_commit.count,
            "suffix_frame_count": suffix_commit.count,
            "first_risk_id": full_commit.frames[0].risk_id,
            "last_risk_id": full_commit.frames[-1].risk_id,
        },
        "endpoint_mapping": endpoint_mapping.to_document(),
        "routes": {
            "initial": initial_routes,
            "replanned": replanned_routes,
        },
        "replanning": {
            "trigger_time": _iso(
                intake.run_context.simulation_start
                + timedelta(hours=spec.replan_after_hours)
            ),
            "observation_time": _iso(
                intake.run_context.simulation_start
                + timedelta(hours=spec.replan_after_hours)
            ),
            "current_route_waypoint": {
                "longitude": current_waypoint.longitude,
                "latitude": current_waypoint.latitude,
                "eta": _iso(current_waypoint.eta),
            },
            "triggered": replanning.decision.triggered,
            "reasons": [reason.value for reason in replanning.decision.reasons],
            "published": _replan_published(spec.planning_contract, replanning),
        },
        "performance": {
            "timings_seconds": timings,
            "process_peak_rss_bytes": _process_peak_rss_bytes(),
            "c_objective_parallelism": parallel_telemetry,
        },
        "environment": {
            "python": platform.python_version(),
            "prefix": sys.prefix,
            "packages": {
                "arctic_route_contracts": arctic_route_contracts.__version__,
                "work_package_a": arctic_route_data.__version__,
                "work_package_b": arctic_route_risk.__version__,
                "work_package_c": arctic_route_planning.__version__,
            },
            "lock_sha256": _lockfile_hashes(),
        },
        "warnings": [
            "B is a deterministic demo_unvalidated rule baseline, not a calibrated predictor.",
            "bathymetry and legal restriction layers are not formal hard constraints in this run.",
            "Outputs are for research demonstration only and must not be used for navigation.",
        ],
        "artifacts": list(artifact_paths),
    }


def _planning_summary(planning_contract: str, result) -> tuple[Any, list[dict[str, Any]]]:
    if planning_contract == "cd.route-plan.v2":
        plans = result.plans
        documents = {objective.value: route_plan_to_dict(plan) for objective, plan in plans.items()}
        identity = {
            objective: semantic_route_plan_digest(document)
            for objective, document in sorted(documents.items())
        }
        return identity, [_route_summary(plan, None) for plan in plans.values()]
    plan_set = result.plan_set
    identity = {
        "layer_set_id": plan_set.layer_set_id,
        "semantic_digest": four_layer_route_plan_set_semantic_digest(plan_set),
    }
    routes = [
        _route_summary(plan, bundle.planning_layer.value)
        for bundle in plan_set.layers
        for plan in bundle.plans.values()
    ]
    return identity, routes


def _route_summary(plan, planning_layer: str | None) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "planning_layer": planning_layer,
        "objective_mode": plan.objective_mode.value,
        "plan_kind": plan.plan_kind.value,
        "input_revision": plan.input_revision,
        "destination_reached": plan.destination_reached,
        "layer_goal_reached": getattr(
            plan,
            "layer_goal_reached",
            plan.destination_reached,
        ),
        "waypoint_count": len(plan.waypoints),
        "source_risk_id_count": len(plan.source_risk_ids),
        "metrics": {
            "distance_km": plan.metrics.distance_km,
            "eta_hours": plan.metrics.eta_hours,
            "avg_risk": plan.metrics.avg_risk,
            "max_risk": plan.metrics.max_risk,
            "integrated_risk_hours": plan.metrics.integrated_risk_hours,
            "minimum_confidence": plan.metrics.minimum_confidence,
            "hard_constraint_violations": plan.metrics.hard_constraint_violations,
            "turn_count": plan.metrics.turn_count,
            "expanded_nodes": plan.metrics.expanded_nodes,
            "compute_ms": plan.metrics.compute_ms,
            "objective_cost": plan.metrics.objective_cost,
        },
    }


def _replan_published(planning_contract: str, result) -> bool:
    if planning_contract == "cd.route-plan.v2":
        return result.batch is not None and result.batch.published
    return result.outcome is not None and result.outcome.published


def _lockfile_hashes() -> dict[str, str | None]:
    orchestrator_root = Path(__file__).resolve().parents[2]
    workspace = orchestrator_root.parent
    paths = {
        "orchestrator": orchestrator_root / "uv.lock",
        "contracts": workspace / "arctic_route_contracts" / "uv.lock",
        "work_package_a": workspace / "work_package_a" / "uv.lock",
        "work_package_b": workspace / "work_package_b" / "uv.lock",
        "work_package_c": workspace / "work_package_c" / "uv.lock",
    }
    return {name: _optional_sha256(path) for name, path in paths.items()}


def _optional_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _process_peak_rss_bytes() -> int:
    if resource is None:
        return 0
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(value)
    return int(value) * 1024


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = ["FormalRunResult", "RunPaths", "execute_formal_run"]
