"""Replay-driven presentation/L2 preflight tests (Strategy B)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np

from arctic_route_orchestrator.replay.geospatial import LandMaskSampler
from arctic_route_orchestrator.replay.preflight import (
    run_l2_preflight,
    run_viewer_preflight,
)

ARTIFACT_ROOT = Path(
    "/root/my_project/work_package_a/data/output/rc2-smoke/causal-replay-mvp/"
    "sb-viewer-baseline-12h-det"
)
DATA_ROOT = Path("/root/my_project/work_package_a/data")
ROUTE_ID = "tromso_to_isfjorden_outer"


def _load_artifact() -> tuple[dict, list[dict]]:
    manifest = json.loads(
        (ARTIFACT_ROOT / "causal-replay-manifest.json").read_text(encoding="utf-8")
    )
    snapshots = [
        json.loads(
            (ARTIFACT_ROOT / "snapshots" / f"{entry['index']:04d}.json").read_text(
                encoding="utf-8"
            )
        )
        for entry in manifest["snapshots"]
    ]
    return manifest, snapshots


def test_l2_preflight_real_authoritative_route_passes() -> None:
    manifest, snapshots = _load_artifact()
    land_mask = next(
        (DATA_ROOT / "raw" / ROUTE_ID / "land_sea_mask").glob("**/*.nc")
    )
    result = run_l2_preflight(
        manifest,
        snapshots,
        land_mask,
        output_path=ARTIFACT_ROOT.parent / "test-l2-preflight.json",
    )
    assert result.overall == "PASS"
    assert result.document["presentation_eligible_l2"] is True
    assert result.document["dataset"]["kind"] == "GEBCO_2026_land_sea_mask"
    for route in result.document["route_checks"]:
        assert route["status"] == "PASS"
        assert route["land_cells"] == 0
    assert result.document["sampling_contract"]["method"] == (
        "raster_cell_traversal_linear_lon_lat"
    )


def test_viewer_preflight_real_artifact_eligible() -> None:
    manifest, snapshots = _load_artifact()
    result = run_viewer_preflight(
        manifest,
        snapshots,
        data_root=DATA_ROOT,
        route_id=ROUTE_ID,
        output_path=ARTIFACT_ROOT.parent / "test-viewer-preflight.json",
    )
    assert result.overall == "PASS"
    assert result.document["presentation_eligible"] is True
    assert result.document["artifact_validation"]["snapshots"] == "PASS"
    assert result.document["artifact_validation"]["manifest"] == "PASS"
    counts = result.document["event_counts"]
    assert counts.get("REPLAN_DECIDED") == 5
    assert counts.get("ROUTE_CHANGED") == 4
    assert counts.get("REPLAN_SKIPPED") == 1


def test_l2_preflight_fails_closed_on_invalid_route() -> None:
    manifest, snapshots = _load_artifact()

    def _land_mask(_path):
        longitude = tuple(float(value) for value in range(10, 13))
        latitude = tuple(float(value) for value in range(68, 71))
        sea = np.zeros((3, 3), dtype=bool)
        sea[1, 1] = True  # only the center cell is water
        return LandMaskSampler(
            longitude=longitude,
            latitude=latitude,
            land=sea,
            source="synthetic",
        )

    with patch(
        "arctic_route_orchestrator.replay.preflight.load_netcdf_land_mask",
        side_effect=_land_mask,
    ):
        result = run_l2_preflight(
            manifest,
            snapshots,
            "/nonexistent.nc",
            output_path=None,
        )
    assert result.overall == "FAIL"
    assert result.document["presentation_eligible_l2"] is False
    assert len(result.document["route_checks"]) > 0
    assert any(item["status"] == "FAIL" for item in result.document["route_checks"])
