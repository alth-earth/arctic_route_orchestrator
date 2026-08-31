"""Export a stable Replay-driven Viewer presentation package.

This is the orchestrator-owned artifact boundary: it consumes the causal replay
manifest + Presentation Adapter and produces the basemap PNG, basemap metadata,
bundle JSON and presentation preflight that work_package_d renders.  The Viewer
application in work_package_d never imports orchestrator internals.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import struct
import sys
import tempfile
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import Any

import numpy as np

from arctic_route_orchestrator.replay.geospatial import (
    BasemapMetadata,
    find_land_sea_mask,
    load_netcdf_land_mask,
)
from arctic_route_orchestrator.replay.preflight import run_viewer_preflight
from arctic_route_orchestrator.replay.presentation import PresentationAdapter
from arctic_route_orchestrator.replay.research_route_motion import (
    SIDECAR_SCHEMA_VERSION_V2,
    normalize_research_route_sidecar,
    validate_research_route_sidecar,
)
from arctic_route_orchestrator.route_motion import (
    load_bound_route_motion_set,
    validate_route_motion_context,
)
from arctic_route_orchestrator.route_presentation import load_route_candidates

SEA_RGB = (46, 102, 150)
LAND_RGB = (108, 132, 98)
OUT_RGB = (36, 44, 56)
RISK_HORIZONS: tuple[tuple[str, int], ...] = (
    ("current", 0),
    ("+6h", 6),
    ("+12h", 12),
    ("+24h", 24),
)

VIEWER_PRESENTATION = {
    "schema_version": "presentation.viewer-presentation.v1",
    "risk_rendering": {
        "source_schema": "presentation.risk-overlay.v1",
        "geometry_policy": "exact_authoritative_cells_no_interpolation",
        "hard_reason_policy": "separate_exact_cells_fail_closed",
    },
    "route_rendering": {
        "source": "route_motion_sets.motion_samples_or_routes.waypoints",
        "geometry_policy": "producer_motion_samples_when_formally_bound",
        "fallback_policy": "authoritative_route_waypoints",
        "authoritative_semantics_unchanged": True,
        "candidate_source": "route_candidates",
        "candidate_empty_policy": "keep_single_authoritative_route",
    },
    "vessel_rendering": {
        "position_source": "formal_route_motion_or_timeline.vessel_at",
        "heading_source": "formal_route_motion_course_or_route_segment_bearing",
        "pixel_motion": "none",
    },
}

ROUTE_CANDIDATES_PACKAGE = {
    "schema_version": "presentation.route-candidates.v1",
    "status": "NOT_PUBLISHED",
    "candidates": [],
    "reason": "candidate_geometry_and_metrics_not_published",
}

WINTER_COMBINED_SCHEMA = "presentation.winter-combined-viewer.v1"
WINTER_MANIFEST_SCHEMA = "presentation.winter-combined-manifest.v1"


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


def _route_candidates_package(
    path: Path | None,
    *,
    scenario_id: str | None = None,
) -> dict:
    document = load_route_candidates(path) if path is not None else ROUTE_CANDIDATES_PACKAGE
    if scenario_id is not None and document.get("status") == "PUBLISHED":
        candidate_scenarios = {
            candidate.get("provenance", {}).get("scenario_id")
            for candidate in document.get("candidates", [])
        }
        if candidate_scenarios != {scenario_id}:
            raise ValueError("route candidate scenario does not match replay manifest")
    return document


def _write_png(path: Path, width: int, height: int, pixels: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    rowbytes = width * 3
    raw = b"".join(
        b"\x00"
        + pixels[rowbytes * row : rowbytes * row + rowbytes]
        for row in range(height)
    )
    with path.open("wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(
            chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
            )
        )
        handle.write(chunk(b"IDAT", zlib.compress(raw, level=6)))
        handle.write(chunk(b"IEND", b""))


def _render_basemap(
    *,
    land: np.ndarray,
    lon0: float,
    lat0: float,
    lon_step: float,
    lat_step: float,
    width: int,
    height: int,
) -> bytes:
    lon_count = land.shape[1]
    lat_count = land.shape[0]
    left = lon0 - lon_step / 2.0
    top = lat0 - lat_step / 2.0
    cols = np.arange(width, dtype=np.float64)
    rows = np.arange(height, dtype=np.float64)
    px_lon = left + (cols / (width - 1)) * (lon_count * lon_step)
    px_lat = top + (1.0 - rows / (height - 1)) * (lat_count * lat_step)
    lon_idx = np.floor((px_lon - left) / lon_step).astype(np.int64)
    lat_idx = np.floor((px_lat - top) / lat_step).astype(np.int64)
    valid_x = (lon_idx >= 0) & (lon_idx < lon_count)
    valid_y = (lat_idx >= 0) & (lat_idx < lat_count)
    valid = np.outer(valid_y, valid_x)
    safe_x = np.clip(lon_idx, 0, lon_count - 1)
    safe_y = np.clip(lat_idx, 0, lat_count - 1)
    values = land[safe_y[:, None], safe_x[None, :]]
    values = np.where(valid, values, -1)  # -1 = out of bounds

    rgb = np.empty((height, width, 3), dtype=np.uint8)
    sea = values == 1
    land_cells = values == 0
    rgb[sea] = SEA_RGB
    rgb[land_cells] = LAND_RGB
    rgb[~sea & ~land_cells] = OUT_RGB
    return bytes(rgb.tobytes())


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _route_meta(adapter: PresentationAdapter, revision: int) -> dict:
    entry = adapter._routes_by_revision[revision]
    route = entry["route"]
    waypoints = [
        {
            "lon": item["longitude"],
            "lat": item["latitude"],
            "eta": item["eta"],
        }
        for item in route["waypoints"]
    ]
    decision_time = None
    adopt_time = None
    mode = "INITIAL"
    for event in adapter._events:
        if str(event.get("revision")) == str(revision):
            if event["type"] == "REPLAN_DECIDED":
                decision_time = event["simulation_time"]
                mode = "NEXT_WAYPOINT_DEFERRED"
            elif event["type"] == "REPLAN_TRIGGERED":
                decision_time = event["simulation_time"]
                mode = "IMMEDIATE"
            elif event["type"] == "ROUTE_CHANGED":
                adopt_time = event["simulation_time"]
    if revision == 1:
        adopt_time = adapter.replay_start.isoformat().replace("+00:00", "Z")
    return {
        "revision": revision,
        "layer": "full_voyage",
        "objective": "recommended",
        "decision_time": decision_time,
        "effective_adoption_time": adopt_time,
        "adoption_mode": mode,
        "distance_km": route.get("distance_km"),
        "arrival_eta": waypoints[-1]["eta"] if waypoints else None,
        "metrics": {
            "distance_km": route.get("distance_km"),
            "average_risk": route.get("average_risk", route.get("avg_risk")),
            "maximum_risk": route.get("maximum_risk", route.get("max_risk")),
        },
        "waypoints": waypoints,
    }


def _timeline(adapter: PresentationAdapter, cadence_seconds: int) -> list[dict]:
    results: list[dict] = []
    previous_track_key: tuple | None = None
    previous_pending_key: tuple | None = None
    previous_superseded_key: tuple | None = None
    moment = adapter.replay_start
    while moment <= adapter.replay_end:
        state = adapter.state_at(moment)
        plan = state.plan
        vessel = state.vessel
        plan_dict = plan.to_dict()
        segment = plan_dict["current_authoritative_segment"]
        track = [dict(item) for item in plan.completed_track]
        track_key = tuple((p["longitude"], p["latitude"], p["eta"]) for p in track)
        pending = plan.pending_candidate
        pending_key = (
            (
                pending["plan_revision"],
                pending["decision_time"],
                pending["effective_adoption_time"],
                len(pending["route"].get("waypoints", ())),
            )
            if pending
            else None
        )
        entry: dict = {
            "t": _iso(moment),
            "v": {
                "lon": vessel.longitude,
                "lat": vessel.latitude,
                "kn": vessel.speed_knots,
                "status": vessel.status,
                "ep": vessel.edge_progress,
                "eidx": vessel.current_edge_index,
            },
            "arv": plan_dict["active_plan_revision"],
            "prv": plan_dict["pending_plan_revision"],
            "prs": plan_dict["pending_plan_status"],
            "dt": plan_dict["pending_adoption"]["decision_time"]
            if plan_dict["pending_adoption"]
            else None,
            "eat": None,
            "ctl": len(track),
            "seg": {
                "index": segment["index"],
                "start_eta": segment["start_eta"],
                "end_eta": segment["end_eta"],
            },
        }
        if plan_dict["pending_adoption"]:
            pending_revision = plan_dict["pending_plan_revision"]
            effective_event = next(
                (
                    event
                    for event in adapter._events
                    if event["type"] in {"REPLAN_ADOPTED", "ROUTE_CHANGED"}
                    and str(event.get("revision")) == str(pending_revision)
                ),
                None,
            )
            entry["eat"] = (
                effective_event["simulation_time"]
                if effective_event is not None
                else plan_dict["pending_adoption"]["effective_adoption_time"]
            )
        if track_key != previous_track_key:
            entry["track"] = track
            previous_track_key = track_key
        if pending_key != previous_pending_key:
            if pending:
                entry["pending"] = {
                    "revision": pending["plan_revision"],
                    "decision_time": pending["decision_time"],
                    "effective_adoption_time": pending["effective_adoption_time"],
                    "route": [
                        {
                            "lon": item["longitude"],
                            "lat": item["latitude"],
                            "eta": item["eta"],
                        }
                        for item in pending["route"]["waypoints"]
                    ],
                }
            else:
                entry["pending"] = None
            previous_pending_key = pending_key
        superseded = plan_dict.get("superseded_future_route")
        superseded_key = (
            tuple(
                (
                    item.get("longitude"),
                    item.get("latitude"),
                    item.get("eta"),
                )
                for item in superseded or ()
            )
            if superseded
            else None
        )
        if superseded_key != previous_superseded_key:
            entry["superseded"] = (
                [
                    {
                        "lon": item["longitude"],
                        "lat": item["latitude"],
                        "eta": item["eta"],
                    }
                    for item in superseded
                ]
                if superseded
                else None
            )
            previous_superseded_key = superseded_key
        results.append(entry)
        moment += timedelta(seconds=cadence_seconds)
    return results


def _select_risk_horizon(
    frames: list[dict],
    *,
    simulation_time: datetime,
    horizon_hours: int,
) -> dict:
    requested_time = simulation_time + timedelta(hours=horizon_hours)
    unavailable = {
        "requested_horizon_hours": horizon_hours,
        "requested_valid_time": _iso(requested_time),
        "actual_valid_time": None,
        "actual_horizon_seconds": None,
        "selection_method": "unavailable",
        "availability": "UNAVAILABLE",
        "reason": "no_frame_available",
        "frame_index": None,
        "risk_id": None,
    }
    if not frames:
        return unavailable

    valid_times = [_parse_utc(frame["valid_time"]) for frame in frames]
    selected_index: int | None = None
    selection_method = "unavailable"
    if horizon_hours == 0:
        candidates = [
            index for index, valid_time in enumerate(valid_times) if valid_time <= simulation_time
        ]
        if candidates:
            selected_index = candidates[-1]
            selection_method = "latest_valid_time_at_or_before_simulation_time"
    elif requested_time > valid_times[-1]:
        unavailable["reason"] = "requested_valid_time_after_available_range"
    else:
        candidates = [
            index
            for index, valid_time in enumerate(valid_times)
            if simulation_time <= valid_time <= requested_time
        ]
        if candidates:
            selected_index = candidates[-1]
            selection_method = (
                "exact_requested_valid_time"
                if valid_times[selected_index] == requested_time
                else "floor_valid_time_at_or_before_requested_valid_time"
            )

    if selected_index is None:
        return unavailable
    actual_time = valid_times[selected_index]
    return {
        "requested_horizon_hours": horizon_hours,
        "requested_valid_time": _iso(requested_time),
        "actual_valid_time": _iso(actual_time),
        "actual_horizon_seconds": int((actual_time - simulation_time).total_seconds()),
        "selection_method": selection_method,
        "availability": "AVAILABLE",
        "reason": None,
        "frame_index": selected_index,
        "risk_id": frames[selected_index].get("risk_id"),
    }


def _risk_horizon_selections(frames: list[dict], simulation_times: list[datetime]) -> list[dict]:
    selections = []
    for simulation_time in simulation_times:
        by_horizon = {
            key: _select_risk_horizon(
                frames,
                simulation_time=simulation_time,
                horizon_hours=hours,
            )
            for key, hours in RISK_HORIZONS
        }
        selections.append(
            {
                "simulation_time": _iso(simulation_time),
                "available_horizons": [
                    key
                    for key, selection in by_horizon.items()
                    if selection["availability"] == "AVAILABLE"
                ],
                "selections": by_horizon,
            }
        )
    return selections


def _risk_frame_summary(frame: dict) -> dict:
    """Add presentation-only distribution facts without changing B semantics."""

    levels = [int(value) for value in frame["risk_levels"]]
    scores = [
        float(value)
        for value in frame["risk_scores"]
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    reasons = [str(value or "NONE") for value in frame["hard_reasons"]]
    total = len(levels)
    level_counts = {str(level): levels.count(level) for level in range(1, 6)}
    reason_counts = {
        reason: reasons.count(reason)
        for reason in sorted(set(reasons))
    }
    return {
        "total_cells": total,
        "risk_level_counts": level_counts,
        "risk_level_percentages": {
            level: round(count * 100.0 / total, 4) if total else 0.0
            for level, count in level_counts.items()
        },
        "risk_score_finite_count": len(scores),
        "risk_score_min": min(scores) if scores else None,
        "risk_score_max": max(scores) if scores else None,
        "risk_score_mean": fmean(scores) if scores else None,
        "hard_reason_counts": reason_counts,
        "land_count": reason_counts.get("LAND", 0),
        "data_unavailable_count": reason_counts.get("DATA_UNAVAILABLE", 0),
        "hard_cell_count": sum(
            count for reason, count in reason_counts.items() if reason != "NONE"
        ),
    }


def _risk_forecast_summary(frames: list[dict]) -> dict:
    summaries = [
        frame.get("summary")
        for frame in frames
        if isinstance(frame.get("summary"), dict)
        and isinstance(frame["summary"].get("risk_score_mean"), (int, float))
        and math.isfinite(float(frame["summary"]["risk_score_mean"]))
    ]
    if not summaries:
        return {
            "status": "NOT_AVAILABLE",
            "trend": "unavailable",
            "trend_method": "first_to_last_finite_mean_score",
            "mean_score_delta": None,
        }
    first = float(summaries[0]["risk_score_mean"])
    last = float(summaries[-1]["risk_score_mean"])
    delta = last - first
    if abs(delta) <= 1e-6:
        trend = "stable"
    elif delta > 0:
        trend = "increasing"
    else:
        trend = "decreasing"
    return {
        "status": "PASS",
        "trend": trend,
        "trend_method": "first_to_last_finite_mean_score",
        "first_valid_time": frames[0]["valid_time"],
        "last_valid_time": frames[-1]["valid_time"],
        "first_mean_score": first,
        "last_mean_score": last,
        "mean_score_delta": delta,
    }


def _presentation_risk(
    *,
    risk_store_root: Path | None,
    scenario_id: str,
    replay_start: datetime,
    replay_end: datetime,
    simulation_times: list[datetime] | None = None,
) -> dict:
    """Project committed B risk frames into a bounded viewer-ready payload.

    The exporter reads immutable ``bc.risk-frame.v2`` files only.  It does not
    calculate risk, reinterpret hard reasons, or expose raw source data to D.
    One frame per valid time is retained for the replay interval; the Viewer
    selects the latest valid frame at or before its single simulation clock.
    """

    empty = {
        "schema_version": "presentation.risk-overlay.v1",
        "status": "NOT_EXPORTED",
        "selection_rule": "latest_valid_time_at_or_before_simulation_time",
        "horizon_selection_rules": {
            "current": "latest_valid_time_at_or_before_simulation_time",
            "future": "floor_valid_time_at_or_before_requested_valid_time",
            "future_availability": "requested_valid_time_must_be_within_frame_range",
        },
        "cadence_seconds": 3600,
        "level_range": [1, 5],
        "supported_horizons_hours": [hours for _, hours in RISK_HORIZONS],
        "hard_reasons": [],
        "frames": [],
        "horizon_selections": [],
        "forecast_summary": {
            "status": "NOT_AVAILABLE",
            "trend": "unavailable",
            "trend_method": "first_to_last_finite_mean_score",
            "mean_score_delta": None,
        },
    }
    if risk_store_root is None or not risk_store_root.is_dir():
        return empty

    frames_by_valid_time: dict[str, dict] = {}
    for path in sorted((risk_store_root / "frames").glob("risk-sha256-*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read risk frame {path}: {exc}") from exc
        if document.get("schema_version") != "bc.risk-frame.v2":
            continue
        if document.get("scenario_id") != scenario_id:
            continue
        valid_time = document.get("valid_time")
        if not valid_time:
            continue
        valid = _parse_utc(valid_time)
        if valid < replay_start or valid > replay_end:
            continue
        payload = document.get("payload") or {}
        coordinates = payload.get("coordinates") or {}
        variables = payload.get("variables") or {}
        latitudes = coordinates.get("latitude")
        longitudes = coordinates.get("longitude")
        hard_reasons = variables.get("hard_reason")
        risk_levels = variables.get("risk_level")
        risk_scores = variables.get("risk_score")
        confidences = variables.get("confidence")
        if not all(
            isinstance(value, list)
            for value in (
                latitudes,
                longitudes,
                hard_reasons,
                risk_levels,
                risk_scores,
                confidences,
            )
        ):
            raise ValueError(f"risk frame {path} is missing spatial arrays")
        if any(
            len(matrix) != len(latitudes)
            or any(len(row) != len(longitudes) for row in matrix)
            for matrix in (hard_reasons, risk_levels, risk_scores, confidences)
        ):
            raise ValueError(f"risk frame {path} spatial arrays have mismatched shapes")
        flattened = {
            "hard_reasons": [reason for row in hard_reasons for reason in row],
            "risk_levels": [level for row in risk_levels for level in row],
            "risk_scores": [score for row in risk_scores for score in row],
            "confidences": [confidence for row in confidences for confidence in row],
        }
        frame = {
            "valid_time": valid_time,
            "risk_id": document.get("risk_id"),
            "as_of_time": document.get("as_of_time"),
            "provenance": document.get("provenance"),
            "coordinates": {"latitude": latitudes, "longitude": longitudes},
            **flattened,
        }
        previous = frames_by_valid_time.get(valid_time)
        if previous is not None and previous["risk_id"] != frame["risk_id"]:
            raise ValueError(f"multiple risk frames for valid_time {valid_time}")
        frames_by_valid_time[valid_time] = frame

    frames = [frames_by_valid_time[key] for key in sorted(frames_by_valid_time)]
    if not frames:
        return empty
    first = frames[0]
    latitude = first["coordinates"]["latitude"]
    longitude = first["coordinates"]["longitude"]
    risk_grid = {
        "rows": len(latitude),
        "cols": len(longitude),
        "latitude_resolution_degrees": (
            abs(latitude[1] - latitude[0]) if len(latitude) > 1 else None
        ),
        "longitude_resolution_degrees": (
            abs(longitude[1] - longitude[0]) if len(longitude) > 1 else None
        ),
    }
    for frame in frames:
        frame["summary"] = _risk_frame_summary(frame)
    hard_reasons = sorted(
        {
            reason
            for frame in frames
            for reason in frame["hard_reasons"]
            if reason not in (None, "NONE")
        }
    )
    return {
        **empty,
        "status": "PASS",
        "source": {
            "schema_version": "bc.risk-frame.v2",
            "risk_store_root": str(risk_store_root),
            "scenario_id": scenario_id,
            "provenance": sorted(
                {str(frame["provenance"]) for frame in frames}
            ),
        },
        "grid": risk_grid,
        "hard_reasons": hard_reasons,
        "frames": frames,
        "horizon_selections": _risk_horizon_selections(frames, simulation_times or []),
        "forecast_summary": _risk_forecast_summary(frames),
    }


def _build_basemap(
    *,
    land_mask_path: Path,
    width: int,
    height: int,
    version: str,
) -> tuple[BasemapMetadata, bytes]:
    mask = load_netcdf_land_mask(land_mask_path)
    lon_step, lat_step = mask.cell_step_degrees()
    bbox = mask.bbox()
    metadata = BasemapMetadata(
        projection="EPSG:4326",
        bbox={
            "min_lon": bbox["min_lon"] - lon_step / 2.0,
            "max_lon": bbox["max_lon"] + lon_step / 2.0,
            "min_lat": bbox["min_lat"] - lat_step / 2.0,
            "max_lat": bbox["max_lat"] + lat_step / 2.0,
        },
        width=width,
        height=height,
        source=str(land_mask_path),
        version=version,
        provenance={
            "product_id": "GEBCO_2026",
            "doi": "https://doi.org/10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa",
            "resolution_degrees": {"longitude": lon_step, "latitude": lat_step},
        },
    )
    pixels = _render_basemap(
        land=mask.land,
        lon0=mask.longitude[0],
        lat0=mask.latitude[0],
        lon_step=lon_step,
        lat_step=lat_step,
        width=width,
        height=height,
    )
    return metadata, pixels


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _load_v2_route_smoothing_sidecar(
    sidecar: dict[str, Any],
    *,
    route: dict[str, Any],
) -> dict[str, Any]:
    """Validate and bind an R1 v2 sidecar without changing the formal route."""

    coordinates = [
        [waypoint["lon"], waypoint["lat"]]
        for waypoint in route.get("waypoints", [])
    ]
    expected_route_digest = _canonical_sha256(coordinates)
    validation = validate_research_route_sidecar(
        sidecar,
        expected_route_digest=expected_route_digest,
    )
    _require(
        validation.valid,
        "route smoothing sidecar v2 validation failed: "
        f"{validation.reason or 'unknown_reason'}",
    )
    normalized = normalize_research_route_sidecar(
        sidecar,
        expected_route_digest=expected_route_digest,
    )
    _require(normalized is not None, "route smoothing sidecar v2 motion view is invalid")

    route_identity = sidecar["route_identity"]
    _require(
        route_identity.get("route_id") == route.get("route_id"),
        "route smoothing route identity differs",
    )
    if sidecar.get("plan_revision") is not None and route.get("revision") is not None:
        _require(
            sidecar["plan_revision"] == route["revision"],
            "route smoothing plan revision differs",
        )
    if (
        sidecar.get("adoption_time") is not None
        and route.get("effective_adoption_time") is not None
    ):
        _require(
            sidecar["adoption_time"] == route["effective_adoption_time"],
            "route smoothing adoption time differs",
        )

    authoritative = sidecar["authoritative_route"]
    authoritative_waypoints = authoritative.get("waypoints")
    _require(
        isinstance(authoritative_waypoints, list)
        and len(authoritative_waypoints) == len(route.get("waypoints", [])),
        "route smoothing authoritative waypoints are incomplete",
    )
    for expected, observed in zip(route["waypoints"], authoritative_waypoints, strict=True):
        _require(
            isinstance(observed, dict)
            and observed.get("lon") == expected["lon"]
            and observed.get("lat") == expected["lat"]
            and observed.get("eta") == expected["eta"],
            "route smoothing authoritative waypoint differs from the Winter route",
        )
    return sidecar


def _load_route_smoothing_sidecar(
    path: Path,
    *,
    route: dict[str, Any],
) -> dict[str, Any]:
    """Load an explicitly requested research-only motion sidecar."""

    sidecar = _read_json_object(path, label="route smoothing sidecar")
    if sidecar.get("schema_version") == SIDECAR_SCHEMA_VERSION_V2:
        return _load_v2_route_smoothing_sidecar(sidecar, route=route)
    _require(
        sidecar.get("schema_version") == "c.research-route-smoothing-sidecar.v1",
        "route smoothing sidecar schema is unsupported",
    )
    _require(
        sidecar.get("research_only") is True
        and sidecar.get("status") == "ACCEPTED"
        and sidecar.get("applied") is True,
        "route smoothing sidecar is not an accepted research result",
    )
    _require(
        sidecar.get("research_eligible") is True,
        "route smoothing sidecar did not pass the research qualification gate",
    )
    validation = sidecar.get("validation")
    _require(
        isinstance(validation, dict)
        and validation.get("research_gate_passed") is True
        and all(
            validation.get(name) is True
            for name in (
                "risk_rechecked",
                "hard_mask_rechecked",
                "coverage_complete",
                "eta_recomputed",
                "speed_checked",
            )
        ),
        "route smoothing sidecar qualification evidence is incomplete",
    )
    declared_digest = sidecar.get("sidecar_digest")
    _require(isinstance(declared_digest, str), "route smoothing sidecar digest is missing")
    digest_payload = dict(sidecar)
    digest_payload.pop("sidecar_digest", None)
    _require(
        declared_digest == _canonical_sha256(digest_payload),
        "route smoothing sidecar digest is invalid",
    )
    route_id = sidecar.get("route_id")
    _require(route_id in (None, route.get("route_id")), "route smoothing route identity differs")
    if sidecar.get("plan_revision") is not None and route.get("revision") is not None:
        _require(
            sidecar.get("plan_revision") == route.get("revision"),
            "route smoothing plan revision differs",
        )
    if (
        sidecar.get("adoption_time") is not None
        and route.get("effective_adoption_time") is not None
    ):
        _require(
            sidecar.get("adoption_time") == route.get("effective_adoption_time"),
            "route smoothing adoption time differs",
        )
    authoritative = sidecar.get("authoritative_route")
    _require(isinstance(authoritative, dict), "route smoothing authoritative route is missing")
    _require(
        authoritative.get("route_digest") == sidecar.get("raw_route_digest"),
        "route smoothing authoritative digest is inconsistent",
    )
    coordinates = [
        [waypoint["lon"], waypoint["lat"]]
        for waypoint in route.get("waypoints", [])
    ]
    _require(
        _canonical_sha256(coordinates) == sidecar.get("raw_route_digest"),
        "route smoothing sidecar does not bind the Winter route",
    )
    authoritative_waypoints = authoritative.get("waypoints")
    _require(
        isinstance(authoritative_waypoints, list)
        and len(authoritative_waypoints) == len(route.get("waypoints", [])),
        "route smoothing authoritative waypoints are incomplete",
    )
    for expected, observed in zip(route["waypoints"], authoritative_waypoints, strict=True):
        _require(
            isinstance(observed, dict)
            and observed.get("lon") == expected["lon"]
            and observed.get("lat") == expected["lat"]
            and observed.get("eta") == expected["eta"],
            "route smoothing authoritative waypoint differs from the Winter route",
        )
    samples = sidecar.get("motion_samples")
    _require(isinstance(samples, list) and len(samples) >= 2, "route smoothing samples are missing")
    previous_eta: datetime | None = None
    for sample in samples:
        _require(isinstance(sample, dict), "route smoothing sample is not an object")
        try:
            sample_eta = _parse_utc(sample["eta"])
            sample_lon = float(sample["lon"])
            sample_lat = float(sample["lat"])
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError("route smoothing sample is invalid") from None
        _require(
            math.isfinite(sample_lon)
            and math.isfinite(sample_lat)
            and -180.0 <= sample_lon <= 180.0
            and -90.0 <= sample_lat <= 90.0,
            "route smoothing sample coordinates are invalid",
        )
        _require(
            previous_eta is None or sample_eta > previous_eta,
            "route smoothing sample ETA is not strictly increasing",
        )
        previous_eta = sample_eta
    return sidecar


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _winter_recommended_plan(plan_set: dict[str, Any]) -> dict[str, Any]:
    layers = plan_set.get("layers")
    _require(isinstance(layers, list), "Winter C plan set layers are missing")
    full_voyage = next(
        (
            layer
            for layer in layers
            if isinstance(layer, dict) and layer.get("planning_layer") == "full_voyage"
        ),
        None,
    )
    _require(isinstance(full_voyage, dict), "Winter C full_voyage layer is missing")
    plans = full_voyage.get("plans")
    _require(isinstance(plans, dict), "Winter C full_voyage plans are missing")
    recommended = plans.get("recommended")
    _require(isinstance(recommended, dict), "Winter C recommended plan is missing")
    return recommended


def _validate_winter_identity(
    *,
    dataset_bundle: dict[str, Any],
    run_context: dict[str, Any],
    risk_index: dict[str, Any],
    risk_commit: dict[str, Any],
    plan_set: dict[str, Any],
    route_candidates: dict[str, Any],
    route_integrity: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fail closed when any Winter source identity crosses experiment boundaries."""

    _require(
        dataset_bundle.get("schema_version") == "a.dataset-bundle.v2",
        "Winter source is not a.dataset-bundle.v2",
    )
    _require(
        run_context.get("schema_version") == "run-context.v2",
        "Winter source is not RunContext.v2",
    )
    bundle_id = dataset_bundle.get("bundle_id")
    bundle_digest = dataset_bundle.get("bundle_digest")
    run_id = run_context.get("run_id")
    scenario_id = run_context.get("scenario_id")
    _require(
        run_context.get("dataset_bundle_id") == bundle_id
        and run_context.get("dataset_bundle_digest") == bundle_digest,
        "RunContext does not bind the supplied DatasetBundle",
    )
    _require(
        risk_index.get("status") == "FORMAL_VALIDATED"
        and risk_index.get("frame_schema") == "bc.risk-frame.v2",
        "Winter RiskFrame index is not FORMAL_VALIDATED bc.risk-frame.v2",
    )
    for label, value in (
        ("RiskFrame index run", risk_index.get("run_id")),
        ("RiskWindow run", risk_commit.get("run_id")),
        ("C plan set run", plan_set.get("run_id")),
        ("route candidate run", route_candidates.get("provenance", {}).get("source_run_id")),
    ):
        _require(value == run_id, f"{label} does not match RunContext")
    for label, value in (
        ("RiskFrame index scenario", risk_index.get("scenario_id")),
        ("RiskWindow scenario", risk_commit.get("scenario_id")),
        ("C plan set scenario", plan_set.get("scenario_id")),
    ):
        _require(value == scenario_id, f"{label} does not match RunContext")
    _require(
        risk_index.get("dataset_bundle_id") == bundle_id
        and risk_index.get("dataset_bundle_digest") == bundle_digest,
        "RiskFrame index does not bind the supplied DatasetBundle",
    )
    _require(
        risk_commit.get("schema_version") == "bc.risk-window-commit.v1",
        "Winter risk window is not bc.risk-window-commit.v1",
    )
    risk_window_id = risk_commit.get("commit_id")
    risk_window_digest = risk_commit.get("content_digest")
    _require(
        risk_index.get("commit_id") == risk_window_id
        and risk_index.get("content_digest") == risk_window_digest,
        "RiskFrame index and RiskWindow commit identity differ",
    )
    commit_frames = risk_commit.get("frames")
    frame_ids = risk_index.get("frame_ids")
    _require(
        isinstance(commit_frames, list)
        and isinstance(frame_ids, list)
        and len(commit_frames) == len(frame_ids) == risk_commit.get("count"),
        "RiskWindow frame cardinality is inconsistent",
    )
    commit_ids = [frame.get("risk_id") for frame in commit_frames if isinstance(frame, dict)]
    _require(commit_ids == frame_ids, "RiskFrame index order differs from RiskWindow commit")
    _require(
        risk_commit.get("interval_seconds") == 3600,
        "Winter RiskWindow is not hourly",
    )
    _require(
        risk_commit.get("start") == run_context.get("simulation_start")
        and risk_commit.get("end") == run_context.get("simulation_end"),
        "RiskWindow does not cover the formal RunContext window",
    )
    _require(
        dataset_bundle.get("minimum_required_end") >= run_context.get("simulation_end"),
        "DatasetBundle minimum_required_end does not cover RunContext",
    )
    _require(
        plan_set.get("schema_version") == "cd.four-layer-route-plan-set.v3",
        "Winter C source is not cd.four-layer-route-plan-set.v3",
    )
    recommended = _winter_recommended_plan(plan_set)
    selected_id = route_candidates.get("selected_candidate_id")
    _require(
        recommended.get("plan_id") == selected_id,
        "C recommended plan does not match selected_candidate_id",
    )
    selected_candidate = next(
        (
            candidate
            for candidate in route_candidates.get("candidates", [])
            if candidate.get("candidate_id") == selected_id
        ),
        None,
    )
    _require(isinstance(selected_candidate, dict), "selected route candidate is missing")
    plan_coordinates = [
        [waypoint.get("longitude"), waypoint.get("latitude")]
        for waypoint in recommended.get("waypoints", [])
    ]
    _require(
        selected_candidate.get("geometry", {}).get("coordinates") == plan_coordinates,
        "selected route candidate geometry differs from C plan",
    )
    metrics = recommended.get("metrics", {})
    candidate_metrics = selected_candidate.get("risk_metrics", {})
    _require(
        selected_candidate.get("distance_km") == metrics.get("distance_km")
        and selected_candidate.get("travel_hours") == metrics.get("eta_hours")
        and candidate_metrics.get("average_risk") == metrics.get("avg_risk")
        and candidate_metrics.get("maximum_risk") == metrics.get("max_risk")
        and candidate_metrics.get("integrated_risk_hours")
        == metrics.get("integrated_risk_hours"),
        "selected route candidate metrics differ from C plan",
    )
    candidate_ids = {
        candidate.get("candidate_id") for candidate in route_candidates.get("candidates", [])
    }
    integrity_ids = {
        item.get("route_id") for item in route_integrity if isinstance(item, dict)
    }
    _require(
        len(candidate_ids) == 12 and integrity_ids == candidate_ids,
        "route integrity evidence does not cover all 12 published candidates",
    )
    _require(
        all(
            item.get("status") == "PASS"
            and item.get("land_intersections") == 0
            and item.get("data_unavailable_violations") == 0
            and item.get("edge_hard_violations") == 0
            for item in route_integrity
        ),
        "route integrity evidence is not fail-closed PASS",
    )
    source_risk_ids = {
        risk_id
        for candidate in route_candidates.get("candidates", [])
        for risk_id in candidate.get("provenance", {}).get("source_risk_ids", [])
    }
    _require(
        source_risk_ids <= set(frame_ids),
        "route candidates reference RiskFrames outside the supplied RiskWindow",
    )
    return {
        "dataset_bundle_id": bundle_id,
        "dataset_bundle_digest": bundle_digest,
        "run_id": run_id,
        "scenario_id": scenario_id,
        "corridor_id": run_context.get("corridor_id"),
        "vessel_profile_id": run_context.get("vessel_profile_id"),
        "risk_window_id": risk_window_id,
        "risk_window_digest": risk_window_digest,
        "risk_frame_count": len(frame_ids),
        "risk_window_start": risk_commit.get("start"),
        "risk_window_end": risk_commit.get("end"),
        "layer_set_id": plan_set.get("layer_set_id"),
        "candidate_set_id": route_candidates.get("candidate_set_id"),
        "selected_candidate_id": selected_id,
    }


