"""Geospatial transform + L2 coastline gate unit tests (Strategy B)."""

from __future__ import annotations

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
    land = np.zeros((5, 5), dtype=bool)
    for lat_index, lon_index in land_cells:
        land[lat_index, lon_index] = True
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
    nc_path = Path(
        "/root/my_project/work_package_a/data/raw/tromso_to_isfjorden_outer/"
        "land_sea_mask/163a3f67b391a1d90ac83cad/"
        "land_sea_mask_tromso_to_isfjorden_outer_valid_20260423T000000Z_"
        "issued_20260423T000000Z_gebco-2026-d5a7e2fe3915-7baad866_"
        "3640a87b2f5a2d15.nc"
    )
    sampler = load_netcdf_land_mask(nc_path)
    water = [
        {"longitude": 17.55, "latitude": 68.49791666666667},
        {"longitude": 22.0, "latitude": 68.49791666666667},
    ]
    land = [
        {"longitude": 12.0, "latitude": 69.5},
        {"longitude": 18.0, "latitude": 70.5},
    ]
    water_result = l2_coastline_gate(water, sampler)
    land_result = l2_coastline_gate(land, sampler)
    assert water_result["status"] == "PASS"
    assert land_result["status"] == "FAIL"
    assert any(
        item["status"] == "LAND" for item in land_result["violations"]
    )
    assert sampler.sample(19.0, 68.5) == "WATER"
