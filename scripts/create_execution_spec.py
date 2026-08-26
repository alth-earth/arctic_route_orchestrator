#!/usr/bin/env python3
"""Create a write-once ExecutionSpec bound to a frozen scenario/run identity."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from arctic_route_orchestrator.models import ExecutionSpec


def main() -> int:
    parser = argparse.ArgumentParser(prog="create-execution-spec")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generation-id", type=int, default=0)
    parser.add_argument("--input-revision", type=int, default=0)
    parser.add_argument(
        "--planning-contract",
        default="cd.four-layer-route-plan-set.v3",
        choices=("cd.route-plan.v2", "cd.four-layer-route-plan-set.v3"),
    )
    parser.add_argument("--max-snap-km", type=float, default=150.0)
    parser.add_argument("--replan-after-hours", type=int, default=6)
    parser.add_argument("--per-stage-timeout-seconds", type=float, default=3600.0)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite existing spec: {args.output}")
    generated_at = datetime.fromisoformat(args.generated_at.replace("Z", "+00:00"))
    spec = ExecutionSpec(
        schema_version="orchestrator.execution-spec.v1",
        run_id=args.run_id,
        scenario_id=args.scenario_id,
        generation_id=args.generation_id,
        input_revision=args.input_revision,
        generated_at=generated_at,
        planning_contract=args.planning_contract,
        max_snap_km=args.max_snap_km,
        replan_after_hours=args.replan_after_hours,
        per_stage_timeout_seconds=args.per_stage_timeout_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    temporary.write_text(
        json.dumps(spec.to_document(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(json.dumps(spec.to_document(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
