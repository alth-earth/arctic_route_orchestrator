"""Run a bounded, target-only Winter cold-path diagnostic.

This is a research-only companion to ``winter_p2_shadow.py``.  It keeps the
formal runner unchanged and executes one selected rolling or executable cold
search in fresh child processes.  The target is intentionally not a
four-layer publication and the output is never a formal M2 verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any

import winter_p2_shadow as shadow
from arctic_route_planning.domain import ObjectiveMode
from arctic_route_planning.ingress import (
    _MeasuredShadowControlPlanner,
    _ShadowEdgeCounter,
    _ShadowMeasurement,
    _TemporalShadowCandidatePlanner,
)
from arctic_route_planning.planners import PlanningRequest

TARGET_LAYER = "rolling_0_24h"
TARGET_OBJECTIVE = ObjectiveMode.FASTEST
TARGET_GOAL = (14, 5)
TARGET_MAXIMUM_ELAPSED_HOURS = 24
TARGET_LAYER_INDEX = {"rolling_0_24h": 2, "executable_0_6h": 3}


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prepare_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        risk_store_root=args.risk_store_root,
        risk_commit=args.risk_commit,
        execution_spec=args.execution_spec,
        run_context=args.run_context,
        c_config_root=args.c_config_root,
        contracts_config_root=args.contracts_config_root,
    )


def _worker(args: argparse.Namespace) -> int:
    cpu_pin_cpu = shadow._pin_worker_cpu()
    cpu_pin_succeeded = cpu_pin_cpu is not None
    prepared = shadow._prepare(_prepare_args(args))
    planner_config = prepared.configuration.planner
    target_layer = str(args.target_layer)
    target_objective = ObjectiveMode(str(args.target_objective))
    target_goal = tuple(int(value) for value in args.target_goal)
    target_maximum_elapsed_hours = int(args.target_maximum_elapsed_hours)
    target_request = PlanningRequest(
        start=prepared.request.start,
        goal=target_goal,
        departure_time=prepared.request.start_time,
        objective=target_objective,
        time_bucket_size=timedelta(minutes=planner_config.time_bucket_minutes),
        edge_sample_count=planner_config.edge_sample_count,
        maximum_elapsed=timedelta(hours=target_maximum_elapsed_hours),
        maximum_risk=prepared.request.maximum_risk,
    )
    started = time.perf_counter()
    with prepared.store.lease_committed_window(prepared.query) as current:
        if current.commit_id != prepared.commit["commit_id"]:
            raise ValueError("cold diagnostic committed-window identity changed")
        measurement = _ShadowMeasurement()
        swap_before = shadow._swap_counters()
        process_swap_before_kib = shadow._process_swap_kib()
        if args.track == "control":
            adapter: Any = _MeasuredShadowControlPlanner(
                prepared.prepared._private_planner(current), measurement
            )
        else:
            candidate_planner = prepared.prepared._private_planner(current)
            fallback_planner = prepared.prepared._private_planner(current)
            adapter = _TemporalShadowCandidatePlanner(
                candidate_planner,
                request=prepared.request,
                window=current,
                candidate_mode="control_trace",
                control_planner=fallback_planner,
                measurement=measurement,
            )
            # Skip the expensive full/main setup while retaining the adapter
            # state that identifies this call as the rolling cold fallback.
            adapter._layer_index = TARGET_LAYER_INDEX[target_layer]
            adapter._full_traces[target_objective] = object()

        underlying = [getattr(adapter, "_planner", adapter)]
        fallback = getattr(adapter, "_control_planner", None)
        if fallback is not None:
            underlying.append(fallback)
        counters = [_ShadowEdgeCounter(planner, measurement) for planner in underlying]
        try:
            for counter in counters:
                counter.__enter__()
            adapter.plan_candidates(target_request, (target_objective,))[target_objective]
        finally:
            for counter in reversed(counters):
                counter.__exit__(None, None, None)
        swap_after = shadow._swap_counters()
        process_swap_after_kib = shadow._process_swap_kib()
        timing = adapter.timing_observations[-1]
        record = asdict(timing)
        record.update(
            {
                "track": args.track,
                "target_layer": target_layer,
                "target_objective": target_objective.value,
                "target_goal": list(target_goal),
                "target_maximum_elapsed_hours": target_maximum_elapsed_hours,
                "risk_commit_id": current.commit_id,
                "risk_content_digest": current.content_digest,
                "status": "PASS",
            }
        )
    record["process_wall_ms"] = (time.perf_counter() - started) * 1000.0
    record["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    record["swap_before"] = swap_before
    record["swap_after"] = swap_after
    record["swap_delta"] = shadow._swap_delta(swap_before, swap_after)
    record["process_swap_before_kib"] = process_swap_before_kib
    record["process_swap_after_kib"] = process_swap_after_kib
    record["process_swap_delta_kib"] = shadow._process_swap_delta(
        process_swap_before_kib,
        process_swap_after_kib,
    )
    record["swap_measurement"] = {
        "kernel_counters": swap_before is not None and swap_after is not None,
        "process_vm_swap": (
            process_swap_before_kib is not None and process_swap_after_kib is not None
        ),
        "status": shadow._swap_measurement_status(
            swap_before=swap_before,
            swap_after=swap_after,
            process_swap_before_kib=process_swap_before_kib,
            process_swap_after_kib=process_swap_after_kib,
        ),
    }
    record["pid"] = os.getpid()
    record["cpu_pin_cpu"] = cpu_pin_cpu
    record["cpu_pin_succeeded"] = cpu_pin_succeeded
    record["cpu_affinity"] = shadow._worker_cpu_affinity()
    record["cpu_measurement"] = {
        "status": shadow._cpu_measurement_status(
            cpu_pin_cpu=cpu_pin_cpu,
            cpu_pin_succeeded=cpu_pin_succeeded,
            cpu_affinity=record["cpu_affinity"],
        )
    }
    record["python"] = platform.python_version()
    args.worker_output.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return 0


def _run_pair(
    args: argparse.Namespace,
    repetition: int,
    order: str,
    pair_dir: Path,
) -> dict[str, Any]:
    pair_dir.mkdir(parents=True, exist_ok=False)
    tracks = ("control", "candidate") if order == "control-first" else ("candidate", "control")
    records: dict[str, dict[str, Any]] = {}
    for track in tracks:
        output = pair_dir / f"{track}.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--risk-store-root",
            str(args.risk_store_root),
            "--risk-commit",
            str(args.risk_commit),
            "--run-context",
            str(args.run_context),
            "--execution-spec",
            str(args.execution_spec),
            "--c-config-root",
            str(args.c_config_root),
            "--contracts-config-root",
            str(args.contracts_config_root),
            "--output-dir",
            str(pair_dir),
            "--track",
            track,
            "--worker-output",
            str(output),
            "--target-layer",
            str(args.target_layer),
            "--target-objective",
            str(args.target_objective),
            "--target-goal",
            *(str(value) for value in args.target_goal),
            "--target-maximum-elapsed-hours",
            str(args.target_maximum_elapsed_hours),
            "--_worker",
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0 or not output.exists():
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"{track} diagnostic worker failed: {detail[-1000:]}")
        records[track] = json.loads(output.read_text(encoding="utf-8"))

    control = records["control"]
    candidate = records["candidate"]
    control_wall = float(control["wall_ms"])
    candidate_wall = float(candidate["wall_ms"])
    pair = {
        "case_id": f"case-{repetition:03d}",
        "repetition": repetition,
        "execution_order": order,
        "control": control,
        "candidate": candidate,
        "paired": {
            "route_digest_equal": control.get("route_digest") == candidate.get("route_digest"),
            "expanded_equal": control.get("expanded") == candidate.get("expanded"),
            "edge_equal": control.get("edge") == candidate.get("edge"),
            "cache_delta_equal": (
                control.get("edge_geometry_cache_delta") is not None
                and candidate.get("edge_geometry_cache_delta") is not None
                and control.get("edge_geometry_cache_delta")
                == candidate.get("edge_geometry_cache_delta")
            ),
            "candidate_regression_percent": ((candidate_wall / control_wall) - 1.0) * 100.0,
            "planner_delta_ms": float(candidate["planner_ms"]) - float(control["planner_ms"]),
            "pre_delta_ms": float(candidate["pre_ms"]) - float(control["pre_ms"]),
            "post_delta_ms": float(candidate["post_ms"]) - float(control["post_ms"]),
            "swap_zero": all(
                shadow._swap_observation_pass(record)
                for record in (control, candidate)
            ),
            "swap_measured": all(
                isinstance(record.get("swap_measurement"), dict)
                and record["swap_measurement"].get("status") == "PASS"
                for record in (control, candidate)
            ),
            "cpu_affinity_equal": (
                shadow._cpu_measurement_status(
                    cpu_pin_cpu=control.get("cpu_pin_cpu"),
                    cpu_pin_succeeded=control.get("cpu_pin_succeeded"),
                    cpu_affinity=control.get("cpu_affinity"),
                )
                == "PASS"
                and shadow._cpu_measurement_status(
                    cpu_pin_cpu=candidate.get("cpu_pin_cpu"),
                    cpu_pin_succeeded=candidate.get("cpu_pin_succeeded"),
                    cpu_affinity=candidate.get("cpu_affinity"),
                )
                == "PASS"
                and control.get("cpu_pin_cpu") == candidate.get("cpu_pin_cpu")
                and control.get("cpu_affinity") == candidate.get("cpu_affinity")
            ),
        },
        "status": "PASS"
        if control.get("route_digest") is not None
        and control.get("route_digest") == candidate.get("route_digest")
        and control.get("expanded") == candidate.get("expanded")
        and control.get("edge") == candidate.get("edge")
        and control.get("edge_geometry_cache_delta") is not None
        and candidate.get("edge_geometry_cache_delta") is not None
        and control.get("edge_geometry_cache_delta")
        == candidate.get("edge_geometry_cache_delta")
        and all(
            shadow._swap_observation_pass(record)
            for record in (control, candidate)
        )
        and all(
            shadow._cpu_measurement_status(
                cpu_pin_cpu=record.get("cpu_pin_cpu"),
                cpu_pin_succeeded=record.get("cpu_pin_succeeded"),
                cpu_affinity=record.get("cpu_affinity"),
            )
            == "PASS"
            for record in (control, candidate)
        )
        else "FAIL",
    }
    return pair


def _run(args: argparse.Namespace) -> int:
    if args.repetitions < 1:
        raise ValueError("repetitions must be positive")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise ValueError(f"output directory must be new and empty: {args.output_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prepared = shadow._prepare(_prepare_args(args))
    key = {
        "script": Path(__file__).name,
        "target_layer": args.target_layer,
        "target_objective": args.target_objective,
        "target_goal": list(args.target_goal),
        "target_maximum_elapsed_hours": args.target_maximum_elapsed_hours,
        "risk_commit": prepared.commit["commit_id"],
        "risk_content_digest": prepared.commit["content_digest"],
        "run_id": prepared.spec.run_id,
        "repetitions": args.repetitions,
    }
    manifest = {
        "schema_version": "orchestrator.winter-cold-target-diagnostic.v1",
        "experiment_id": f"winter-cold-target-{_canonical_digest(key)[:16]}",
        "identity_kind": "experimental_diagnostic",
        "diagnostic_only": True,
        "formal_gate_verdict": "NOT_APPLICABLE",
        "formal_m2_verdict_unchanged": "FAIL",
        "target": {
            "layer": args.target_layer,
            "objective": args.target_objective,
            "goal": list(args.target_goal),
            "maximum_elapsed_hours": args.target_maximum_elapsed_hours,
        },
        "input_identity": prepared.input_identity,
        "repetitions": args.repetitions,
        "implementation": {
            "diagnostic_script": str(Path(__file__).resolve()),
            "orchestrator_commit": shadow._git_environment(Path(__file__).resolve().parents[1]),
            "work_package_c_commit": shadow._git_environment(
                shadow._workspace_root() / "work_package_c"
            ),
        },
        "publication_boundary": {
            "formal_latest_store_written": False,
            "frozen_artifact_written": False,
            "production_published": False,
            "output_directory": str(args.output_dir),
        },
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    pairs: list[dict[str, Any]] = []
    for repetition in range(1, args.repetitions + 1):
        order = "control-first" if repetition % 2 else "candidate-first"
        pairs.append(_run_pair(args, repetition, order, args.output_dir / f"case-{repetition:03d}"))
    regressions = [pair["paired"]["candidate_regression_percent"] for pair in pairs]
    planner_deltas = [pair["paired"]["planner_delta_ms"] for pair in pairs]
    median_regression = median(regressions)
    failed_pairs = sum(pair["status"] != "PASS" for pair in pairs)
    swap_zero_pair_count = sum(pair["paired"]["swap_zero"] for pair in pairs)
    swap_measured_pair_count = sum(pair["paired"]["swap_measured"] for pair in pairs)
    diagnostic_gate = (
        median_regression <= 3.0
        and swap_zero_pair_count == len(pairs)
        and swap_measured_pair_count == len(pairs)
        and all(pair["paired"]["cache_delta_equal"] for pair in pairs)
        and all(
            pair["paired"].get("cpu_affinity_equal", False)
            for pair in pairs
        )
    )
    summary = {
        "schema_version": "orchestrator.winter-cold-target-summary.v1",
        "diagnostic_only": True,
        "formal_gate_verdict": "NOT_APPLICABLE",
        "formal_m2_verdict_unchanged": "FAIL",
        "pair_count": len(pairs),
        "passed_pairs": sum(pair["status"] == "PASS" for pair in pairs),
        "failed_pairs": failed_pairs,
        "diagnostic_gate": {
            "median_regression_ceiling_percent": 3.0,
            "median_regression_percent": median_regression,
            "swap_required_zero": True,
            "swap_zero_pair_count": swap_zero_pair_count,
            "swap_measured_pair_count": swap_measured_pair_count,
            "gate": "PASS" if diagnostic_gate else "FAIL",
        },
        "candidate_regression_percent": {
            "median": median(regressions),
            "min": min(regressions),
            "max": max(regressions),
            "values": regressions,
        },
        "planner_delta_ms": {"median": median(planner_deltas), "values": planner_deltas},
        "route_digest_equal_pair_count": sum(
            pair["paired"]["route_digest_equal"] for pair in pairs
        ),
        "expanded_equal_pair_count": sum(pair["paired"]["expanded_equal"] for pair in pairs),
        "edge_equal_pair_count": sum(pair["paired"]["edge_equal"] for pair in pairs),
        "cache_delta_equal_pair_count": sum(
            pair["paired"]["cache_delta_equal"] for pair in pairs
        ),
        "cpu_affinity_equal_pair_count": sum(
            pair["paired"]["cpu_affinity_equal"] for pair in pairs
        ),
        "swap_zero_pair_count": swap_zero_pair_count,
        "swap_measured_pair_count": swap_measured_pair_count,
        "status": (
            "PASS"
            if failed_pairs == 0 and diagnostic_gate
            else "FAIL"
        ),
    }
    (args.output_dir / "cases.json").write_text(
        json.dumps(pairs, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (args.output_dir / "cold-path-diagnostic.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    manifest.update({"status": summary["status"], "summary": summary})
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0 if summary["failed_pairs"] == 0 else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="winter-cold-target-diagnostic")
    parser.add_argument("--risk-store-root", type=Path, required=True)
    parser.add_argument("--risk-commit", type=Path, required=True)
    parser.add_argument("--run-context", type=Path, required=True)
    parser.add_argument("--execution-spec", type=Path, required=True)
    parser.add_argument("--c-config-root", type=Path, required=True)
    parser.add_argument("--contracts-config-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument(
        "--target-layer",
        choices=tuple(TARGET_LAYER_INDEX),
        default=TARGET_LAYER,
    )
    parser.add_argument(
        "--target-objective",
        choices=tuple(objective.value for objective in ObjectiveMode),
        default=TARGET_OBJECTIVE.value,
    )
    parser.add_argument("--target-goal", nargs=2, type=int, default=list(TARGET_GOAL))
    parser.add_argument(
        "--target-maximum-elapsed-hours",
        type=int,
        default=TARGET_MAXIMUM_ELAPSED_HOURS,
    )
    parser.add_argument("--track", choices=("control", "candidate"), default="control")
    parser.add_argument("--worker-output", type=Path, default=None)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args._worker:
        if args.worker_output is None:
            raise ValueError("--worker-output is required for a worker")
        return _worker(args)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
