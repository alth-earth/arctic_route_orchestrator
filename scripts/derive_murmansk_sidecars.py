#!/usr/bin/env python3
"""Derive D-side presentation sidecars for the frozen Murmansk->Dikson (A) route.

Consumes ONLY frozen execution artifacts (no re-run of A/B/C planners):
  - risk window commit   (output-mur-opt/risk/full-window-commit.json)
  - risk store           (risk-store-mur-opt/)
  - plan set v3          (output-mur-opt/routes/v3/initial.json)
  - dataset bundle       (frozen_demo_backup/murmansk_dikson_aug2026/...bundle.json)

Produces:
  - frame-index.json           (orchestrator-replay-risk-frame-index, FORMAL_VALIDATED)
  - route-candidates.json      (presentation.route-candidates.v1, 12 candidates)
  - route-integrity.json       (audit_route PASS/FAIL evidence, 12 routes)

Output directory is immutable: refuses to overwrite an existing directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from arctic_route_orchestrator.viewer_sidecars import derive_viewer_sidecars


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="derive-murmansk-sidecars")
    parser.add_argument("--risk-commit", type=Path, required=True)
    parser.add_argument("--risk-store-root", type=Path, required=True)
    parser.add_argument("--plan-set", type=Path, required=True)
    parser.add_argument("--dataset-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    frame_path, candidate_path, integrity_path = derive_viewer_sidecars(
        risk_commit_path=args.risk_commit,
        risk_store_root=args.risk_store_root,
        plan_set_path=args.plan_set,
        dataset_bundle_path=args.dataset_bundle,
        output_dir=args.output_dir,
    )
    summary = {
        "status": "PASS",
        "frame_index": str(frame_path),
        "route_candidates": str(candidate_path),
        "route_integrity": str(integrity_path),
        "output_dir": str(args.output_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
