"""Run the formal Winter B-to-C validation and publish presentation inputs.

This runner consumes an existing committed RiskFrame window.  It never invokes
A or B and writes all generated experiment evidence under the requested output
directory.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctic_route_contracts import load_run_context
from arctic_route_planning import (
    RiskSourcePlanningIngress,
    ServicePlanningRequest,
    four_layer_route_plan_set_from_dict,
    four_layer_route_plan_set_to_dict,
    four_layer_route_plan_set_to_geojson,
    map_corridor_endpoints,
)
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import ProvenanceKind, RiskWindowQuery
from arctic_route_risk import PersistentRiskStore
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.replay.route_integrity import audit_route
from arctic_route_orchestrator.route_presentation import project_route_candidates


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


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _risk_query(commit: dict[str, Any]) -> RiskWindowQuery:
    return RiskWindowQuery(
        start=_parse_utc(commit["start"]),
        end=_parse_utc(commit["end"]),
        interval=timedelta(seconds=int(commit["interval_seconds"])),
        run_id=commit["run_id"],
        scenario_id=commit["scenario_id"],
        corridor_id=commit["corridor_id"],
        generation_id=commit["generation_id"],
        vessel_profile_id=commit["vessel_profile_id"],
        config_digest=commit["config_digest"],
        model_config_digest=commit["model_config_digest"],
        as_of=_parse_utc(commit["as_of"]),
    )


def _validate_c_schema(document: dict[str, Any], schema_root: Path) -> None:
    names = ("route-plan-v3.schema.json", "four-layer-route-plan-set-v3.schema.json")
    schemas: dict[str, dict[str, Any]] = {}
    resources: list[tuple[str, Resource[Any]]] = []
    for name in names:
        schema = json.loads((schema_root / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        schemas[name] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    Draft202012Validator(
        schemas["four-layer-route-plan-set-v3.schema.json"],
        registry=Registry().with_resources(resources),
        format_checker=FormatChecker(),
    ).validate(document)


def _route_metrics(document: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for layer in document["layers"]:
        for objective, route in layer["plans"].items():
            metrics = route["metrics"]
            results.append(
                {
                    "layer": layer["planning_layer"],
                    "objective": objective,
                    "plan_id": route["plan_id"],
                    "route_success": route["layer_goal_reached"],
                    "destination_reached": route["destination_reached"],
                    "distance_km": metrics["distance_km"],
                    "eta_hours": metrics["eta_hours"],
                    "average_risk": metrics["avg_risk"],
                    "maximum_risk": metrics["max_risk"],
                    "integrated_risk_hours": metrics["integrated_risk_hours"],
                    "expanded_nodes": metrics["expanded_nodes"],
                    "compute_ms": metrics["compute_ms"],
                    "waypoint_count": len(route["waypoints"]),
                }
            )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run-winter-c-validation")
    parser.add_argument("--risk-store-root", type=Path, required=True)
    parser.add_argument("--risk-commit", type=Path, required=True)
    parser.add_argument("--run-context", type=Path, required=True)
    parser.add_argument("--execution-spec", type=Path, required=True)
    parser.add_argument(
        "--c-config-root",
        type=Path,
        default=_workspace_root() / "work_package_c" / "configs",
    )
    parser.add_argument(
        "--contracts-config-root",
        type=Path,
        default=_workspace_root() / "arctic_route_contracts" / "configs",
    )
    parser.add_argument(
        "--c-schema-root",
        type=Path,
        default=_workspace_root() / "work_package_c" / "schemas",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    commit = json.loads(args.risk_commit.read_text(encoding="utf-8"))
    spec = ExecutionSpec.from_path(args.execution_spec)
    run_context = load_run_context(args.run_context)
    query = _risk_query(commit)
    if spec.planning_contract != "cd.four-layer-route-plan-set.v3":
        raise ValueError("Winter C validation requires the formal v3 planning contract")
    for name, actual, expected in (
        ("run_id", spec.run_id, query.run_id),
        ("scenario_id", spec.scenario_id, query.scenario_id),
        ("generation_id", spec.generation_id, query.generation_id),
        ("run_context.run_id", run_context.run_id, query.run_id),
        ("run_context.config_digest", run_context.config_digest, query.config_digest),
    ):
        if actual != expected:
            raise ValueError(f"{name} does not match the committed Winter risk window")

    configuration = load_configuration(
        args.c_config_root,
        spec.scenario_id,
        shared_config_root=args.contracts_config_root,
    )
    store = PersistentRiskStore(args.risk_store_root)
    window = store.get_committed_window(query)
    if window.commit_id != commit["commit_id"] or window.content_digest != commit["content_digest"]:
        raise ValueError("loaded RiskFrame window identity differs from the selected commit")
    endpoint_mapping = map_corridor_endpoints(
        configuration,
        window.frames[0],
        max_adjustment_km=spec.max_snap_km,
    )
    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=query.model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=spec.generation_id,
        input_revision=spec.input_revision,
        as_of_time=query.as_of,
        start_time=query.start,
        start=endpoint_mapping.start.node,
        goal=endpoint_mapping.goal.node,
        maximum_elapsed=query.end - query.start,
    )
    ingress = RiskSourcePlanningIngress(store, configuration=configuration)
    prepared = ingress.prepare(request)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(args.output_dir / "endpoint-mapping.json", endpoint_mapping.to_document())
    intake = {
        "status": "PASS",
        "planning_contract": spec.planning_contract,
        "risk_commit_id": window.commit_id,
        "risk_content_digest": window.content_digest,
        "risk_schema": window.frames[0].schema_version,
        "risk_frame_count": window.count,
        "risk_window_start": query.start.isoformat().replace("+00:00", "Z"),
        "risk_window_end": query.end.isoformat().replace("+00:00", "Z"),
        "risk_interval_seconds": int(query.interval.total_seconds()),
        "risk_provenance": window.frames[0].provenance.value,
        "grid_shape": list(endpoint_mapping.grid_shape),
        "endpoint_mapping": endpoint_mapping.to_document(),
        "prepared_commit_id": prepared.window.commit_id,
    }
    _write_json(args.output_dir / "consumer-smoke.json", intake)
    if args.prepare_only:
        print(json.dumps(intake, ensure_ascii=False, sort_keys=True), flush=True)
        return 0

    planning_started = time.perf_counter()
    outcome = prepared.execute_four_layer()
    planning_seconds = time.perf_counter() - planning_started
    plan_document = four_layer_route_plan_set_to_dict(outcome.plan_set)
    _validate_c_schema(plan_document, args.c_schema_root)
    if four_layer_route_plan_set_from_dict(plan_document) != outcome.plan_set:
        raise ValueError("C v3 strict codec round-trip changed the published plan set")
    integrity = [
        audit_route(plan, window.frames)
        for bundle in outcome.plan_set.layers
        for plan in bundle.plans.values()
    ]
    if any(item["status"] != "PASS" for item in integrity):
        raise ValueError("one or more published C routes failed geospatial integrity")
    candidate_document = project_route_candidates(outcome.plan_set)

    _write_json(args.output_dir / "winter-four-layer-route-plan-set-v3.json", plan_document)
    _write_json(
        args.output_dir / "winter-four-layer-route-plan-set-v3.geojson",
        four_layer_route_plan_set_to_geojson(outcome.plan_set),
    )
    _write_json(args.output_dir / "route-candidates.json", candidate_document)
    _write_json(args.output_dir / "route-integrity.json", integrity)
    metrics = _route_metrics(plan_document)
    summary = {
        **intake,
        "status": "PASS",
        "published": outcome.published,
        "layer_set_id": outcome.plan_set.layer_set_id,
        "route_count": len(metrics),
        "selected_plan_id": outcome.plan_set.recommended.plan_id,
        "candidate_set_id": candidate_document["candidate_set_id"],
        "planning_wall_seconds": planning_seconds,
        "total_wall_seconds": time.perf_counter() - started,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "routes": metrics,
        "integrity_status": "PASS",
    }
    _write_json(args.output_dir / "validation-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
