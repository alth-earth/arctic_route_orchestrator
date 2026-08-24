"""Geospatial transform + L2 coastline gate unit tests (Strategy B)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from arctic_route_orchestrator.replay.geospatial import (
    EPSG4326,
    BasemapMetadata,
    CanonicalGeographicTransform,
    LandMaskSampler,
    l2_coastline_gate,
    load_netcdf_land_mask,
    projection_consistency_gate,
)


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


def _metadata(**overrides: Any) -> BasemapMetadata:
    values = {
        "projection": EPSG4326,
        "bbox": {"min_lon": 10.0, "max_lon": 22.05, "min_lat": 68.5, "max_lat": 79.55},
        "width": 1000,
        "height": 800,
        "source": "GEBCO_2026/CEDA OPeNDAP",
        "version": "gebco-2026-d5a7e2fe3915-7baad866",
    }
    values.update(overrides)
    return BasemapMetadata(**values)


def _sampler(land_cells: set[tuple[int, int]]) -> LandMaskSampler:
    # Canonical semantics: True=sea (1), False=land/coast (0).
    land = np.ones((5, 5), dtype=bool)
    for lat_index, lon_index in land_cells:
        land[lat_index, lon_index] = False
    return LandMaskSampler(
        longitude=(10.0, 10.5, 11.0, 11.5, 12.0),
        latitude=(60.0, 60.5, 61.0, 61.5, 62.0),
        land=land,
        source="synthetic",
    )


def test_canonical_transform_projects_corners() -> None:
    transform = CanonicalGeographicTransform(_metadata())
    assert transform.metadata.projection == EPSG4326
    assert transform.project(10.0, 79.55) == (0.0, 0.0)
    assert transform.project(22.05, 68.5) == (1000.0, 800.0)
    x, y = transform.project(16.025, 74.025)
    assert x == pytest.approx(500.0)
    assert y == pytest.approx(400.0)


def test_projection_consistency_gate_detects_divergent_layer_transform() -> None:
    canonical = CanonicalGeographicTransform(_metadata())
    same = CanonicalGeographicTransform(_metadata())
    divergent = CanonicalGeographicTransform(
        _metadata(bbox={"min_lon": 0.0, "max_lon": 30.0, "min_lat": 60.0, "max_lat": 80.0})
    )
    passed = projection_consistency_gate(
        canonical,
        {
            "risk": same,
            "route": same,
            "completed_track": same,
            "vessel": same,
        },
    )
    assert passed["status"] == "PASS"
    assert passed["shared_transform_count"] == 4
    failed = projection_consistency_gate(
        canonical,
        {"route": same, "vessel": divergent},
    )
    assert failed["status"] == "FAIL"
    assert any("vessel transform differs" in item for item in failed["violations"])


def test_l2_gate_synthetic_water_pass() -> None:
    sampler = _sampler({(4, 4)})
    waypoints = [
        {"longitude": 10.0, "latitude": 60.0},
        {"longitude": 11.0, "latitude": 60.5},
    ]
    result = l2_coastline_gate(waypoints, sampler, sample_step_km=5.0)
    assert result["status"] == "PASS"
    assert result["violations"] == []


def test_l2_gate_synthetic_land_and_unavailable_fail() -> None:
    sampler = _sampler({(2, 2)})
    land_route = [
        {"longitude": 10.0, "latitude": 61.0},
        {"longitude": 12.0, "latitude": 61.0},
    ]
    assert l2_coastline_gate(land_route, sampler)["status"] == "FAIL"
    out_of_bounds = [
        {"longitude": 12.0, "latitude": 62.0},
        {"longitude": 12.2, "latitude": 62.0},
    ]
    result = l2_coastline_gate(out_of_bounds, sampler)
    assert result["status"] == "FAIL"
    assert any(item["status"] == "DATA_UNAVAILABLE" for item in result["violations"])


def test_l2_real_data_smoke_water_and_land() -> None:
    nc_path = (
        _workspace_root() / "work_package_a" / "data" / "raw" / "tromso_to_isfjorden_outer"
        / "land_sea_mask" / "163a3f67b391a1d90ac83cad"
        / "land_sea_mask_tromso_to_isfjorden_outer_valid_20260423T000000Z_issued_20260423T000000Z_gebco-2026-d5a7e2fe3915-7baad866_3640a87b2f5a2d15.nc"
    )
    sampler = load_netcdf_land_mask(nc_path)
    water = [
        {"longitude": 18.4, "latitude": 70.5},
        {"longitude": 18.4, "latitude": 73.0},
    ]
    land = [
        {"longitude": 15.0, "latitude": 68.5},
        {"longitude": 16.0, "latitude": 68.5},
    ]
    water_result = l2_coastline_gate(water, sampler)
    land_result = l2_coastline_gate(land, sampler)
    assert water_result["status"] == "PASS"
    assert land_result["status"] == "FAIL"
    assert any(
        item["status"] == "LAND" for item in land_result["violations"]
    )
    assert sampler.sample(18.4, 71.8) == "WATER"
    assert sampler.sample(15.5, 68.5) == "LAND"
