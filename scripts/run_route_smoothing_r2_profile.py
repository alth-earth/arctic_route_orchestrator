#!/usr/bin/env python3
"""Run the R2-only route-smoothing performance and blocker profile.

This runner preserves the R1 sidecar semantics and records cold and warm
optimisation evidence separately.  It never grants production qualification,
changes RoutePlan, or enables a Viewer consumer.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import run_route_smoothing_r1 as r1
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.research.route_smoothing_qualification import (
    _integrate_path,
    _route_records,
)
from arctic_route_planning.research.route_smoothing_qualification_v2 import (
    build_qualified_route_smoothing_sidecar_v2,
)
from arctic_route_planning.research.route_smoothing_r2 import (
    StageTimingCollector,
    assess_production_proposal_readiness,
    build_eta_drift_diagnostic,
)
from arctic_route_planning.risk import (
    ExperimentalRiskSampler,
    RiskSampler,
    SampleCacheMode,
)

from arctic_route_orchestrator.replay.raster_corridor_evidence import (
    evaluate_raster_corridor_evidence,
    prepare_raster_corridor_evidence,
)

SCHEMA_VERSION = "c.r2-route-smoothing-performance-profile.v1"
EXPERIMENT_PREFIX = "c-r2-route-smoothing-performance-profile-"
PERFORMANCE_FAIL = "R2_PROFILE_ONLY_PERFORMANCE_GATE_FAIL_NO_PRODUCTION_CUTOVER"
EXTERNAL_BLOCKED = (
    "R2_PERFORMANCE_PASS_EXTERNAL_EVIDENCE_BLOCKED_NO_PRODUCTION_CUTOVER"
)


def _ensure_empty_output(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise ValueError("R2 output directory must not contain existing artifacts")
    path.mkdir(parents=True, exist_ok=True)


def _profiled_sidecar(
    *,
    route: dict[str, Any],
    experiment_id: str,
    sampler: RiskSampler,
    vessel: VesselPerformanceModel,
    corridor: Callable[..., dict[str, Any]],
    input_identity: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], float]:
    collector = StageTimingCollector()
    started = time.perf_counter()
    sidecar = build_qualified_route_smoothing_sidecar_v2(
        route,
        experiment_id=experiment_id,
        risk_sampler=sampler,
        vessel_model=vessel,
        corridor_validator=corridor,
        input_identity=input_identity,
        stage_observer=collector.observe,
    )
    return sidecar, collector.summary(), time.perf_counter() - started


def _timed_sidecar(
    *,
    route: dict[str, Any],
    experiment_id: str,
    sampler: RiskSampler,
    vessel: VesselPerformanceModel,
    corridor: Callable[..., dict[str, Any]],
    input_identity: dict[str, Any],
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    sidecar = build_qualified_route_smoothing_sidecar_v2(
        route,
        experiment_id=experiment_id,
        risk_sampler=sampler,
        vessel_model=vessel,
        corridor_validator=corridor,
        input_identity=input_identity,
    )
    return sidecar, time.perf_counter() - started


def _cgroup_after(evidence: dict[str, Any]) -> dict[str, Any]:
    root = Path(evidence["cgroup_path"])

    def read(name: str) -> str | None:
        try:
            return (root / name).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    evidence["memory.events.after"] = read("memory.events")
    evidence["memory.swap.current.after"] = read("memory.swap.current")
    return evidence


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.experiment_id.startswith(EXPERIMENT_PREFIX):
        raise ValueError(f"experiment_id must start with {EXPERIMENT_PREFIX}")
    if args.warm_repeats != 3:
        raise ValueError("R2 requires exactly three warm-cache repeats")
    _ensure_empty_output(args.output_dir)

    bundle = r1._read_json(args.bundle)
    routes = bundle.get("routes", [])
    if len(routes) != 1:
        raise ValueError("R2 requires the single authoritative Winter route")
    route = routes[0]
    canonical_sampler, commit_path, commit = r1._load_risk_sampler(args.risk_store)
    first = route["waypoints"][0]
    raster_metadata, raster_cells, raster_source = r1._local_raster(
        args.land_sea_mask,
        lon0=float(first["lon"]),
        lat0=float(first["lat"]),
    )
    prepared_raster = prepare_raster_corridor_evidence(
        raster_metadata, raster_cells
    )

    def decorate(evidence: dict[str, Any]) -> dict[str, Any]:
        evidence["source"] = raster_source
        evidence["claim_limit"] = "NO_CONTINUOUS_OCEAN_OR_NAVIGABILITY_PROOF"
        return evidence

    def generic_corridor(hulls, _points, _times, expansion_m):
        return decorate(
            evaluate_raster_corridor_evidence(
                raster_metadata, raster_cells, hulls, expansion_m=expansion_m
            )
        )

    def prepared_corridor(hulls, _points, _times, expansion_m):
        return decorate(prepared_raster.evaluate(hulls, expansion_m=expansion_m))

    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=3.0,
        maximum_speed_knots=15.7,
        minimum_speed_factor=0.2,
        model_version="nordic_odyssey_scale_synthetic_manoeuvring_v1",
    )
    input_identity = {
        "plan_revision": route.get("revision"),
        "adoption_time": route.get("effective_adoption_time"),
        "route_semantic_digest": route.get("route_digest"),
        "risk_window_commit": commit.get("commit_id"),
    }
    cgroup = r1._cgroup_evidence()
    rss_before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    raw_points, published_times = _route_records(route)
    raw_baseline_times = []
    raw_recomputed_times = None
    for _ in range(3):
        started = time.perf_counter()
        raw_recomputed_times, _, _, _ = _integrate_path(
            canonical_sampler,
            vessel,
            raw_points,
            published_times,
            max_iterations=8,
            convergence_tolerance_s=0.5,
        )
        raw_baseline_times.append(time.perf_counter() - started)
    assert raw_recomputed_times is not None

    canonical_profile, canonical_stages, canonical_profile_wall = _profiled_sidecar(
        route=route,
        experiment_id=args.experiment_id,
        sampler=canonical_sampler,
        vessel=vessel,
        corridor=generic_corridor,
        input_identity=input_identity,
    )
    prepared_profile, prepared_stages, prepared_profile_wall = _profiled_sidecar(
        route=route,
        experiment_id=args.experiment_id,
        sampler=RiskSampler(canonical_sampler.frames),
        vessel=vessel,
        corridor=prepared_corridor,
        input_identity=input_identity,
    )

    profiled_cache = ExperimentalRiskSampler(
        canonical_sampler.frames,
        mode=SampleCacheMode.BOUNDED_LRU,
        capacity=50_000,
    )
    cached_profile, cached_stages, cached_profile_wall = _profiled_sidecar(
        route=route,
        experiment_id=args.experiment_id,
        sampler=profiled_cache,
        vessel=vessel,
        corridor=prepared_corridor,
        input_identity=input_identity,
    )

    measured_cache = ExperimentalRiskSampler(
        canonical_sampler.frames,
        mode=SampleCacheMode.BOUNDED_LRU,
        capacity=50_000,
    )
    cold_sidecar, cold_wall = _timed_sidecar(
        route=route,
        experiment_id=args.experiment_id,
        sampler=measured_cache,
        vessel=vessel,
        corridor=prepared_corridor,
        input_identity=input_identity,
    )
    cache_after_cold = dict(measured_cache.experiment_stats)
    warm_sidecars = []
    warm_wall_times = []
    for _ in range(args.warm_repeats):
        sidecar, wall = _timed_sidecar(
            route=route,
            experiment_id=args.experiment_id,
            sampler=measured_cache,
            vessel=vessel,
            corridor=prepared_corridor,
            input_identity=input_identity,
        )
        warm_sidecars.append(sidecar)
        warm_wall_times.append(wall)
    cache_after_warm = dict(measured_cache.experiment_stats)

    rss_after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    _cgroup_after(cgroup)
    baseline_median = statistics.median(raw_baseline_times)
    cold_overhead_ratio = (
        (cold_wall - baseline_median) / baseline_median
        if baseline_median > 0.0
        else None
    )
    warm_median = statistics.median(warm_wall_times)
    warm_overhead_ratio = (
        (warm_median - baseline_median) / baseline_median
        if baseline_median > 0.0
        else None
    )
    all_sidecars = [
        canonical_profile,
        prepared_profile,
        cached_profile,
        cold_sidecar,
        *warm_sidecars,
    ]
    digests = [sidecar.get("sidecar_digest") for sidecar in all_sidecars]
    semantic_digest_match = len(set(digests)) == 1
    memory_events = cgroup.get("memory.events.after") or ""
    resource_evidence = {
        **cgroup,
        "complete": (
            cgroup.get("cgroup_limits_enforced") is True
            and cgroup.get("memory.events.before") is not None
            and cgroup.get("memory.events.after") is not None
        ),
        "additional_peak_rss_mib": max(0, rss_after_kib - rss_before_kib) / 1024.0,
        "additional_peak_rss_gate_mib": 128.0,
        "raw_recomputed_baseline_wall_seconds": raw_baseline_times,
        "cold_optimized_wall_seconds": cold_wall,
        "warm_optimized_wall_seconds": warm_wall_times,
        "cold_wall_overhead_ratio": cold_overhead_ratio,
        "warm_wall_overhead_ratio": warm_overhead_ratio,
        "wall_overhead_gate": 0.10,
        "wall_baseline_status": "SAME_RISKFRAME_VESSEL_RAW_ROUTE_RECOMPUTED",
        "semantic_digest_match": semantic_digest_match,
    }
    no_oom = "oom 0" in memory_events and "oom_kill 0" in memory_events
    no_swap = cgroup.get("memory.swap.current.after") == "0"
    resource_evidence["qualified"] = (
        resource_evidence["complete"]
        and no_oom
        and no_swap
        and resource_evidence["additional_peak_rss_mib"] <= 128.0
        and semantic_digest_match
        and cold_overhead_ratio is not None
        and cold_overhead_ratio <= 0.10
    )

    eta_diagnostic = build_eta_drift_diagnostic(
        raw_points,
        published_times,
        raw_recomputed_times,
        route_identity={
            "route_id": cold_sidecar.get("route_id"),
            "route_digest": cold_sidecar.get("raw_route_digest"),
        },
        risk_window_identity={"commit_id": commit.get("commit_id")},
        vessel_profile_id=vessel.model_version,
        vessel_model_version=vessel.model_version,
        published_distance_km=route.get("metrics", {}).get("distance_km"),
    )
    expected_identity = {
        "route_digest": cold_sidecar.get("raw_route_digest"),
        "risk_window_commit": commit.get("commit_id"),
        "vessel_profile_id": vessel.model_version,
    }
    readiness = assess_production_proposal_readiness(
        performance_evidence=resource_evidence,
        manoeuvring_calibration=cold_sidecar.get("manoeuvring_evidence"),
        continuous_corridor_evidence=cold_sidecar.get("corridor_evidence", {}).get(
            "primary"
        ),
        eta_diagnostic=eta_diagnostic,
        expected_identity=expected_identity,
    )
    conclusion = (
        PERFORMANCE_FAIL
        if resource_evidence["qualified"] is not True
        else readiness["status"]
        if readiness["proposal_ready"] is True
        else EXTERNAL_BLOCKED
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": args.experiment_id,
        "conclusion": conclusion,
        "production_qualified": False,
        "cutover_authorized": False,
        "r1_terminal_unchanged": "DISPLAY_ONLY_RETAINED_NO_PRODUCTION_CUTOVER",
        "semantic_digest_match": semantic_digest_match,
        "sidecar_digests": digests,
        "profiles": {
            "canonical_unprepared": {
                "wall_seconds": canonical_profile_wall,
                "stages": canonical_stages,
            },
            "prepared_raster": {
                "wall_seconds": prepared_profile_wall,
                "stages": prepared_stages,
            },
            "prepared_raster_and_exact_risk_cache_cold": {
                "wall_seconds": cached_profile_wall,
                "stages": cached_stages,
                "cache": profiled_cache.experiment_stats,
            },
        },
        "resource_evidence": resource_evidence,
        "risk_cache": {
            "after_cold": cache_after_cold,
            "after_warm": cache_after_warm,
        },
        "claim_limits": [
            "NO_PRODUCTION_CUTOVER",
            "NO_REAL_VESSEL_CALIBRATION",
            "NO_CONTINUOUS_OCEAN_OR_NAVIGABILITY_PROOF",
            "WARM_CACHE_RESULT_IS_NOT_COLD_QUALIFICATION",
        ],
        "generated_at": r1._utc_now(),
    }

    r1._write_json(args.output_dir / "profile-summary.json", summary)
    r1._write_json(args.output_dir / "eta-diagnostic.json", eta_diagnostic)
    r1._write_json(args.output_dir / "proposal-readiness.json", readiness)
    r1._write_json(args.output_dir / "optimized-sidecar-v2.json", cold_sidecar)
    artifact_names = (
        "profile-summary.json",
        "eta-diagnostic.json",
        "proposal-readiness.json",
        "optimized-sidecar-v2.json",
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": args.experiment_id,
        "conclusion": conclusion,
        "inputs": {
            "bundle": {"path": str(args.bundle), "sha256": r1._sha256_file(args.bundle)},
            "risk_window_commit": {
                "path": str(commit_path),
                "sha256": r1._sha256_file(commit_path),
            },
            "land_sea_mask": raster_source,
        },
        "artifacts": {
            name: {
                "path": str(args.output_dir / name),
                "sha256": r1._sha256_file(args.output_dir / name),
            }
            for name in artifact_names
        },
        "git": {
            "c": r1._git_snapshot(Path(__file__).resolve().parents[2] / "work_package_c"),
            "orchestrator": r1._git_snapshot(Path(__file__).resolve().parents[1]),
            "d": r1._git_snapshot(Path(__file__).resolve().parents[2] / "work_package_d"),
        },
        "production_qualified": False,
        "created_at": r1._utc_now(),
    }
    r1._write_json(args.output_dir / "manifest.json", manifest)
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
    parser.add_argument("--warm-repeats", type=int, default=3)
    args = parser.parse_args(argv)
    summary = run(args)
    print(
        json.dumps(
            {
                "conclusion": summary["conclusion"],
                "semantic_digest_match": summary["semantic_digest_match"],
                "cold_wall_overhead_ratio": summary["resource_evidence"][
                    "cold_wall_overhead_ratio"
                ],
                "warm_wall_overhead_ratio": summary["resource_evidence"][
                    "warm_wall_overhead_ratio"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
