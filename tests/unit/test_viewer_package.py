from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from arctic_route_orchestrator import viewer_package


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_package(output: Path, bundle: dict, identity: dict) -> None:
    output.mkdir()
    presentation = bundle.setdefault("combined_presentation", {})
    presentation.setdefault("assembly_id", "assembly-a")
    presentation.setdefault("assembly_digest", "a" * 64)
    candidate_set = {
        "schema_version": "cd.route-motion-candidate-set.v1",
        "motion_candidate_set_id": "route-motion-candidate-set-sha256-" + "b" * 64,
    }
    bundle.setdefault("route_motion_candidate_sets", [candidate_set])
    candidate_ids = [
        item["motion_candidate_set_id"] for item in bundle["route_motion_candidate_sets"]
    ]
    presentation.setdefault("route_motion_candidate_set_ids", candidate_ids)
    presentation.setdefault(
        "route_motion_candidate_set_bindings",
        [{"motion_candidate_set_id": item} for item in candidate_ids],
    )
    motion_set = {
        "schema_version": "cd.route-motion-set.v1",
        "motion_set_id": "route-motion-set-sha256-" + "c" * 64,
    }
    bundle.setdefault("route_motion_sets", [motion_set])
    motion_ids = [item["motion_set_id"] for item in bundle["route_motion_sets"]]
    presentation.setdefault("route_motion_set_ids", motion_ids)
    presentation.setdefault(
        "route_motion_set_bindings",
        [{"motion_set_id": item} for item in motion_ids],
    )
    _write(output / "bundle.json", bundle)
    (output / "gebco_basemap.png").write_bytes(b"png")
    _write(output / "basemap_metadata.json", {})
    _write(output / "replay-viewer-preflight.json", {"overall": "PASS"})
    _write(
        output / "winter-combined-viewer-manifest.json",
        {
            "status": "PASS",
            "identity": {**identity, "risk_frame_count": 145},
            "bundle_path": str(output / "bundle.json"),
            "formal_motion_required": True,
            "route_motion_sets": len(motion_ids),
            "route_motion_candidate_sets": len(candidate_ids),
            "transport_files": {
                "plan_set": "four-layer-route-plan-set-v3.json",
                "route_motion_sets": ["route-motion-set-r1.json"],
                "route_motion_candidate_sets": ["route-motion-candidate-set-r1.json"],
            },
        },
    )
    _write(output / "four-layer-route-plan-set-v3.json", {})
    _write(output / "route-motion-set-r1.json", motion_set)
    _write(output / "route-motion-candidate-set-r1.json", candidate_set)
    names = [
        "bundle.json",
        "gebco_basemap.png",
        "basemap_metadata.json",
        "replay-viewer-preflight.json",
        "winter-combined-viewer-manifest.json",
        "four-layer-route-plan-set-v3.json",
        "route-motion-set-r1.json",
        "route-motion-candidate-set-r1.json",
    ]
    _write(output / "checksums.json", {"files": {name: _sha256(output / name) for name in names}})


def _inputs(tmp_path: Path) -> dict[str, Path]:
    return {
        name: _write(tmp_path / f"{name}.json", {})
        for name in (
            "dataset",
            "context",
            "commit",
            "plan",
            "candidates",
            "integrity",
            "index",
        )
    }


def _argv(tmp_path: Path, inputs: dict[str, Path]) -> list[str]:
    return [
        "--scenario-id",
        "scenario-a",
        "--contracts-config-root",
        str(tmp_path),
        "--dataset-bundle",
        str(inputs["dataset"]),
        "--run-context",
        str(inputs["context"]),
        "--risk-window-commit",
        str(inputs["commit"]),
        "--plan-set",
        str(inputs["plan"]),
        "--route-candidates",
        str(inputs["candidates"]),
        "--route-integrity",
        str(inputs["integrity"]),
        "--risk-frame-index",
        str(inputs["index"]),
        "--output-dir",
        str(tmp_path / "published"),
    ]


