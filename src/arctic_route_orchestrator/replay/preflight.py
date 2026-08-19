"""Replay-driven presentation preflight: L2 GEBCO + transform + artifacts.

This is the formal preflight for Replay-driven Viewer eligibility:

    L1 Planner Grid Integrity          (already proven by replay validation /
                                        route_integrity on authoritative runs)
    L2 GEBCO Real-world Coastline      (this module, fail-closed)
    Canonical EPSG:4326 transform      (all layers inside basemap bbox)

Only when all gates PASS is a presentation marked eligible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arctic_route_orchestrator.replay.geospatial import (
    BasemapMetadata,
    CanonicalGeographicTransform,
    LandMaskSampler,
    find_land_sea_mask,
    l2_coastline_gate,
    load_netcdf_land_mask,
    projection_consistency_gate,
)
from arctic_route_orchestrator.replay.presentation import PresentationAdapter
from arctic_route_orchestrator.replay.validation import (
    validate_manifest,
    validate_replay,
)


@dataclass(frozen=True, slots=True)
class PreflightResult:
    document: dict[str, Any]
    overall: str


def _write_json(path: Path | None, document: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _sampler_contract(mask: LandMaskSampler, sample_step_km: float) -> dict[str, Any]:
    lon_step, lat_step = mask.cell_step_degrees()
    bbox = mask.bbox()
    return {
        "projection": "EPSG:4326",
        "resolution_degrees": {"longitude": lon_step, "latitude": lat_step},
        "bbox": bbox,
        "shape": {
            "latitude": mask.grid_shape()[0],
            "longitude": mask.grid_shape()[1],
        },
        "method": "raster_cell_traversal_linear_lon_lat",
        "oversample_spacing_km_requested": sample_step_km,
        "source": mask.source,
    }


def _basemap_metadata(mask: LandMaskSampler, version: str, source: str) -> BasemapMetadata:
    lon_step, lat_step = mask.cell_step_degrees()
    bbox = mask.bbox()
    return BasemapMetadata(
        projection="EPSG:4326",
        bbox={
            "min_lon": bbox["min_lon"] - lon_step / 2.0,
            "max_lon": bbox["max_lon"] + lon_step / 2.0,
            "min_lat": bbox["min_lat"] - lat_step / 2.0,
            "max_lat": bbox["max_lat"] + lat_step / 2.0,
        },
        width=mask.grid_shape()[1],
        height=mask.grid_shape()[0],
        source=source,
        version=version,
    )


def _run_route_l2(
    label: str,
    waypoints: list[dict[str, float]],
    mask: LandMaskSampler,
    sample_step_km: float,
) -> dict[str, Any]:
    if len(waypoints) < 2:
        return {
            "label": label,
            "status": "SKIP_TOO_SHORT",
            "waypoint_count": len(waypoints),
            "cells_traversed": 0,
            "land_cells": 0,
            "data_unavailable_cells": 0,
            "violations": [],
            "first_failure": None,
        }
    result = l2_coastline_gate(waypoints, mask, sample_step_km=sample_step_km)
    first = result["violations"][0] if result["violations"] else None
    return {
        "label": label,
        "status": result["status"],
        "waypoint_count": len(waypoints),
        "edge_count": result["edge_count"],
        "cells_traversed": result["cells_traversed"],
        "sample_checks": result["sample_checks"],
        "land_cells": result["land_cells"],
        "data_unavailable_cells": result["data_unavailable_cells"],
        "violations": result["violations"][:20],
        "first_failure": first,
    }


def run_l2_preflight(
    manifest: dict[str, Any],
    snapshots: list[dict[str, Any]],
    land_mask_path: str | Path,
    *,
    sample_step_km: float = 5.0,
    output_path: str | Path | None = None,
) -> PreflightResult:
    """Run GEBCO L2 on every authoritative route revision + completed track."""

    adapter = PresentationAdapter(manifest, snapshots)
    mask = load_netcdf_land_mask(land_mask_path)
    revisions = sorted(adapter._routes_by_revision)
    route_checks: list[dict[str, Any]] = []
    track_checks: list[dict[str, Any]] = []
    for revision in revisions:
        entry = adapter._routes_by_revision[revision]
        route = entry["route"]
        waypoints = [
            {"longitude": item["longitude"], "latitude": item["latitude"]}
            for item in route.get("waypoints", ())
        ]
        route_checks.append(
            _run_route_l2(
                f"revision-{revision}-full_voyage-recommended",
                waypoints,
                mask,
                sample_step_km,
            )
        )
        snapshot = entry["snapshot"]
        track = snapshot.get("ship_state", {}).get("completed_track", ())
        track_waypoints = [
            {"longitude": item["longitude"], "latitude": item["latitude"]}
            for item in track
        ]
        track_checks.append(
            _run_route_l2(
                f"revision-{revision}-completed_track",
                track_waypoints,
                mask,
                sample_step_km,
            )
        )
    all_statuses = [
        item["status"]
        for item in [*route_checks, *track_checks]
        if item["status"] != "SKIP_TOO_SHORT"
    ]
    overall = "PASS" if all(item == "PASS" for item in all_statuses) else "FAIL"
    document: dict[str, Any] = {
        "schema_version": "orchestrator.replay-l2-preflight.v1",
        "overall": overall,
        "presentation_eligible_l2": overall == "PASS",
        "dataset": {
            "kind": "GEBCO_2026_land_sea_mask",
            "source": mask.source,
            "projection": "EPSG:4326",
            "resolution_degrees": {
                "longitude": mask.cell_step_degrees()[0],
                "latitude": mask.cell_step_degrees()[1],
            },
            "bbox": mask.bbox(),
        },
        "sampling_contract": _sampler_contract(mask, sample_step_km),
        "replay": {
            "replay_id": manifest.get("replay_id"),
            "scenario_id": manifest.get("scenario_id"),
            "replay_start": manifest.get("replay_start"),
            "replay_end": manifest.get("replay_end"),
            "manifest_semantic_digest": manifest.get("semantic_digest"),
        },
        "route_checks": route_checks,
        "track_checks": track_checks,
    }
    _write_json(Path(output_path) if output_path else None, document)
    return PreflightResult(document=document, overall=overall)


def run_viewer_preflight(
    manifest: dict[str, Any],
    snapshots: list[dict[str, Any]],
    *,
    data_root: str | Path,
    route_id: str,
    land_mask_path: str | Path | None = None,
    sample_step_km: float = 5.0,
    basemap_version: str = "gebco-2026-d5a7e2fe3915-7baad866",
    output_path: str | Path | None = None,
) -> PreflightResult:
    """Full presentation preflight for the Replay-driven viewer.

    Includes artifact validation, route/track L2, a canonical EPSG:4326
    transform and per-layer coverage (routes/track/vessel positions inside
    the GEBCO basemap bbox).
    """

    if land_mask_path is None:
        resolved = find_land_sea_mask(route_id, data_root)
        if resolved is None:
            raise FileNotFoundError(f"no land_sea_mask for route_id {route_id!r}")
        land_mask_path = resolved
    adapter = PresentationAdapter(manifest, snapshots)
    mask = load_netcdf_land_mask(land_mask_path)

    l2 = run_l2_preflight(
        manifest,
        snapshots,
        land_mask_path,
        sample_step_km=sample_step_km,
        output_path=None,
    )
    sequence = validate_replay(snapshots)
    manifest_check = validate_manifest(manifest, snapshots)
    metadata = _basemap_metadata(mask, basemap_version, source=str(land_mask_path))
    canonical = CanonicalGeographicTransform(metadata)
    shared = {
        "basemap_route": canonical,
        "completed_track": canonical,
        "vessel": canonical,
        "risk": canonical,
    }
    consistency = projection_consistency_gate(canonical, shared)

    def coverage_for(points: list[dict[str, float]], label: str) -> dict[str, Any]:
        inside = sum(
            1
            for point in points
            if metadata.bbox["min_lon"]
            <= point["longitude"]
            <= metadata.bbox["max_lon"]
            and metadata.bbox["min_lat"]
            <= point["latitude"]
            <= metadata.bbox["max_lat"]
        )
        return {
            "label": label,
            "point_count": len(points),
            "inside_bbox": inside,
            "outside_bbox": len(points) - inside,
            "status": "PASS" if inside == len(points) else "FAIL",
        }

    route_points: list[dict[str, float]] = []
    vessel_points: list[dict[str, float]] = []
    track_points: list[dict[str, float]] = []
    for revision in sorted(adapter._routes_by_revision):
        entry = adapter._routes_by_revision[revision]
        route_points.extend(
            {"longitude": item["longitude"], "latitude": item["latitude"]}
            for item in entry["route"].get("waypoints", ())
        )
        snapshot = entry["snapshot"].get("ship_state", {})
        position = snapshot.get("current_position")
        if position:
            vessel_points.append(position)
        track_points.extend(snapshot.get("completed_track", ()))
    coverage = [
        coverage_for(route_points, "route"),
        coverage_for(track_points, "completed_track"),
        coverage_for(vessel_points, "vessel"),
    ]
    coverage_status = (
        "PASS" if all(item["status"] == "PASS" for item in coverage) else "FAIL"
    )
    event_counts: dict[str, int] = {}
    for event in manifest.get("events", ()):
        event_counts[event["type"]] = event_counts.get(event["type"], 0) + 1
    statuses = [
        l2.overall,
        sequence["status"],
        manifest_check["status"],
        consistency["status"],
        coverage_status,
    ]
    overall = "PASS" if all(item == "PASS" for item in statuses) else "FAIL"
    document: dict[str, Any] = {
        "schema_version": "orchestrator.replay-viewer-preflight.v1",
        "overall": overall,
        "presentation_eligible": overall == "PASS",
        "l2": l2.document,
        "artifact_validation": {
            "snapshots": sequence["status"],
            "manifest": manifest_check["status"],
            "snapshot_count": len(snapshots),
            "violations": [
                *sequence["violations"],
                *manifest_check["violations"],
            ][:20],
        },
        "canonical_transform": metadata.to_dict(),
        "projection_consistency": consistency,
        "layer_coverage": {
            "status": coverage_status,
            "layers": coverage,
        },
        "event_counts": event_counts,
        "replay": {
            "replay_id": manifest.get("replay_id"),
            "scenario_id": manifest.get("scenario_id"),
            "scenario_mode": manifest.get("scenario_mode"),
            "manifest_semantic_digest": manifest.get("semantic_digest"),
        },
    }
    _write_json(Path(output_path) if output_path else None, document)
    return PreflightResult(document=document, overall=overall)
