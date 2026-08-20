"""Export a stable Replay-driven Viewer presentation package.

This is the orchestrator-owned artifact boundary: it consumes the causal replay
manifest + Presentation Adapter and produces the basemap PNG, basemap metadata,
bundle JSON and presentation preflight that work_package_d renders.  The Viewer
application in work_package_d never imports orchestrator internals.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from arctic_route_orchestrator.replay.geospatial import (
    BasemapMetadata,
    find_land_sea_mask,
    load_netcdf_land_mask,
)
from arctic_route_orchestrator.replay.preflight import run_viewer_preflight
from arctic_route_orchestrator.replay.presentation import PresentationAdapter

SEA_RGB = (46, 102, 150)
LAND_RGB = (108, 132, 98)
OUT_RGB = (36, 44, 56)
RISK_HORIZONS: tuple[tuple[str, int], ...] = (
    ("current", 0),
    ("+6h", 6),
    ("+12h", 12),
    ("+24h", 24),
)


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
        "waypoints": [
            {
                "lon": item["longitude"],
                "lat": item["latitude"],
                "eta": item["eta"],
            }
            for item in route["waypoints"]
        ],
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
        "hard_reasons": hard_reasons,
        "frames": frames,
        "horizon_selections": _risk_horizon_selections(frames, simulation_times or []),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay-viewer-export")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--snapshots-dir", type=Path, default=None)
    parser.add_argument("--risk-store-root", type=Path, default=None)
    data_default = Path("/root/my_project/work_package_a/data")
    parser.add_argument("--data-root", type=Path, default=data_default)
    parser.add_argument("--route-id", default="tromso_to_isfjorden_outer")
    parser.add_argument("--land-mask", type=Path, default=None)
    parser.add_argument("--cadence-seconds", type=int, default=60)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--sample-step-km", type=float, default=5.0)
    parser.add_argument("--basemap-version", default="gebco-2026-d5a7e2fe3915-7baad866")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/root/my_project/work_package_d/viewer"),
    )
    args = parser.parse_args(argv)

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
        "gates": gates,
        "routes": [
            _route_meta(adapter, revision)
            for revision in sorted(adapter._routes_by_revision)
        ],
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
