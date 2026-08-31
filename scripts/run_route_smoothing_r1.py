#!/usr/bin/env python3
"""Run the bounded R1 multi-span route-smoothing shadow experiment.

The script consumes existing Winter replay, RiskFrame and A raster artifacts.
It writes only research artifacts, never changes the formal route, and never
turns synthetic manoeuvring assumptions into production qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import statistics
import subprocess
import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import xarray as xr
from arctic_route_planning.contracts import risk_frame_from_document
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.research.route_smoothing import EARTH_RADIUS_M
from arctic_route_planning.research.route_smoothing_multispan import (
    evaluate_clamped_cubic_bspline,
)
from arctic_route_planning.research.route_smoothing_qualification import (
    _finish_digest,
    _integrate_path,
    _route_records,
)
from arctic_route_planning.research.route_smoothing_qualification_v2 import (
    build_qualified_route_smoothing_sidecar_v2,
)
from arctic_route_planning.research.route_smoothing_v2 import POLICY
from arctic_route_planning.risk import RiskSampler

from arctic_route_orchestrator.replay.raster_corridor_evidence import (
    evaluate_raster_corridor_evidence,
)

EXPERIMENT_SCHEMA = "c.r1-route-smoothing-shadow.v1"
CONCLUSIONS = {
    "SYNTHETIC_VESSEL_AND_REAL_ENVIRONMENT_SHADOW_PASS",
    "SHADOW_PASS_VISUAL_GAIN_INSUFFICIENT",
    "DISPLAY_ONLY_RETAINED",
    "FALLBACK_RAW_ROUTE",
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git_snapshot(repository: Path) -> dict[str, Any]:
    def run(*arguments: str) -> str | None:
        completed = subprocess.run(
            arguments,
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None

    status = run("git", "status", "--porcelain")
    return {
        "repository": str(repository),
        "head": run("git", "rev-parse", "HEAD"),
        "tree": run("git", "rev-parse", "HEAD^{tree}"),
        "working_tree_clean": status == "",
    }


def _load_risk_sampler(risk_store: Path) -> tuple[RiskSampler, Path, dict[str, Any]]:
    commits = sorted((risk_store / "commits").glob("*.json"))
    if len(commits) != 1:
        raise ValueError("R1 requires exactly one explicit committed RiskWindow")
    commit = _read_json(commits[0])
    frames = []
    for record in commit.get("frames", []):
        risk_id = record.get("risk_id") if isinstance(record, dict) else None
        if not isinstance(risk_id, str):
            raise ValueError("RiskWindow contains an invalid frame identity")
        frame_path = risk_store / "frames" / f"{risk_id}.json"
        frames.append(risk_frame_from_document(_read_json(frame_path)))
    if len(frames) != commit.get("count"):
        raise ValueError("RiskWindow frame count differs from commit")
    return RiskSampler(tuple(frames)), commits[0], commit


def _local_raster(
    mask_path: Path, *, lon0: float, lat0: float
) -> tuple[dict[str, Any], dict[tuple[int, int], str], dict[str, Any]]:
    with xr.open_dataset(mask_path) as dataset:
        latitudes = dataset["latitude"].values.astype(float)
        longitudes = dataset["longitude"].values.astype(float)
        values = dataset["land_sea_mask"].isel(time=0).values
        attributes = dict(dataset["land_sea_mask"].attrs)
    if len(latitudes) < 2 or len(longitudes) < 2:
        raise ValueError("land/sea raster is too small")
    delta_lat = float(latitudes[1] - latitudes[0])
    delta_lon = float(longitudes[1] - longitudes[0])
    cos_lat0 = math.cos(math.radians(lat0))
    cell_width_m = EARTH_RADIUS_M * math.radians(delta_lon) * cos_lat0
    cell_height_m = EARTH_RADIUS_M * math.radians(delta_lat)
    origin_x_m = (
        EARTH_RADIUS_M
        * math.radians(float(longitudes[0]) - lon0)
        * cos_lat0
        - cell_width_m / 2.0
    )
    origin_y_m = (
        EARTH_RADIUS_M * math.radians(float(latitudes[0]) - lat0) - cell_height_m / 2.0
    )
    metadata = {
        "coordinate_frame": "c_local_equirectangular_east_north_m",
        "origin_x_m": origin_x_m,
        "origin_y_m": origin_y_m,
        "cell_width_m": cell_width_m,
        "cell_height_m": cell_height_m,
        "rows": len(latitudes),
        "cols": len(longitudes),
        "coverage_complete": True,
    }
    cells = {
        (row, column): "SEA" if float(values[row, column]) == 1.0 else "LAND"
        for row in range(len(latitudes))
        for column in range(len(longitudes))
    }
    source = {
        "path": str(mask_path),
        "sha256": _sha256_file(mask_path),
        "variable_attributes": attributes,
        "interpretation": "1=SEA; 0=LAND_OR_COAST",
        "scope": "RASTER_RESOLUTION_CONTAINMENT_ONLY",
        "navigation_semantics": attributes.get("navigation_semantics"),
        "hard_mask_semantics": attributes.get("hard_mask_semantics"),
    }
    return metadata, cells, source


def _cgroup_evidence() -> dict[str, Any]:
    cgroup_relative = "/"
    for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        if line.startswith("0::"):
            cgroup_relative = line[3:]
            break
    root = Path("/sys/fs/cgroup") / cgroup_relative.lstrip("/")

    def read(name: str) -> str | None:
        path = root / name
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    memory_max = read("memory.max")
    swap_max = read("memory.swap.max")
    pids_max = read("pids.max")
    enforced = memory_max == str(2 * 1024**3) and swap_max == "0" and pids_max == "256"
    return {
        "cgroup_limits_enforced": enforced,
        "complete": False,
        "cgroup_path": str(root),
        "memory.max": memory_max,
        "memory.swap.max": swap_max,
        "pids.max": pids_max,
        "memory.events.before": read("memory.events"),
        "memory.swap.current.before": read("memory.swap.current"),
    }


def _pixel(point: tuple[float, float], basemap: dict[str, Any]) -> tuple[float, float]:
    bbox = basemap["bbox"]
    x = (point[0] - bbox["min_lon"]) / (bbox["max_lon"] - bbox["min_lon"])
    y = (bbox["max_lat"] - point[1]) / (bbox["max_lat"] - bbox["min_lat"])
    return x * basemap["width"], y * basemap["height"]


def _point_segment_distance(
    point: tuple[float, float], first: tuple[float, float], second: tuple[float, float]
) -> float:
    delta = second[0] - first[0], second[1] - first[1]
    length_squared = delta[0] ** 2 + delta[1] ** 2
    if length_squared == 0.0:
        return math.dist(point, first)
    ratio = max(
        0.0,
        min(
            1.0,
            ((point[0] - first[0]) * delta[0] + (point[1] - first[1]) * delta[1])
            / length_squared,
        ),
    )
    projected = first[0] + ratio * delta[0], first[1] + ratio * delta[1]
    return math.dist(point, projected)


def _visual_evidence(sidecar: dict[str, Any], basemap: dict[str, Any]) -> dict[str, Any]:
    raw = [_pixel(tuple(point), basemap) for point in sidecar["geometry"]["raw_points"]]
    deviations = []
    maximum_screen_chord_error = 0.0
    maximum_arc_chord_deficit_m = 0.0
    lon0, lat0 = sidecar["geometry"]["raw_points"][0]
    cos_lat0 = math.cos(math.radians(lat0))

    def local_to_geo(point: tuple[float, float]) -> tuple[float, float]:
        return (
            lon0 + math.degrees(point[0] / (EARTH_RADIUS_M * cos_lat0)),
            lat0 + math.degrees(point[1] / EARTH_RADIUS_M),
        )

    for segment in sidecar["geometry"].get("segments", []):
        values = []
        for point in segment.get("samples_m", []):
            projected = _pixel(local_to_geo((float(point[0]), float(point[1]))), basemap)
            values.append(
                min(
                    _point_segment_distance(projected, first, second)
                    for first, second in pairwise(raw)
                )
            )
        deviations.append(max(values, default=0.0))
        controls = tuple(
            (float(point[0]), float(point[1]))
            for point in segment.get("control_points_m", [])
        )
        parameters = tuple(float(value) for value in segment.get("parameters", []))
        samples_m = tuple(
            (float(point[0]), float(point[1])) for point in segment.get("samples_m", [])
        )
        if len(controls) == 7 and len(parameters) == len(samples_m):
            for index, (left_parameter, right_parameter) in enumerate(pairwise(parameters)):
                midpoint = evaluate_clamped_cubic_bspline(
                    controls, (left_parameter + right_parameter) / 2.0
                )
                left = samples_m[index]
                right = samples_m[index + 1]
                maximum_arc_chord_deficit_m = max(
                    maximum_arc_chord_deficit_m,
                    math.dist(left, midpoint) + math.dist(midpoint, right) - math.dist(left, right),
                )
                maximum_screen_chord_error = max(
                    maximum_screen_chord_error,
                    _point_segment_distance(
                        _pixel(local_to_geo(midpoint), basemap),
                        _pixel(local_to_geo(left), basemap),
                        _pixel(local_to_geo(right), basemap),
                    ),
                )
    ordered = sorted(deviations)
    median = (
        0.0
        if not ordered
        else ordered[len(ordered) // 2]
        if len(ordered) % 2
        else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2.0
    )
    maximum = max(deviations, default=0.0)
    screen_error_gate = 0.5
    arc_error_gate_m = 25.0
    return {
        "viewport": {
            "width_css_px": basemap["width"],
            "height_css_px": basemap["height"],
            "map_zoom": 1,
            "projection": basemap["projection"],
        },
        "accepted_corner_normal_deviation_css_px": deviations,
        "median_css_px": median,
        "maximum_css_px": maximum,
        "median_gate_css_px": 3.0,
        "maximum_gate_css_px": 5.0,
        "maximum_screen_chord_error_css_px": maximum_screen_chord_error,
        "maximum_screen_chord_error_gate_css_px": screen_error_gate,
        "maximum_arc_chord_deficit_m": maximum_arc_chord_deficit_m,
        "maximum_arc_chord_deficit_gate_m": arc_error_gate_m,
        "passed": (
            median >= 3.0
            and maximum >= 5.0
            and maximum_screen_chord_error <= screen_error_gate
            and maximum_arc_chord_deficit_m <= arc_error_gate_m
        ),
    }


def _corner_cases(sidecar: dict[str, Any], route: dict[str, Any]) -> list[dict[str, Any]]:
    accepted = {
        int(value["corner_index"]): value
        for value in sidecar.get("geometry", {}).get("segments", [])
    }
    rejected = {
        int(value["corner_index"]): value
        for value in sidecar.get("geometry", {}).get("rejected_corners", [])
        if isinstance(value.get("corner_index"), int)
    }
    indices = sorted(accepted.keys() | rejected.keys())
    cases = []
    for index in indices:
        eta = datetime.fromisoformat(route["waypoints"][index]["eta"].replace("Z", "+00:00"))
        segment = accepted.get(index)
        cases.append(
            {
                "case_id": f"candidate-corner-{index}",
                "corner_index": index,
                "window_start": (eta - timedelta(hours=3)).isoformat(),
                "window_end": (eta + timedelta(hours=3)).isoformat(),
                "window_kind": "CANDIDATE_CENTERED_6H_SHADOW",
                "status": "PASS" if segment is not None else "FALLBACK",
                "selected_radius_m": segment.get("selected_radius_m") if segment else None,
                "minimum_radius_m": segment.get("minimum_radius_m") if segment else None,
                "reason": rejected.get(index, {}).get("reason"),
                "production_qualified": False,
            }
        )
    return cases


def run(args: argparse.Namespace) -> dict[str, Any]:
    bundle = _read_json(args.bundle)
    routes = bundle.get("routes", [])
    if len(routes) != 1:
        raise ValueError("R1 requires the single authoritative Winter route")
    route = routes[0]
    sampler, commit_path, commit = _load_risk_sampler(args.risk_store)
    first = route["waypoints"][0]
    raster_metadata, raster_cells, raster_source = _local_raster(
        args.land_sea_mask, lon0=float(first["lon"]), lat0=float(first["lat"])
    )

    def corridor(hulls, _points, _times, expansion_m):
        evidence = evaluate_raster_corridor_evidence(
            raster_metadata, raster_cells, hulls, expansion_m=expansion_m
        )
        evidence["source"] = raster_source
        evidence["claim_limit"] = "NO_CONTINUOUS_OCEAN_OR_NAVIGABILITY_PROOF"
        return evidence

    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=3.0,
        maximum_speed_knots=15.7,
        minimum_speed_factor=0.2,
        model_version="nordic_odyssey_scale_synthetic_manoeuvring_v1",
    )
    cgroup = _cgroup_evidence()
    rss_before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    sidecars = []
    wall_times = []
    baseline_wall_times = []
    raw_points, raw_times = _route_records(route)
    for _ in range(args.repeats):
        baseline_started = time.perf_counter()
        _integrate_path(
            sampler,
            vessel,
            raw_points,
            raw_times,
            max_iterations=8,
            convergence_tolerance_s=0.5,
        )
        baseline_wall_times.append(time.perf_counter() - baseline_started)
        started = time.perf_counter()
        sidecars.append(
            build_qualified_route_smoothing_sidecar_v2(
                route,
                experiment_id=args.experiment_id,
                risk_sampler=sampler,
                vessel_model=vessel,
                corridor_validator=corridor,
                input_identity={
                    "plan_revision": route.get("revision"),
                    "adoption_time": route.get("effective_adoption_time"),
                    "route_semantic_digest": route.get("route_digest"),
                    "risk_window_commit": commit.get("commit_id"),
                },
            )
        )
        wall_times.append(time.perf_counter() - started)
    rss_after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    cgroup_root = Path(cgroup["cgroup_path"])

    def cgroup_read(name: str) -> str | None:
        try:
            return (cgroup_root / name).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    cgroup["memory.events.after"] = cgroup_read("memory.events")
    cgroup["memory.swap.current.after"] = cgroup_read("memory.swap.current")
    cgroup["additional_peak_rss_mib"] = max(0, rss_after_kib - rss_before_kib) / 1024.0
    cgroup["additional_peak_rss_gate_mib"] = 128.0
    cgroup["wall_seconds"] = wall_times
    cgroup["raw_recomputed_baseline_wall_seconds"] = baseline_wall_times
    baseline_median = statistics.median(baseline_wall_times)
    smoothing_median = statistics.median(wall_times)
    cgroup["wall_overhead_ratio"] = (
        (smoothing_median - baseline_median) / baseline_median
        if baseline_median > 0.0
        else None
    )
    cgroup["wall_overhead_gate"] = 0.10
    cgroup["wall_baseline_status"] = "SAME_RISKFRAME_VESSEL_RAW_ROUTE_RECOMPUTED"
    cgroup["timeout_seconds"] = 90 * 60
    cgroup["timeout_observed"] = False
    cgroup["complete"] = (
        cgroup["cgroup_limits_enforced"]
        and len(wall_times) == args.repeats
        and len(baseline_wall_times) == args.repeats
        and cgroup["memory.events.before"] is not None
        and cgroup["memory.events.after"] is not None
    )
    memory_events_after = cgroup["memory.events.after"] or ""
    no_oom = "oom 0" in memory_events_after and "oom_kill 0" in memory_events_after
    no_swap = cgroup["memory.swap.current.after"] == "0"
    cgroup["qualified"] = (
        cgroup["complete"]
        and no_oom
        and no_swap
        and cgroup["additional_peak_rss_mib"] <= cgroup["additional_peak_rss_gate_mib"]
        and cgroup["wall_overhead_ratio"] is not None
        and cgroup["wall_overhead_ratio"] <= cgroup["wall_overhead_gate"]
    )

    for value in sidecars:
        value["resource_evidence"] = dict(cgroup)
        value["validation"]["resource_evidence_complete"] = cgroup["complete"]
        _finish_digest(value)
    sidecar = sidecars[0]
    digests = [value.get("sidecar_digest") for value in sidecars]
    deterministic = len(set(digests)) == 1 and len(digests) == args.repeats
    cases = _corner_cases(sidecar, route)
    accepted_cases = sum(value["status"] == "PASS" for value in cases)
    visual = (
        _visual_evidence(sidecar, bundle["basemap"])
        if sidecar.get("status") == "ACCEPTED"
        else {"passed": False, "reason": "sidecar_fallback"}
    )
    semantic_pass = (
        sidecar.get("status") == "ACCEPTED"
        and accepted_cases >= 5
        and deterministic
        and sidecar.get("validation", {}).get("research_gate_passed") is True
    )
    if sidecar.get("status") != "ACCEPTED":
        conclusion = "FALLBACK_RAW_ROUTE"
    elif not visual.get("passed"):
        conclusion = "SHADOW_PASS_VISUAL_GAIN_INSUFFICIENT"
    elif semantic_pass and cgroup.get("qualified"):
        conclusion = "SYNTHETIC_VESSEL_AND_REAL_ENVIRONMENT_SHADOW_PASS"
    else:
        conclusion = "DISPLAY_ONLY_RETAINED"
    assert conclusion in CONCLUSIONS

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(args.output_dir / "route-smoothing-sidecar-v2.json", sidecar)
    with (args.output_dir / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "schema_version": EXPERIMENT_SCHEMA,
        "experiment_id": args.experiment_id,
        "conclusion": conclusion,
        "terminal_marker": f"{conclusion}_NO_PRODUCTION_CUTOVER",
        "status": "PASS" if semantic_pass else "FALLBACK",
        "sidecar_status": sidecar.get("status"),
        "fallback_reason": sidecar.get("fallback_reason"),
        "candidate_centered_6h": {
            "case_count": len(cases),
            "pass_count": accepted_cases,
            "required_pass_count": 5,
            "passed": accepted_cases >= 5,
        },
        "full_route": {
            "duration_hours": (
                datetime.fromisoformat(route["waypoints"][-1]["eta"].replace("Z", "+00:00"))
                - datetime.fromisoformat(route["waypoints"][0]["eta"].replace("Z", "+00:00"))
            ).total_seconds()
            / 3600.0,
            "repeat_count": args.repeats,
            "sidecar_digests": digests,
            "deterministic": deterministic,
        },
        "visual_evidence": visual,
        "resource_evidence": cgroup,
        "risk_window": {
            "commit_path": str(commit_path),
            "commit_sha256": _sha256_file(commit_path),
            "commit_id": commit.get("commit_id"),
            "frame_count": commit.get("count"),
        },
        "raster_source": raster_source,
        "synthetic_vessel": {
            "profile_id": vessel.model_version,
            "length_m": 225.0,
            "breadth_m": 32.31,
            "calibration_status": "SYNTHETIC_UNCALIBRATED",
        },
        "claim_limits": [
            "NO_PRODUCTION_CUTOVER",
            "NO_REAL_VESSEL_CALIBRATION",
            "NO_CONTINUOUS_OCEAN_OR_NAVIGABILITY_PROOF",
            "Dijkstra is not a performance baseline",
        ],
        "production_qualified": False,
        "policy": POLICY,
        "generated_at": _utc_now(),
    }
    _write_json(args.output_dir / "comparison-summary.json", summary)
    manifest = {
        "schema_version": EXPERIMENT_SCHEMA,
        "experiment_id": args.experiment_id,
        "status": summary["status"],
        "conclusion": conclusion,
        "inputs": {
            "bundle": {"path": str(args.bundle), "sha256": _sha256_file(args.bundle)},
            "risk_window_commit": {
                "path": str(commit_path),
                "sha256": _sha256_file(commit_path),
            },
            "land_sea_mask": raster_source,
        },
        "artifacts": {
            name: {
                "path": str(args.output_dir / name),
                "sha256": _sha256_file(args.output_dir / name),
            }
            for name in (
                "route-smoothing-sidecar-v2.json",
                "cases.jsonl",
                "comparison-summary.json",
            )
        },
        "git": {
            "c": _git_snapshot(Path(__file__).resolve().parents[2] / "work_package_c"),
            "orchestrator": _git_snapshot(Path(__file__).resolve().parents[1]),
            "d": _git_snapshot(Path(__file__).resolve().parents[2] / "work_package_d"),
        },
        "production_qualified": False,
        "created_at": _utc_now(),
    }
    _write_json(args.output_dir / "manifest.json", manifest)
    (args.output_dir / "ALL_DONE").write_text(
        f"conclusion={conclusion}\nNO_PRODUCTION_CUTOVER\nproduction_qualified=false\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--risk-store", type=Path, required=True)
    parser.add_argument("--land-sea-mask", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    if args.repeats != 3:
        raise ValueError("R1 full-route shadow requires exactly three repeats")
    summary = run(args)
    print(
        f"conclusion={summary['conclusion']} sidecar={summary['sidecar_status']} "
        f"corners={summary['candidate_centered_6h']['pass_count']}/"
        f"{summary['candidate_centered_6h']['case_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
