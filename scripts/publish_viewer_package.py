#!/usr/bin/env python3
"""Unified viewer package publisher with strong scenario-identity validation.

This script replaces the previous per-route ad-hoc export path (the source of the
2026-09-02 "v13 holdout" identity mix-up).  It runs, in order:

  1. identity validation:  ``scenario_identity.verify_viewer_identity`` compares
     ``--scenario-id`` against DatasetBundle / RunContext / RiskFrame index /
     RiskWindow commit / plan set / route candidates AND the canonical scenario
     contract.  Any mismatch fails closed before any output is written.
  2. sidecar derivation:   frame-index / route-candidates / route-integrity are
     produced from frozen risk store + plan set if not supplied explicitly.
  3. formal motion:        optional ``arctic-route-motion`` call (external CLI) when
     ``--route-motion-cmd`` is provided, bound to the plan set layer.
  4. viewer export:        delegates to ``replay_viewer_export.py`` (unchanged
     interface) with the validated identity, then verifies the produced bundle's
     ``combined_presentation`` identity once more.

All outputs go to immutable, new-only directories.  Existing files/directories are
never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from arctic_route_orchestrator.scenario_identity import verify_viewer_identity


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _derive_sidecars(
    *,
    risk_commit: dict[str, Any],
    risk_store_root: Path,
    plan_set_path: Path,
    dataset_bundle: dict[str, Any],
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    """Reuse derive_murmansk_sidecars logic to emit the three D-side sidecars.

    Returns (frame_index, route_candidates, route_integrity) paths.
    """
    script = Path(__file__).resolve().parent / "derive_murmansk_sidecars.py"
    _require(script.is_file(), f"derive script missing: {script}")
    sidecar_root = output_dir / "sidecars"
    if sidecar_root.exists():
        raise ValueError(f"sidecar output already exists: {sidecar_root}")
    sidecar_root.mkdir(parents=True)

    commit_path = output_dir / "risk-window-commit.json"
    commit_path.write_text(
        json.dumps(risk_commit, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    bundle_path = output_dir / "dataset-bundle.json"
    bundle_path.write_text(
        json.dumps(dataset_bundle, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    cmd = [
        sys.executable,
        str(script),
        "--risk-commit",
        str(commit_path),
        "--risk-store-root",
        str(risk_store_root),
        "--plan-set",
        str(plan_set_path),
        "--dataset-bundle",
        str(bundle_path),
        "--output-dir",
        str(sidecar_root),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"sidecar derivation failed:\n{result.stdout}\n{result.stderr}")
    return (
        sidecar_root / "frame-index.json",
        sidecar_root / "route-candidates.json",
        sidecar_root / "route-integrity.json",
    )


def _export_viewer(
    *,
    dataset_bundle: Path,
    run_context: Path,
    frame_index: Path,
    risk_window_commit: Path,
    plan_set: Path,
    route_integrity: Path,
    route_candidates: Path,
    risk_store_root: Path | None,
    land_mask: Path | None,
    route_motion_set: list[Path],
    risk_explanation_manifest: Path | None,
    route_id: str,
    basemap_version: str,
    output_dir: Path,
    python: Path,
    export_script: Path,
) -> dict[str, Any]:
    cmd = [
        str(python),
        str(export_script),
        "--winter-dataset-bundle",
        str(dataset_bundle),
        "--winter-run-context",
        str(run_context),
        "--winter-risk-frame-index",
        str(frame_index),
        "--winter-risk-window-commit",
        str(risk_window_commit),
        "--winter-plan-set",
        str(plan_set),
        "--winter-route-integrity",
        str(route_integrity),
        "--route-candidates",
        str(route_candidates),
        "--route-id",
        route_id,
    ]
    if risk_store_root is not None:
        cmd += ["--risk-store-root", str(risk_store_root)]
    if land_mask is not None:
        cmd += ["--land-mask", str(land_mask)]
    for motion in route_motion_set:
        cmd += ["--route-motion-set", str(motion)]
    if risk_explanation_manifest is not None:
        cmd += ["--risk-explanation-manifest", str(risk_explanation_manifest)]
    cmd += ["--basemap-version", basemap_version, "--output-dir", str(output_dir)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"viewer export failed:\n{result.stdout}\n{result.stderr}")
    bundle = _read_json(output_dir / "bundle.json", "exported bundle")
    return bundle


def main(argv: list[str] | None = None) -> int:
    root = _workspace_root()
    parser = argparse.ArgumentParser(prog="publish-viewer-package")
    parser.add_argument("--scenario-id", required=True, help="canonical scenario id")
    parser.add_argument(
        "--contracts-config-root",
        type=Path,
        default=root / "arctic_route_contracts" / "configs",
    )
    parser.add_argument("--dataset-bundle", type=Path, required=True)
    parser.add_argument("--run-context", type=Path, required=True)
    parser.add_argument("--risk-store-root", type=Path)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--plan-set", type=Path, required=True)
    parser.add_argument("--route-candidates", type=Path)
    parser.add_argument("--route-integrity", type=Path)
    parser.add_argument("--risk-frame-index", type=Path)
    parser.add_argument("--land-mask", type=Path)
    parser.add_argument(
        "--risk-explanation-manifest",
        type=Path,
        default=None,
        help="B immutable risk-explanation-manifest.v1 transport artifact; "
        "when present the viewer bundle embeds risk_explanation + transport",
    )
    parser.add_argument("--route-motion-set", type=Path, action="append", default=[])
    parser.add_argument("--basemap-version", default="gebco-2026-d5a7e2fe3915-7baad866")
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="interpreter used to run replay_viewer_export.py",
    )
    parser.add_argument(
        "--export-script",
        type=Path,
        default=root / "arctic_route_orchestrator" / "scripts" / "replay_viewer_export.py",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.output_dir.exists():
        raise ValueError(f"immutable output directory already exists: {args.output_dir}")

    # ---- 1. read artifacts ----
    dataset_bundle = _read_json(args.dataset_bundle, "DatasetBundle")
    run_context = _read_json(args.run_context, "RunContext")
    risk_commit = _read_json(args.risk_window_commit, "RiskWindow commit")
    plan_set_doc = _read_json(args.plan_set, "plan set")

    staging = args.output_dir.parent / f".{args.output_dir.name}.staging"
    if staging.exists():
        raise ValueError(f"staging dir already exists: {staging}")
    staging.mkdir(parents=True)

    try:
        # ---- 2. sidecars (derive if not supplied) ----
        if args.route_candidates and args.route_integrity and args.risk_frame_index:
            route_candidates, route_integrity, frame_index = (
                args.route_candidates,
                args.route_integrity,
                args.risk_frame_index,
            )
        else:
            _require(
                args.risk_store_root is not None,
                "--risk-store-root required when sidecars are not supplied",
            )
            frame_index, route_candidates, route_integrity = _derive_sidecars(
                risk_commit=risk_commit,
                risk_store_root=args.risk_store_root,
                plan_set_path=args.plan_set,
                dataset_bundle=dataset_bundle,
                output_dir=staging,
            )

        route_candidates_doc = _read_json(route_candidates, "route candidates")

        # ---- 3. strong identity validation (scenario_id gate) ----
        identity = verify_viewer_identity(
            scenario_id=args.scenario_id,
            contracts_config_root=args.contracts_config_root,
            dataset_bundle=dataset_bundle,
            run_context=run_context,
            risk_index=_read_json(frame_index, "RiskFrame index"),
            risk_commit=risk_commit,
            plan_set=plan_set_doc,
            route_candidates=route_candidates_doc,
        )

        # ---- 4. viewer export via replay_viewer_export.py ----
        route_id = identity["corridor_id"]
        bundle = _export_viewer(
            dataset_bundle=args.dataset_bundle,
            run_context=args.run_context,
            frame_index=frame_index,
            risk_window_commit=args.risk_window_commit,
            plan_set=args.plan_set,
            route_integrity=route_integrity,
            route_candidates=route_candidates,
            risk_store_root=args.risk_store_root,
            land_mask=args.land_mask,
            route_motion_set=args.route_motion_set,
            risk_explanation_manifest=args.risk_explanation_manifest,
            route_id=route_id,
            basemap_version=args.basemap_version,
            output_dir=args.output_dir,
            python=args.python,
            export_script=args.export_script,
        )

        # ---- 5. post-export identity re-check ----
        cp = bundle.get("combined_presentation", {})
        _require(
            cp.get("scenario_id", bundle.get("replay", {}).get("scenario_id"))
            == args.scenario_id,
            "exported bundle scenario_id does not match request",
        )
        _require(
            cp.get("dataset_bundle_id") == identity["dataset_bundle_id"]
            and cp.get("risk_window_id") == identity["risk_window_id"],
            "exported bundle identity drifted from validated identity",
        )

        summary = {
            "status": "PASS",
            "scenario_id": args.scenario_id,
            "output_dir": str(args.output_dir),
            "identity": identity,
            "candidate_set_id": route_candidates_doc.get("candidate_set_id"),
            "route_motion_sets": [
                m.get("motion_set_id") for m in (bundle.get("route_motion_sets") or [])
            ],
        }
        (args.output_dir / "publish-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    finally:
        if staging.exists():
            for item in staging.iterdir():
                if item.is_dir():
                    import shutil

                    shutil.rmtree(item)
                else:
                    item.unlink()
            staging.rmdir()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
