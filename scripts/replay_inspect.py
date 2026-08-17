"""Minimal replay inspector CLI: timeline + events from a replay manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from arctic_route_orchestrator.replay.validation import (
    validate_manifest,
    validate_replay,
    validate_snapshot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay-inspect")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--snapshots-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    snapshots_dir = args.snapshots_dir or args.manifest.parent / "snapshots"
    snapshots = [
        json.loads((snapshots_dir / f"{entry['index']:04d}.json").read_text(encoding="utf-8"))
        for entry in manifest["snapshots"]
    ]
    print(
        f"replay {manifest['replay_id']}  mode={manifest['scenario_mode']}  "
        f"ticks={manifest['snapshot_count']}  "
        f"start={manifest['replay_start']}  end={manifest['replay_end']}"
    )
    print("  simulation_time      data b_in risk plan  risk_window")
    for snapshot in snapshots:
        result = validate_snapshot(snapshot)
        mark = "OK " if result["status"] == "PASS" else "BAD"
        risk_identity = snapshot["risk"]["resource_identity"]
        print(
            f"  {snapshot['simulation_time']}  "
            f"{snapshot['visibility']['data_revision']:>4} "
            f"{snapshot['visibility']['b_input_revision']:>4} "
            f"{snapshot['risk']['risk_revision']:>4} "
            f"{snapshot['planning']['plan_revision']:>4}  "
            f"{mark} {(risk_identity or '')[:20]}"
        )
    print("  events:")
    for event in manifest["events"]:
        print(
            f"    {event['simulation_time']}  {event['type']:<26} "
            f"rev={event.get('revision','')}  {event.get('description','')}"
        )
    sequence = validate_replay(snapshots)
    manifest_check = validate_manifest(manifest, snapshots)
    print(f"validation: snapshots={sequence['status']} manifest={manifest_check['status']}")
    for violation in [*sequence["violations"], *manifest_check["violations"]]:
        print(f"  VIOLATION: {violation}")
    return 0 if sequence["status"] == manifest_check["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
