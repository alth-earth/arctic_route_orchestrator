"""Presentation eligibility preflight for the Replay-driven viewer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from arctic_route_orchestrator.replay.preflight import run_viewer_preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay-viewer-preflight")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--snapshots-dir", type=Path, default=None)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("/root/my_project/work_package_a/data"),
    )
    parser.add_argument("--route-id", default="tromso_to_isfjorden_outer")
    parser.add_argument("--land-mask", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--sample-step-km", type=float, default=5.0)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    snapshots_dir = args.snapshots_dir or args.manifest.parent / "snapshots"
    snapshots = [
        json.loads(
            (snapshots_dir / f"{entry['index']:04d}.json").read_text(encoding="utf-8")
        )
        for entry in manifest["snapshots"]
    ]
    result = run_viewer_preflight(
        manifest,
        snapshots,
        data_root=args.data_root,
        route_id=args.route_id,
        land_mask_path=args.land_mask,
        sample_step_km=args.sample_step_km,
        output_path=args.output,
    )
    print(json.dumps(result.document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.overall == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
