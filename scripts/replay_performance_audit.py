"""Replan cost / vessel-motion audit for a published causal replay.

Reads the manifest + snapshots (and optionally the runner's JSON progress
log) and reports:

* candidate computations, accepted/rejected plans and their compute time;
* replan reasons;
* navigation snap adjustments (min/median/p95/max);
* continuous vessel motion invariants.

Usage:
    python replay_performance_audit.py \\
        --manifest <output>/causal-replay-manifest.json \\
        --log <runtime>/logs/replay-12h.out
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from itertools import pairwise
from pathlib import Path
from typing import Any


def _load_snapshots(manifest: dict[str, Any], snapshots_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(
            (snapshots_dir / f"{entry['index']:04d}.json").read_text(encoding="utf-8")
        )
        for entry in manifest.get("snapshots", [])
    ]


def _load_log_ticks(log: Path | None) -> dict[int, float]:
    if log is None:
        return {}
    pattern = re.compile(r'"tick":\s*(\d+).*?"tick_seconds":\s*([0-9.]+)')
    ticks: dict[int, float] = {}
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.search(line)
        if match:
            ticks[int(match.group(1))] = float(match.group(2))
    return ticks


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[index]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay-performance-audit")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--snapshots-dir", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    snapshots_dir = args.snapshots_dir or args.manifest.parent / "snapshots"
    snapshots = _load_snapshots(manifest, snapshots_dir)
    log_ticks = _load_log_ticks(args.log)
    summary = (
        json.loads(args.summary.read_text(encoding="utf-8"))
        if args.summary and args.summary.is_file()
        else {}
    )

    rows: list[dict[str, Any]] = []
    accepted_seconds: list[float] = []
    rejected_seconds: list[float] = []
    skip_seconds: list[float] = []
    snap_adjustments: list[float] = []
    cumulative: list[float] = []
    positions: list[tuple[float, float] | None] = []

    for snapshot in snapshots:
        index = snapshot["snapshot_index"]
        events = snapshot.get("events", [])
        event_types = [event.get("type") for event in events]
        accepted = any(
            event_type in ("PLAN_COMPUTED", "REPLAN_TRIGGERED")
            for event_type in event_types
        )
        rejected = any(
            event_type == "PLAN_REUSED"
            and not any(item == "REPLAN_SKIPPED" for item in event_types)
            for event_type in event_types
        )
        skipped = "REPLAN_SKIPPED" in event_types
        planning_seconds = None
        tick_performance = summary.get("tick_performance") or []
        if index < len(tick_performance):
            planning_seconds = tick_performance[index].get("planning_seconds")
        if planning_seconds is None and index in log_ticks:
            planning_seconds = log_ticks[index]
        if planning_seconds is not None:
            if accepted:
                accepted_seconds.append(float(planning_seconds))
            elif rejected:
                rejected_seconds.append(float(planning_seconds))
            elif skipped:
                skip_seconds.append(float(planning_seconds))
        ship = snapshot.get("ship_state", {})
        position = ship.get("current_position")
        positions.append(
            (position["longitude"], position["latitude"]) if position else None
        )
        if ship.get("snap_adjustment_km") is not None:
            snap_adjustments.append(float(ship["snap_adjustment_km"]))
        if ship.get("cumulative_travelled_km") is not None:
            cumulative.append(float(ship["cumulative_travelled_km"]))
        rows.append(
            {
                "tick": index,
                "simulation_time": snapshot["simulation_time"],
                "plan_revision": snapshot["planning"]["plan_revision"],
                "replan_reasons": snapshot["planning"]["replan_reasons"],
                "candidate_accepted": accepted,
                "candidate_rejected": rejected,
                "pre_planning_skip": skipped,
                "planning_seconds": planning_seconds,
                "position": position,
                "current_node": ship.get("current_node"),
                "current_edge_index": ship.get("current_edge_index"),
                "edge_progress": ship.get("edge_progress"),
                "effective_speed_knots": ship.get("effective_speed_knots"),
                "executed_distance_km": ship.get("executed_distance_km"),
                "cumulative_travelled_km": ship.get("cumulative_travelled_km"),
                "remaining_distance_km": ship.get("remaining_distance_km"),
                "snap_adjustment_km": ship.get("snap_adjustment_km"),
                "completed_track_len": len(ship.get("completed_track", [])),
            }
        )

    cumulative_monotonic = all(
        current >= previous - 1e-9
        for previous, current in pairwise(cumulative)
    )
    report = {
        "replay_id": manifest["replay_id"],
        "snapshot_count": len(snapshots),
        "replan_candidate_computations": summary.get(
            "replan_candidate_computations", len(accepted_seconds) + len(rejected_seconds)
        ),
        "replan_candidates_accepted": summary.get(
            "replan_candidates_accepted", len(accepted_seconds)
        ),
        "replan_candidates_rejected": summary.get(
            "replan_candidates_rejected", len(rejected_seconds)
        ),
        "pre_planning_skips": summary.get("pre_planning_skips", len(skip_seconds)),
        "accepted_planning_seconds_total": round(sum(accepted_seconds), 2),
        "rejected_planning_seconds_total": round(sum(rejected_seconds), 2),
        "pre_planning_skip_seconds_total": round(sum(skip_seconds), 2),
        "wasted_percentage": (
            round(
                100.0
                * sum(rejected_seconds)
                / max(1e-9, sum(accepted_seconds) + sum(rejected_seconds)),
                1,
            )
            if accepted_seconds or rejected_seconds
            else None
        ),
        "planning_elapsed_seconds": summary.get("planning_elapsed_seconds"),
        "total_elapsed_seconds": summary.get("total_elapsed_seconds"),
        "snap_adjustment_km": (
            {
                "min": round(min(snap_adjustments), 3),
                "median": round(statistics.median(snap_adjustments), 3),
                "p95": round(_p95(snap_adjustments), 3),
                "max": round(max(snap_adjustments), 3),
            }
            if snap_adjustments
            else None
        ),
        "vessel_motion": {
            "cumulative_monotonic": cumulative_monotonic,
            "cumulative_start_km": cumulative[0] if cumulative else None,
            "cumulative_end_km": cumulative[-1] if cumulative else None,
            "position_changes": sum(
                1
                for previous, current in pairwise(positions)
                if previous is not None and current is not None and previous != current
            ),
        },
        "ticks": rows,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
