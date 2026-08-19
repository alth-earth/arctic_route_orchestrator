"""Render a viewer-like proof PNG from the authoritative bundle + GEBCO mask.

Uses exactly the same EPSG:4326 canonical transform that the browser applies,
so the proof image demonstrates basemap / route / track / vessel alignment
without needing a running browser.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_basemap import _render, _write_png

from arctic_route_orchestrator.replay.geospatial import (
    find_land_sea_mask,
    load_netcdf_land_mask,
)


def _draw_disc(image: np.ndarray, x: int, y: int, radius: int, color: tuple) -> None:
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy <= radius * radius:
                px = int(x) + dx
                py = int(y) + dy
                if 0 <= px < image.shape[1] and 0 <= py < image.shape[0]:
                    image[py, px] = color


def _draw_segment(
    image: np.ndarray,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    color: tuple,
    width: int,
    *,
    dash: int | None = None,
) -> None:
    steps = max(int(abs(x1 - x0)), int(abs(y1 - y0))) + 1
    for index in range(steps + 1):
        fraction = index / max(1, steps)
        px = x0 + (x1 - x0) * fraction
        py = y0 + (y1 - y0) * fraction
        if dash is not None and (index // dash) % 2 == 1:
            continue
        _draw_disc(image, px, py, width, color)


def _project(basemap: dict, lon: float, lat: float) -> tuple[float, float]:
    box = basemap["bbox"]
    width = basemap["width"]
    height = basemap["height"]
    x = (lon - box["min_lon"]) / (box["max_lon"] - box["min_lon"]) * width
    y = (box["max_lat"] - lat) / (box["max_lat"] - box["min_lat"]) * height
    return x, y


def _polylines_for(
    bundle: dict,
    basemap: dict,
    target_time: str,
) -> tuple[
    list[tuple[float, float]],
    list[tuple[float, float]],
    list[tuple[float, float]],
    tuple[float, float],
]:
    timeline = bundle["timeline"]
    entry = timeline[0]
    previous = None
    for item in timeline:
        if item["t"] <= target_time:
            entry = item
            previous = item
        else:
            break
    if previous is None:
        previous = entry
    active = next(
        route for route in bundle["routes"] if route["revision"] == entry["arv"]
    )
    active_pts = [
        _project(basemap, w["lon"], w["lat"])
        for w in active["waypoints"]
        if w["eta"] >= target_time
    ]
    track_pts = [
        _project(basemap, p["longitude"], p["latitude"])
        for p in entry.get("track", [])
    ]
    vessel = previous["v"]
    vessel_pt = _project(basemap, vessel["lon"], vessel["lat"])
    pending_pts: list[tuple[float, float]] = []
    pending = entry.get("pending")
    if pending and pending["revision"] != entry["arv"]:
        pending_pts = [
            _project(basemap, w["lon"], w["lat"]) for w in pending["route"]
        ]
    return active_pts, track_pts, pending_pts, vessel_pt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="viewer-render-proof")
    parser.add_argument(
        "--bundle", type=Path, default=Path(__file__).parent / "bundle.json"
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("/root/my_project/work_package_a/data"),
    )
    parser.add_argument("--route-id", default="tromso_to_isfjorden_outer")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--time", default="2026-08-15T10:30:00Z")
    args = parser.parse_args(argv)

    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    basemap = bundle["basemap"]
    mask_path = find_land_sea_mask(args.route_id, args.data_root)
    if mask_path is None:
        sys.exit("no local GEBCO land_sea_mask found")
    mask = load_netcdf_land_mask(mask_path)
    lon_step, lat_step = mask.cell_step_degrees()
    image = _render(
        land=mask.land,
        lon0=mask.longitude[0],
        lat0=mask.latitude[0],
        lon_step=lon_step,
        lat_step=lat_step,
        width=basemap["width"],
        height=basemap["height"],
    )
    rgb = np.frombuffer(image, dtype=np.uint8).reshape(
        basemap["height"], basemap["width"], 3
    ).copy()
    active_pts, track_pts, pending_pts, vessel_pt = _polylines_for(
        bundle, basemap, args.time
    )
    if track_pts:
        for index in range(len(track_pts) - 1):
            _draw_segment(
                rgb,
                track_pts[index][0],
                track_pts[index][1],
                track_pts[index + 1][0],
                track_pts[index + 1][1],
                (92, 196, 122),
                2,
            )
    if pending_pts:
        for index in range(len(pending_pts) - 1):
            _draw_segment(
                rgb,
                pending_pts[index][0],
                pending_pts[index][1],
                pending_pts[index + 1][0],
                pending_pts[index + 1][1],
                (242, 177, 52),
                2,
                dash=10,
            )
    if active_pts:
        for index in range(len(active_pts) - 1):
            _draw_segment(
                rgb,
                active_pts[index][0],
                active_pts[index][1],
                active_pts[index + 1][0],
                active_pts[index + 1][1],
                (61, 155, 233),
                2,
            )
    _draw_disc(rgb, vessel_pt[0], vessel_pt[1], 8, (255, 255, 255))
    _draw_disc(rgb, vessel_pt[0], vessel_pt[1], 3, (15, 43, 59))
    output = args.output or Path(__file__).parent / "replay-viewer-proof.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_png(output, basemap["width"], basemap["height"], bytes(rgb.tobytes()))
    print("wrote", output, "time", args.time)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
