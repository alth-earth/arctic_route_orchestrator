"""Shared geospatial transform and L2 coastline integrity base (Strategy B).

Viewer basemap contract:

* projection = EPSG:4326 (lon/lat);
* one canonical ``CanonicalGeographicTransform`` is shared by risk grid,
  route, completed track, and vessel; the viewer must never use a second
  transform for route geometry;
* route geometry remains linear lon/lat, matching the backend ship-motion
  and ``RegularGrid.edge_sample_points`` contract.

L2 coastline integrity is a fail-closed gate over the real land-sea mask
(GEBCO-derived, already present locally).  Out-of-bounds sampling is
reported as DATA_UNAVAILABLE.  No large download is performed by this module;
``load_netcdf_land_mask`` reads an already-prepared local ``.nc`` file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

EPSG4326 = "EPSG:4326"
EARTH_RADIUS_KM = 6_371.0088


def _haversine_km(start_lon: float, start_lat: float, end_lon: float, end_lat: float) -> float:
    lat1 = math.radians(start_lat)
    lat2 = math.radians(end_lat)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(end_lon - start_lon)
    a = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True, slots=True)
class BasemapMetadata:
    projection: str
    bbox: dict[str, float]
    width: int
    height: int
    source: str
    version: str
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.projection != EPSG4326:
            raise ValueError("presentation basemap must use EPSG:4326")
        for key in ("min_lon", "max_lon", "min_lat", "max_lat"):
            if key not in self.bbox:
                raise ValueError(f"basemap bbox missing {key}")
        if self.bbox["min_lon"] >= self.bbox["max_lon"]:
            raise ValueError("basemap bbox min_lon must be < max_lon")
        if self.bbox["min_lat"] >= self.bbox["max_lat"]:
            raise ValueError("basemap bbox min_lat must be < max_lat")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("basemap width/height must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection": self.projection,
            "bbox": dict(self.bbox),
            "width": self.width,
            "height": self.height,
            "source": self.source,
            "version": self.version,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class CanonicalGeographicTransform:
    """Single EPSG:4326 pixel transform shared by all spatial layers."""

    metadata: BasemapMetadata

    def project(self, longitude: float, latitude: float) -> tuple[float, float]:
        box = self.metadata.bbox
        lon_span = box["max_lon"] - box["min_lon"]
        lat_span = box["max_lat"] - box["min_lat"]
        x = (longitude - box["min_lon"]) / lon_span * self.metadata.width
        y = (box["max_lat"] - latitude) / lat_span * self.metadata.height
        return x, y

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CanonicalGeographicTransform):
            return NotImplemented
        return self.metadata == other.metadata


@dataclass(frozen=True, slots=True)
class LandMaskSampler:
    longitude: tuple[float, ...]
    latitude: tuple[float, ...]
    land: np.ndarray
    source: str = "GEBCO_2026_land_sea_mask"

    # Canonical GEBCO-derived semantics (work_package_a::derive_land_sea_mask):
    # 1 = sea (elevation < 0), 0 = land/coast.  The stored boolean array keeps
    # the raw value; True means SEA, False means LAND.

    def cell_step_degrees(self) -> tuple[float, float]:
        if len(self.longitude) < 2 or len(self.latitude) < 2:
            raise ValueError("land mask needs at least 2x2 cells for traversal")
        lon_step = self.longitude[1] - self.longitude[0]
        lat_step = self.latitude[1] - self.latitude[0]
        if lon_step <= 0 or lat_step <= 0:
            raise ValueError("land mask coordinates must be strictly increasing")
        return float(lon_step), float(lat_step)

    def bbox(self) -> dict[str, float]:
        return {
            "min_lon": float(self.longitude[0]),
            "max_lon": float(self.longitude[-1]),
            "min_lat": float(self.latitude[0]),
            "max_lat": float(self.latitude[-1]),
        }

    def grid_shape(self) -> tuple[int, int]:
        return int(self.land.shape[0]), int(self.land.shape[1])

    def sample(self, longitude: float, latitude: float) -> str:
        lons = self.longitude
        lats = self.latitude
        if not (lons[0] <= longitude <= lons[-1] and lats[0] <= latitude <= lats[-1]):
            return "DATA_UNAVAILABLE"
        lon_index = int(np.argmin(np.abs(np.asarray(lons) - longitude)))
        lat_index = int(np.argmin(np.abs(np.asarray(lats) - latitude)))
        return "WATER" if bool(self.land[lat_index, lon_index]) else "LAND"

    def cell_status(self, lat_index: int, lon_index: int) -> str:
        if not (0 <= lat_index < self.land.shape[0] and 0 <= lon_index < self.land.shape[1]):
            return "DATA_UNAVAILABLE"
        return "WATER" if bool(self.land[lat_index, lon_index]) else "LAND"

    def cells_between(
        self,
        start_lon: float,
        start_lat: float,
        end_lon: float,
        end_lat: float,
        *,
        oversample: float = 2.0,
        max_samples: int | None = None,
    ) -> list[tuple[int, int, str]]:
        """Traverse the mask grid along a linear lon/lat segment.

        Returns a list of (lat_index, lon_index, status).  The segment is
        sampled at <= half-cell spacing in grid space so long edges cannot
        skip a small island cell between their endpoints.  Out-of-bounds
        portions are reported as DATA_UNAVAILABLE cells.
        """

        lat_array = np.asarray(self.latitude)
        lon_array = np.asarray(self.longitude)
        lon_step, lat_step = self.cell_step_degrees()

        def grid_position(lon: float, lat: float) -> tuple[float, float]:
            gx = (lon - (lon_array[0] - lon_step / 2.0)) / lon_step
            gy = (lat - (lat_array[0] - lat_step / 2.0)) / lat_step
            return float(gx), float(gy)

        x0, y0 = grid_position(start_lon, start_lat)
        x1, y1 = grid_position(end_lon, end_lat)
        distance_cells = max(abs(x1 - x0), abs(y1 - y0))
        sample_count = max(2, math.ceil(distance_cells * oversample) + 2)
        if max_samples is not None and max_samples > 0:
            sample_count = max(2, min(sample_count, max_samples))
        cells: dict[tuple[int, int], str] = {}
        lon_count = len(lon_array)
        lat_count = len(lat_array)
        for sample_index in range(sample_count):
            fraction = sample_index / (sample_count - 1)
            gx = x0 + (x1 - x0) * fraction
            gy = y0 + (y1 - y0) * fraction
            lon_index = math.floor(gx)
            lat_index = math.floor(gy)
            key = (lat_index, lon_index)
            out_of_bounds = (
                gx < -0.5
                or gx > lon_count - 0.5
                or gy < -0.5
                or gy > lat_count - 0.5
            )
            if out_of_bounds:
                cells[key] = "DATA_UNAVAILABLE"
            elif key not in cells:
                cells[key] = (
                    self.cell_status(lat_index, lon_index)
                )
        return [
            (lat_index, lon_index, status)
            for (lat_index, lon_index), status in cells.items()
        ]


def load_netcdf_land_mask(path: str | Path) -> LandMaskSampler:
    """Load a local GEBCO-derived land_sea_mask ``.nc`` into an in-memory sampler."""

    import xarray as xr

    with xr.open_dataset(path) as dataset:
        land = np.asarray(
            dataset["land_sea_mask"].isel(time=0, missing_dims="ignore").values
        ).astype(bool)
        latitude = tuple(float(value) for value in dataset["latitude"].values)
        longitude = tuple(float(value) for value in dataset["longitude"].values)
    return LandMaskSampler(
        longitude=longitude,
        latitude=latitude,
        land=land,
        source=str(path),
    )


def find_land_sea_mask(
    route_id: str,
    data_root: str | Path,
) -> Path | None:
    """Resolve the local GEBCO-derived land_sea_mask for a corridor."""

    base = Path(data_root) / "raw" / route_id / "land_sea_mask"
    candidates: list[Path] = []
    for path in base.glob("**/*.nc"):
        candidates.append(path)
    if not candidates:
        return None
    return sorted(candidates)[-1]


def projection_consistency_gate(
    canonical: CanonicalGeographicTransform,
    layers: dict[str, CanonicalGeographicTransform],
) -> dict[str, Any]:
    """Gate: every spatial layer must share the canonical EPSG:4326 transform."""

    violations: list[str] = []
    for name, transform in layers.items():
        if transform != canonical:
            violations.append(
                f"{name} transform differs from canonical "
                f"(projection/bbox/scale must match)"
            )
    return {
        "status": "PASS" if not violations else "FAIL",
        "shared_transform_count": len(layers),
        "violations": violations,
    }


def l2_coastline_gate(
    waypoints: list[dict[str, float]],
    sampler: LandMaskSampler,
    *,
    sample_step_km: float = 5.0,
    max_samples_per_edge: int = 400,
) -> dict[str, Any]:
    """Fail-closed real-coastline check over route/track waypoints.

    Edges are sampled using the same linear lon/lat interpolation as backend
    ship motion and grid edges.  LAND and DATA_UNAVAILABLE samples are
    violations; the gate never bends routes to "visually avoid" land.
    """

    if len(waypoints) < 2:
        raise ValueError("L2 gate needs at least two waypoints")
    violations: list[dict[str, Any]] = []
    cells_checked = 0
    land_cells = 0
    data_unavailable_cells = 0
    sample_checks = 0
    for edge_index in range(len(waypoints) - 1):
        start = waypoints[edge_index]
        end = waypoints[edge_index + 1]
        cell_km = _haversine_km(
            sampler.longitude[0],
            sampler.latitude[0],
            sampler.longitude[1],
            sampler.latitude[1],
        ) if sampler.grid_shape()[1] > 1 and sampler.grid_shape()[0] > 1 else sample_step_km
        oversample = max(2.0, min(8.0, cell_km / max(sample_step_km, 1e-9)))
        cells = sampler.cells_between(
            start["longitude"],
            start["latitude"],
            end["longitude"],
            end["latitude"],
            oversample=oversample,
            max_samples=max_samples_per_edge,
        )
        sample_checks += len(cells)
        for lat_index, lon_index, status in cells:
            cells_checked += 1
            if status == "LAND":
                land_cells += 1
            if status == "DATA_UNAVAILABLE":
                data_unavailable_cells += 1
            if status in ("LAND", "DATA_UNAVAILABLE"):
                lon = (
                    sampler.longitude[lon_index]
                    if 0 <= lon_index < len(sampler.longitude)
                    else None
                )
                lat = (
                    sampler.latitude[lat_index]
                    if 0 <= lat_index < len(sampler.latitude)
                    else None
                )
                violations.append(
                    {
                        "edge_index": edge_index,
                        "status": status,
                        "longitude": lon,
                        "latitude": lat,
                        "cell": [lat_index, lon_index],
                    }
                )
    return {
        "status": "PASS" if not violations else "FAIL",
        "edge_count": len(waypoints) - 1,
        "cells_traversed": cells_checked,
        "sample_checks": sample_checks,
        "land_cells": land_cells,
        "data_unavailable_cells": data_unavailable_cells,
        "violations": violations,
    }
