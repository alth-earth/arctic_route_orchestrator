"""Scenario B short-window causal replay MVP CLI (Strategy B).

Usage examples:

    causal_replay_mvp.py --replay-id sb12h --window-hours 12
    causal_replay_mvp.py --replay-id sb24h --window-hours 24 --resume
    causal_replay_mvp.py --replay-id sb44h --window-hours 44

The runner is replay-local and never touches the frozen retrospective path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arctic_route_orchestrator.replay.runner import ReplayRunner


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="causal-replay-mvp")
    parser.add_argument("--replay-id", default="sb-causal-12h")
    parser.add_argument("--scenario-id", default="tromso_isfjorden_august_2026_demo_v1")
    parser.add_argument("--corridor-id", default="tromso_to_isfjorden_outer")
    parser.add_argument("--replay-start", default="2026-08-15T10:00:00Z")
    parser.add_argument("--replay-end", default=None)
    parser.add_argument("--window-hours", type=int, default=12)
    parser.add_argument(
        "--replay-mode",
        choices=("causal_replay", "retrospective_dynamic_replay"),
        default="causal_replay",
        help="causal issue-time replay or explicitly labelled post-hoc dynamic replay",
    )
    parser.add_argument("--risk-forecast-end", default=None)
    parser.add_argument("--planning-horizon-hours", type=int, default=None)
    parser.add_argument("--v2-only", action="store_true")
    parser.add_argument("--tick-hours", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=(
            _workspace_root()
            / "work_package_a"
            / "data"
            / "output"
            / "rc2-smoke"
            / "causal-replay-mvp"
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=_workspace_root() / "work_package_a" / "data" / "manifest" / "manifest.sqlite3",
    )
    parser.add_argument(
        "--a-data-root",
        type=Path,
        default=_workspace_root() / "work_package_a" / "data",
    )
    parser.add_argument(
        "--b-config",
        type=Path,
        default=(
            _workspace_root() / "work_package_b" / "configs" / "models"
            / "demo_unvalidated_tromso_smoke_grid_v1.json"
        ),
    )
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
        "--frozen-run-context",
        type=Path,
        default=(
            _workspace_root() / "work_package_a" / "data" / "output" / "rc2-smoke"
            / "output-tromso-144h-r2" / "run-context.json"
        ),
    )
    parser.add_argument(
        "--frozen-dataset-bundle",
        type=Path,
        default=None,
        help="exact immutable DatasetBundle.v2 to reuse with a frozen B window",
    )
    parser.add_argument(
        "--frozen-risk-store-root",
        type=Path,
        default=None,
        help="read-only source PersistentRiskStore containing the frozen commit",
    )
    parser.add_argument(
        "--frozen-risk-commit-id",
        default=None,
        help="exact immutable RiskWindow commit to reuse without recomputing A/B",
    )
    parser.add_argument("--max-snap-km", type=float, default=30.0)
    parser.add_argument("--cache-memory-mb", type=float, default=2048.0)
    parser.add_argument(
        "--planning-workers",
        type=int,
        default=3,
        help="C objective-level workers (RC2 default: 3; ticks/layers remain serial)",
    )
    parser.add_argument("--replan-min-interval-hours", type=float, default=None)
    parser.add_argument("--replan-waypoint-aligned-only", action="store_true")
    parser.add_argument(
        "--parallel-pool-mode",
        choices=("persistent", "percall"),
        default="persistent",
    )
    args = parser.parse_args(argv)

    replay_start = _parse_utc(args.replay_start)
    replay_end = (
        _parse_utc(args.replay_end)
        if args.replay_end
        else replay_start + timedelta(hours=args.window_hours)
    )
    risk_forecast_end = (
        _parse_utc(args.risk_forecast_end)
        if args.risk_forecast_end
        else None
    )
    output_root = args.output_root / args.replay_id
    runner = ReplayRunner(
        replay_id=args.replay_id,
        scenario_id=args.scenario_id,
        corridor_id=args.corridor_id,
        replay_start=replay_start,
        replay_end=replay_end,
        risk_forecast_end=risk_forecast_end,
        planning_horizon_hours=args.planning_horizon_hours,
        replay_mode=args.replay_mode,
        v2_only=args.v2_only,
        tick_cadence_hours=args.tick_hours,
        a_data_root=args.a_data_root,
        manifest_path=args.manifest,
        b_config_path=args.b_config,
        c_config_root=args.c_config_root,
        contracts_config_root=args.contracts_config_root,
        frozen_run_context_path=args.frozen_run_context,
        frozen_dataset_bundle_path=args.frozen_dataset_bundle,
        frozen_risk_store_root=args.frozen_risk_store_root,
        frozen_risk_commit_id=args.frozen_risk_commit_id,
        max_snap_km=args.max_snap_km,
        cache_memory_mb=args.cache_memory_mb,
        planning_workers=args.planning_workers,
        replan_min_interval_hours=args.replan_min_interval_hours,
        replan_waypoint_aligned_only=args.replan_waypoint_aligned_only,
        parallel_pool_mode=args.parallel_pool_mode,
    )

    def progress(item: dict) -> None:
        print(json.dumps(item, sort_keys=True), flush=True)

    summary = runner.run(
        output_root=output_root,
        resume=args.resume,
        progress=progress,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
