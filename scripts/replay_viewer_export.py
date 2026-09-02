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
from arctic_route_planning.publishing import four_layer_route_plan_set_from_dict

from arctic_route_orchestrator.replay.digests import replay_semantic_digest
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
    load_bound_route_motion_candidate_set,
    load_bound_route_motion_set,
    validate_route_motion_context,
)
from arctic_route_orchestrator.route_presentation import (
    load_route_candidates,
    project_route_candidates,
    project_runtime_route_candidates,
)

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
        "formal_motion_required_for_production_default": True,
        "formal_motion_failure_policy": "RAW_WAYPOINT_TIMELINE_FAIL_CLOSED",
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


def _route_meta(
    adapter: PresentationAdapter,
    revision: int,
    *,
    route_id_override: str | None = None,
    plan_override: dict[str, Any] | None = None,
    motion_time_offset_seconds: float = 0.0,
) -> dict:
    entry = adapter._routes_by_revision[revision]
    route = entry["route"]
    snapshot_ship = (entry.get("snapshot") or {}).get("ship_state") or {}
    route_id = (
        entry.get("route_id")
        or route.get("route_id")
        or route.get("plan_id")
        or (plan_override or {}).get("plan_id")
        or route_id_override
    )
    waypoints = []
    plan_waypoints = (plan_override or {}).get("waypoints", ())
    for index, item in enumerate(route["waypoints"]):
        waypoint = {
            "lon": item["longitude"],
            "lat": item["latitude"],
            "eta": item["eta"],
        }
        recommended_speed = item.get("recommended_speed_mps")
        if recommended_speed is None and index < len(plan_waypoints):
            recommended_speed = plan_waypoints[index].get("recommended_speed_mps")
        if recommended_speed is not None:
            waypoint["recommended_speed_mps"] = recommended_speed
        waypoints.append(waypoint)
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
            elif event["type"] in {"REPLAN_ADOPTED", "ROUTE_CHANGED"}:
                adopt_time = event["simulation_time"]
    if revision == 1:
        adopt_time = adapter.replay_start.isoformat().replace("+00:00", "Z")
    return {
        "revision": revision,
        "route_id": route_id,
        "plan_digest": snapshot_ship.get("accepted_plan_digest"),
        "layer": "full_voyage",
        "objective": "recommended",
        "decision_time": decision_time,
        "effective_adoption_time": adopt_time,
        "adoption_mode": mode,
        "motion_time_offset_seconds": motion_time_offset_seconds,
        "distance_km": (
            (plan_override or {}).get("metrics", {}).get("distance_km")
            if plan_override is not None
            else route.get("distance_km")
        ),
        "arrival_eta": waypoints[-1]["eta"] if waypoints else None,
        "metrics": {
            "distance_km": (
                (plan_override or {}).get("metrics", {}).get("distance_km")
                if plan_override is not None
                else route.get("distance_km")
            ),
            "average_risk": (
                (plan_override or {}).get("metrics", {}).get("avg_risk")
                if plan_override is not None
                else route.get("average_risk", route.get("avg_risk"))
            ),
            "maximum_risk": (
                (plan_override or {}).get("metrics", {}).get("max_risk")
                if plan_override is not None
                else route.get("maximum_risk", route.get("max_risk"))
            ),
        },
        "waypoints": waypoints,
    }