def test_strict_json_rejects_duplicate_keys_and_non_finite(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        viewer_package._read_json(duplicate, "test")
    non_finite = tmp_path / "nan.json"
    non_finite.write_text('{"a": NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON number"):
        viewer_package._read_json(non_finite, "test")


@pytest.mark.parametrize(
    "value",
    ["/root/build/input.json", "C:\\build\\input.json", "file:///tmp/x", "../risk-store/frames"],
)
def test_portable_viewer_metadata_rejects_absolute_paths(value: str) -> None:
    with pytest.raises(ValueError, match=r"(absolute path|parent path)"):
        viewer_package._require_portable_json_value({"nested": [value]})


def test_portable_viewer_metadata_accepts_relative_and_content_ids() -> None:
    viewer_package._require_portable_json_value(
        {"resource": "bundle.json", "identity": "risk-window-sha256-abc"}
    )


def test_partial_sidecar_selection_fails_before_publication(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    argv = _argv(tmp_path, inputs)
    option = argv.index("--route-integrity")
    del argv[option : option + 2]
    with pytest.raises(ValueError, match="must be supplied together"):
        viewer_package.main(argv)
    assert not (tmp_path / "published").exists()
    assert not list(tmp_path.glob(".*.staging"))


def test_post_export_identity_failure_leaves_no_visible_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    _write(inputs["candidates"], {"candidate_set_id": "candidate-set-a"})
    identity = {
        "corridor_id": "corridor-a",
        "dataset_bundle_id": "bundle-a",
        "risk_window_id": "risk-a",
        "layer_set_id": "layer-a",
        "selected_candidate_id": "route-a",
    }
    monkeypatch.setattr(viewer_package, "verify_viewer_identity", lambda **_: identity)

    def fake_export(**kwargs):
        output = kwargs["output_dir"]
        bundle = {
            "replay": {"scenario_id": "scenario-a"},
            "combined_presentation": {
                "status": "PUBLISHED",
                "dataset_bundle_id": "wrong-bundle",
                "risk_window_id": "risk-a",
                "layer_set_id": "layer-a",
                "candidate_set_id": "candidate-set-a",
                "selected_candidate_id": "route-a",
            },
            "formal_motion_inspection": {"valid": True},
            "route_candidates": {"candidates": [{}] * 12},
        }
        _fake_package(output, bundle, identity)
        return bundle

    monkeypatch.setattr(viewer_package, "_export_viewer", fake_export)
    with pytest.raises(ValueError, match="dataset_bundle_id drifted"):
        viewer_package.main(_argv(tmp_path, inputs))
    assert not (tmp_path / "published").exists()
    assert not list(tmp_path.glob(".*.staging"))


def test_success_is_atomically_promoted_after_full_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    _write(inputs["candidates"], {"candidate_set_id": "candidate-set-a"})
    identity = {
        "corridor_id": "corridor-a",
        "dataset_bundle_id": "bundle-a",
        "risk_window_id": "risk-a",
        "layer_set_id": "layer-a",
        "selected_candidate_id": "route-a",
    }
    monkeypatch.setattr(viewer_package, "verify_viewer_identity", lambda **_: identity)

    def fake_export(**kwargs):
        output = kwargs["output_dir"]
        bundle = {
            "replay": {"scenario_id": "scenario-a"},
            "combined_presentation": {
                "status": "PUBLISHED",
                "dataset_bundle_id": "bundle-a",
                "risk_window_id": "risk-a",
                "layer_set_id": "layer-a",
                "candidate_set_id": "candidate-set-a",
                "selected_candidate_id": "route-a",
            },
            "formal_motion_inspection": {"valid": True},
            "route_candidates": {"candidates": [{}] * 12},
        }
        _fake_package(output, bundle, identity)
        return bundle

    monkeypatch.setattr(viewer_package, "_export_viewer", fake_export)
    assert viewer_package.main(_argv(tmp_path, inputs)) == 0
    published = tmp_path / "published"
    assert (published / "bundle.json").is_file()
    assert json.loads((published / "publish-summary.json").read_text())["status"] == "PASS"
    manifest = json.loads((published / "winter-combined-viewer-manifest.json").read_text())
    assert manifest["bundle_path"] == "bundle.json"
    checksums = json.loads((published / "checksums.json").read_text())["files"]
    assert "publish-summary.json" in checksums
    assert checksums["publish-summary.json"] == _sha256(published / "publish-summary.json")
    assert not list(tmp_path.glob(".*.staging"))


def test_current_standard_export_rejects_missing_motion_candidate_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    _write(inputs["candidates"], {"candidate_set_id": "candidate-set-a"})
    identity = {
        "corridor_id": "corridor-a",
        "dataset_bundle_id": "bundle-a",
        "risk_window_id": "risk-a",
        "layer_set_id": "layer-a",
        "selected_candidate_id": "route-a",
    }
    monkeypatch.setattr(viewer_package, "verify_viewer_identity", lambda **_: identity)

    def fake_export(**kwargs):
        bundle = {
            "replay": {"scenario_id": "scenario-a"},
            "combined_presentation": {
                "status": "PUBLISHED",
                "dataset_bundle_id": "bundle-a",
                "risk_window_id": "risk-a",
                "layer_set_id": "layer-a",
                "candidate_set_id": "candidate-set-a",
                "selected_candidate_id": "route-a",
                "assembly_id": "assembly-a",
                "assembly_digest": "a" * 64,
            },
            "formal_motion_inspection": {"valid": True},
            "route_candidates": {"candidates": [{}] * 12},
        }
        _fake_package(kwargs["output_dir"], bundle, identity)
        bundle["route_motion_candidate_sets"] = []
        return bundle

    monkeypatch.setattr(viewer_package, "_export_viewer", fake_export)
    with pytest.raises(ValueError, match="route motion candidate sets"):
        viewer_package.main(_argv(tmp_path, inputs))
    assert not (tmp_path / "published").exists()
    assert not list(tmp_path.glob(".*.staging"))


def test_export_requires_formal_motion_and_forwards_dynamic_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        output.mkdir()
        _write(output / "bundle.json", {})

    monkeypatch.setattr(viewer_package, "_run", fake_run)
    common = tmp_path / "input.json"
    _write(common, {})
    viewer_package._export_viewer(
        dataset_bundle=common,
        run_context=common,
        frame_index=common,
        risk_window_commit=common,
        plan_set=common,
        route_integrity=common,
        route_candidates=common,
        risk_store_root=tmp_path,
        land_mask=None,
        route_motion_sets=[common],
        route_motion_candidate_sets=[common],
        risk_explanation_manifest=None,
        replay_manifest=common,
        replay_snapshots_dir=tmp_path,
        route_id="corridor-a",
        basemap_version="map-a",
        output_dir=output,
        python=Path("python"),
        export_script=Path("export.py"),
        timeout_seconds=12,
    )
    command = captured["command"]
    assert "--require-route-motion" in command
    assert "--route-motion-candidate-set" in command
    assert "--winter-replay-manifest" in command
    assert "--winter-replay-snapshots-dir" in command


def test_manifest_only_dynamic_replay_is_allowed_before_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    argv = [
        *_argv(tmp_path, inputs),
        "--winter-replay-manifest",
        str(inputs["context"]),
    ]
    identity = {
        "corridor_id": "corridor-a",
        "dataset_bundle_id": "bundle-a",
        "risk_window_id": "risk-a",
        "layer_set_id": "layer-a",
        "selected_candidate_id": "route-a",
    }
    _write(inputs["candidates"], {"candidate_set_id": "candidate-set-a"})
    monkeypatch.setattr(viewer_package, "verify_viewer_identity", lambda **_: identity)

    def fake_export(**kwargs):
        assert kwargs["replay_manifest"] == inputs["context"]
        assert kwargs["replay_snapshots_dir"] is None
        bundle = {
            "replay": {"scenario_id": "scenario-a"},
            "combined_presentation": {
                "status": "PUBLISHED",
                "dataset_bundle_id": "bundle-a",
                "risk_window_id": "risk-a",
                "layer_set_id": "layer-a",
                "candidate_set_id": "candidate-set-a",
                "selected_candidate_id": "route-a",
                "replanning_status": "PUBLISHED_CAUSAL_REPLAY",
            },
            "formal_motion_inspection": {"valid": True},
            "route_candidates": {"candidates": [{}] * 12},
        }
        _fake_package(kwargs["output_dir"], bundle, identity)
        return bundle

    monkeypatch.setattr(viewer_package, "_export_viewer", fake_export)
    assert viewer_package.main(argv) == 0


def test_frozen_script_command_dispatches_through_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "control-center"
    script = tmp_path / "replay_viewer_export.py"
    monkeypatch.setattr(viewer_package.sys, "frozen", True, raising=False)
    monkeypatch.setattr(viewer_package.sys, "executable", str(executable))
    assert viewer_package._script_command(executable, script, ["--x", "y"]) == [
        str(executable),
        "--orchestrator-script",
        "replay_viewer_export.py",
        "--",
        "--x",
        "y",
    ]
