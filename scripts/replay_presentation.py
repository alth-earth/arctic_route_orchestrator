"""Replay presentation adapter CLI (Strategy B viewer foundation).

Usage:

    replay_presentation.py <manifest> [--state 2026-08-15T10:30:00Z]
                                          [--audit]
                                          [--sample-step-minutes 5]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arctic_route_orchestrator.replay.presentation import PresentationAdapter


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay-presentation")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--snapshots-dir", type=Path, default=None)
    parser.add_argument("--state", type=str, default=None)
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--sample-step-minutes", type=int, default=5)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    snapshots_dir = args.snapshots_dir or args.manifest.parent / "snapshots"
    snapshots = [
        json.loads((snapshots_dir / f"{entry['index']:04d}.json").read_text(encoding="utf-8"))
        for entry in manifest["snapshots"]
    ]
    adapter = PresentationAdapter(manifest, snapshots)
    if args.audit:
        print(json.dumps(adapter.adoption_audit(), ensure_ascii=False, indent=2, sort_keys=True))
    if args.state:
        rendered = adapter.state_at(args.state).to_dict()
        print(json.dumps(rendered, ensure_ascii=False, indent=2, sort_keys=True))
    if not args.audit and not args.state:
        start = _parse_utc(manifest["replay_start"])
        end = _parse_utc(manifest["replay_end"])
        moment = start
        while moment <= end:
            print(
                json.dumps(
                    adapter.state_at(moment).to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            moment += timedelta(minutes=args.sample_step_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