def _winter_route_meta(plan: dict[str, Any]) -> dict[str, Any]:
    waypoints = [
        {
            "lon": waypoint["longitude"],
            "lat": waypoint["latitude"],
            "eta": waypoint["eta"],
            "recommended_speed_mps": waypoint["recommended_speed_mps"],
        }
        for waypoint in plan["waypoints"]
    ]
    metrics = plan["metrics"]
    return {
        "revision": 1,
        "route_id": plan["plan_id"],
        "layer": plan["planning_layer"],
        "objective": plan["objective_mode"],
        "decision_time": plan["start_time"],
        "effective_adoption_time": plan["start_time"],
        "adoption_mode": "INITIAL",
        "distance_km": metrics["distance_km"],
        "arrival_eta": waypoints[-1]["eta"],
        "metrics": {
            "distance_km": metrics["distance_km"],
            "travel_hours": metrics["eta_hours"],
            "average_risk": metrics["avg_risk"],
            "maximum_risk": metrics["max_risk"],
            "integrated_risk_hours": metrics["integrated_risk_hours"],
            "minimum_confidence": metrics["minimum_confidence"],
        },
        "waypoints": waypoints,
    }


def _winter_vessel_timeline(
    plan: dict[str, Any],
    *,
    cadence_seconds: int,
) -> list[dict[str, Any]]:
    """Project C waypoint ETA into the existing timeline shape without pixel motion."""

    _require(cadence_seconds > 0, "timeline cadence must be positive")
    waypoints = plan.get("waypoints")
    _require(isinstance(waypoints, list) and len(waypoints) >= 2, "route needs waypoints")
    eta = [_parse_utc(waypoint["eta"]) for waypoint in waypoints]
    _require(eta == sorted(eta) and len(set(eta)) == len(eta), "waypoint ETA must increase")
    moments: list[datetime] = []
    moment = eta[0]
    while moment < eta[-1]:
        moments.append(moment)
        moment += timedelta(seconds=cadence_seconds)
    moments.append(eta[-1])

    result: list[dict[str, Any]] = []
    previous_track_count = -1
    for moment in moments:
        arrived = moment >= eta[-1]
        edge_index = min(max(0, bisect.bisect_right(eta, moment) - 1), len(eta) - 2)
        start = waypoints[edge_index]
        end = waypoints[edge_index + 1]
        duration = (eta[edge_index + 1] - eta[edge_index]).total_seconds()
        progress = 1.0 if arrived else (moment - eta[edge_index]).total_seconds() / duration
        progress = max(0.0, min(1.0, progress))
        longitude = start["longitude"] + (end["longitude"] - start["longitude"]) * progress
        latitude = start["latitude"] + (end["latitude"] - start["latitude"]) * progress
        completed_count = bisect.bisect_right(eta, moment)
        speed_mps = 0.0 if arrived else float(start["recommended_speed_mps"])
        entry: dict[str, Any] = {
            "t": _iso(moment),
            "v": {
                "lon": longitude,
                "lat": latitude,
                "kn": speed_mps * 1.9438444924406,
                "status": "ARRIVED" if arrived else "UNDERWAY",
                "ep": progress,
                "eidx": edge_index,
            },
            "arv": 1,
            "prv": None,
            "prs": "NONE",
            "dt": None,
            "eat": None,
            "ctl": completed_count,
            "seg": {
                "index": edge_index,
                "start_eta": start["eta"],
                "end_eta": end["eta"],
            },
        }
        if completed_count != previous_track_count:
            entry["track"] = [
                {
                    "longitude": waypoint["longitude"],
                    "latitude": waypoint["latitude"],
                    "eta": waypoint["eta"],
                }
                for waypoint in waypoints[:completed_count]
            ]
            previous_track_count = completed_count
        result.append(entry)
    return result


