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
from datetime import timedelta
from pathlib import Path

from arctic_route_contracts.timeutils import parse_utc
from arctic_route_orchestrator.replay.route_integrity import audit_route
from arctic_route_orchestrator.route_presentation import project_route_candidates
from arctic_route_planning.contracts.windows import RiskWindowQuery
from arctic_route_planning.publishing import four_layer_route_plan_set_from_dict
from arctic_route_risk import PersistentRiskStore


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="derive-murmansk-sidecars")
    parser.add_argument("--risk-commit", type=Path, required=True)
    parser.add_argument("--risk-store-root", type=Path, required=True)
    parser.add_argument("--plan-set", type=Path, required=True)
    parser.add_argument("--dataset-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.output_dir.exists():
        raise ValueError(
            f"immutable output directory already exists: {args.output_dir}"
        )
    args.output_dir.mkdir(parents=True)

    commit = json.loads(args.risk_commit.read_text(encoding="utf-8"))
    if commit.get("schema_version") != "bc.risk-window-commit.v1":
        raise ValueError(f"unexpected risk commit schema: {commit.get('schema_version')}")
    query = RiskWindowQuery(
        start=parse_utc(commit["start"]),
        end=parse_utc(commit["end"]),
        interval=timedelta(seconds=int(commit["interval_seconds"])),
        run_id=commit["run_id"],
        scenario_id=commit["scenario_id"],
        corridor_id=commit["corridor_id"],
        generation_id=commit["generation_id"],
        vessel_profile_id=commit["vessel_profile_id"],
        config_digest=commit["config_digest"],
        model_config_digest=commit["model_config_digest"],
        as_of=parse_utc(commit["as_of"]),
    )

    store = PersistentRiskStore(args.risk_store_root)
    window = store.get_committed_window(query)
    if window.commit_id != commit["commit_id"]:
        raise ValueError("loaded window commit_id differs from commit file")
    if window.content_digest != commit["content_digest"]:
        raise ValueError("loaded window content_digest differs from commit file")

    plan_document = json.loads(args.plan_set.read_text(encoding="utf-8"))
    plan_set = four_layer_route_plan_set_from_dict(plan_document)

    # 1. frame-index.json (mirrors orchestrator runner.py L1688)
    bundle_document = json.loads(args.dataset_bundle.read_text(encoding="utf-8"))
    frame_index = {
        "artifact_kind": "orchestrator-replay-risk-frame-index",
        "status": "FORMAL_VALIDATED",
        "commit_id": window.commit_id,
        "content_digest": window.content_digest,
        "dataset_bundle_id": bundle_document.get("bundle_id"),
        "dataset_bundle_digest": bundle_document.get("bundle_digest"),
        "frame_ids": [item.risk_id for item in window.frames],
        "frame_schema": "bc.risk-frame.v2",
        "grid_profile": "formal_replay",
        "model_config_digest": window.frames[0].model_config_digest,
        "risk_store": args.risk_store_root.name,
        "run_id": commit["run_id"],
        "scenario_id": commit["scenario_id"],
    }
    _write_json(args.output_dir / "frame-index.json", frame_index)

    # 2. route-candidates.json (atomic 4 x 3 presentation projection)
    candidate_document = project_route_candidates(plan_set)
    _write_json(args.output_dir / "route-candidates.json", candidate_document)

    # 3. route-integrity.json (audit every route against committed frames)
    integrity = [
        audit_route(plan, window.frames)
        for bundle in plan_set.layers
        for plan in bundle.plans.values()
    ]
    if len(integrity) != 12:
        raise ValueError(f"expected 12 audited routes, got {len(integrity)}")
    if any(item["status"] != "PASS" for item in integrity):
        failed = [item["route_id"] for item in integrity if item["status"] != "PASS"]
        raise ValueError(f"route integrity FAIL for: {failed}")
    _write_json(args.output_dir / "route-integrity.json", integrity)

    summary = {
        "status": "PASS",
        "scenario_id": commit["scenario_id"],
        "run_id": commit["run_id"],
        "commit_id": window.commit_id,
        "frame_count": window.count,
        "layer_set_id": plan_set.layer_set_id,
        "candidate_set_id": candidate_document["candidate_set_id"],
        "selected_candidate_id": candidate_document["selected_candidate_id"],
        "integrity_routes": len(integrity),
        "integrity_status": "PASS",
        "output_dir": str(args.output_dir),
    }
    _write_json(args.output_dir / "derive-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
