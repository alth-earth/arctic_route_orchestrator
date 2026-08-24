"""Live Demo runner: drives demo_live_worker under the interruptible watchdog."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.service import RunPaths
from arctic_route_orchestrator.timeout_runner import (
    StageTimeoutError,
    run_with_timeout,
)


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 1:
        print("usage: demo_live_runner <live-paths.json>", file=sys.stderr)
        return 2
    paths = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    worker_script = Path(__file__).with_name("demo_live_worker.py")
    output_path = Path(paths["output_path"])
    live_paths = {
        "risk_store_root": paths["risk_store_root"],
        "commit_dir": paths["commit_dir"],
        "c_config_root": paths["c_config_root"],
        "contracts_config_root": paths["contracts_config_root"],
        "corridor_version": paths.get("corridor_version", "1.2.0"),
    }
    live_paths_file = output_path.parent / ".demo-live-paths.json"
    live_paths_file.write_text(json.dumps(live_paths, sort_keys=True), encoding="utf-8")
    spec = ExecutionSpec(
        schema_version="orchestrator.execution-spec.v1",
        run_id="run-00000000-0000-4000-8000-0000000d0001",
        scenario_id=paths.get("scenario_id", "tromso_isfjorden_rc2_smoke_v1"),
        generation_id=0,
        input_revision=1,
        generated_at=datetime.now(UTC),
        planning_contract="cd.four-layer-route-plan-set.v3",
        per_stage_timeout_seconds=float(paths.get("timeout_seconds", 110)),
    )
    run_paths = RunPaths(
        bundle_path=output_path.parent / "unused.json",
        a_data_root=_workspace_root() / "work_package_a" / "data",
        b_config_path=output_path.parent / "unused.json",
        c_config_root=Path(paths["c_config_root"]),
        contracts_config_root=Path(paths["contracts_config_root"]),
        risk_store_root=Path(paths["risk_store_root"]),
        output_dir=output_path.parent / "output-live-demo",
        run_context_path=Path(paths.get("run_context_path", "unused.json")),
    )
    try:
        payload = run_with_timeout(
            spec,
            run_paths,
            worker_cmd_factory=lambda result_path: [
                sys.executable,
                str(worker_script),
                str(live_paths_file),
                str(result_path),
            ],
        )
    except StageTimeoutError as exc:
        output_path.write_text(
            json.dumps(
                {
                    "schema_version": "d.live-result.v1",
                    "result_origin": "LIVE_COMPUTED",
                    "status": "TIMEOUT",
                    "stage": exc.stage,
                    "elapsed_seconds": exc.elapsed,
                    "timeout_seconds": exc.timeout,
                    "message": "live replanning exceeded the demo timeout",
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 2
    document = payload["live_result"]
    output_path.write_text(
        json.dumps(document, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "result_origin": document["result_origin"],
                "wall_seconds": round(document["wall_seconds"], 2),
                "distance_km": round(document["metrics"]["distance_km"], 2),
                "eta_hours": round(document["metrics"]["eta_hours"], 2),
                "output": str(output_path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