def _winter_acceptance_positions(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    start = _parse_utc(timeline[0]["t"])

    def at_offset(minutes: int) -> dict[str, Any]:
        target = start + timedelta(minutes=minutes)
        index = min(
            range(len(timeline)),
            key=lambda item: abs((_parse_utc(timeline[item]["t"]) - target).total_seconds()),
        )
        vessel = timeline[index]["v"]
        return {
            "time": timeline[index]["t"],
            "longitude": vessel["lon"],
            "latitude": vessel["lat"],
            "speed_knots": vessel["kn"],
            "status": vessel["status"],
        }

    return {"departure": at_offset(0), "+30m": at_offset(30), "+60m": at_offset(60)}


def _winter_combined_identity(
    identity: dict[str, Any],
    *,
    route: dict[str, Any],
    cadence_seconds: int,
    route_smoothing_sidecar_digest: str | None = None,
    route_motion_set_ids: list[str] | None = None,
) -> tuple[str, str]:
    semantic_identity = {
        **identity,
        "simulation_start": route["waypoints"][0]["eta"],
        "simulation_end": route["waypoints"][-1]["eta"],
        "timeline_source": "cd.route-plan.v3.waypoints.eta",
        "timeline_cadence_seconds": cadence_seconds,
    }
    if route_smoothing_sidecar_digest is not None:
        semantic_identity["route_smoothing_sidecar_digest"] = route_smoothing_sidecar_digest
    if route_motion_set_ids:
        semantic_identity["route_motion_set_ids"] = route_motion_set_ids
    digest = _canonical_sha256(semantic_identity)
    return f"winter-viewer-sha256-{digest}", digest


def _export_winter_combined(args: argparse.Namespace) -> int:
    required_paths = {
        "dataset_bundle": args.winter_dataset_bundle,
        "run_context": args.winter_run_context,
        "risk_frame_index": args.winter_risk_frame_index,
        "risk_window_commit": args.winter_risk_window_commit,
        "plan_set": args.winter_plan_set,
        "route_candidates": args.route_candidates,
        "route_integrity": args.winter_route_integrity,
    }
    missing = [name for name, path in required_paths.items() if path is None]
    _require(not missing, f"Winter combined export missing arguments: {', '.join(missing)}")

    dataset_bundle = _read_json_object(required_paths["dataset_bundle"], label="DatasetBundle")
    run_context = _read_json_object(required_paths["run_context"], label="RunContext")
    risk_index = _read_json_object(required_paths["risk_frame_index"], label="RiskFrame index")
    risk_commit = _read_json_object(
        required_paths["risk_window_commit"], label="RiskWindow commit"
    )
    plan_set = _read_json_object(required_paths["plan_set"], label="C plan set")
    route_candidates = load_route_candidates(required_paths["route_candidates"])
    integrity_value = json.loads(required_paths["route_integrity"].read_text(encoding="utf-8"))
    _require(isinstance(integrity_value, list), "route integrity evidence must be a list")
    identity = _validate_winter_identity(
        dataset_bundle=dataset_bundle,
        run_context=run_context,
        risk_index=risk_index,
        risk_commit=risk_commit,
        plan_set=plan_set,
        route_candidates=route_candidates,
        route_integrity=integrity_value,
    )
    recommended = _winter_recommended_plan(plan_set)
    route = _winter_route_meta(recommended)
    route_motion_set = None
    if args.route_motion_set is not None:
        route_motion_set = load_bound_route_motion_set(
            args.route_motion_set,
            plan_set_document=plan_set,
            replay_routes=[route],
        )
        validate_route_motion_context(
            route_motion_set,
            risk_window_id=identity["risk_window_id"],
            risk_window_digest=identity["risk_window_digest"],
            vessel_profile_id=run_context["vessel_profile_id"],
            vessel_profile_version=run_context["vessel_profile_version"],
            vessel_profile_digest=run_context["vessel_profile_digest"],
        )
    route_smoothing_sidecar = None
    if args.route_smoothing_sidecar is not None:
        route_smoothing_sidecar = _load_route_smoothing_sidecar(
            args.route_smoothing_sidecar,
            route=route,
        )
    timeline = _winter_vessel_timeline(recommended, cadence_seconds=args.cadence_seconds)
    risk_store_root = (
        args.risk_store_root
        or required_paths["risk_frame_index"].parent / "risk-store"
    )
    risk = _presentation_risk(
        risk_store_root=risk_store_root,
        scenario_id=identity["scenario_id"],
        replay_start=_parse_utc(identity["risk_window_start"]),
        replay_end=_parse_utc(identity["risk_window_end"]),
        simulation_times=[_parse_utc(entry["t"]) for entry in timeline],
    )
    _require(risk.get("status") == "PASS", "Winter RiskFrame presentation projection failed")
    projected_ids = [frame.get("risk_id") for frame in risk.get("frames", [])]
    _require(
        projected_ids == risk_index.get("frame_ids"),
        "projected RiskFrames differ from the formal RiskWindow index",
    )
    risk["source"].update(
        {
            "run_id": identity["run_id"],
            "dataset_bundle_id": identity["dataset_bundle_id"],
            "dataset_bundle_digest": identity["dataset_bundle_digest"],
            "risk_window_id": identity["risk_window_id"],
            "risk_window_digest": identity["risk_window_digest"],
        }
    )

    land_mask_path = args.land_mask or find_land_sea_mask(args.route_id, args.data_root)
    _require(land_mask_path is not None, "no local GEBCO land_sea_mask found")
    metadata, pixels = _build_basemap(
        land_mask_path=land_mask_path,
        width=args.width,
        height=args.height,
        version=args.basemap_version,
    )
    assembly_id, assembly_digest = _winter_combined_identity(
        identity,
        route=route,
        cadence_seconds=args.cadence_seconds,
        route_smoothing_sidecar_digest=(
            route_smoothing_sidecar.get("sidecar_digest")
            if route_smoothing_sidecar is not None
            else None
        ),
        route_motion_set_ids=(
            [route_motion_set["motion_set_id"]]
            if route_motion_set is not None
            else None
        ),
    )
    source_files = {
        name: {"path": str(path), "sha256": _sha256_file(path)}
        for name, path in required_paths.items()
    }
    if args.route_smoothing_sidecar is not None:
        source_files["route_smoothing_sidecar"] = {
            "path": str(args.route_smoothing_sidecar),
            "sha256": _sha256_file(args.route_smoothing_sidecar),
        }
    if args.route_motion_set is not None:
        source_files["route_motion_set"] = {
            "path": str(args.route_motion_set),
            "sha256": _sha256_file(args.route_motion_set),
        }
    research_validation = {
        "label": "Winter C Validation",
        "scenario_label": "Winter Arctic Research",
        "dataset_bundle_id": identity["dataset_bundle_id"],
        "run_context_id": identity["run_id"],
        "risk_window_id": identity["risk_window_id"],
        "risk_schema": "bc.risk-frame.v2",
        "risk_frame_count": identity["risk_frame_count"],
        "route_schema": "cd.four-layer-route-plan-set.v3",
        "candidate_schema": "presentation.route-candidates.v1",
    }
    if route_smoothing_sidecar is not None:
        research_validation["route_smoothing"] = route_smoothing_sidecar
    bundle = {
        "schema_version": "replay.viewer-bundle.v1",
        "replay": {
            "replay_id": assembly_id,
            "scenario_id": identity["scenario_id"],
            "scenario_mode": "research_navigation_simulation",
            "identity_kind": "combined_presentation_assembly",
            "start": route["waypoints"][0]["eta"],
            "end": route["waypoints"][-1]["eta"],
            "manifest_semantic_digest": assembly_digest,
        },
        "combined_presentation": {
            "schema_version": WINTER_COMBINED_SCHEMA,
            "status": "PUBLISHED",
            "assembly_id": assembly_id,
            "assembly_digest": assembly_digest,
            "scenario_label": "Winter Arctic Research",
            "dataset_bundle_id": identity["dataset_bundle_id"],
            "dataset_bundle_digest": identity["dataset_bundle_digest"],
            "run_context_id": identity["run_id"],
            "risk_window_id": identity["risk_window_id"],
            "risk_window_digest": identity["risk_window_digest"],
            "layer_set_id": identity["layer_set_id"],
            "candidate_set_id": identity["candidate_set_id"],
            "selected_candidate_id": identity["selected_candidate_id"],
            "timeline_source": "cd.route-plan.v3.waypoints.eta",
            "source_replay": None,
        },
        "research_validation": research_validation,
        "basemap": metadata.to_dict(),
        "presentation": VIEWER_PRESENTATION,
        "gates": {
            "status": "PASS",
            "l2_status": "PASS",
            "source": "winter_identity_and_existing_route_integrity",
            "route_integrity_count": len(integrity_value),
        },
        "routes": [route],
        "route_candidates": route_candidates,
        "events": [
            {"t": route["waypoints"][0]["eta"], "type": "PLAN_COMPUTED", "rev": 1}
        ],
        "timeline": timeline,
        "risk": risk,
        "acceptance_positions": _winter_acceptance_positions(timeline),
    }
    if route_motion_set is not None:
        bundle["combined_presentation"]["route_motion_set_ids"] = [
            route_motion_set["motion_set_id"]
        ]
        bundle["combined_presentation"]["route_motion_set_bindings"] = [
            {
                "motion_set_id": route_motion_set["motion_set_id"],
                "layer_set_id": route_motion_set["layer_set_id"],
                "risk_window_id": route_motion_set["risk_window_id"],
                "risk_window_digest": route_motion_set["risk_window_digest"],
            }
        ]
        bundle["route_motion_sets"] = [route_motion_set]
    target_output_dir = args.output_dir.resolve()
    _require(
        not target_output_dir.exists(),
        f"immutable Viewer output already exists: {target_output_dir}",
    )
    target_output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{target_output_dir.name}.",
            dir=target_output_dir.parent,
        )
    )
    _write_png(output_dir / "gebco_basemap.png", args.width, args.height, pixels)
    (output_dir / "basemap_metadata.json").write_text(
        json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    bundle_path = output_dir / "bundle.json"
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    preflight_path = output_dir / "replay-viewer-preflight.json"
    preflight_path.write_text(
        json.dumps(
            {
                "schema_version": "presentation.winter-combined-preflight.v1",
                "overall": "PASS",
                "l2": {"overall": "PASS", "route_count": len(integrity_value)},
                "identity": identity,
                "checks": {
                    "dataset_run_binding": "PASS",
                    "risk_window_binding": "PASS",
                    "route_candidate_binding": "PASS",
                    "route_integrity": "PASS",
                    "timeline_eta_projection": "PASS",
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": WINTER_MANIFEST_SCHEMA,
        "status": "PASS",
        "assembly_id": assembly_id,
        "assembly_digest": assembly_digest,
        "bundle_path": str(target_output_dir / "bundle.json"),
        "bundle_sha256": _sha256_file(bundle_path),
        "basemap_sha256": _sha256_file(output_dir / "gebco_basemap.png"),
        "preflight_sha256": _sha256_file(preflight_path),
        "source_artifacts": source_files,
        "identity": identity,
        "timeline_samples": len(timeline),
        "risk_frames": len(risk["frames"]),
        "route_candidates": len(route_candidates["candidates"]),
        "route_motion_sets": 1 if route_motion_set is not None else 0,
    }
    (output_dir / "winter-combined-viewer-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(output_dir, target_output_dir)
    output_dir = target_output_dir
    print(
        "wrote",
        output_dir,
        "assembly=",
        assembly_id,
        "timeline=",
        len(timeline),
        "risk_frames=",
        len(risk["frames"]),
        "candidates=",
        len(route_candidates["candidates"]),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay-viewer-export")
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("--snapshots-dir", type=Path, default=None)
    parser.add_argument("--risk-store-root", type=Path, default=None)
    data_default = _workspace_root() / "work_package_a" / "data"
    parser.add_argument("--data-root", type=Path, default=data_default)
    parser.add_argument("--route-id", default="tromso_to_isfjorden_outer")
    parser.add_argument("--land-mask", type=Path, default=None)
    parser.add_argument("--cadence-seconds", type=int, default=60)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--sample-step-km", type=float, default=5.0)
    parser.add_argument(
        "--route-candidates",
        type=Path,
        default=None,
        help="optional validated presentation.route-candidates.v1 artifact",
    )
    parser.add_argument("--winter-dataset-bundle", type=Path, default=None)
    parser.add_argument("--winter-run-context", type=Path, default=None)
    parser.add_argument("--winter-risk-frame-index", type=Path, default=None)
    parser.add_argument("--winter-risk-window-commit", type=Path, default=None)
    parser.add_argument("--winter-plan-set", type=Path, default=None)
    parser.add_argument("--winter-route-integrity", type=Path, default=None)
    parser.add_argument(
        "--route-smoothing-sidecar",
        type=Path,
        default=None,
        help="optional accepted C research-only route smoothing sidecar",
    )
    parser.add_argument(
        "--route-motion-set",
        type=Path,
        default=None,
        help="optional formally bound cd.route-motion-set.v1 artifact",
    )
    parser.add_argument("--basemap-version", default="gebco-2026-d5a7e2fe3915-7baad866")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_workspace_root() / "work_package_d" / "viewer",
    )
    args = parser.parse_args(argv)

    if args.winter_plan_set is not None:
        return _export_winter_combined(args)
    if args.route_motion_set is not None:
        parser.error("--route-motion-set currently requires --winter-plan-set binding")
    if args.manifest is None:
        parser.error("manifest is required unless --winter-plan-set is supplied")

    manifest_doc = json.loads(args.manifest.read_text(encoding="utf-8"))
    snapshots_dir = args.snapshots_dir or args.manifest.parent / "snapshots"
    risk_store_root = args.risk_store_root or args.manifest.parent / "risk-store"
    snapshots = [
        json.loads((snapshots_dir / f"{entry['index']:04d}.json").read_text(encoding="utf-8"))
        for entry in manifest_doc["snapshots"]
    ]

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    preflight_path = output_dir / "replay-viewer-preflight.json"
    preflight = run_viewer_preflight(
        manifest_doc,
        snapshots,
        data_root=args.data_root,
        route_id=args.route_id,
        land_mask_path=args.land_mask,
        sample_step_km=args.sample_step_km,
        basemap_version=args.basemap_version,
        output_path=preflight_path,
    )

    land_mask_path = args.land_mask or find_land_sea_mask(args.route_id, args.data_root)
    if land_mask_path is None:
        sys.exit("no local GEBCO land_sea_mask found")
    metadata, pixels = _build_basemap(
        land_mask_path=land_mask_path,
        width=args.width,
        height=args.height,
        version=args.basemap_version,
    )
    _write_png(output_dir / "gebco_basemap.png", args.width, args.height, pixels)
    (output_dir / "basemap_metadata.json").write_text(
        json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    adapter = PresentationAdapter(manifest_doc, snapshots)
    replay_start = adapter.replay_start
    replay_end = adapter.replay_end
    gates = {
        "status": preflight.overall,
        "l2_status": preflight.document.get("l2", {}).get("overall"),
    }
    positions = {
        time_label: adapter.vessel_at(moment)
        for time_label, moment in (
            ("10:00", adapter.replay_start),
            ("10:30", adapter.replay_start + timedelta(minutes=30)),
            ("11:00", adapter.replay_start + timedelta(minutes=60)),
        )
    }
    timeline = _timeline(adapter, args.cadence_seconds)
    risk = _presentation_risk(
        risk_store_root=risk_store_root,
        scenario_id=str(manifest_doc.get("scenario_id", "")),
        replay_start=replay_start,
        replay_end=replay_end,
        simulation_times=[_parse_utc(entry["t"]) for entry in timeline],
    )
    route_candidates = _route_candidates_package(
        args.route_candidates,
        scenario_id=str(manifest_doc.get("scenario_id", "")),
    )
    bundle = {
        "schema_version": "replay.viewer-bundle.v1",
        "replay": {
            "replay_id": manifest_doc.get("replay_id"),
            "scenario_id": manifest_doc.get("scenario_id"),
            "scenario_mode": manifest_doc.get("scenario_mode"),
            "start": manifest_doc.get("replay_start"),
            "end": manifest_doc.get("replay_end"),
            "manifest_semantic_digest": manifest_doc.get("semantic_digest"),
        },
        "basemap": metadata.to_dict(),
        "presentation": VIEWER_PRESENTATION,
        "gates": gates,
        "routes": [
            _route_meta(adapter, revision)
            for revision in sorted(adapter._routes_by_revision)
        ],
        "route_candidates": route_candidates,
        "events": [
            {
                "t": event["simulation_time"],
                "type": event["type"],
                "rev": event.get("revision"),
            }
            for event in adapter._events
        ],
        "timeline": timeline,
        "risk": risk,
        "acceptance_positions": positions,
    }
    (output_dir / "bundle.json").write_text(
        json.dumps(bundle, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        "wrote",
        output_dir,
        "preflight=",
        gates["status"],
        "l2=",
        gates["l2_status"],
        "timeline=",
        len(bundle["timeline"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