def _timeline(
    adapter: PresentationAdapter,
    cadence_seconds: int,
    *,
    end_time: datetime | None = None,
) -> list[dict]:
    results: list[dict] = []
    previous_track_key: tuple | None = None
    previous_pending_key: tuple | None = None
    previous_superseded_key: tuple | None = None
    moment = adapter.replay_start
    final_time = end_time or adapter.replay_end
    _require(final_time >= moment, "timeline end must not precede replay start")
    while moment <= final_time:
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
        }
        if segment["index"] is not None:
            entry["seg"] = {
                "index": segment["index"],
                "start_eta": segment["start_eta"],
                "end_eta": segment["end_eta"],
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
        next_moment = moment + timedelta(seconds=cadence_seconds)
        if next_moment > final_time:
            next_moment = final_time
        if next_moment == moment:
            break
        moment = next_moment
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
        hard_mask = variables.get("hard_mask")
        risk_levels = variables.get("risk_level")
        risk_scores = variables.get("risk_score")
        confidences = variables.get("confidence")
        _require(
            isinstance(hard_mask, list),
            f"risk frame {path} is missing hard_mask",
        )
        if hard_reasons is None:
            # bc.risk-frame.v2 intentionally keeps the per-cell reason in
            # producer-side attributes in some formal builds.  Preserve the
            # exact hard-mask cells for D, but never invent a physical cause.
            hard_reasons = [
                [
                    "HARD_MASK_REASON_UNAVAILABLE" if bool(value) else "NONE"
                    for value in row
                ]
                for row in hard_mask
            ]
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
            for matrix in (
                hard_reasons,
                hard_mask,
                risk_levels,
                risk_scores,
                confidences,
            )
        ):
            raise ValueError(f"risk frame {path} spatial arrays have mismatched shapes")
        flattened = {
            "hard_reasons": [reason for row in hard_reasons for reason in row],
            "risk_levels": [level for row in risk_levels for level in row],
            "risk_scores": [score for row in risk_scores for score in row],
            "confidences": [confidence for row in confidences for confidence in row],
            "hard_mask": [value for row in hard_mask for value in row],
        }
        frame = {
            "valid_time": valid_time,
            "risk_id": document.get("risk_id"),
            "as_of_time": document.get("as_of_time"),
            "provenance": document.get("provenance"),
            "run_id": document.get("run_id"),
            "corridor_id": document.get("corridor_id"),
            "vessel_profile_id": document.get("vessel_profile_id"),
            "risk_window_id": document.get("risk_window_id"),
            "hard_reason_source": (
                "payload.hard_reason"
                if variables.get("hard_reason") is not None
                else "producer_hard_mask_only_reason_unavailable"
            ),
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
            "run_id": first.get("run_id"),
            "corridor_id": first.get("corridor_id"),
            "vessel_profile_id": first.get("vessel_profile_id"),
            "risk_window_id": first.get("risk_window_id"),
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


def _load_risk_explanation_manifest(
    manifest_path: Path,
    *,
    expected_identity: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a B immutable explanation artifact through its manifest.

    Orchestrator transports the already-produced sidecar; it never computes
    contributors or repairs a missing explanation.  Every identity field that
    is available at this assembly boundary is compared before the sidecar is
    allowed into the Viewer bundle.
    """

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"risk explanation manifest is missing or invalid: {manifest_path}"
        ) from exc
    _require(
        isinstance(manifest, dict)
        and manifest.get("schema_version") == "risk-explanation-manifest.v1"
        and manifest.get("status") == "PUBLISHED"
        and manifest.get("sidecar_schema_version") == "risk-explanation.v1",
        "risk explanation manifest is unsupported",
    )
    relative = manifest.get("artifact_path")
    _require(
        isinstance(relative, str) and not Path(relative).is_absolute(),
        "risk explanation artifact path must be relative",
    )
    artifact_path = (manifest_path.parent / relative).resolve()
    store_root = manifest_path.parent.parent.resolve()
    try:
        artifact_path.relative_to(store_root)
    except ValueError as exc:
        raise ValueError("risk explanation artifact escapes manifest directory") from exc
    _require(artifact_path.is_file(), f"risk explanation artifact is missing: {artifact_path}")
    artifact_bytes = artifact_path.read_bytes()
    artifact_sha256 = manifest.get("artifact_sha256")
    _require(
        isinstance(artifact_sha256, str)
        and len(artifact_sha256) == 64
        and all(character in "0123456789abcdef" for character in artifact_sha256)
        and _sha256_bytes(artifact_bytes) == artifact_sha256,
        "risk explanation artifact digest mismatch",
    )
    artifact_id = manifest.get("artifact_id")
    _require(
        artifact_id == f"risk-explanation-sha256-{artifact_sha256}"
        and artifact_path.name == f"{artifact_id}.json",
        "risk explanation artifact identity mismatch",
    )
    try:
        sidecar = json.loads(artifact_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("risk explanation artifact is invalid JSON") from exc
    _require(
        isinstance(sidecar, dict)
        and sidecar.get("schema_version") == "risk-explanation.v1"
        and sidecar.get("identity") == manifest.get("identity"),
        "risk explanation artifact identity or schema mismatch",
    )
    identity = sidecar["identity"]
    _require(
        isinstance(identity, dict)
        and isinstance(identity.get("risk_window_id"), str)
        and manifest_path.name == f"{identity['risk_window_id']}.json",
        "risk explanation manifest identity is invalid",
    )
    for name, expected in expected_identity.items():
        if expected is not None:
            _require(
                identity.get(name) == expected,
                f"risk explanation {name} does not match assembly",
            )
    return manifest, sidecar


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_causal_replay_source(
    manifest_path: Path,
    *,
    snapshots_dir: Path | None,
    expected_identity: dict[str, Any],
    expected_route: dict[str, Any],
    allow_retrospective: bool = False,
) -> tuple[PresentationAdapter, dict[str, Any], Path]:
    """Load a real, identity-bound replay for Winter assembly.

    ``causal_replay`` keeps issue-time visibility.  The explicitly opt-in
    ``retrospective_dynamic_replay`` mode consumes the same producer events
    after the fact and is labelled as such; it is never treated as historical
    information available at the simulation clock.
    """

    manifest = _read_json_object(manifest_path, label="causal replay manifest")
    scenario_mode = manifest.get("scenario_mode")
    allowed_modes = {"causal_replay"}
    if allow_retrospective:
        allowed_modes.add("retrospective_dynamic_replay")
    _require(
        manifest.get("schema_version") == "orchestrator.replay-manifest.v1"
        and scenario_mode in allowed_modes,
        "Winter source replay is not an allowed orchestrator replay",
    )
    snapshots_root = snapshots_dir or manifest_path.parent / "snapshots"
    entries = manifest.get("snapshots")
    _require(isinstance(entries, list) and entries, "causal replay snapshots are missing")
    snapshots = []
    for entry in entries:
        _require(isinstance(entry, dict) and isinstance(entry.get("resource"), str),
                 "causal replay snapshot resource is invalid")
        resource = Path(entry["resource"])
        # Runner manifests use ``snapshots/0000.json`` relative to the
        # manifest directory.  A caller may instead pass a copied snapshots
        # directory and use ``0000.json``.  Accept both spellings, but keep
        # the resolved file inside the selected source root.
        relative = (
            Path(*resource.parts[1:])
            if resource.parts
            and resource.parts[0] in {"snapshots", snapshots_root.name}
            else resource
        )
        snapshot_path = (snapshots_root / relative).resolve()
        if not snapshot_path.is_file() and not resource.is_absolute():
            manifest_relative = (manifest_path.parent / resource).resolve()
            if manifest_relative.is_file():
                snapshot_path = manifest_relative
        try:
            snapshot_path.relative_to(snapshots_root.resolve())
        except ValueError as exc:
            raise ValueError("causal replay snapshot escapes source directory") from exc
        _require(snapshot_path.is_file(), f"causal replay snapshot is missing: {snapshot_path}")
        snapshot = _read_json_object(snapshot_path, label="causal replay snapshot")
        declared_digest = entry.get("digest")
        if declared_digest is not None:
            # ``ReplayRunner`` publishes the semantic snapshot digest (the
            # same value checked by replay.validation), not the filesystem
            # byte hash.  Recompute it from the loaded document so real
            # runner-produced manifests remain consumable and mutations of
            # business fields fail closed.
            snapshot_digest = snapshot.get("snapshot_digest")
            _require(
                isinstance(declared_digest, str)
                and snapshot_digest == declared_digest
                and replay_semantic_digest(
                    {
                        key: value
                        for key, value in snapshot.items()
                        if key != "snapshot_digest"
                    }
                ) == snapshot_digest,
                "causal replay snapshot digest mismatch",
            )
        snapshots.append(snapshot)
    provenance = manifest.get("provenance") or {}
    bound = provenance.get("presentation_identity") or provenance.get("identity")
    _require(isinstance(bound, dict), "causal replay has no presentation identity binding")
    for name, expected in expected_identity.items():
        if expected is not None:
            _require(
                bound.get(name) == expected,
                f"causal replay {name} does not match Winter assembly",
            )
    expected_start = _parse_utc(expected_route["waypoints"][0]["eta"])
    expected_end = _parse_utc(expected_route["waypoints"][-1]["eta"])
    replay_start = _parse_utc(str(manifest.get("replay_start")))
    replay_end = _parse_utc(str(manifest.get("replay_end")))
    _require(
        manifest.get("scenario_id") == expected_identity.get("scenario_id")
        and replay_start == expected_start
        and expected_start <= replay_end
        and (allow_retrospective or replay_end <= expected_end),
        "causal replay time or scenario identity differs from Winter route",
    )
    events = manifest.get("events") or []
    observed_events = [
        event for event in events
        if isinstance(event, dict) and event.get("observed") is True
    ]
    types = [event.get("type") for event in observed_events]
    _require(
        "REPLAN_DECIDED" in types and "REPLAN_ADOPTED" in types,
        "replay does not contain an observed replan decision/adoption",
    )
    adapter = PresentationAdapter(manifest, snapshots)
    revisions = sorted(adapter._routes_by_revision)
    _require(len(revisions) > 1, "replay has only one route revision")
    initial = _route_meta(adapter, revisions[0])
    _require(
        (
            initial.get("route_id") in (None, expected_route.get("route_id"))
            and [
            {key: point[key] for key in ("lon", "lat", "eta")}
            for point in initial.get("waypoints", [])
            ] == [
            {"lon": point["lon"], "lat": point["lat"], "eta": point["eta"]}
            for point in expected_route["waypoints"]
            ]
        ),
        "causal replay initial route differs from Winter authoritative route",
    )
    source = {
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "replay_id": manifest.get("replay_id"),
        "scenario_mode": scenario_mode,
        "temporal_claim": (
            "historically_available_causal"
            if scenario_mode == "causal_replay"
            else "retrospective_post_hoc_dynamic_projection"
        ),
        "semantic_digest": manifest.get("semantic_digest"),
        "revision_count": len(revisions),
        "event_types": sorted(set(types)),
    }
    revision_index = _load_plan_revision_index(manifest_path, manifest)
    if revision_index is not None:
        source.update(revision_index)
    elif scenario_mode == "retrospective_dynamic_replay":
        raise ValueError(
            "retrospective dynamic replay has no immutable plan revision index"
        )
    provenance = manifest.get("provenance") or {}
    source["knowledge_as_of"] = provenance.get("knowledge_as_of")
    return adapter, source, manifest_path


def _load_plan_revision_index(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate the content-addressed C plan-set revision transport."""

    resources = manifest.get("resources") or {}
    relative_value = resources.get("planning_revision_index")
    if not isinstance(relative_value, str):
        return None
    relative = Path(relative_value)
    _require(not relative.is_absolute(), "plan revision index path must be relative")
    root = manifest_path.parent.resolve()
    index_path = (root / relative).resolve()
    try:
        index_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("plan revision index escapes replay directory") from exc
    _require(index_path.is_file(), "plan revision index is missing")
    index = _read_json_object(index_path, label="plan revision index")
    _require(
        index.get("schema_version") == "orchestrator.plan-revision-index.v1",
        "plan revision index schema is unsupported",
    )
    declared = index.get("content_digest")
    payload = {key: value for key, value in index.items() if key != "content_digest"}
    _require(
        isinstance(declared, str) and declared == _canonical_sha256(payload),
        "plan revision index digest mismatch",
    )
    _require(
        index.get("replay_id") == manifest.get("replay_id")
        and index.get("scenario_id") == manifest.get("scenario_id")
        and index.get("layer_count") == 4
        and index.get("routes_per_layer") == 3
        and index.get("route_count") == 12,
        "plan revision index identity or cardinality is invalid",
    )
    entries = index.get("entries")
    _require(isinstance(entries, list) and entries, "plan revision index entries are missing")
    validated: list[dict[str, Any]] = []
    documents: dict[int, dict[str, Any]] = {}
    for entry in entries:
        _require(isinstance(entry, dict), "plan revision index entry is invalid")
        resource = entry.get("resource")
        _require(isinstance(resource, str), "plan revision resource path is invalid")
        resource_path = Path(resource)
        _require(
            not resource_path.is_absolute(),
            "plan revision resource path must be relative",
        )
        plan_path = (root / resource_path).resolve()
        try:
            plan_path.relative_to(root)
        except ValueError as exc:
            raise ValueError("plan revision resource escapes replay directory") from exc
        _require(plan_path.is_file(), "plan revision resource is missing")
        plan_set = _read_json_object(plan_path, label="plan revision resource")
        _require(
            plan_set.get("schema_version") == "cd.four-layer-route-plan-set.v3",
            "plan revision resource is not a C v3 plan set",
        )
        _require(
            entry.get("digest") == _canonical_sha256(plan_set)
            and entry.get("layer_set_id") == plan_set.get("layer_set_id")
            and entry.get("layer_count") == 4
            and entry.get("route_count") == 12,
            "plan revision resource identity or cardinality is invalid",
        )
        revision = entry.get("plan_revision")
        _require(
            isinstance(revision, int) and revision > 0 and revision not in documents,
            "plan revision resource revision is invalid or duplicated",
        )
        validated.append(dict(entry))
        documents[revision] = plan_set
    return {
        "plan_revision_index": {
            "resource": relative_value,
            "content_digest": declared,
            "entry_count": len(validated),
        },
        "plan_revision_resources": validated,
        # Export assembly consumes these already-validated immutable documents
        # to restore route IDs and per-revision candidate/motion bindings.  The
        # private key is removed before source metadata enters the Viewer.
        "_plan_sets_by_revision": documents,
    }


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


def _bind_replay_revision_plan(
    adapter: PresentationAdapter,
    revision: int,
    plan_set: dict[str, Any],
) -> tuple[dict[str, Any], float]:
    """Bind one replay revision to its immutable C recommended plan."""

    plan = _winter_recommended_plan(plan_set)
    replay_route = adapter._routes_by_revision[revision]["route"]
    replay_points = [
        (point.get("longitude"), point.get("latitude"))
        for point in replay_route.get("waypoints", ())
    ]
    plan_points = [
        (point.get("longitude"), point.get("latitude"))
        for point in plan.get("waypoints", ())
    ]
    _require(
        replay_points == plan_points,
        f"replay revision {revision} geometry differs from its C plan resource",
    )
    replay_waypoints = replay_route.get("waypoints", ())
    plan_waypoints = plan.get("waypoints", ())
    offsets = [
        (_parse_utc(replay_point["eta"]) - _parse_utc(plan_point["eta"])).total_seconds()
        for replay_point, plan_point in zip(
            replay_waypoints,
            plan_waypoints,
            strict=True,
        )
    ]
    _require(
        offsets and max(offsets) - min(offsets) <= 1e-6,
        f"replay revision {revision} does not use one uniform execution-time offset",
    )
    _require(
        isinstance(plan.get("plan_id"), str),
        f"replay revision {revision} C plan ID is missing",
    )
    return plan, offsets[0]


def _revision_candidate_sets(
    plan_sets_by_revision: dict[int, dict[str, Any]],
    revision_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    states = {
        int(entry["plan_revision"]): entry.get("state")
        for entry in revision_entries
    }
    result = []
    for revision in sorted(plan_sets_by_revision):
        package = project_route_candidates(
            four_layer_route_plan_set_from_dict(plan_sets_by_revision[revision])
        )
        result.append(
            {
                "revision": revision,
                "state": states.get(revision),
                "route_candidates": package,
            }
        )
    return result


def _revision_runtime_candidate_sets(
    plan_sets_by_revision: dict[int, dict[str, Any]],
    revision_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project authoritative C waypoint/ETA candidates for every replay revision."""

    states = {
        int(entry["plan_revision"]): entry.get("state")
        for entry in revision_entries
    }
    result = []
    for revision in sorted(plan_sets_by_revision):
        package = project_runtime_route_candidates(
            four_layer_route_plan_set_from_dict(plan_sets_by_revision[revision])
        )
        result.append(
            {
                "revision": revision,
                "state": states.get(revision),
                "runtime_route_candidates": package,
            }
        )
    return result


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
        }
        if not arrived:
            entry["seg"] = {
                "index": edge_index,
                "start_eta": start["eta"],
                "end_eta": end["eta"],
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
    route_motion_candidate_set_ids: list[str] | None = None,
    runtime_route_candidate_set_ids: list[str] | None = None,
    timeline_source: str = "cd.route-plan.v3.waypoints.eta",
    source_replay_digest: str | None = None,
    risk_explanation_digest: str | None = None,
    simulation_start: str | None = None,
    simulation_end: str | None = None,
) -> tuple[str, str]:
    semantic_identity = {
        **identity,
        "simulation_start": simulation_start or route["waypoints"][0]["eta"],
        "simulation_end": simulation_end or route["waypoints"][-1]["eta"],
        "timeline_source": timeline_source,
        "timeline_cadence_seconds": cadence_seconds,
    }
    if route_smoothing_sidecar_digest is not None:
        semantic_identity["route_smoothing_sidecar_digest"] = route_smoothing_sidecar_digest
    if route_motion_set_ids:
        semantic_identity["route_motion_set_ids"] = route_motion_set_ids
    if route_motion_candidate_set_ids:
        semantic_identity["route_motion_candidate_set_ids"] = route_motion_candidate_set_ids
    if runtime_route_candidate_set_ids:
        semantic_identity["runtime_route_candidate_set_ids"] = runtime_route_candidate_set_ids
    if source_replay_digest is not None:
        semantic_identity["source_replay_digest"] = source_replay_digest
    if risk_explanation_digest is not None:
        semantic_identity["risk_explanation_digest"] = risk_explanation_digest
    digest = _canonical_sha256(semantic_identity)
    return f"winter-viewer-sha256-{digest}", digest


def _export_winter_combined(args: argparse.Namespace) -> int:
    default_viewer_dir = (_workspace_root() / "work_package_d" / "viewer").resolve()
    formal_motion_required = (
        getattr(args, "require_route_motion", False)
        or args.output_dir.resolve() == default_viewer_dir
    )
    if formal_motion_required and not args.route_motion_set:
        raise ValueError(
            "production Winter export requires --route-motion-set; "
            "use a separately generated formal cd.route-motion-set.v1 artifact"
        )

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
    authoritative_route = dict(route)
    route_motion_sets: list[dict[str, Any]] = []
    route_motion_candidate_sets: list[dict[str, Any]] = []
    motion_context_by_layer_set = {
        identity["layer_set_id"]: {
            "risk_window_id": identity["risk_window_id"],
            "risk_window_digest": identity["risk_window_digest"],
        }
    }
    route_candidate_sets = [
        {
            "revision": 1,
            "state": "current",
            "route_candidates": route_candidates,
        }
    ]
    plan_sets_by_revision: dict[int, dict[str, Any]] = {1: plan_set}
    runtime_route_candidate_sets = _revision_runtime_candidate_sets(
        plan_sets_by_revision, []
    )
    route_smoothing_sidecar = None
    if args.route_smoothing_sidecar is not None:
        route_smoothing_sidecar = _load_route_smoothing_sidecar(
            args.route_smoothing_sidecar,
            route=route,
        )
    replay_adapter = None
    source_replay = None
    if args.winter_replay_manifest is not None:
        replay_adapter, source_replay, _ = _load_causal_replay_source(
            args.winter_replay_manifest,
            snapshots_dir=args.winter_replay_snapshots_dir,
            expected_identity={
                "run_id": identity["run_id"],
                "scenario_id": identity["scenario_id"],
                "dataset_bundle_id": identity["dataset_bundle_id"],
                "dataset_bundle_digest": identity["dataset_bundle_digest"],
                "risk_window_id": identity["risk_window_id"],
                "risk_window_digest": identity["risk_window_digest"],
                "layer_set_id": identity["layer_set_id"],
            },
            expected_route=route,
            allow_retrospective=True,
        )
        final_state = replay_adapter.state_at(replay_adapter.replay_end)
        _require(
            final_state.vessel.status == "ARRIVED"
            and final_state.plan.pending_candidate is None,
            "published dynamic replay must run through ARRIVED with no pending "
            "adoption; ETA-only timeline extension is prohibited",
        )
        timeline = _timeline(replay_adapter, args.cadence_seconds)
        plan_sets_by_revision = source_replay.pop("_plan_sets_by_revision", {})
        replay_revisions = sorted(replay_adapter._routes_by_revision)
        _require(
            set(replay_revisions).issubset(plan_sets_by_revision),
            "accepted replay revisions are missing immutable C plan resources",
        )
        replay_routes = []
        for revision in replay_revisions:
            bound_plan, motion_time_offset_seconds = _bind_replay_revision_plan(
                replay_adapter,
                revision,
                plan_sets_by_revision[revision],
            )
            replay_routes.append(
                _route_meta(
                    replay_adapter,
                    revision,
                    plan_override=bound_plan,
                    motion_time_offset_seconds=motion_time_offset_seconds,
                )
            )
        route_candidate_sets = _revision_candidate_sets(
            plan_sets_by_revision,
            source_replay.get("plan_revision_resources", []),
        )
        runtime_route_candidate_sets = _revision_runtime_candidate_sets(
            plan_sets_by_revision,
            source_replay.get("plan_revision_resources", []),
        )
        snapshots_by_time = {
            snapshot["simulation_time"]: snapshot
            for snapshot in replay_adapter.snapshots
        }
        for entry in source_replay.get("plan_revision_resources", []):
            snapshot = snapshots_by_time.get(entry.get("simulation_time"))
            _require(
                snapshot is not None,
                "plan revision has no same-time replay RiskWindow binding",
            )
            risk_state = snapshot.get("risk") or {}
            risk_window_id = risk_state.get("resource_identity")
            risk_window_digest = risk_state.get("resource_digest")
            _require(
                isinstance(risk_window_id, str)
                and isinstance(risk_window_digest, str),
                "plan revision replay snapshot lacks RiskWindow identity",
            )
            motion_context_by_layer_set[entry["layer_set_id"]] = {
                "risk_window_id": risk_window_id,
                "risk_window_digest": risk_window_digest,
            }
        _require(
            route_candidate_sets[0]["route_candidates"] == route_candidates,
            "initial revision candidate projection differs from authoritative package",
        )
        replay_events = [
            {
                "t": event["simulation_time"],
                "type": event["type"],
                "rev": event.get("revision"),
                "observed": bool(event.get("observed", False)),
                "description": event.get("description", ""),
            }
            for event in replay_adapter._events
        ]
        replay_initial = replay_routes[0]
        replay_metrics = {
            key: value
            for key, value in replay_initial.get("metrics", {}).items()
            if value is not None
        }
        route = {
            **authoritative_route,
            **replay_initial,
            "metrics": {
                **authoritative_route.get("metrics", {}),
                **replay_metrics,
            },
        }
        replay_routes[0] = route
        # Revisions and pending/superseded state come verbatim from the replay.
        # Publication never extends beyond the last producer snapshot.
    else:
        timeline = _winter_vessel_timeline(recommended, cadence_seconds=args.cadence_seconds)
        replay_routes = [route]
        # A waypoint projection is not a causal replay.  Do not manufacture a
        # PLAN_COMPUTED (or any replan) event merely to populate the timeline;
        # the Viewer will display an explicit unavailable status until a real,
        # identity-bound replay is supplied.
        replay_events = []

    # Runtime candidates are derived directly from each immutable C plan set;
    # unlike the compact v1 projection they carry the authoritative waypoint
    # ETA and are therefore safe to use for an explicit pre-run choice.
    runtime_by_layer_set = {
        item["runtime_route_candidates"]["layer_set_id"]: item
        for item in runtime_route_candidate_sets
    }
    _require(
        len(runtime_by_layer_set) == len(runtime_route_candidate_sets),
        "runtime route candidate layer bindings are duplicated",
    )

    layer_sets = {
        document.get("layer_set_id"): document
        for document in plan_sets_by_revision.values()
    }
    for motion_path in args.route_motion_set or ():
        motion_document = _read_json_object(motion_path, label="route motion set")
        motion_layer_set_id = motion_document.get("layer_set_id")
        bound_plan_set = layer_sets.get(motion_layer_set_id)
        _require(
            bound_plan_set is not None,
            f"route motion set {motion_path} has no matching replay plan revision",
        )
        motion_set = load_bound_route_motion_set(
            motion_path,
            plan_set_document=bound_plan_set,
            replay_routes=replay_routes,
        )
        motion_context = motion_context_by_layer_set.get(motion_layer_set_id)
        _require(
            motion_context is not None,
            f"route motion set {motion_path} has no replay RiskWindow binding",
        )
        validate_route_motion_context(
            motion_set,
            risk_window_id=motion_context["risk_window_id"],
            risk_window_digest=motion_context["risk_window_digest"],
            vessel_profile_id=run_context["vessel_profile_id"],
            vessel_profile_version=run_context["vessel_profile_version"],
            vessel_profile_digest=run_context["vessel_profile_digest"],
        )
        _require(
            all(
                existing["motion_set_id"] != motion_set["motion_set_id"]
                and existing["layer_set_id"] != motion_set["layer_set_id"]
                for existing in route_motion_sets
            ),
            "route motion set ID or layer binding is duplicated",
        )
        route_motion_sets.append(motion_set)

    for motion_path in getattr(args, "route_motion_candidate_set", ()) or ():
        motion_document = _read_json_object(
            motion_path, label="route motion candidate set"
        )
        motion_layer_set_id = motion_document.get("layer_set_id")
        runtime_entry = runtime_by_layer_set.get(motion_layer_set_id)
        bound_plan_set = layer_sets.get(motion_layer_set_id)
        _require(
            bound_plan_set is not None and runtime_entry is not None,
            f"route motion candidate set {motion_path} has no matching C revision",
        )
        motion_set = load_bound_route_motion_candidate_set(
            motion_path,
            plan_set_document=bound_plan_set,
            runtime_candidates_document=runtime_entry["runtime_route_candidates"],
        )
        motion_context = motion_context_by_layer_set.get(motion_layer_set_id)
        _require(
            motion_context is not None,
            f"route motion candidate set {motion_path} has no RiskWindow binding",
        )
        validate_route_motion_context(
            motion_set,
            risk_window_id=motion_context["risk_window_id"],
            risk_window_digest=motion_context["risk_window_digest"],
            vessel_profile_id=run_context["vessel_profile_id"],
            vessel_profile_version=run_context["vessel_profile_version"],
            vessel_profile_digest=run_context["vessel_profile_digest"],
        )
        _require(
            all(
                existing["motion_candidate_set_id"] != motion_set["motion_candidate_set_id"]
                and existing["layer_set_id"] != motion_set["layer_set_id"]
                for existing in route_motion_candidate_sets
            ),
            "route motion candidate set ID or layer binding is duplicated",
        )
        route_motion_candidate_sets.append(motion_set)
    if formal_motion_required:
        covered_plan_ids = {
            record["plan_id"]
            for motion_set in route_motion_sets
            for record in motion_set.get("records", [])
            if record.get("planning_layer") == "full_voyage"
        }
        missing_route_ids = [
            item["route_id"]
            for item in replay_routes
            if item.get("effective_adoption_time") is not None
            and item.get("route_id") not in covered_plan_ids
        ]
        _require(
            not missing_route_ids,
            "formal motion does not cover adopted replay routes: "
            + ", ".join(missing_route_ids),
        )
    risk_explanation_manifest = None
    risk_explanation = None
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
    if args.risk_explanation_manifest is not None:
        risk_explanation_manifest, risk_explanation = _load_risk_explanation_manifest(
            args.risk_explanation_manifest,
            expected_identity={
                "risk_window_id": identity["risk_window_id"],
                "run_id": identity["run_id"],
                "scenario_id": identity["scenario_id"],
                "corridor_id": identity["corridor_id"],
                "vessel_profile_id": identity["vessel_profile_id"],
            },
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
        route_motion_set_ids=[item["motion_set_id"] for item in route_motion_sets],
        route_motion_candidate_set_ids=[
            item["motion_candidate_set_id"] for item in route_motion_candidate_sets
        ],
        runtime_route_candidate_set_ids=[
            item["runtime_route_candidates"]["runtime_candidate_set_id"]
            for item in runtime_route_candidate_sets
        ],
        timeline_source=(
            (
                "orchestrator.presentation_adapter.retrospective_dynamic_replay"
                if source_replay is not None
                and source_replay.get("scenario_mode")
                == "retrospective_dynamic_replay"
                else "orchestrator.presentation_adapter.causal_replay"
            )
            if replay_adapter is not None
            else "cd.route-plan.v3.waypoints.eta"
        ),
        source_replay_digest=(source_replay or {}).get("manifest_sha256"),
        risk_explanation_digest=(
            risk_explanation_manifest or {}
        ).get("artifact_sha256"),
        simulation_start=timeline[0]["t"],
        simulation_end=timeline[-1]["t"],
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
    for index, motion_path in enumerate(args.route_motion_set or (), start=1):
        source_files[f"route_motion_set_{index}"] = {
            "path": str(motion_path),
            "sha256": _sha256_file(motion_path),
        }
    for index, motion_path in enumerate(
        getattr(args, "route_motion_candidate_set", ()) or (), start=1
    ):
        source_files[f"route_motion_candidate_set_{index}"] = {
            "path": str(motion_path),
            "sha256": _sha256_file(motion_path),
        }
    if args.winter_replay_manifest is not None:
        source_files["causal_replay_manifest"] = {
            "path": str(args.winter_replay_manifest),
            "sha256": _sha256_file(args.winter_replay_manifest),
        }
    if args.risk_explanation_manifest is not None:
        source_files["risk_explanation_manifest"] = {
            "path": str(args.risk_explanation_manifest),
            "sha256": _sha256_file(args.risk_explanation_manifest),
        }
        artifact_path = (
            args.risk_explanation_manifest.parent
            / risk_explanation_manifest["artifact_path"]
        )
        source_files["risk_explanation_artifact"] = {
            "path": str(artifact_path),
            "sha256": risk_explanation_manifest["artifact_sha256"],
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
        "runtime_candidate_schema": "presentation.runtime-route-candidates.v1",
    }
    if route_smoothing_sidecar is not None:
        research_validation["route_smoothing"] = route_smoothing_sidecar
    bundle = {
        "schema_version": "replay.viewer-bundle.v1",
        "formal_motion_inspection": {
            "valid": bool(route_motion_sets),
            "source": "orchestrator_binding_preflight",
            "schema_version": "cd.route-motion-set.v1" if route_motion_sets else None,
            "motion_set_ids": [item["motion_set_id"] for item in route_motion_sets],
            "set_count": len(route_motion_sets),
            "record_count": sum(
                len(item.get("records", [])) for item in route_motion_sets
            ),
            "record_layers": [
                layer
                for layer in (
                    "full_voyage",
                    "main_corridor_24_72h",
                    "rolling_0_24h",
                    "executable_0_6h",
                )
                if any(
                    record.get("planning_layer") == layer
                    for item in route_motion_sets
                    for record in item.get("records", [])
                )
            ],
            "covered_layer_set_ids": [
                item["layer_set_id"] for item in route_motion_sets
            ],
            "reason": None if route_motion_sets else "missing_formal_motion_set",
        },
        "replay": {
            "replay_id": assembly_id,
            "scenario_id": identity["scenario_id"],
            "scenario_mode": "research_navigation_simulation",
            "identity_kind": "combined_presentation_assembly",
            "start": timeline[0]["t"],
            "end": timeline[-1]["t"],
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
            "timeline_source": (
                (
                    "orchestrator.presentation_adapter.retrospective_dynamic_replay"
                    if source_replay is not None
                    and source_replay.get("scenario_mode")
                    == "retrospective_dynamic_replay"
                    else "orchestrator.presentation_adapter.causal_replay"
                )
                if replay_adapter is not None
                else "cd.route-plan.v3.waypoints.eta"
            ),
            "replanning_status": (
                (
                    "PUBLISHED_RETROSPECTIVE_DYNAMIC_REPLAY"
                    if source_replay is not None
                    and source_replay.get("scenario_mode")
                    == "retrospective_dynamic_replay"
                    else "PUBLISHED_CAUSAL_REPLAY"
                )
                if replay_adapter is not None
                else "UNAVAILABLE_IDENTITY_BOUND_CAUSAL_REPLAY_REQUIRED"
            ),
            "source_replay": source_replay,
            "plan_revision_index": (
                (source_replay or {}).get("plan_revision_index")
            ),
            "plan_revision_resources": (
                (source_replay or {}).get("plan_revision_resources", [])
            ),
            "risk_explanation_manifest": (
                {
                    "artifact_id": risk_explanation_manifest["artifact_id"],
                    "artifact_sha256": risk_explanation_manifest["artifact_sha256"],
                    "manifest_path": str(args.risk_explanation_manifest),
                }
                if risk_explanation_manifest is not None
                else None
            ),
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
        "routes": replay_routes,
        "route_candidates": route_candidates,
        "route_candidate_sets": route_candidate_sets,
        "runtime_route_candidate_sets": runtime_route_candidate_sets,
        "events": replay_events,
        "timeline": timeline,
        "risk": risk,
        "acceptance_positions": _winter_acceptance_positions(timeline),
    }
    if route_motion_sets:
        bundle["combined_presentation"]["route_motion_set_ids"] = [
            item["motion_set_id"] for item in route_motion_sets
        ]
        bundle["combined_presentation"]["route_motion_set_bindings"] = [
            {
                "motion_set_id": item["motion_set_id"],
                "layer_set_id": item["layer_set_id"],
                "risk_window_id": item["risk_window_id"],
                "risk_window_digest": item["risk_window_digest"],
            }
            for item in route_motion_sets
        ]
        bundle["route_motion_sets"] = route_motion_sets
    if route_motion_candidate_sets:
        bundle["combined_presentation"]["route_motion_candidate_set_ids"] = [
            item["motion_candidate_set_id"] for item in route_motion_candidate_sets
        ]
        bundle["combined_presentation"]["route_motion_candidate_set_bindings"] = [
            {
                "motion_candidate_set_id": item["motion_candidate_set_id"],
                "layer_set_id": item["layer_set_id"],
                "risk_window_id": item["risk_window_id"],
                "risk_window_digest": item["risk_window_digest"],
            }
            for item in route_motion_candidate_sets
        ]
        bundle["route_motion_candidate_sets"] = route_motion_candidate_sets
    bundle["combined_presentation"]["runtime_route_candidate_set_bindings"] = [
        {
            "revision": item["revision"],
            "state": item.get("state"),
            "layer_set_id": item["runtime_route_candidates"]["layer_set_id"],
            "runtime_candidate_set_id": item["runtime_route_candidates"][
                "runtime_candidate_set_id"
            ],
            "selected_candidate_id": item["runtime_route_candidates"][
                "selected_candidate_id"
            ],
        }
        for item in runtime_route_candidate_sets
    ]
    bundle["combined_presentation"]["runtime_route_candidate_set_ids"] = [
        item["runtime_route_candidates"]["runtime_candidate_set_id"]
        for item in runtime_route_candidate_sets
    ]
    bundle["combined_presentation"]["route_candidate_set_bindings"] = [
        {
            "revision": item["revision"],
            "state": item.get("state"),
            "layer_set_id": item["route_candidates"]["layer_set_id"],
            "candidate_set_id": item["route_candidates"]["candidate_set_id"],
            "selected_candidate_id": item["route_candidates"][
                "selected_candidate_id"
            ],
        }
        for item in route_candidate_sets
    ]
    if risk_explanation is not None:
        bundle["risk_explanation"] = risk_explanation
        bundle["risk_explanation_transport"] = {
            "schema_version": "risk-explanation-transport.v1",
            "status": "PUBLISHED",
            "manifest_path": str(args.risk_explanation_manifest),
            "manifest_sha256": _sha256_file(args.risk_explanation_manifest),
            "artifact_id": risk_explanation_manifest["artifact_id"],
            "artifact_sha256": risk_explanation_manifest["artifact_sha256"],
        }
    bundle["combined_presentation"]["formal_motion_policy"] = {
        "required_for_production_default": True,
        "runtime_failure_fallback": "RAW_WAYPOINT_TIMELINE",
        "provided": bool(route_motion_sets),
        "runtime_candidate_motion_sets": len(route_motion_candidate_sets),
    }
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
    plan_set_transport_path = output_dir / "four-layer-route-plan-set-v3.json"
    plan_set_transport_path.write_text(
        json.dumps(plan_set, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    motion_set_transport_paths: list[Path] = []
    for index, route_motion_set in enumerate(route_motion_sets, start=1):
        motion_set_transport_path = output_dir / f"route-motion-set-r{index}.json"
        motion_set_transport_path.write_text(
            json.dumps(route_motion_set, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        motion_set_transport_paths.append(motion_set_transport_path)
    motion_candidate_set_transport_paths: list[Path] = []
    for index, motion_candidate_set in enumerate(route_motion_candidate_sets, start=1):
        motion_candidate_set_transport_path = (
            output_dir / f"route-motion-candidate-set-r{index}.json"
        )
        motion_candidate_set_transport_path.write_text(
            json.dumps(
                motion_candidate_set, ensure_ascii=False, indent=2, sort_keys=True
            ),
            encoding="utf-8",
        )
        motion_candidate_set_transport_paths.append(motion_candidate_set_transport_path)
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
                    "replay_timeline_boundary": "PASS",
                    "formal_route_motion": (
                        "PASS"
                        if route_motion_sets
                        else "NOT_PROVIDED_OPTIONAL_LEGACY_EXPORT"
                    ),
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
        "route_candidate_sets": len(route_candidate_sets),
        "route_motion_sets": len(route_motion_sets),
        "runtime_route_candidate_sets": len(runtime_route_candidate_sets),
        "route_motion_candidate_sets": len(route_motion_candidate_sets),
        "formal_motion_required": formal_motion_required,
        "transport_files": {
            "plan_set": plan_set_transport_path.name,
            "route_motion_sets": [path.name for path in motion_set_transport_paths],
            "route_motion_candidate_sets": [
                path.name for path in motion_candidate_set_transport_paths
            ],
            "causal_replay_manifest": (
                str(args.winter_replay_manifest)
                if args.winter_replay_manifest is not None
                else None
            ),
            "risk_explanation_manifest": (
                str(args.risk_explanation_manifest)
                if args.risk_explanation_manifest is not None
                else None
            ),
        },
    }
    (output_dir / "winter-combined-viewer-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    checksum_names = [
        "bundle.json",
        "four-layer-route-plan-set-v3.json",
        "basemap_metadata.json",
        "gebco_basemap.png",
        "replay-viewer-preflight.json",
        "winter-combined-viewer-manifest.json",
    ]
    checksum_names[2:2] = [path.name for path in motion_set_transport_paths]
    checksum_names[2:2] = [
        path.name for path in motion_candidate_set_transport_paths
    ]
    checksums = {
        "schema_version": "presentation.winter-combined-checksums.v1",
        "files": {
            name: _sha256_file(output_dir / name) for name in checksum_names
        },
    }
    (output_dir / "checksums.json").write_text(
        json.dumps(checksums, ensure_ascii=False, indent=2, sort_keys=True),
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
        action="append",
        default=[],
        help=(
            "formally bound cd.route-motion-set.v1 artifact; repeat once for "
            "each adopted replay revision"
        ),
    )
    parser.add_argument(
        "--route-motion-candidate-set",
        type=Path,
        action="append",
        default=[],
        help=(
            "formally bound cd.route-motion-candidate-set.v1 artifact; repeat once "
            "for each revision with runnable full-voyage candidates"
        ),
    )
    parser.add_argument(
        "--winter-replay-manifest",
        type=Path,
        default=None,
        help=(
            "identity-bound causal replay manifest for Winter publication; "
            "events/routes are consumed verbatim when present"
        ),
    )
    parser.add_argument(
        "--winter-replay-snapshots-dir",
        type=Path,
        default=None,
        help="optional snapshots directory for --winter-replay-manifest",
    )
    parser.add_argument(
        "--risk-explanation-manifest",
        type=Path,
        default=None,
        help="B immutable risk-explanation-manifest.v1 transport artifact",
    )
    parser.add_argument(
        "--require-route-motion",
        action="store_true",
        help=(
            "require a valid formal motion set for the production/default Winter export; "
            "runtime still fails closed to the raw timeline"
        ),
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
    if args.winter_replay_manifest is not None or args.winter_replay_snapshots_dir is not None:
        parser.error("--winter-replay-manifest requires --winter-plan-set")
    if args.route_motion_set:
        parser.error("--route-motion-set currently requires --winter-plan-set binding")
    if args.route_motion_candidate_set:
        parser.error(
            "--route-motion-candidate-set currently requires --winter-plan-set binding"
        )
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
    risk_explanation_manifest = None
    risk_explanation = None
    if args.risk_explanation_manifest is not None:
        risk_explanation_manifest, risk_explanation = _load_risk_explanation_manifest(
            args.risk_explanation_manifest,
            expected_identity={
                "risk_window_id": risk.get("source", {}).get("risk_window_id"),
                "run_id": risk.get("source", {}).get("run_id"),
                "scenario_id": manifest_doc.get("scenario_id"),
                "corridor_id": risk.get("source", {}).get("corridor_id"),
                "vessel_profile_id": risk.get("source", {}).get("vessel_profile_id"),
            },
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
    if risk_explanation is not None:
        bundle["risk_explanation"] = risk_explanation
        bundle["risk_explanation_transport"] = {
            "schema_version": "risk-explanation-transport.v1",
            "status": "PUBLISHED",
            "manifest_path": str(args.risk_explanation_manifest),
            "manifest_sha256": _sha256_file(args.risk_explanation_manifest),
            "artifact_id": risk_explanation_manifest["artifact_id"],
            "artifact_sha256": risk_explanation_manifest["artifact_sha256"],
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
