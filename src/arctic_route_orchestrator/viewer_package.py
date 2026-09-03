"""Atomic, identity-bound publication of a formal D Viewer package.

The public publisher accepts already-produced A/B/C/Replay artifacts. It does
not claim to create those upstream artifacts. Every output is built below a
private staging directory and becomes visible only after post-export identity
checks pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from arctic_route_orchestrator.scenario_identity import verify_viewer_identity
from arctic_route_orchestrator.strict_json import read_strict_json_object
from arctic_route_orchestrator.viewer_sidecars import derive_viewer_sidecars


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and (Path(env) / "arctic_route_contracts").is_dir():
        return Path(env).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


def _script_path(name: str) -> Path:
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "orchestrator_scripts" / name
    if getattr(sys, "_MEIPASS", None) and bundled.is_file():
        return bundled
    packaged = Path(__file__).resolve().parent / "_scripts" / name
    if packaged.is_file():
        return packaged
    source = Path(__file__).resolve().parents[2] / "scripts" / name
    if source.is_file():
        return source
    raise ValueError(f"required orchestrator script is missing: {name}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    return read_strict_json_object(path, label=label)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _run(command: list[str], *, label: str, timeout_seconds: float) -> None:
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(command, **popen_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            import signal

            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        stdout, stderr = process.communicate()
        detail = "\n".join(item for item in (stdout, stderr) if item)
        if detail:
            detail = f"\n{detail[-8000:]}"
        raise RuntimeError(f"{label} timed out after {timeout_seconds:g} seconds{detail}") from exc
    if process.returncode != 0:
        raise RuntimeError(f"{label} failed:\n{stdout}\n{stderr}")


def _script_command(python: Path, script: Path, arguments: list[str]) -> list[str]:
    if getattr(sys, "frozen", False) and python.resolve() == Path(sys.executable).resolve():
        return [
            sys.executable,
            "--orchestrator-script",
            script.name,
            "--",
            *arguments,
        ]
    return [str(python), str(script), *arguments]


def _derive_sidecars(
    *,
    risk_commit: dict[str, Any],
    risk_store_root: Path,
    plan_set_path: Path,
    dataset_bundle: dict[str, Any],
    staging: Path,
    timeout_seconds: float,
) -> tuple[Path, Path, Path]:
    sidecar_root = staging / "sidecars"
    commit_path = staging / "risk-window-commit.json"
    bundle_path = staging / "dataset-bundle.json"
    commit_path.write_text(
        json.dumps(risk_commit, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    bundle_path.write_text(
        json.dumps(dataset_bundle, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    _ = timeout_seconds
    result = derive_viewer_sidecars(
        risk_commit_path=commit_path,
        risk_store_root=risk_store_root,
        plan_set_path=plan_set_path,
        dataset_bundle_path=bundle_path,
        output_dir=sidecar_root,
    )
    _require(all(path.is_file() for path in result), "sidecar derivation did not publish all files")
    return result


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
    route_motion_sets: list[Path],
    route_motion_candidate_sets: list[Path],
    risk_explanation_manifest: Path | None,
    replay_manifest: Path | None,
    replay_snapshots_dir: Path | None,
    route_id: str,
    basemap_version: str,
    output_dir: Path,
    python: Path,
    export_script: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    arguments = [
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
        "--require-route-motion",
    ]
    if risk_store_root is not None:
        arguments += ["--risk-store-root", str(risk_store_root)]
    if land_mask is not None:
        arguments += ["--land-mask", str(land_mask)]
    for path in route_motion_sets:
        arguments += ["--route-motion-set", str(path)]
    for path in route_motion_candidate_sets:
        arguments += ["--route-motion-candidate-set", str(path)]
    if risk_explanation_manifest is not None:
        arguments += ["--risk-explanation-manifest", str(risk_explanation_manifest)]
    if replay_manifest is not None:
        arguments += ["--winter-replay-manifest", str(replay_manifest)]
    if replay_snapshots_dir is not None:
        arguments += ["--winter-replay-snapshots-dir", str(replay_snapshots_dir)]
    arguments += ["--basemap-version", basemap_version, "--output-dir", str(output_dir)]
    command = _script_command(python, export_script, arguments)
    _run(command, label="viewer export", timeout_seconds=timeout_seconds)
    return _read_json(output_dir / "bundle.json", "exported bundle")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _require_portable_json_value(value: object, *, location: str = "$") -> None:
    """Reject host-specific paths from immutable Viewer metadata."""

    if isinstance(value, dict):
        for key, item in value.items():
            _require_portable_json_value(item, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_portable_json_value(item, location=f"{location}[{index}]")
    elif isinstance(value, str) and (
        value.startswith(("/", "file://")) or _WINDOWS_ABSOLUTE_PATH.match(value)
    ):
        raise ValueError(f"Viewer metadata contains a build-host absolute path at {location}")


def _finalize_and_validate_package(
    package_dir: Path,
    *,
    bundle: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    required = {
        "bundle.json",
        "gebco_basemap.png",
        "basemap_metadata.json",
        "replay-viewer-preflight.json",
        "winter-combined-viewer-manifest.json",
        "checksums.json",
        "four-layer-route-plan-set-v3.json",
    }
    missing = sorted(name for name in required if not (package_dir / name).is_file())
    _require(not missing, "viewer export is missing required files: " + ", ".join(missing))
    preflight = _read_json(package_dir / "replay-viewer-preflight.json", "Viewer preflight")
    manifest = _read_json(package_dir / "winter-combined-viewer-manifest.json", "Viewer manifest")
    _require(preflight.get("overall") == "PASS", "Viewer preflight is not PASS")
    _require(manifest.get("status") == "PASS", "Viewer manifest is not PASS")
    manifest_identity = manifest.get("identity")
    _require(isinstance(manifest_identity, dict), "Viewer manifest identity is missing")
    for field, expected in summary["identity"].items():
        _require(
            manifest_identity.get(field) == expected,
            f"Viewer manifest identity drifted: {field}",
        )
    manifest["bundle_path"] = "bundle.json"
    source_artifacts = manifest.get("source_artifacts")
    if isinstance(source_artifacts, dict):
        for item in source_artifacts.values():
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                item["path"] = Path(item["path"]).name
    _write_json(package_dir / "winter-combined-viewer-manifest.json", manifest)
    _write_json(package_dir / "publish-summary.json", summary)
    for json_path in sorted(package_dir.glob("*.json")):
        _require_portable_json_value(_read_json(json_path, json_path.name))
    checksums = _read_json(package_dir / "checksums.json", "Viewer checksums")
    files = checksums.get("files")
    _require(isinstance(files, dict), "Viewer checksums.files must be an object")
    checksum_names = set(files) | {
        "publish-summary.json",
        "winter-combined-viewer-manifest.json",
    }
    for name in checksum_names:
        _require(Path(name).name == name, f"unsafe Viewer checksum entry: {name}")
        _require((package_dir / name).is_file(), f"checksum target is missing: {name}")
    checksums["files"] = {name: _sha256_file(package_dir / name) for name in sorted(checksum_names)}
    _write_json(package_dir / "checksums.json", checksums)
    checked = _read_json(package_dir / "checksums.json", "final Viewer checksums")["files"]
    for name, expected in checked.items():
        _require(_sha256_file(package_dir / name) == expected, f"checksum mismatch: {name}")
    presentation = bundle.get("combined_presentation") or {}
    candidates = bundle.get("route_candidates") or {}
    motion = bundle.get("formal_motion_inspection") or {}
    _require(presentation.get("status") == "PUBLISHED", "Viewer presentation is not published")
    _require(len(candidates.get("candidates") or []) == 12, "Viewer must contain 12 routes")
    _require(motion.get("valid") is True, "Viewer formal motion validation is not PASS")


def build_parser() -> argparse.ArgumentParser:
    root = _workspace_root()
    parser = argparse.ArgumentParser(prog="arctic-route-publish-viewer")
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument(
        "--contracts-config-root", type=Path, default=root / "arctic_route_contracts" / "configs"
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
    parser.add_argument("--risk-explanation-manifest", type=Path)
    parser.add_argument("--route-motion-set", type=Path, action="append", default=[])
    parser.add_argument("--route-motion-candidate-set", type=Path, action="append", default=[])
    parser.add_argument("--winter-replay-manifest", type=Path)
    parser.add_argument("--winter-replay-snapshots-dir", type=Path)
    parser.add_argument("--basemap-version", default="gebco-2026-d5a7e2fe3915-7baad866")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--export-script", type=Path, default=None)
    parser.add_argument("--subprocess-timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _require(args.subprocess_timeout_seconds > 0, "subprocess timeout must be positive")
    target = args.output_dir.resolve()
    if target.exists():
        raise ValueError(f"immutable output directory already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    supplied = (args.route_candidates, args.route_integrity, args.risk_frame_index)
    _require(
        all(supplied) or not any(supplied),
        "route-candidates, route-integrity and risk-frame-index must be supplied together",
    )
    _require(
        args.winter_replay_manifest is not None or args.winter_replay_snapshots_dir is None,
        "winter replay snapshots directory requires a replay manifest",
    )

    dataset_bundle = _read_json(args.dataset_bundle, "DatasetBundle")
    run_context = _read_json(args.run_context, "RunContext")
    risk_commit = _read_json(args.risk_window_commit, "RiskWindow commit")
    plan_set = _read_json(args.plan_set, "plan set")
    staging = target.parent / f".{target.name}.{os.getpid()}.staging"
    if staging.exists():
        raise ValueError(f"staging directory already exists: {staging}")
    staging.mkdir()
    publish_dir = staging / "package"
    try:
        if all(supplied):
            route_candidates, route_integrity, frame_index = supplied
        else:
            _require(
                args.risk_store_root is not None,
                "--risk-store-root is required when sidecars are derived",
            )
            frame_index, route_candidates, route_integrity = _derive_sidecars(
                risk_commit=risk_commit,
                risk_store_root=args.risk_store_root,
                plan_set_path=args.plan_set,
                dataset_bundle=dataset_bundle,
                staging=staging,
                timeout_seconds=args.subprocess_timeout_seconds,
            )
        candidates_doc = _read_json(route_candidates, "route candidates")
        identity = verify_viewer_identity(
            scenario_id=args.scenario_id,
            contracts_config_root=args.contracts_config_root,
            dataset_bundle=dataset_bundle,
            run_context=run_context,
            risk_index=_read_json(frame_index, "RiskFrame index"),
            risk_commit=risk_commit,
            plan_set=plan_set,
            route_candidates=candidates_doc,
        )
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
            route_motion_sets=args.route_motion_set,
            route_motion_candidate_sets=args.route_motion_candidate_set,
            risk_explanation_manifest=args.risk_explanation_manifest,
            replay_manifest=args.winter_replay_manifest,
            replay_snapshots_dir=args.winter_replay_snapshots_dir,
            route_id=identity["corridor_id"],
            basemap_version=args.basemap_version,
            output_dir=publish_dir,
            python=args.python,
            export_script=args.export_script or _script_path("replay_viewer_export.py"),
            timeout_seconds=args.subprocess_timeout_seconds,
        )
        presentation = bundle.get("combined_presentation")
        _require(isinstance(presentation, dict), "exported combined presentation is missing")
        expected = {
            "dataset_bundle_id": identity["dataset_bundle_id"],
            "risk_window_id": identity["risk_window_id"],
            "layer_set_id": identity["layer_set_id"],
            "candidate_set_id": candidates_doc.get("candidate_set_id"),
            "selected_candidate_id": identity["selected_candidate_id"],
        }
        actual_scenario = presentation.get(
            "scenario_id", bundle.get("replay", {}).get("scenario_id")
        )
        _require(actual_scenario == args.scenario_id, "exported scenario identity drifted")
        for field, expected_value in expected.items():
            _require(
                presentation.get(field) == expected_value,
                f"exported {field} drifted from validated identity",
            )
        if args.winter_replay_manifest is not None:
            _require(
                presentation.get("replanning_status")
                in {
                    "PUBLISHED_CAUSAL_REPLAY",
                    "PUBLISHED_RETROSPECTIVE_DYNAMIC_REPLAY",
                },
                "dynamic replay input did not produce a published dynamic replay",
            )
        summary = {
            "status": "PASS",
            "scenario_id": args.scenario_id,
            # The published package is relocatable; build-host paths belong in
            # operator logs, never in its immutable metadata.
            "output_dir": ".",
            "identity": identity,
            "candidate_set_id": candidates_doc.get("candidate_set_id"),
            "route_motion_sets": [
                item.get("motion_set_id") for item in (bundle.get("route_motion_sets") or [])
            ],
            "dynamic_replay": args.winter_replay_manifest is not None,
        }
        _finalize_and_validate_package(publish_dir, bundle=bundle, summary=summary)
        if os.name == "posix":
            directory_fd = os.open(publish_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        os.replace(publish_dir, target)
        if os.name == "posix":
            parent_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


__all__ = ["build_parser", "main"]
