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

    def sample(self, longitude: float, latitude: float) -> str:
        lons = self.longitude
        lats = self.latitude
        if not (lons[0] <= longitude <= lons[-1] and lats[0] <= latitude <= lats[-1]):
            return "DATA_UNAVAILABLE"
        lon_index = int(np.argmin(np.abs(np.asarray(lons) - longitude)))
        lat_index = int(np.argmin(np.abs(np.asarray(lats) - latitude)))
        return "LAND" if bool(self.land[lat_index, lon_index]) else "WATER"


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
    for edge_index in range(len(waypoints) - 1):
        start = waypoints[edge_index]
        end = waypoints[edge_index + 1]
        distance_km = _haversine_km(
            start["longitude"], start["latitude"], end["longitude"], end["latitude"]
        )
        sample_count = max(
            2,
            min(int(distance_km / sample_step_km) + 2, max_samples_per_edge),
        )
        for sample_index in range(sample_count):
            fraction = sample_index / (sample_count - 1)
            lon = start["longitude"] + (end["longitude"] - start["longitude"]) * fraction
            lat = start["latitude"] + (end["latitude"] - start["latitude"]) * fraction
            status = sampler.sample(lon, lat)
            if status in ("LAND", "DATA_UNAVAILABLE"):
                violations.append(
                    {
                        "edge_index": edge_index,
                        "status": status,
                        "longitude": lon,
                        "latitude": lat,
                        "sample_index": sample_index,
                    }
                )
    return {
        "status": "PASS" if not violations else "FAIL",
        "edge_count": len(waypoints) - 1,
        "violations": violations,
    }
