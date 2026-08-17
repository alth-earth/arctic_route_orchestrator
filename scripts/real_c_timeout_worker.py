"""Real-C stage worker for watchdog timeout smoke (diagnostic).

Usage (same argument shape as ``stage_worker``):
    python real_c_timeout_worker.py <spec.json> <paths.json> <result.json>

The worker loads a committed RC1 risk window from the store, maps corridor
endpoints, and runs the real four-layer C planner while emitting stage
heartbeats.  It is intended to be driven by ``timeout_runner.run_with_timeout``
with a deliberately short per-stage timeout so the parent can interrupt a real
CPU-bound A* search.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arctic_route_contracts import load_run_context
from arctic_route_planning import (
    RiskSourcePlanningIngress,
    ServicePlanningRequest,
    map_corridor_endpoints,
)
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import ProvenanceKind, RiskWindowQuery
from arctic_route_risk import PersistentRiskStore

from arctic_route_orchestrator.models import ExecutionSpec


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 3:
        print(
            "usage: real_c_timeout_worker <spec.json> <paths.json> <result.json>",
            file=sys.stderr,
        )
        return 2
    spec_path, paths_json, result_path = args
    spec = ExecutionSpec.from_path(spec_path)
    paths = json.loads(paths_json)
    result = Path(result_path)

    def heartbeat(event: dict[str, object]) -> None:
        print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)

    try:
        commit_document = json.loads(
            (Path(paths["commit_dir"]) / "risk" / "full-window-commit.json").read_text(
                encoding="utf-8"
            )
        )
        store = PersistentRiskStore(paths["risk_store_root"])
        configuration = load_configuration(
            paths["c_config_root"],
            spec.scenario_id,
            shared_config_root=Path(paths["contracts_config_root"]),
        )
        run_context = load_run_context(paths["run_context_path"])
        query = RiskWindowQuery(
            start=_parse_utc(commit_document["start"]),
            end=_parse_utc(commit_document["end"]),
            interval=timedelta(hours=1),
            run_id=commit_document["run_id"],
            scenario_id=commit_document["scenario_id"],
            corridor_id=commit_document["corridor_id"],
            generation_id=commit_document["generation_id"],
            vessel_profile_id=commit_document["vessel_profile_id"],
            config_digest=commit_document["config_digest"],
            model_config_digest=commit_document["model_config_digest"],
            as_of=_parse_utc(commit_document["as_of"]),
        )
        window = store.get_committed_window(query)
        frames = window.frames
        endpoint_mapping = map_corridor_endpoints(
            configuration,
            frames[0],
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
        heartbeat({"event": "stage_start", "stage": "c_initial_planning"})
        started = time.perf_counter()
        outcome = ingress.execute_four_layer(request)
        heartbeat(
            {
                "event": "stage_done",
                "stage": "c_initial_planning",
                "duration_seconds": time.perf_counter() - started,
            }
        )
        result.write_text(
            json.dumps(
                {
                    "ok": True,
                    "published": bool(outcome.published),
                    "layer_set_id": outcome.plan_set.layer_set_id,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return 0
    except Exception as exc:
        result.write_text(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "worker_crash",
                    "message": f"{type(exc).__name__}: {exc}",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
