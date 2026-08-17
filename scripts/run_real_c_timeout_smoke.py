"""Drive ``real_c_timeout_worker.py`` through the interruptible watchdog.

Uses the committed RC1 risk window (worker-mode success run) so the smoke
skips intake/B and targets a real CPU-bound four-layer C search.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.service import RunPaths
from arctic_route_orchestrator.timeout_runner import (
    StageTimeoutError,
    run_with_timeout,
)

WORKER = Path(__file__).with_name("real_c_timeout_worker.py")
RC2_ROOT = Path("/root/my_project/work_package_a/data/output/rc2-smoke")
OUTPUT_DIR = RC2_ROOT / "output-real-c-timeout"


def main() -> int:
    spec = ExecutionSpec(
        schema_version="orchestrator.execution-spec.v1",
        run_id="run-00000000-0000-4000-8000-0000000b0005",
        scenario_id="murmansk_dikson_august_2026_demo_v1",
        generation_id=0,
        input_revision=0,
        generated_at=datetime(2026, 8, 17, tzinfo=UTC),
        planning_contract="cd.four-layer-route-plan-set.v3",
        per_stage_timeout_seconds=45.0,
    )
    spec_path = OUTPUT_DIR.parent / ".real-c-timeout.spec.json"
    spec_path.write_text(json.dumps(spec.to_document(), sort_keys=True), encoding="utf-8")
    paths = RunPaths(
        bundle_path=RC2_ROOT / "x.json",
        a_data_root=Path("/root/my_project/work_package_a/data"),
        b_config_path=Path("/root/my_project/work_package_b/configs/models/x.json"),
        c_config_root=Path("/root/my_project/work_package_c/configs"),
        contracts_config_root=Path("/root/my_project/arctic_route_contracts/configs"),
        risk_store_root=RC2_ROOT / "risk-store-mur-worker",
        output_dir=OUTPUT_DIR,
        run_context_path=Path(
            "/root/my_project/work_package_a/data/output/bundles/"
            "murmansk_dikson_august_2026_demo_v1.run-context.json"
        ),
    )
    paths_json = json.dumps(
        {
            "risk_store_root": str(paths.risk_store_root),
            "commit_dir": str(RC2_ROOT / "output-mur-worker"),
            "c_config_root": str(paths.c_config_root),
            "contracts_config_root": str(paths.contracts_config_root),
            "run_context_path": str(paths.run_context_path),
        },
        sort_keys=True,
    )

    def factory(result_path: Path) -> list[str]:
        return [
            sys.executable,
            str(WORKER),
            str(spec_path),
            paths_json,
            str(result_path),
        ]

    try:
        payload = run_with_timeout(spec, paths, worker_cmd_factory=factory)
    except StageTimeoutError as exc:
        print(
            json.dumps(
                {
                    "smoke": "real_c_timeout",
                    "stage": exc.stage,
                    "elapsed": round(exc.elapsed, 1),
                    "timeout": exc.timeout,
                    "expected": "TIMEOUT during c_initial_planning",
                },
                sort_keys=True,
            )
        )
        return 0 if exc.stage == "c_initial_planning" else 2
    print(json.dumps({"smoke": "real_c_timeout", "unexpected_success": payload}, sort_keys=True))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
