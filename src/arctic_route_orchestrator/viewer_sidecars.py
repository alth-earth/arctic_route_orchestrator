"""Derive formal Viewer transport sidecars from already-frozen B/C artifacts."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from arctic_route_contracts.timeutils import parse_utc
from arctic_route_planning.contracts.windows import RiskWindowQuery
from arctic_route_planning.publishing import four_layer_route_plan_set_from_dict
from arctic_route_risk.publishing import PersistentRiskStore

from .replay.route_integrity import audit_route
from .route_presentation import project_route_candidates
from .strict_json import read_strict_json_object


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def derive_viewer_sidecars(
    *,
    risk_commit_path: Path,
    risk_store_root: Path,
    plan_set_path: Path,
    dataset_bundle_path: Path,
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    """Publish frame index, route candidates and route integrity atomically by directory."""

    if output_dir.exists():
        raise ValueError(f"immutable output directory already exists: {output_dir}")
    commit = read_strict_json_object(risk_commit_path, label="RiskWindow commit")
    if commit.get("schema_version") != "bc.risk-window-commit.v1":
        raise ValueError(f"unexpected risk commit schema: {commit.get('schema_version')}")
    plan_document = read_strict_json_object(plan_set_path, label="four-layer plan set")
    bundle_document = read_strict_json_object(dataset_bundle_path, label="DatasetBundle")
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
    window = PersistentRiskStore(risk_store_root).get_committed_window(query)
    if window.commit_id != commit["commit_id"] or window.content_digest != commit["content_digest"]:
        raise ValueError("loaded risk window identity differs from commit file")
    plan_set = four_layer_route_plan_set_from_dict(plan_document)
    frame_index: dict[str, Any] = {
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
        "risk_store": risk_store_root.name,
        "run_id": commit["run_id"],
        "scenario_id": commit["scenario_id"],
    }
    candidates = project_route_candidates(plan_set)
    integrity = [
        audit_route(plan, window.frames)
        for layer in plan_set.layers
        for plan in layer.plans.values()
    ]
    if len(integrity) != 12 or any(item["status"] != "PASS" for item in integrity):
        failed = [item.get("route_id") for item in integrity if item.get("status") != "PASS"]
        raise ValueError(f"route integrity is not a complete 12-route PASS: {failed}")
    output_dir.mkdir(parents=True)
    frame_path = output_dir / "frame-index.json"
    candidate_path = output_dir / "route-candidates.json"
    integrity_path = output_dir / "route-integrity.json"
    _write_json(frame_path, frame_index)
    _write_json(candidate_path, candidates)
    _write_json(integrity_path, integrity)
    return frame_path, candidate_path, integrity_path


__all__ = ["derive_viewer_sidecars"]
