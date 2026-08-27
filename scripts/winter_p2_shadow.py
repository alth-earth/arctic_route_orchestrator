"""Run an isolated Winter C control/candidate P2 shadow experiment.

The existing ``run_winter_c_validation.py`` remains the formal validation
entrypoint.  This script deliberately constructs two in-memory C planning
services while holding a committed RiskWindow lease.  Candidate results and
diagnostics are written only below the caller-provided, empty experiment
directory; no formal latest store, frozen artifact, or presentation package is
updated.

P2 certificate reuse is invoked through C's internal ``temporal_reuse`` and
``control_trace_reuse`` APIs.  Exact and monotonic hits are recorded in a
research-only sidecar; misses fall back to an explicit search and never become
a formal claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import resource
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from arctic_route_contracts import load_run_context, run_context_to_dict
from arctic_route_planning import ServicePlanningRequest, map_corridor_endpoints
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import (
    CommittedRiskWindow,
    ProvenanceKind,
    RiskWindowQuery,
    risk_frame_from_document,
    risk_frame_to_document,
)
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain import ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.ingress import RiskSourcePlanningIngress
from arctic_route_planning.layered import (
    FourLayerPlanningOutcome,
    FourLayerPlanningService,
)
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.planners._archive.control_trace_reuse import (
    trace_plan as control_trace_plan,
)
from arctic_route_planning.planners._archive.control_trace_reuse import (
    try_reuse as try_control_trace_reuse,
)
from arctic_route_planning.planners.temporal_label_astar import TemporalLabelAStar
from arctic_route_planning.planners._archive.temporal_reuse import (
    TemporalCertifiedGoal,
    TemporalReuseOutcome,
    certify_session,
    try_reuse,
)
from arctic_route_planning.planners._archive.temporal_session import TemporalSessionIdentity
from arctic_route_planning.publishing import (
    four_layer_route_plan_set_to_dict,
    route_plan_v3_to_dict,
)
from arctic_route_planning.publishing.layered_serialization import (
    four_layer_route_plan_set_from_dict,
)
from arctic_route_planning.publishing.layered_store import (
    LayeredRoutePlanLatestStore,
    LayeredStoreSnapshot,
)
from arctic_route_planning.replanning import (
    PlanningCoordinator,
    ReplanTriggerEvaluator,
    RouteSwitchGate,
)
from arctic_route_risk import PersistentRiskStore

from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.replay.route_integrity import audit_route

_SCRIPT_VERSION = "winter-p2-shadow.v4"
_CANDIDATE_VERSION = "temporal-label-astar.shadow.v1"
_CONTROL_TRACE_VERSION = "time-dependent-a-star.control-trace.v1"
_ORDER_VALUES = ("control-first", "candidate-first", "alternate")
_CANDIDATE_MODE_VALUES = ("exact-temporal", "control-trace")
_RSS_MODE_VALUES = ("in-process", "isolated")
_ISOLATION_VALUES = ("per-track", "per-unit-phase")
_EVIDENCE_MODE_VALUES = ("auto", "diagnostic", "screening", "formal")
_DIAGNOSTIC_PROFILE_VALUES = (
    "baseline",
    "force-main-cold",
    "post-main-normalize",
    "trace-release-only",
)
_CONTROL_TRACE_LAYER_NAMES = (
    "full_voyage",
    "main_corridor_24_72h",
    "rolling_0_24h",
    "executable_0_6h",
)
_ETA_TOLERANCE_SECONDS = 1.0
_NUMERIC_TOLERANCE = 1e-8
_M2_MIN_REPETITIONS = 3
_M2_SCREENING_REPETITIONS = 2
_M2_SCREENING_IMPROVEMENT_FLOOR_PERCENT = 10.0
_M2_TOTAL_IMPROVEMENT_FLOOR_PERCENT = 15.0
_M2_CELL_REGRESSION_CEILING_PERCENT = 5.0
_M2_P95_REGRESSION_CEILING_PERCENT = 5.0
_M2_EXPECTED_TRACE_COUNT = 3
_M2_EXPECTED_HIT_COUNT = 3
_M2_EXPECTED_COLD_COUNT = 6
_M2_TRACE_SOURCE_LAYER = "full_voyage"
_M2_TRACE_SOURCE_OVERHEAD_CEILING_PERCENT = 5.0
_TRACE_CAPTURE_STATUS = "TRACE_CAPTURED"
_TRACE_HIT_STATUSES = frozenset({"HIT_EXACT", "HIT_TRACE_EQUIVALENT"})
_RUNNER_COMPARISON_ROUTE_RUNTIME_FIELDS = frozenset(
    {
        "plan_id",
        "layer_set_id",
        "planning_request_id",
        "generated_at",
        "planner_version",
        "plan_version",
        "reference_plan_id",
    }
)
_RUNNER_COMPARISON_METRIC_RUNTIME_FIELDS = frozenset({"compute_ms", "expanded_nodes"})
_RUNNER_COMPARISON_SET_RUNTIME_FIELDS = frozenset(
    {"layer_set_id", "planning_request_id", "generated_at"}
)


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return parsed.astimezone(UTC)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_environment(repo: Path) -> dict[str, Any]:
    """Capture repository identity without changing the caller's worktree."""

    def _git(*args: str) -> str | None:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if completed.returncode != 0:
            return None
        value = completed.stdout.strip()
        return value or None

    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    ahead: int | None = None
    behind: int | None = None
    if upstream is not None:
        counts = _git("rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if counts is not None:
            try:
                ahead, behind = (int(part) for part in counts.split())
            except ValueError:
                ahead = behind = None
    return {
        "path": str(repo),
        "commit": _git("rev-parse", "HEAD"),
        "branch": _git("branch", "--show-current"),
        "dirty": bool(_git("status", "--porcelain")),
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
    }


def _swap_counters() -> dict[str, int] | None:
    """Read cumulative kernel swap-in/out counters when available."""

    path = Path("/proc/vmstat")
    if not path.exists():
        return None
    counters: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[0] not in {"pswpin", "pswpout"}:
                continue
            value = int(parts[1])
            if value < 0:
                return None
            counters[parts[0]] = value
    except (OSError, ValueError):
        return None
    return counters if set(counters) == {"pswpin", "pswpout"} else None


def _process_swap_kib(pid: int | None = None) -> int | None:
    """Read a process' resident swap amount from ``/proc`` when available.

    The kernel counters above are host-wide and cumulative.  This process
    scoped value makes the isolated worker evidence explicit and prevents a
    missing host counter from being mistaken for a zero-swap observation.
    """

    try:
        process_id = os.getpid() if pid is None else int(pid)
    except (TypeError, ValueError):
        return None
    path = Path(f"/proc/{process_id}/status")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("VmSwap:"):
                continue
            parts = line.split()
            if len(parts) < 2:
                return None
            value = int(parts[1])
            if value < 0:
                return None
            unit = parts[2].lower() if len(parts) >= 3 else "kb"
            if unit in {"kb", "kib"}:
                return value
            if unit in {"mb", "mib"}:
                return value * 1024
            return None
    except (OSError, ValueError, IndexError):
        return None
    return None


def _pin_worker_cpu() -> int | None:
    """Pin a worker to the lowest available CPU for paired measurements."""

    if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
        return None
    try:
        available = sorted(os.sched_getaffinity(0))
        if not available:
            return None
        cpu = int(available[0])
        os.sched_setaffinity(0, {cpu})
        return cpu
    except (OSError, TypeError, ValueError):
        return None


def _worker_cpu_affinity() -> list[int] | None:
    """Return the worker's effective CPU affinity as JSON-safe data."""

    if not hasattr(os, "sched_getaffinity"):
        return None
    try:
        return sorted(int(cpu) for cpu in os.sched_getaffinity(0))
    except (OSError, TypeError, ValueError):
        return None


def _swap_delta(
    before: dict[str, int] | None,
    after: dict[str, int] | None,
) -> dict[str, int] | None:
    if before is None or after is None:
        return None
    names = ("pswpin", "pswpout")
    if set(before) != set(names) or set(after) != set(names):
        return None
    values: dict[str, int] = {}
    for name in names:
        before_value = before[name]
        after_value = after[name]
        if (
            isinstance(before_value, bool)
            or not isinstance(before_value, int)
            or before_value < 0
            or isinstance(after_value, bool)
            or not isinstance(after_value, int)
            or after_value < 0
            or after_value < before_value
        ):
            return None
        values[name] = after_value - before_value
    return values


def _process_swap_delta(
    before_kib: int | None,
    after_kib: int | None,
) -> int | None:
    if (
        before_kib is None
        or after_kib is None
        or isinstance(before_kib, bool)
        or not isinstance(before_kib, int)
        or isinstance(after_kib, bool)
        or not isinstance(after_kib, int)
        or before_kib < 0
        or after_kib < 0
        or after_kib < before_kib
    ):
        return None
    return after_kib - before_kib


def _swap_measurement_status(
    *,
    swap_before: dict[str, int] | None,
    swap_after: dict[str, int] | None,
    process_swap_before_kib: int | None,
    process_swap_after_kib: int | None,
) -> str:
    """Classify swap evidence without turning unavailable data into zero."""

    if (
        swap_before is None
        or swap_after is None
        or process_swap_before_kib is None
        or process_swap_after_kib is None
    ):
        return "NOT_MEASURED"
    host_delta = _swap_delta(swap_before, swap_after)
    process_delta = _process_swap_delta(
        process_swap_before_kib,
        process_swap_after_kib,
    )
    if host_delta is None or process_delta is None:
        return "FAIL"
    if any(host_delta[name] != 0 for name in ("pswpin", "pswpout")) or process_delta != 0:
        return "FAIL"
    return "PASS"


def _valid_swap_delta(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"pswpin", "pswpout"}:
        return False
    return all(
        isinstance(value[name], int)
        and not isinstance(value[name], bool)
        and value[name] >= 0
        for name in ("pswpin", "pswpout")
    )


def _valid_process_swap_delta(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _swap_measurement_complete(resources: Mapping[str, Any]) -> bool:
    measurement = resources.get("swap_measurement")
    return (
        isinstance(measurement, Mapping)
        and measurement.get("status") == "PASS"
        and _valid_swap_delta(resources.get("swap_delta"))
        and _valid_process_swap_delta(resources.get("process_swap_delta_kib"))
    )


def _swap_observation_pass(resources: Mapping[str, Any]) -> bool:
    if not _swap_measurement_complete(resources):
        return False
    host_delta = resources["swap_delta"]
    return (
        host_delta["pswpin"] == 0
        and host_delta["pswpout"] == 0
        and resources["process_swap_delta_kib"] == 0
    )


def _cpu_measurement_status(
    *,
    cpu_pin_cpu: Any,
    cpu_pin_succeeded: Any,
    cpu_affinity: Any,
) -> str:
    if cpu_pin_succeeded is None or cpu_pin_cpu is None or cpu_affinity is None:
        return "NOT_MEASURED"
    if (
        cpu_pin_succeeded is not True
        or isinstance(cpu_pin_cpu, bool)
        or not isinstance(cpu_pin_cpu, int)
        or not isinstance(cpu_affinity, list)
        or cpu_affinity != [cpu_pin_cpu]
    ):
        return "FAIL"
    return "PASS"


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if percentile == 0.5:
        return float(median(ordered))
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _risk_query(commit: dict[str, Any]) -> RiskWindowQuery:
    return RiskWindowQuery(
        start=_parse_utc(commit["start"]),
        end=_parse_utc(commit["end"]),
        interval=timedelta(seconds=int(commit["interval_seconds"])),
        run_id=commit["run_id"],
        scenario_id=commit["scenario_id"],
        corridor_id=commit["corridor_id"],
        generation_id=commit["generation_id"],
        vessel_profile_id=commit["vessel_profile_id"],
        config_digest=commit["config_digest"],
        model_config_digest=commit["model_config_digest"],
        as_of=_parse_utc(commit["as_of"]),
    )


def _empty_output_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"output path is not a directory: {path}")
        if any(path.iterdir()):
            raise ValueError(
                f"P2 shadow requires a new empty experiment directory; refusing overwrite: {path}"
            )
    else:
        path.mkdir(parents=True, exist_ok=False)


def _validate_order(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
        "control,candidate": "control-first",
        "candidate,control": "candidate-first",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in _ORDER_VALUES:
        raise ValueError(
            "execution-order must be control-first, candidate-first, alternate, "
            "control,candidate, or candidate,control"
        )
    return normalized


def _order_for(repetition: int, requested: str) -> str:
    if requested == "alternate":
        return "control-first" if repetition % 2 else "candidate-first"
    return requested


def _validate_candidate_mode(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized not in _CANDIDATE_MODE_VALUES:
        raise ValueError("candidate-mode must be exact-temporal or control-trace")
    return normalized


def _validate_rss_mode(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized not in _RSS_MODE_VALUES:
        raise ValueError("rss-mode must be in-process or isolated")
    return normalized


def _validate_evidence_mode(value: str) -> str:
    normalized = str(value).strip().lower().replace("_", "-")
    if normalized not in _EVIDENCE_MODE_VALUES:
        raise ValueError(
            "evidence-mode must be auto, diagnostic, screening, or formal"
        )
    return normalized


def _validate_diagnostic_profile(value: str) -> str:
    normalized = str(value).strip().lower().replace("_", "-")
    if normalized not in _DIAGNOSTIC_PROFILE_VALUES:
        raise ValueError(
            "diagnostic-profile must be baseline, force-main-cold, "
            "post-main-normalize, or trace-release-only"
        )
    return normalized


def _effective_evidence_mode(args: argparse.Namespace) -> str:
    mode = _validate_evidence_mode(getattr(args, "evidence_mode", "auto"))
    if mode != "auto":
        return mode
    if args.repetitions == _M2_SCREENING_REPETITIONS:
        return "screening"
    if args.repetitions >= _M2_MIN_REPETITIONS:
        return "formal"
    return "auto"


def _mode_metadata(candidate_mode: str) -> dict[str, str]:
    candidate_mode = _validate_candidate_mode(candidate_mode)
    if candidate_mode == "control-trace":
        return {
            "candidate_algorithm": _CONTROL_TRACE_VERSION,
            "candidate_schema": "orchestrator.winter-p2-control-trace-candidate.v1",
            "sidecar_schema": "orchestrator.winter-p2-control-trace-sidecar.v1",
            "p2_reuse_claim": "SHADOW_ONLY_CONTROL_TRACE_REUSE",
        }
    return {
        "candidate_algorithm": _CANDIDATE_VERSION,
        "candidate_schema": "orchestrator.winter-p2-exact-temporal-candidate.v1",
        "sidecar_schema": "orchestrator.winter-p2-reuse-sidecar.v1",
        "p2_reuse_claim": "SHADOW_ONLY_CERTIFICATE_REUSE",
    }


def _validate_identity(
    *,
    commit: dict[str, Any],
    spec: ExecutionSpec,
    run_context: Any,
    query: RiskWindowQuery,
) -> None:
    if spec.planning_contract != "cd.four-layer-route-plan-set.v3":
        raise ValueError("Winter P2 shadow requires the formal v3 planning contract")
    expected = {
        "run_id": query.run_id,
        "scenario_id": query.scenario_id,
        "generation_id": query.generation_id,
        "input_revision": commit.get("input_revision", spec.input_revision),
        "spec.run_id": spec.run_id,
        "spec.scenario_id": spec.scenario_id,
        "spec.generation_id": spec.generation_id,
        "spec.input_revision": spec.input_revision,
        "run_context.run_id": run_context.run_id,
        "run_context.scenario_id": run_context.scenario_id,
        "run_context.config_digest": run_context.config_digest,
    }
    actual = {
        "run_id": commit["run_id"],
        "scenario_id": commit["scenario_id"],
        "generation_id": commit["generation_id"],
        "input_revision": spec.input_revision,
        "spec.run_id": query.run_id,
        "spec.scenario_id": query.scenario_id,
        "spec.generation_id": query.generation_id,
        "spec.input_revision": spec.input_revision,
        "run_context.run_id": query.run_id,
        "run_context.scenario_id": query.scenario_id,
        "run_context.config_digest": query.config_digest,
    }
    mismatched = [
        name for name, expected_value in expected.items() if actual[name] != expected_value
    ]
    if mismatched:
        raise ValueError("formal Winter identity mismatch: " + ", ".join(mismatched))


def _private_window(current: CommittedRiskWindow, query: RiskWindowQuery) -> CommittedRiskWindow:
    private_frames = tuple(
        risk_frame_from_document(risk_frame_to_document(frame)) for frame in current.frames
    )
    private = CommittedRiskWindow.create(query, private_frames)
    if private.commit_id != current.commit_id or private.content_digest != current.content_digest:
        raise ValueError("committed RiskWindow changed during shadow preparation")
    return private


@dataclass(frozen=True, slots=True)
class _PreparedShadow:
    spec: ExecutionSpec
    commit: dict[str, Any]
    commit_path: Path
    query: RiskWindowQuery
    run_context: Any
    configuration: Any
    endpoint_mapping: Any
    request: ServicePlanningRequest
    store: PersistentRiskStore
    prepared: Any
    input_identity: dict[str, Any]


class _CandidateAdapter:
    """Adapt P1 sessions and P2 certificate reuse to C's shadow protocol.

    The layered service supplies one request per layer.  A source certificate
    is therefore cached by ``(objective, goal)`` and is built with the complete
    run horizon.  A shorter layer request for the same goal is a legitimate
    monotonic tightening; other goals remain independent cold searches.
    """

    def __init__(
        self,
        planner: TemporalLabelAStar,
        *,
        window: CommittedRiskWindow,
        input_revision: int,
        maximum_elapsed: timedelta,
    ) -> None:
        self.planner = planner
        self.window = window
        self.input_revision = input_revision
        self.maximum_elapsed = maximum_elapsed
        self.records: list[dict[str, Any]] = []
        self._certificates: dict[tuple[ObjectiveMode, tuple[int, int]], TemporalCertifiedGoal] = {}

    @property
    def risk_identity(self) -> Any:
        return self.planner.risk_identity

    @property
    def risk_as_of_times(self) -> tuple[datetime, ...]:
        return self.planner.risk_as_of_times

    @property
    def planner_config(self) -> Any:
        return self.planner.planner_config

    def _identity(self, request: PlanningRequest) -> TemporalSessionIdentity:
        return TemporalSessionIdentity.from_planner(
            self.planner,
            request,
            input_revision=self.input_revision,
            risk_window_content_digest=self.window.content_digest,
            risk_window_commit_id=self.window.commit_id,
        )

    def _certificate_for(
        self,
        request: PlanningRequest,
    ) -> tuple[TemporalCertifiedGoal, dict[str, Any]]:
        key = (request.objective, request.goal)
        cached = self._certificates.get(key)
        if cached is not None:
            return cached, {
                "cached": True,
                "source_session_id": cached.checkpoint.identity.session_id,
                "source_wall_ms": 0.0,
                "source_diagnostics": asdict(cached.result.diagnostics),
            }
        source_request = replace(request, maximum_elapsed=self.maximum_elapsed)
        started = time.perf_counter()
        source_session = self.planner.create_session(
            source_request,
            identity=self._identity(source_request),
        )
        source_result = self.planner.advance_session(source_session)
        if source_result is None:
            raise RuntimeError("P2 source session did not reach a terminal result")
        certificate = certify_session(source_session)
        self._certificates[key] = certificate
        return certificate, {
            "cached": False,
            "source_session_id": source_session.session_id,
            "source_wall_ms": (time.perf_counter() - started) * 1000.0,
            "source_diagnostics": asdict(source_result.diagnostics),
        }

    @staticmethod
    def _certificate_document(certificate: TemporalCertifiedGoal) -> dict[str, Any]:
        proof = certificate.certificate
        return {
            "status": proof.certificate_status,
            "U": proof.U,
            "LB": proof.LB,
            "epsilon": proof.epsilon,
            "open_termination": proof.open_termination.value,
            "state_digest": proof.state_digest,
            "route_digest": proof.route_digest,
            "source_constraints": asdict(proof.source_constraints),
        }

    def _cold_search(
        self,
        request: PlanningRequest,
    ) -> tuple[Any, Any, float]:
        started = time.perf_counter()
        session = self.planner.create_session(request, identity=self._identity(request))
        result = self.planner.advance_session(session)
        if result is None:
            raise RuntimeError("candidate session did not reach a terminal result")
        return result, session, (time.perf_counter() - started) * 1000.0

    def plan_candidates(
        self,
        request: PlanningRequest,
        objectives: tuple[ObjectiveMode, ...],
    ) -> dict[ObjectiveMode, Any]:
        results: dict[ObjectiveMode, Any] = {}
        for objective in objectives:
            core_request = replace(request, objective=objective)
            source_started = time.perf_counter()
            reuse_outcome: TemporalReuseOutcome | None = None
            source_info: dict[str, Any] = {}
            source_error: str | None = None
            try:
                certificate, source_info = self._certificate_for(core_request)
                reuse_outcome = try_reuse(certificate, self.planner, core_request)
            except Exception as error:
                source_error = f"{type(error).__name__}: {error}"

            target_search_ms = 0.0
            if reuse_outcome is not None and reuse_outcome.hit:
                candidate_result = reuse_outcome.result
                if candidate_result is None:
                    raise RuntimeError("P2 reported a hit without a candidate result")
                session = None
                search_used = False
            else:
                candidate_result, session, target_search_ms = self._cold_search(core_request)
                search_used = True

            results[objective] = candidate_result.planning_result
            if reuse_outcome is None:
                lookup_status = "CERTIFICATE_ERROR"
                reason = f"SOURCE_CERTIFICATE_ERROR: {source_error}"
                certificate_document = None
                source_session_id = source_info.get("source_session_id")
            else:
                raw_status = reuse_outcome.status.value
                lookup_status = raw_status
                reason = reuse_outcome.fallback_reason
                certificate_document = (
                    self._certificate_document(certificate)
                    if source_info.get("source_session_id") is not None
                    else None
                )
                source_session_id = source_info.get("source_session_id")
            source_wall_ms = source_info.get("source_wall_ms", 0.0)
            diagnostics = asdict(candidate_result.diagnostics)
            target_identity_digest = self._identity(core_request).session_id
            status = lookup_status if not search_used else "COLD_CANDIDATE"
            self.records.append(
                {
                    "objective": objective.value,
                    "source_goal": list(core_request.goal),
                    "target_goal": list(core_request.goal),
                    "source_session_id": source_session_id,
                    "source_identity_digest": source_session_id,
                    "target_identity_digest": target_identity_digest,
                    "target_constraints": {
                        "maximum_elapsed_seconds": (
                            core_request.maximum_elapsed.total_seconds()
                            if core_request.maximum_elapsed is not None
                            else None
                        ),
                        "maximum_risk": core_request.maximum_risk,
                    },
                    "session_id": (
                        session.session_id if session is not None else source_session_id
                    ),
                    "session_state": (
                        session.state.value if session is not None else "GOAL_CERTIFIED"
                    ),
                    "reuse_status": status,
                    "reuse_lookup_status": lookup_status,
                    "reuse_hit": bool(reuse_outcome is not None and reuse_outcome.hit),
                    "reuse_reason": reason,
                    "search_used": search_used,
                    "source_cached": source_info.get("cached", False),
                    "source_wall_ms": source_wall_ms,
                    "reuse_validation_ms": (time.perf_counter() - source_started) * 1000.0,
                    "target_search_ms": target_search_ms,
                    "zero_search_metrics": (
                        {"expanded_labels": 0, "edge_evaluations": 0} if not search_used else None
                    ),
                    "certificate": certificate_document,
                    "source_diagnostics": source_info.get("source_diagnostics"),
                    "wall_ms": source_wall_ms + target_search_ms,
                    "planning_result_metrics": asdict(candidate_result.planning_result.metrics),
                    "candidate_diagnostics": diagnostics,
                }
            )
        return results


class _ControlTraceAdapter:
    """Run traced control full once, then reuse only into the main layer.

    The adapter intentionally follows the four-layer service call order.  The
    first call is the full-voyage source; only the second call, when the goal
    is unchanged, may query the P2.1 control-trace certificate.  Other layers
    remain explicit cold control searches.
    """

    def __init__(
        self,
        planner: TimeDependentAStar,
        *,
        window: CommittedRiskWindow,
        input_revision: int,
        generation_id: int,
    ) -> None:
        self.planner = planner
        self.window = window
        self.input_revision = input_revision
        self.generation_id = generation_id
        self.layer_index = 0
        self._full_traces: dict[tuple[ObjectiveMode, tuple[int, int]], Any] = {}
        self.records: list[dict[str, Any]] = []

    @property
    def risk_identity(self) -> Any:
        return self.planner.risk_identity

    @property
    def risk_as_of_times(self) -> tuple[datetime, ...]:
        return self.planner.risk_as_of_times

    @property
    def planner_config(self) -> Any:
        return self.planner.planner_config

    def _identity(self, request: PlanningRequest) -> dict[str, Any]:
        """Bind external window/generation and immutable request identity."""

        return {
            "risk_window_commit_id": self.window.commit_id,
            "risk_window_content_digest": self.window.content_digest,
            "generation_id": self.generation_id,
            "input_revision": self.input_revision,
            "planner_request_identity": {
                "start": request.start,
                "goal": request.goal,
                "departure_time": request.departure_time,
                "objective": request.objective.value,
                "time_bucket_seconds": request.time_bucket_size.total_seconds(),
                "edge_sample_count": request.edge_sample_count,
                "use_heuristic": request.use_heuristic,
                "max_expansions": request.max_expansions,
            },
        }

    @staticmethod
    def _trace_document(trace: Any) -> dict[str, Any]:
        identity = getattr(trace, "identity", None)
        return {
            "status": "CERTIFIED_TRACE",
            "digest": getattr(trace, "digest", getattr(trace, "trace_digest", None)),
            "identity_digest": getattr(identity, "digest", None),
            "write_count": getattr(trace, "write_count", getattr(trace, "count", None)),
            "replacement_count": getattr(trace, "replacement_count", None),
            "maximum_inserted_elapsed": getattr(trace, "maximum_inserted_elapsed", None),
            "maximum_inserted_path_edge_risk": getattr(
                trace,
                "maximum_inserted_path_edge_risk",
                None,
            ),
            "source_route_digest": getattr(trace, "source_route_digest", None),
            "termination": getattr(trace, "termination", None),
            "route_elapsed_seconds": getattr(trace, "route_elapsed_seconds", None),
            "route_max_edge_risk": getattr(trace, "route_max_edge_risk", None),
        }

    def _record(
        self,
        *,
        layer_index: int,
        layer_name: str,
        objective_request: PlanningRequest,
        result: Any,
        search_used: bool,
        status: str,
        lookup_status: str,
        reason: str,
        elapsed_ms: float,
        trace: Any | None,
        source_wall_ms: float = 0.0,
        reuse_validation_ms: float = 0.0,
        zero_search_metrics: dict[str, int] | None = None,
    ) -> None:
        trace_document = self._trace_document(trace) if trace is not None else None
        self.records.append(
            {
                "candidate_mode": "control-trace",
                "candidate_algorithm": _CONTROL_TRACE_VERSION,
                "layer_index": layer_index,
                "layer": layer_name,
                "objective": objective_request.objective.value,
                "source_goal": list(objective_request.goal),
                "target_goal": list(objective_request.goal),
                "source_trace_digest": (
                    trace_document.get("digest") if trace_document is not None else None
                ),
                "target_identity_digest": (
                    getattr(getattr(trace, "identity", None), "digest", None)
                    if trace is not None
                    else None
                ),
                "reuse_status": status,
                "reuse_lookup_status": lookup_status,
                "reuse_hit": status in {"HIT_EXACT", "HIT_TRACE_EQUIVALENT"},
                "reuse_reason": reason,
                "search_used": search_used,
                "source_cached": trace is not None,
                "source_wall_ms": source_wall_ms,
                "reuse_validation_ms": reuse_validation_ms,
                "target_search_ms": elapsed_ms if search_used else 0.0,
                "zero_search_metrics": zero_search_metrics,
                "certificate": trace_document,
                "source_diagnostics": None,
                "candidate_diagnostics": (
                    asdict(result.metrics) if search_used else None
                ),
                "planning_result_metrics": asdict(result.metrics),
                "wall_ms": source_wall_ms + reuse_validation_ms + elapsed_ms,
            }
        )

    def plan_candidates(
        self,
        request: PlanningRequest,
        objectives: tuple[ObjectiveMode, ...],
    ) -> dict[ObjectiveMode, Any]:
        layer_index = self.layer_index
        self.layer_index += 1
        layer_name = (
            _CONTROL_TRACE_LAYER_NAMES[layer_index]
            if layer_index < len(_CONTROL_TRACE_LAYER_NAMES)
            else f"layer_{layer_index}"
        )
        results: dict[ObjectiveMode, Any] = {}
        for objective in objectives:
            core_request = replace(request, objective=objective)
            key = (objective, core_request.goal)
            external_identity = self._identity(core_request)
            trace = self._full_traces.get(key)

            if layer_index == 0:
                started = time.perf_counter()
                result, trace = control_trace_plan(
                    self.planner,
                    core_request,
                    identity=external_identity,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if trace is None:  # pragma: no cover - API contract guard
                    raise RuntimeError("control trace planner returned no completed trace")
                self._full_traces[key] = trace
                self._record(
                    layer_index=layer_index,
                    layer_name=layer_name,
                    objective_request=core_request,
                    result=result,
                    search_used=True,
                    status="TRACE_CAPTURED",
                    lookup_status="TRACE_CAPTURED",
                    reason="FULL_SOURCE_TRACE",
                    elapsed_ms=0.0,
                    trace=trace,
                    source_wall_ms=elapsed_ms,
                )
                results[objective] = result
                continue

            same_goal = trace is not None and core_request.goal == trace.identity.goal
            trace_transition = layer_index == 1 and same_goal
            if trace_transition:
                lookup_started = time.perf_counter()
                outcome = try_control_trace_reuse(
                    trace,
                    self.planner,
                    core_request,
                    identity=external_identity,
                )
                lookup_ms = (time.perf_counter() - lookup_started) * 1000.0
                if outcome.hit and outcome.result is not None:
                    result = outcome.result
                    self._record(
                        layer_index=layer_index,
                        layer_name=layer_name,
                        objective_request=core_request,
                        result=result,
                        search_used=False,
                        status=outcome.status.value,
                        lookup_status=outcome.status.value,
                        reason=outcome.reason.value,
                        elapsed_ms=0.0,
                        trace=outcome.trace or trace,
                        source_wall_ms=0.0,
                        reuse_validation_ms=lookup_ms,
                        zero_search_metrics={
                            "expanded_states": 0,
                            "edge_evaluations": 0,
                        },
                    )
                    results[objective] = result
                    continue

                started = time.perf_counter()
                result = self.planner.plan(core_request)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                self._record(
                    layer_index=layer_index,
                    layer_name=layer_name,
                    objective_request=core_request,
                    result=result,
                    search_used=True,
                    status="FALLBACK_CONTROL",
                    lookup_status=outcome.status.value,
                    reason=outcome.reason.value,
                    elapsed_ms=elapsed_ms,
                    trace=outcome.trace or trace,
                    reuse_validation_ms=lookup_ms,
                )
                results[objective] = result
                continue

            started = time.perf_counter()
            result = self.planner.plan(core_request)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            reason = (
                "TARGET_GOAL_DIFFERS_FROM_FULL"
                if trace is None or core_request.goal != trace.identity.goal
                else "TRACE_REUSE_LIMITED_TO_FULL_MAIN"
            )
            self._record(
                layer_index=layer_index,
                layer_name=layer_name,
                objective_request=core_request,
                result=result,
                search_used=True,
                status="COLD_CONTROL",
                lookup_status="NOT_ATTEMPTED",
                reason=reason,
                elapsed_ms=elapsed_ms,
                trace=trace,
            )
            results[objective] = result
        return results


def _service(
    planner: Any,
    *,
    request: ServicePlanningRequest,
    configuration: Any,
    planner_version: str,
) -> FourLayerPlanningService:
    coordinator = PlanningCoordinator()
    store = LayeredRoutePlanLatestStore()
    return FourLayerPlanningService(
        planner,
        planner_config=configuration.planner,
        coordinator=coordinator,
        store=store,
        switch_gate=RouteSwitchGate(),
        trigger_evaluator=ReplanTriggerEvaluator(),
        clock=lambda: request.start_time,
        planner_version=planner_version,
    )


def _plan_set_document(outcome: FourLayerPlanningOutcome) -> dict[str, Any]:
    document = four_layer_route_plan_set_to_dict(outcome.plan_set)
    # Strictly parse the result before writing it as evidence.  This does not
    # publish it to a formal store.
    if four_layer_route_plan_set_from_dict(document) != outcome.plan_set:
        raise ValueError("shadow plan set failed strict codec round-trip")
    return document


def _plan_pairs(control: Any, candidate: Any) -> Iterable[tuple[str, str, Any, Any]]:
    for control_bundle, candidate_bundle in zip(
        control.layers,
        candidate.layers,
        strict=True,
    ):
        if control_bundle.planning_layer is not candidate_bundle.planning_layer:
            raise ValueError("control/candidate layer order differs")
        for objective in ObjectiveMode:
            yield (
                control_bundle.planning_layer.value,
                objective.value,
                control_bundle.plans[objective],
                candidate_bundle.plans[objective],
            )


def _waypoint_signature(plan: Any) -> tuple[tuple[float, float, str], ...]:
    return tuple(
        (
            waypoint.longitude,
            waypoint.latitude,
            waypoint.eta.isoformat(),
        )
        for waypoint in plan.waypoints
    )


def _runner_comparison_route_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one route for cross-planner semantic comparison.

    The formal C digest intentionally retains some publication/runtime
    identity fields.  This runner compares two independent algorithms, so
    those fields would create a false route mismatch even when all business
    route fields are identical.  Reference identity values are omitted, but
    their None/non-None structure is checked by ``_plan_comparison``.
    """

    normalized = dict(document)
    for key in _RUNNER_COMPARISON_ROUTE_RUNTIME_FIELDS:
        normalized.pop(key, None)
    metrics = normalized.get("metrics")
    if isinstance(metrics, Mapping):
        normalized["metrics"] = {
            key: value
            for key, value in metrics.items()
            if key not in _RUNNER_COMPARISON_METRIC_RUNTIME_FIELDS
        }
    return normalized


def _runner_comparison_plan_set_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a four-layer plan-set with the same route policy."""

    normalized = dict(document)
    for key in _RUNNER_COMPARISON_SET_RUNTIME_FIELDS:
        normalized.pop(key, None)
    layers = normalized.get("layers")
    if isinstance(layers, list):
        normalized["layers"] = []
        for layer in layers:
            if not isinstance(layer, Mapping):
                normalized["layers"].append(layer)
                continue
            normalized_layer = dict(layer)
            plans = normalized_layer.get("plans")
            if isinstance(plans, Mapping):
                normalized_layer["plans"] = {
                    objective: _runner_comparison_route_document(plan)
                    if isinstance(plan, Mapping)
                    else plan
                    for objective, plan in plans.items()
                }
            normalized["layers"].append(normalized_layer)
    return normalized


def _runner_comparison_route_digest(plan: Any) -> str:
    return _canonical_digest(
        _runner_comparison_route_document(route_plan_v3_to_dict(plan))
    )


def _plan_comparison(control: Any, candidate: Any) -> dict[str, Any]:
    control_doc = four_layer_route_plan_set_to_dict(control)
    candidate_doc = four_layer_route_plan_set_to_dict(candidate)
    pairs: list[dict[str, Any]] = []
    for layer, objective, control_plan, candidate_plan in _plan_pairs(control, candidate):
        control_metrics = control_plan.metrics
        candidate_metrics = candidate_plan.metrics
        eta_deltas = [
            abs((left.eta - right.eta).total_seconds())
            for left, right in zip(control_plan.waypoints, candidate_plan.waypoints, strict=True)
        ]
        metric_deltas = {
            "distance_km": abs(control_metrics.distance_km - candidate_metrics.distance_km),
            "eta_hours": abs(control_metrics.eta_hours - candidate_metrics.eta_hours),
            "avg_risk": abs(control_metrics.avg_risk - candidate_metrics.avg_risk),
            "max_risk": abs(control_metrics.max_risk - candidate_metrics.max_risk),
            "objective_cost": abs(
                control_metrics.objective_cost - candidate_metrics.objective_cost
            ),
            "minimum_confidence": abs(
                control_metrics.minimum_confidence - candidate_metrics.minimum_confidence
            ),
        }
        speed_deltas = [
            abs(left.recommended_speed_mps - right.recommended_speed_mps)
            for left, right in zip(control_plan.waypoints, candidate_plan.waypoints, strict=True)
        ]
        control_route_digest = _runner_comparison_route_digest(control_plan)
        candidate_route_digest = _runner_comparison_route_digest(candidate_plan)
        reference_structure_equal = (
            control_plan.reference_plan_id is None
        ) == (candidate_plan.reference_plan_id is None)
        route_equal = (
            _waypoint_signature(control_plan) == _waypoint_signature(candidate_plan)
            and max(speed_deltas, default=0.0) <= _NUMERIC_TOLERANCE
            and control_plan.source_risk_ids == candidate_plan.source_risk_ids
            and control_plan.destination_reached == candidate_plan.destination_reached
            and control_plan.layer_goal_reached == candidate_plan.layer_goal_reached
            and control_metrics.hard_constraint_violations
            == candidate_metrics.hard_constraint_violations
            and reference_structure_equal
            and control_route_digest == candidate_route_digest
            and max(eta_deltas, default=0.0) <= _ETA_TOLERANCE_SECONDS
            and all(delta <= _NUMERIC_TOLERANCE for delta in metric_deltas.values())
        )
        pairs.append(
            {
                "layer": layer,
                "objective": objective,
                "status": "PASS" if route_equal else "FAIL",
                "control_route_digest": control_route_digest,
                "candidate_route_digest": candidate_route_digest,
                "control_waypoint_count": len(control_plan.waypoints),
                "candidate_waypoint_count": len(candidate_plan.waypoints),
                "max_eta_delta_seconds": max(eta_deltas, default=0.0),
                "max_speed_delta_mps": max(speed_deltas, default=0.0),
                "metric_deltas": metric_deltas,
                "waypoints_equal": _waypoint_signature(control_plan)
                == _waypoint_signature(candidate_plan),
                "source_risk_ids_equal": control_plan.source_risk_ids
                == candidate_plan.source_risk_ids,
                "hard_constraint_violations_equal": control_metrics.hard_constraint_violations
                == candidate_metrics.hard_constraint_violations,
                "reference_plan_structure_equal": reference_structure_equal,
                "layer_goal_reached_equal": control_plan.layer_goal_reached
                == candidate_plan.layer_goal_reached,
                "route_digest_equal": control_route_digest == candidate_route_digest,
                "allowed_runtime_fields": [
                    "compute_ms",
                    "expanded_nodes",
                    "planner_version",
                    "plan_id",
                    "layer_set_id",
                    "planning_request_id",
                    "generated_at",
                ],
            }
        )
    return {
        "status": "PASS" if all(item["status"] == "PASS" for item in pairs) else "FAIL",
        "pair_count": len(pairs),
        "pairs": pairs,
        "control_plan_set_digest": _canonical_digest(
            _runner_comparison_plan_set_document(control_doc)
        ),
        "candidate_plan_set_digest": _canonical_digest(
            _runner_comparison_plan_set_document(candidate_doc)
        ),
        "comparison_policy": {
            "eta_tolerance_seconds": _ETA_TOLERANCE_SECONDS,
            "numeric_tolerance": _NUMERIC_TOLERANCE,
            "compute_and_expansion_differences_are_diagnostic_only": True,
            "runtime_fields_ignored": sorted(_RUNNER_COMPARISON_ROUTE_RUNTIME_FIELDS),
            "metric_runtime_fields_ignored": sorted(
                _RUNNER_COMPARISON_METRIC_RUNTIME_FIELDS
            ),
            "reference_plan_id_policy": (
                "None/non-None structure must match; identity value ignored"
            ),
        },
    }


def _integrity_document(plan_set: Any, frames: tuple[Any, ...], track: str) -> dict[str, Any]:
    results = []
    for bundle in plan_set.layers:
        for objective, plan in bundle.plans.items():
            result = audit_route(plan, frames)
            results.append(
                {
                    "track": track,
                    "layer": bundle.planning_layer.value,
                    "objective": objective.value,
                    **result,
                }
            )
    return {
        "status": "PASS" if all(item["status"] == "PASS" for item in results) else "FAIL",
        "track": track,
        "route_count": len(results),
        "routes": results,
    }


def _timing_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or record.get("_shadow_metadata"):
            continue
        layer = record.get("layer")
        if layer is None and isinstance(record.get("layer_index"), int):
            index = int(record["layer_index"])
            layer = (
                _CONTROL_TRACE_LAYER_NAMES[index]
                if 0 <= index < len(_CONTROL_TRACE_LAYER_NAMES)
                else None
            )
        objective = record.get("objective")
        wall_ms = record.get("wall_ms", record.get("elapsed_ms"))
        if not isinstance(layer, str) or not isinstance(objective, str):
            continue
        if not isinstance(wall_ms, (int, float)):
            continue
        row = {
            "layer": layer,
            "objective": objective,
            "wall_ms": float(wall_ms),
        }
        # The formal C timing row carries these fields.  Keep them in the
        # normalized runner row so a sidecar cannot make a zero-work claim
        # without the actual timing observation proving it.
        for key, aliases in {
            "expanded": ("expanded", "expanded_states", "expanded_labels"),
            "edge": ("edge", "edge_evaluations"),
            "search_used": ("search_used",),
            "trace_status": ("trace_status",),
            "reuse_status": ("reuse_status",),
            "route_digest": ("route_digest",),
            "pre_ms": ("pre_ms",),
            "planner_ms": ("planner_ms",),
            "post_ms": ("post_ms",),
            "trace_context_present": ("trace_context_present",),
            "trace_reuse_used": ("trace_reuse_used",),
            "state_counts": ("state_counts",),
            "identity_digest": ("identity_digest",),
            "identity_summary": ("identity_summary",),
            "edge_geometry_cache_before": ("edge_geometry_cache_before",),
            "edge_geometry_cache_after": ("edge_geometry_cache_after",),
            "edge_geometry_cache_delta": ("edge_geometry_cache_delta",),
            "planner_cpu_ms": ("planner_cpu_ms",),
            "gc_count_before": ("gc_count_before",),
            "gc_count_after": ("gc_count_after",),
            "gc_collections_delta": ("gc_collections_delta",),
            "trace_state": ("trace_state",),
        }.items():
            for alias in aliases:
                if alias in record:
                    row[key] = record[alias]
                    break
        rows.append(row)
    return rows


def _timing_cell_map(records: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    rows = _timing_rows(records)
    return {(row["layer"], row["objective"]): row["wall_ms"] for row in rows}


def _diagnostic_timing_row(
    records: list[dict[str, Any]],
    *,
    layer: str,
    objective: str,
) -> dict[str, Any]:
    matches = [
        row
        for row in _timing_rows(records)
        if row["layer"] == layer and row["objective"] == objective
    ]
    if len(matches) != 1:
        raise ValueError(
            f"cold-path diagnostic expected one timing row for {layer}/{objective}, "
            f"found {len(matches)}"
        )
    return dict(matches[0])


def _cold_path_timing_decomposition(
    records: Mapping[str, list[dict[str, Any]]],
    *,
    layer: str,
    objective: str,
) -> dict[str, Any]:
    """Extract one paired control/candidate cold-path observation.

    This is intentionally a pure adapter over records already produced by the
    isolated runner.  It does not execute planners, publish routes, or apply a
    formal M2 gate.
    """

    control = _diagnostic_timing_row(
        records.get("control", []), layer=layer, objective=objective
    )
    candidate = _diagnostic_timing_row(
        records.get("candidate", []), layer=layer, objective=objective
    )
    control_wall = float(control["wall_ms"])
    candidate_wall = float(candidate["wall_ms"])
    return {
        "target": {"layer": layer, "objective": objective},
        "control": control,
        "candidate": candidate,
        "paired": {
            "wall_delta_ms": candidate_wall - control_wall,
            "candidate_over_control_percent": (
                (candidate_wall - control_wall) / control_wall * 100.0
                if control_wall > 0
                else None
            ),
            "route_digest_equal": (
                control.get("route_digest") is not None
                and control.get("route_digest") == candidate.get("route_digest")
            ),
        },
        "cold_path_observation": {
            "candidate_reuse_status": candidate.get("reuse_status"),
            "candidate_search_used": candidate.get("search_used"),
            "candidate_is_cold_search": (
                candidate.get("reuse_status") == "COLD_CONTROL"
                and candidate.get("search_used") is True
            ),
        },
    }


def _cold_path_diagnostic_summary(
    cases: list[dict[str, Any]],
    *,
    layer: str,
    objective: str,
) -> dict[str, Any]:
    """Summarize paired cold-path observations without a formal gate."""

    valid_cases = [
        case
        for case in cases
        if case.get("status") == "PASS"
        and isinstance(case.get("timing_decomposition"), dict)
        and case["timing_decomposition"].get("target")
        == {"layer": layer, "objective": objective}
        and isinstance(case["timing_decomposition"].get("control"), dict)
        and isinstance(case["timing_decomposition"].get("candidate"), dict)
    ]
    control_rows = [case["timing_decomposition"]["control"] for case in valid_cases]
    candidate_rows = [case["timing_decomposition"]["candidate"] for case in valid_cases]
    control_wall = [float(row["wall_ms"]) for row in control_rows]
    candidate_wall = [float(row["wall_ms"]) for row in candidate_rows]
    wall_deltas = [
        candidate - control
        for candidate, control in zip(candidate_wall, control_wall, strict=True)
    ]
    overheads = [
        (candidate - control) / control * 100.0
        for candidate, control in zip(candidate_wall, control_wall, strict=True)
        if control > 0
    ]

    def _series(values: list[float]) -> dict[str, float | None]:
        return {
            "median": _nearest_rank(values, 0.5),
            "p95": _nearest_rank(values, 0.95),
        }

    rss_ratios: list[float] = []
    for case in valid_cases:
        resources = case.get("track_resources", {})
        control_rss = resources.get("control", {}).get("peak_rss_kib")
        candidate_rss = resources.get("candidate", {}).get("peak_rss_kib")
        if isinstance(control_rss, (int, float)) and control_rss > 0 and isinstance(
            candidate_rss, (int, float)
        ):
            rss_ratios.append(float(candidate_rss) / float(control_rss))

    candidate_statuses: dict[str, int] = {}
    cold_search_pair_count = 0
    route_digest_equal_count = 0
    for row in candidate_rows:
        status = str(row.get("reuse_status", "UNSPECIFIED"))
        candidate_statuses[status] = candidate_statuses.get(status, 0) + 1
    for case in valid_cases:
        decomposition = case["timing_decomposition"]
        if decomposition["cold_path_observation"]["candidate_is_cold_search"]:
            cold_search_pair_count += 1
        if decomposition["paired"]["route_digest_equal"]:
            route_digest_equal_count += 1
    order_counts: dict[str, int] = {}
    for case in cases:
        order = str(case.get("execution_order", "UNSPECIFIED"))
        order_counts[order] = order_counts.get(order, 0) + 1
    return {
        "schema_version": "orchestrator.winter-cold-path-diagnostic-summary.v1",
        "diagnostic_only": True,
        "formal_gate_verdict": "NOT_APPLICABLE",
        "status": (
            "OBSERVED" if cases and len(valid_cases) == len(cases) else "INCOMPLETE"
        ),
        "target": {"layer": layer, "objective": objective},
        "sample_count": len(cases),
        "valid_pair_count": len(valid_cases),
        "execution_order_counts": order_counts,
        "candidate_reuse_status_counts": candidate_statuses,
        "cold_search_pair_count": cold_search_pair_count,
        "route_digest_equal_pair_count": route_digest_equal_count,
        "timing": {
            "control_wall_ms": _series(control_wall),
            "candidate_wall_ms": _series(candidate_wall),
            "candidate_minus_control_wall_ms": _series(wall_deltas),
            "candidate_over_control_percent": _series(overheads),
            "control_expanded": _series(
                [float(row.get("expanded", 0)) for row in control_rows]
            ),
            "candidate_expanded": _series(
                [float(row.get("expanded", 0)) for row in candidate_rows]
            ),
            "control_edge": _series(
                [float(row.get("edge", 0)) for row in control_rows]
            ),
            "candidate_edge": _series(
                [float(row.get("edge", 0)) for row in candidate_rows]
            ),
            "phase_ms": {
                phase: {
                    "control": _series(
                        [float(row.get(phase, 0.0)) for row in control_rows]
                    ),
                    "candidate": _series(
                        [float(row.get(phase, 0.0)) for row in candidate_rows]
                    ),
                    "candidate_minus_control": _series(
                        [
                            float(candidate.get(phase, 0.0))
                            - float(control.get(phase, 0.0))
                            for control, candidate in zip(
                                control_rows, candidate_rows, strict=True
                            )
                        ]
                    ),
                }
                for phase in ("pre_ms", "planner_ms", "post_ms")
            },
        },
        "rss": {
            "comparison": "independent_child_process",
            "candidate_over_control_ratio": _series(rss_ratios),
        },
    }


def _trace_source_overhead(
    cases: list[dict[str, Any]],
    *,
    required_repetitions: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Measure tracing overhead only on the full-voyage source rows.

    The candidate's first full-voyage row is the traced source.  It is paired
    with the independent control row for the same objective and repetition;
    reuse rows from later layers are deliberately excluded.
    """

    objectives = tuple(objective.value for objective in ObjectiveMode)
    summary: list[dict[str, Any]] = []
    for objective in objectives:
        control_values: list[float] = []
        trace_source_values: list[float] = []
        overhead_values: list[float] = []
        for case in cases:
            if case.get("status") != "PASS":
                continue
            control_rows = [
                row
                for row in _timing_rows(case.get("records", {}).get("control", []))
                if row["layer"] == _M2_TRACE_SOURCE_LAYER
                and row["objective"] == objective
            ]
            candidate_rows = [
                row
                for row in _timing_rows(case.get("records", {}).get("candidate", []))
                if row["layer"] == _M2_TRACE_SOURCE_LAYER
                and row["objective"] == objective
                and (
                    row.get("trace_status") == _TRACE_CAPTURE_STATUS
                    or row.get("reuse_status") == _TRACE_CAPTURE_STATUS
                )
            ]
            if len(control_rows) != 1 or len(candidate_rows) != 1:
                continue
            control_wall = float(control_rows[0]["wall_ms"])
            trace_source_wall = float(candidate_rows[0]["wall_ms"])
            if control_wall <= 0:
                continue
            control_values.append(control_wall)
            trace_source_values.append(trace_source_wall)
            overhead_values.append((trace_source_wall - control_wall) / control_wall * 100.0)
        overhead_median = _nearest_rank(overhead_values, 0.5)
        summary.append(
            {
                "layer": _M2_TRACE_SOURCE_LAYER,
                "objective": objective,
                "sample_count": len(overhead_values),
                "required_repetitions": required_repetitions,
                "control_wall_median_ms": _nearest_rank(control_values, 0.5),
                "trace_source_wall_median_ms": _nearest_rank(trace_source_values, 0.5),
                "overhead_median_percent": overhead_median,
                "ceiling_percent": _M2_TRACE_SOURCE_OVERHEAD_CEILING_PERCENT,
                "gate": (
                    "PASS"
                    if len(overhead_values) >= required_repetitions
                    and overhead_median is not None
                    and overhead_median <= _M2_TRACE_SOURCE_OVERHEAD_CEILING_PERCENT
                    else "FAIL"
                    if len(overhead_values) >= required_repetitions
                    else "NOT_MEASURED"
                ),
            }
        )
    return summary, bool(summary) and all(item["gate"] == "PASS" for item in summary)


def _reuse_timing_evidence(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate every reported trace hit against its timing row."""

    rows = _timing_rows(records)
    hit_rows = [row for row in rows if row.get("reuse_status") in _TRACE_HIT_STATUSES]
    invalid_rows = [
        row
        for row in hit_rows
        if row.get("search_used") is not False
        or row.get("expanded") != 0
        or row.get("edge") != 0
    ]
    return {
        "reported_hit_count": len(hit_rows),
        "invalid_hit_count": len(invalid_rows),
        "valid_zero_work_hit_count": len(hit_rows) - len(invalid_rows),
        "gate": "PASS" if hit_rows and not invalid_rows else "FAIL",
    }


def _trace_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    sidecar_records = [
        record
        for record in records
        if isinstance(record, dict) and record.get("record_kind") == "shadow_sidecar"
    ]
    source = sidecar_records or records
    statuses = [
        str(record.get("reuse_status"))
        for record in source
        if isinstance(record, dict) and record.get("reuse_status") is not None
    ]
    hits = sum(
        status in {"HIT_EXACT", "HIT_TRACE_EQUIVALENT"}
        for status in statuses
    )
    return {
        "trace_captured": statuses.count("TRACE_CAPTURED"),
        "trace_hits": hits,
        "cold_control": sum(status == "COLD_CONTROL" for status in statuses),
        "fallback_control": sum(status == "FALLBACK_CONTROL" for status in statuses),
        "record_count": len(statuses),
    }


def _m2_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the formal M2 gates without pooling semantic failures away."""

    cells = {
        (str(pair.get("layer")), str(pair.get("objective")))
        for case in cases
        for pair in case.get("comparison", {}).get("pairs", [])
    }
    overall_control = [
        float(case["track_resources"]["control"]["wall_seconds"])
        for case in cases
        if "track_resources" in case
        and "control" in case["track_resources"]
        and case.get("status") == "PASS"
    ]
    overall_candidate = [
        float(case["track_resources"]["candidate"]["wall_seconds"])
        for case in cases
        if "track_resources" in case
        and "candidate" in case["track_resources"]
        and case.get("status") == "PASS"
    ]
    control_median = _nearest_rank(overall_control, 0.5)
    candidate_median = _nearest_rank(overall_candidate, 0.5)
    improvement = (
        None
        if control_median in (None, 0) or candidate_median is None
        else (control_median - candidate_median) / control_median * 100.0
    )

    cell_summary: list[dict[str, Any]] = []
    for layer, objective in sorted(cells):
        control_values: list[float] = []
        candidate_values: list[float] = []
        for case in cases:
            if case.get("status") != "PASS":
                continue
            control_value = _timing_cell_map(case.get("records", {}).get("control", [])).get(
                (layer, objective)
            )
            candidate_value = _timing_cell_map(
                case.get("records", {}).get("candidate", [])
            ).get((layer, objective))
            if control_value is not None and candidate_value is not None:
                control_values.append(control_value)
                candidate_values.append(candidate_value)
        cell_control_median = _nearest_rank(control_values, 0.5)
        cell_candidate_median = _nearest_rank(candidate_values, 0.5)
        cell_control_p95 = _nearest_rank(control_values, 0.95)
        cell_candidate_p95 = _nearest_rank(candidate_values, 0.95)
        cell_improvement = (
            None
            if cell_control_median in (None, 0) or cell_candidate_median is None
            else (cell_control_median - cell_candidate_median)
            / cell_control_median
            * 100.0
        )
        cell_p95_regression = (
            None
            if cell_control_p95 in (None, 0) or cell_candidate_p95 is None
            else (cell_candidate_p95 - cell_control_p95) / cell_control_p95 * 100.0
        )
        cell_summary.append(
            {
                "layer": layer,
                "objective": objective,
                "sample_count": len(control_values),
                "control_wall_median_ms": cell_control_median,
                "candidate_wall_median_ms": cell_candidate_median,
                "control_wall_p95_ms": cell_control_p95,
                "candidate_wall_p95_ms": cell_candidate_p95,
                "median_improvement_percent": cell_improvement,
                "p95_regression_percent": cell_p95_regression,
                "gate": (
                    "PASS"
                    if cell_improvement is not None
                    and cell_improvement >= -_M2_CELL_REGRESSION_CEILING_PERCENT
                    else "FAIL"
                    if cell_improvement is not None
                    else "NOT_MEASURED"
                ),
            }
        )

    expected_reuse = []
    reuse_timing_evidence = []
    semantic_pass = []
    determinism_inputs: dict[str, list[Any]] = {"control": [], "candidate": []}
    rss_ratios: list[float] = []
    swap_deltas: list[dict[str, int]] = []
    swap_measurement_statuses: list[str] = []
    swap_measurement_incomplete = False
    cpu_observations: list[dict[str, Any]] = []
    cpu_measurement_statuses: list[str] = []
    cpu_measurement_incomplete = False
    for case in cases:
        counts = _trace_counts(case.get("records", {}).get("candidate", []))
        timing_evidence = _reuse_timing_evidence(
            case.get("records", {}).get("candidate", [])
        )
        reuse_timing_evidence.append(
            timing_evidence["reported_hit_count"] == counts["trace_hits"]
            and timing_evidence["reported_hit_count"] == _M2_EXPECTED_HIT_COUNT
            and timing_evidence["gate"] == "PASS"
        )
        expected_reuse.append(
            counts == {
                "trace_captured": _M2_EXPECTED_TRACE_COUNT,
                "trace_hits": _M2_EXPECTED_HIT_COUNT,
                "cold_control": _M2_EXPECTED_COLD_COUNT,
                "fallback_control": 0,
                "record_count": 12,
            }
        )
        comparison = case.get("comparison", {})
        integrity = case.get("route_integrity", {})
        semantic_pass.append(
            comparison.get("pair_count") == 12
            and comparison.get("status") == "PASS"
            and all(
                value.get("status") == "PASS" and value.get("route_count") == 12
                for value in integrity.values()
            )
        )
        for track in ("control", "candidate"):
            determinism_inputs[track].append(
                tuple(
                    (
                        pair.get("layer"),
                        pair.get("objective"),
                        pair.get(f"{track}_route_digest"),
                    )
                    for pair in comparison.get("pairs", [])
                )
            )
        resources = case.get("track_resources", {})
        case_track_resources = [
            resources.get(track) for track in ("control", "candidate")
        ] if isinstance(resources, dict) else [None, None]
        for track, track_resources in zip(
            ("control", "candidate"), case_track_resources, strict=True
        ):
            if not isinstance(track_resources, dict):
                swap_measurement_statuses.append("NOT_MEASURED")
                swap_measurement_incomplete = True
                cpu_measurement_statuses.append("NOT_MEASURED")
                cpu_measurement_incomplete = True
                cpu_observations.append(
                    {
                        "case_id": case.get("case_id"),
                        "track": track,
                        "status": "NOT_MEASURED",
                    }
                )
                continue
            measurement = track_resources.get("swap_measurement")
            reported_swap_status = (
                str(measurement.get("status"))
                if isinstance(measurement, dict) and measurement.get("status") is not None
                else "NOT_MEASURED"
            )
            swap_measurement_statuses.append(reported_swap_status)
            if _valid_swap_delta(track_resources.get("swap_delta")):
                swap_deltas.append(dict(track_resources["swap_delta"]))
            if not _swap_observation_pass(track_resources):
                swap_measurement_incomplete = True

            cpu_status = _cpu_measurement_status(
                cpu_pin_cpu=track_resources.get("cpu_pin_cpu"),
                cpu_pin_succeeded=track_resources.get("cpu_pin_succeeded"),
                cpu_affinity=track_resources.get("cpu_affinity"),
            )
            cpu_measurement_statuses.append(cpu_status)
            if cpu_status != "PASS":
                cpu_measurement_incomplete = True
            cpu_observations.append(
                {
                    "case_id": case.get("case_id"),
                    "track": track,
                    "pin_cpu": track_resources.get("cpu_pin_cpu"),
                    "pin_succeeded": track_resources.get("cpu_pin_succeeded"),
                    "affinity": track_resources.get("cpu_affinity"),
                    "status": cpu_status,
                }
            )
        if case.get("rss_scope") == "independent_child_process":
            control_rss = resources.get("control", {}).get("peak_rss_kib")
            candidate_rss = resources.get("candidate", {}).get("peak_rss_kib")
            if isinstance(control_rss, (int, float)) and control_rss > 0 and isinstance(
                candidate_rss, (int, float)
            ):
                rss_ratios.append(float(candidate_rss) / float(control_rss))
    deterministic = all(
        len(values) > 0 and all(value == values[0] for value in values[1:])
        for values in determinism_inputs.values()
    )
    independent_rss = bool(rss_ratios)
    rss_median = _nearest_rank(rss_ratios, 0.5)
    sample_count = len(cases)
    sufficient = sample_count >= _M2_MIN_REPETITIONS
    screening_sufficient = sample_count >= _M2_SCREENING_REPETITIONS
    trace_source_overhead, trace_source_gate = _trace_source_overhead(
        cases,
        required_repetitions=_M2_MIN_REPETITIONS,
    )
    screening_trace_source_overhead, screening_trace_source_gate = _trace_source_overhead(
        cases,
        required_repetitions=_M2_SCREENING_REPETITIONS,
    )
    p95_control = _nearest_rank(overall_control, 0.95)
    p95_candidate = _nearest_rank(overall_candidate, 0.95)
    p95_regression = (
        None
        if p95_control in (None, 0) or p95_candidate is None
        else (p95_candidate - p95_control) / p95_control * 100.0
    )
    p95_gate = (
        "PASS"
        if sufficient
        and p95_regression is not None
        and p95_regression <= _M2_P95_REGRESSION_CEILING_PERCENT
        else "FAIL"
        if sufficient
        else "NOT_EVALUATED_INSUFFICIENT_REPETITIONS"
    )
    overall_gate = (
        "PASS"
        if sufficient
        and improvement is not None
        and improvement >= _M2_TOTAL_IMPROVEMENT_FLOOR_PERCENT
        and p95_gate == "PASS"
        else "FAIL"
        if sufficient
        else "NOT_EVALUATED_INSUFFICIENT_REPETITIONS"
    )
    cell_gate = (
        all(item["gate"] == "PASS" for item in cell_summary)
        if sufficient and cell_summary
        else False
    )
    semantic_gate = bool(semantic_pass) and all(semantic_pass)
    reuse_gate = bool(expected_reuse) and all(expected_reuse)
    reuse_timing_gate = bool(reuse_timing_evidence) and all(reuse_timing_evidence)
    resource_gate = independent_rss and (rss_median is not None and rss_median <= 1.10)
    swap_measurement_incomplete = swap_measurement_incomplete or (
        len(swap_measurement_statuses) != 2 * len(cases)
    )
    swap_gate = (
        bool(cases)
        and len(swap_deltas) == 2 * len(cases)
        and len(swap_measurement_statuses) == 2 * len(cases)
        and all(status == "PASS" for status in swap_measurement_statuses)
        and not swap_measurement_incomplete
        and all(delta["pswpin"] == 0 and delta["pswpout"] == 0 for delta in swap_deltas)
    )
    cpu_measurement_incomplete = cpu_measurement_incomplete or (
        len(cpu_measurement_statuses) != 2 * len(cases)
    )
    cpu_affinity_gate = (
        bool(cases)
        and len(cpu_observations) == 2 * len(cases)
        and all(status == "PASS" for status in cpu_measurement_statuses)
        and not cpu_measurement_incomplete
        and len(
            {
                (
                    observation.get("pin_cpu"),
                    tuple(observation["affinity"])
                    if isinstance(observation.get("affinity"), list)
                    else None,
                )
                for observation in cpu_observations
            }
        )
        == 1
    )
    gate_verdict = (
        "PASS"
        if sufficient
        and semantic_gate
        and reuse_gate
        and reuse_timing_gate
        and deterministic
        and overall_gate == "PASS"
        and cell_gate
        and trace_source_gate
        and resource_gate
        and swap_gate
        and cpu_affinity_gate
        else "NOT_EVALUATED_INSUFFICIENT_REPETITIONS"
        if not sufficient
        else "FAIL"
    )
    screening_gate = (
        "PASS"
        if screening_sufficient
        and semantic_gate
        and reuse_gate
        and reuse_timing_gate
        and deterministic
        and improvement is not None
        and improvement >= _M2_SCREENING_IMPROVEMENT_FLOOR_PERCENT
        and bool(cell_summary)
        and all(item["gate"] == "PASS" for item in cell_summary)
        and screening_trace_source_gate
        and resource_gate
        and swap_gate
        and cpu_affinity_gate
        else "NOT_EVALUATED_INSUFFICIENT_REPETITIONS"
        if not screening_sufficient
        else "FAIL"
    )
    order_stratified = _order_stratified_summary(cases)
    return {
        "schema_version": "orchestrator.winter-p2-m2-summary.v1",
        "sample_count": sample_count,
        "minimum_repetitions": _M2_MIN_REPETITIONS,
        "semantic_gate": "PASS" if semantic_gate else "FAIL",
        "reuse_matrix_gate": "PASS" if reuse_gate else "FAIL",
        "reuse_timing_gate": (
            "PASS" if reuse_timing_gate else "FAIL"
        ),
        "determinism_gate": "PASS" if deterministic else "FAIL",
        "cpu_affinity_gate": "PASS" if cpu_affinity_gate else "FAIL",
        "trace_source_overhead": {
            "layer": _M2_TRACE_SOURCE_LAYER,
            "ceiling_percent": _M2_TRACE_SOURCE_OVERHEAD_CEILING_PERCENT,
            "objectives": trace_source_overhead,
            "gate": "PASS" if trace_source_gate else "FAIL",
        },
        "overall": {
            "control_wall_median_seconds": control_median,
            "candidate_wall_median_seconds": candidate_median,
            "control_wall_p95_seconds": _nearest_rank(overall_control, 0.95),
            "candidate_wall_p95_seconds": _nearest_rank(overall_candidate, 0.95),
            "median_improvement_percent": improvement,
            "p95_regression_percent": p95_regression,
            "p95_gate": p95_gate,
            "gate": overall_gate,
        },
        "cells": cell_summary,
        "reuse_timing_evidence": reuse_timing_evidence,
        "rss": {
            "comparison": (
                "independent_child_process"
                if independent_rss
                else "NOT_MEASURED_COMBINED_PROCESS"
            ),
            "median_ratio": rss_median,
            "ceiling": 1.10,
            "gate": "PASS" if resource_gate else "FAIL" if sufficient else "NOT_MEASURED",
        },
        "swap": {
            "observations": swap_deltas,
            "measurement_statuses": swap_measurement_statuses,
            "gate": "PASS" if swap_gate else "FAIL" if sufficient else "NOT_MEASURED",
        },
        "cpu": {
            "observations": cpu_observations,
            "measurement_statuses": cpu_measurement_statuses,
            "gate": (
                "PASS"
                if cpu_affinity_gate
                else "FAIL"
                if sufficient
                else "NOT_MEASURED"
            ),
        },
        "expected_trace_hit_cold": {
            "trace_captured": _M2_EXPECTED_TRACE_COUNT,
            "trace_hits": _M2_EXPECTED_HIT_COUNT,
            "cold_control": _M2_EXPECTED_COLD_COUNT,
        },
        "screening": {
            "minimum_repetitions": _M2_SCREENING_REPETITIONS,
            "median_improvement_floor_percent": (
                _M2_SCREENING_IMPROVEMENT_FLOOR_PERCENT
            ),
            "trace_source_overhead": {
                "layer": _M2_TRACE_SOURCE_LAYER,
                "ceiling_percent": _M2_TRACE_SOURCE_OVERHEAD_CEILING_PERCENT,
                "objectives": screening_trace_source_overhead,
                "gate": "PASS" if screening_trace_source_gate else "FAIL",
            },
            "gate_verdict": screening_gate,
        },
        "gate_verdict": gate_verdict,
        "percentile_method": "median exact; p95 nearest-rank ceil(0.95*n)-1",
        "order_stratified": order_stratified,
    }


_M2H_FOCUS_CELLS = (
    ("executable_0_6h", "low_risk"),
    ("rolling_0_24h", "fastest"),
    ("rolling_0_24h", "low_risk"),
    ("rolling_0_24h", "recommended"),
)
_DIAGNOSTIC_REGRESSION_CEILING_PERCENT = 3.0
_DIAGNOSTIC_ORDER_REGRESSION_CEILING_PERCENT = 5.0
_DIAGNOSTIC_ORDER_GAP_CEILING_PERCENT_POINTS = 5.0


def _cell_regressions(
    cases: list[dict[str, Any]],
    *,
    order: str | None = None,
) -> dict[tuple[str, str], list[float]]:
    values: dict[tuple[str, str], list[float]] = {}
    for case in cases:
        if case.get("status") != "PASS":
            continue
        if order is not None and case.get("execution_order") != order:
            continue
        control = _timing_cell_map(case.get("records", {}).get("control", []))
        candidate = _timing_cell_map(case.get("records", {}).get("candidate", []))
        for cell, control_ms in control.items():
            candidate_ms = candidate.get(cell)
            if candidate_ms is None or control_ms <= 0:
                continue
            values.setdefault(cell, []).append(
                (float(candidate_ms) - float(control_ms)) / float(control_ms) * 100.0
            )
    return values


def _order_stratified_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Report paired cell regressions separately for each execution order."""

    orders = ("control-first", "candidate-first")
    by_order: dict[str, Any] = {}
    for order in orders:
        cells = _cell_regressions(cases, order=order)
        by_order[order] = {
            "sample_count": sum(
                1 for case in cases
                if case.get("status") == "PASS"
                and case.get("execution_order") == order
            ),
            "cells": {
                f"{layer}::{objective}": {
                    "sample_count": len(values),
                    "median_regression_percent": _nearest_rank(values, 0.5),
                    "p95_regression_percent": _nearest_rank(values, 0.95),
                }
                for (layer, objective), values in sorted(cells.items())
            },
        }
    all_cells = _cell_regressions(cases)
    overall_cells = {
        f"{layer}::{objective}": {
            "sample_count": len(values),
            "median_regression_percent": _nearest_rank(values, 0.5),
            "p95_regression_percent": _nearest_rank(values, 0.95),
        }
        for (layer, objective), values in sorted(all_cells.items())
    }
    gaps: dict[str, float | None] = {}
    for cell in set(_cell_regressions(cases, order=orders[0])) | set(
        _cell_regressions(cases, order=orders[1])
    ):
        key = f"{cell[0]}::{cell[1]}"
        first = by_order[orders[0]]["cells"].get(key, {}).get(
            "median_regression_percent"
        )
        second = by_order[orders[1]]["cells"].get(key, {}).get(
            "median_regression_percent"
        )
        gaps[key] = (
            abs(float(first) - float(second))
            if first is not None and second is not None
            else None
        )
    measured_gaps = [value for value in gaps.values() if value is not None]
    return {
        "schema_version": "orchestrator.winter-p2-order-stratified-summary.v1",
        "orders": by_order,
        "overall_cells": overall_cells,
        "order_gap_percent_points": gaps,
        "max_order_gap_percent_points": max(measured_gaps) if measured_gaps else None,
        "balanced": (
            by_order[orders[0]]["sample_count"]
            == by_order[orders[1]]["sample_count"]
            and by_order[orders[0]]["sample_count"] > 0
        ),
    }


def _diagnostic_summary(
    cases: list[dict[str, Any]],
    *,
    required_sample_count: int = 8,
    diagnostic_profile: str = "baseline",
) -> dict[str, Any]:
    """Evaluate the non-formal, full-track promotion diagnostic.

    The baseline promotion diagnostic uses eight balanced pairs.  The two
    causal ablations intentionally use six pairs, so their evidence-complete
    gate is parameterized rather than silently judged against the baseline
    sample count.
    """

    diagnostic_profile = _validate_diagnostic_profile(diagnostic_profile)
    expected_counts = {
        "trace_captured": _M2_EXPECTED_TRACE_COUNT,
        "trace_hits": _M2_EXPECTED_HIT_COUNT,
        "cold_control": _M2_EXPECTED_COLD_COUNT,
        "fallback_control": 0,
        "record_count": 12,
    }
    if diagnostic_profile == "force-main-cold":
        expected_counts.update(trace_hits=0, cold_control=9)

    order_summary = _order_stratified_summary(cases)
    focus_keys = [f"{layer}::{objective}" for layer, objective in _M2H_FOCUS_CELLS]
    overall = order_summary["overall_cells"]
    target_cells = {
        key: overall.get(key, {"median_regression_percent": None})
        for key in focus_keys
    }
    target_complete = all(
        item.get("median_regression_percent") is not None
        and item.get("sample_count", 0) >= len(cases)
        for item in target_cells.values()
    )
    target_gate = target_complete and all(
        float(item["median_regression_percent"])
        <= _DIAGNOSTIC_REGRESSION_CEILING_PERCENT
        for item in target_cells.values()
    )
    order_gate = True
    order_cells: dict[str, Any] = {}
    for key in focus_keys:
        observations = {}
        for order in ("control-first", "candidate-first"):
            observation = order_summary["orders"][order]["cells"].get(key, {})
            observations[order] = observation
            value = observation.get("median_regression_percent")
            if value is None or float(value) > _DIAGNOSTIC_ORDER_REGRESSION_CEILING_PERCENT:
                order_gate = False
        order_cells[key] = observations
    focus_gaps = [
        order_summary.get("order_gap_percent_points", {}).get(key)
        for key in focus_keys
    ]
    measured_focus_gaps = [value for value in focus_gaps if value is not None]
    gap = max(measured_focus_gaps) if measured_focus_gaps else None
    gap_gate = gap is not None and gap <= _DIAGNOSTIC_ORDER_GAP_CEILING_PERCENT_POINTS

    evidence_complete = bool(cases) and len(cases) == required_sample_count
    evidence_failures: list[str] = []
    for case in cases:
        if case.get("status") != "PASS":
            evidence_complete = False
            evidence_failures.append(f"{case.get('case_id')}:case_status")
            continue
        for track in ("control", "candidate"):
            rows = _timing_rows(case.get("records", {}).get(track, []))
            if len(rows) != 12:
                evidence_complete = False
                evidence_failures.append(f"{case.get('case_id')}:{track}:timing_count")
            for row in rows:
                for field in (
                    "expanded",
                    "edge",
                    "planner_cpu_ms",
                    "gc_count_before",
                    "gc_count_after",
                    "gc_collections_delta",
                    "trace_state",
                ):
                    if field not in row:
                        evidence_complete = False
                        evidence_failures.append(
                            f"{case.get('case_id')}:{track}:{field}"
                        )
        candidate_counts = _trace_counts(case.get("records", {}).get("candidate", []))
        if candidate_counts != expected_counts:
            evidence_complete = False
            evidence_failures.append(f"{case.get('case_id')}:reuse_counts")
        for track_resources in case.get("track_resources", {}).values():
            if not isinstance(track_resources, Mapping):
                evidence_complete = False
                evidence_failures.append(f"{case.get('case_id')}:resources")
                continue
            if not _swap_observation_pass(track_resources):
                evidence_complete = False
                evidence_failures.append(f"{case.get('case_id')}:swap")
            if _cpu_measurement_status(
                cpu_pin_cpu=track_resources.get("cpu_pin_cpu"),
                cpu_pin_succeeded=track_resources.get("cpu_pin_succeeded"),
                cpu_affinity=track_resources.get("cpu_affinity"),
            ) != "PASS":
                evidence_complete = False
                evidence_failures.append(f"{case.get('case_id')}:cpu")
            if track_resources.get("peak_rss_kib", 0) <= 0:
                evidence_complete = False
                evidence_failures.append(f"{case.get('case_id')}:rss")
        if case.get("publication_boundary", {}).get("production_published", True):
            evidence_complete = False
            evidence_failures.append(f"{case.get('case_id')}:publication")
    summary = {
        "schema_version": "orchestrator.winter-p2-m2i-diagnostic-summary.v1",
        "diagnostic_only": True,
        "formal_gate_verdict": "NOT_APPLICABLE",
        "sample_count": len(cases),
        "required_sample_count": required_sample_count,
        "diagnostic_profile": diagnostic_profile,
        "execution_order_counts": {
            order: order_summary["orders"][order]["sample_count"]
            for order in ("control-first", "candidate-first")
        },
        "evidence_complete": evidence_complete,
        "evidence_failures": evidence_failures,
        "focus_cells": list(focus_keys),
        "focus_cell_overall": target_cells,
        "focus_cell_by_order": order_cells,
        "order_stratified": order_summary,
        "gates": {
            "evidence_complete": "PASS" if evidence_complete else "FAIL",
            "focus_cell_overall_median_regression_le_3_percent": (
                "PASS" if target_gate else "FAIL"
            ),
            "focus_cell_order_median_regression_le_5_percent": (
                "PASS" if order_gate else "FAIL"
            ),
            "focus_cell_order_gap_le_5pp": "PASS" if gap_gate else "FAIL",
        },
        "gate_verdict": (
            "PASS" if evidence_complete and target_gate and order_gate and gap_gate else "FAIL"
        ),
    }
    return summary


def _reuse_sidecar(
    *,
    candidate_records: list[dict[str, Any]],
    screen_objective: ObjectiveMode,
    candidate_mode: str = "exact-temporal",
) -> dict[str, Any]:
    mode_metadata = _mode_metadata(candidate_mode)
    if candidate_mode == "control-trace":
        normalized_records: list[dict[str, Any]] = []
        for record in candidate_records:
            normalized = dict(record)
            certificate = _trace_certificate_document(normalized)
            # A stale/malformed caller certificate must not turn a cold or
            # miss outcome into a certified trace claim.
            normalized["certificate"] = (
                certificate
                if certificate is not None
                else None
            )
            normalized["certificate_status"] = (
                certificate.get("status") if certificate is not None else None
            )
            normalized_records.append(normalized)
        candidate_records = normalized_records
        hits = [record for record in candidate_records if record.get("reuse_hit")]
        trace_records = [
            record
            for record in candidate_records
            if _trace_certificate_allowed(record)
            and (record.get("certificate") or {}).get("status") == "CERTIFIED_TRACE"
        ]
        lookup_attempts = [
            record
            for record in candidate_records
            if record.get("reuse_lookup_status")
            not in {None, "NOT_ATTEMPTED", "TRACE_CAPTURED"}
        ]
        statuses = sorted(
            {
                str(record["reuse_status"])
                for record in candidate_records
                if record.get("reuse_status") is not None
            }
        )
        lookup_statuses = sorted(
            {
                str(record["reuse_lookup_status"])
                for record in candidate_records
                if record.get("reuse_lookup_status") is not None
            }
        )
        fallback_control = any(
            record.get("reuse_status") == "FALLBACK_CONTROL" for record in candidate_records
        )
        cold_control = any(
            record.get("reuse_status") == "COLD_CONTROL" for record in candidate_records
        )
        misses = [
            record
            for record in candidate_records
            if record.get("reuse_status") in {"FALLBACK_CONTROL", "COLD_CONTROL"}
            or record.get("reuse_lookup_status") == "MISS_INCOMPATIBLE"
        ]
        lookup_misses = [
            record
            for record in candidate_records
            if record.get("reuse_lookup_status") == "MISS_INCOMPATIBLE"
        ]
        return {
            "schema_version": mode_metadata["sidecar_schema"],
            "candidate_mode": candidate_mode,
            "candidate_algorithm": mode_metadata["candidate_algorithm"],
            "status": "PASS" if hits else "MISS_COLD_CONTROL",
            "certificate_status": "CERTIFIED_TRACE" if trace_records else "UNAVAILABLE",
            "p2_reuse_claim": mode_metadata["p2_reuse_claim"],
            "screen_objective": screen_objective.value,
            "p1_session_used": False,
            "p2_same_goal_reuse_attempted": bool(lookup_attempts),
            "reuse_hit_count": len(hits),
            "zero_search_hit_count": sum(
                bool(record.get("reuse_hit") and not record.get("search_used"))
                for record in candidate_records
            ),
            "reuse_miss_count": len(misses),
            "reuse_lookup_miss_count": len(lookup_misses),
            "reuse_statuses": statuses,
            "reuse_lookup_statuses": lookup_statuses,
            "fallback": (
                "explicit_fallback_control"
                if fallback_control
                else "cold_control"
                if cold_control
                else None
            ),
            "reason": (
                "full layer trace captured per objective; only same-goal main queried; "
                "rolling/executable and other goals cold control"
            ),
            "candidate_sessions": candidate_records,
        }
    hits = [record for record in candidate_records if record.get("reuse_hit")]
    statuses = sorted({record.get("reuse_status") for record in candidate_records})
    lookup_statuses = sorted({record.get("reuse_lookup_status") for record in candidate_records})
    return {
        "schema_version": mode_metadata["sidecar_schema"],
        "candidate_mode": candidate_mode,
        "candidate_algorithm": mode_metadata["candidate_algorithm"],
        "status": "PASS" if hits else "MISS_COLD_CANDIDATE",
        "certificate_status": "CERTIFIED_REUSABLE"
        if any(record.get("certificate") for record in candidate_records)
        else "UNAVAILABLE",
        "p2_reuse_claim": mode_metadata["p2_reuse_claim"],
        "screen_objective": screen_objective.value,
        "p1_session_used": True,
        "p2_same_goal_reuse_attempted": bool(candidate_records),
        "reuse_hit_count": len(hits),
        "reuse_statuses": statuses,
        "reuse_lookup_statuses": lookup_statuses,
        "fallback": "cold_candidate" if len(hits) < len(candidate_records) else None,
        "reason": "P2 certificate-backed exact/monotonic reuse; misses explicitly cold-search",
        "candidate_sessions": candidate_records,
    }


def _make_planners(
    current: CommittedRiskWindow,
    query: RiskWindowQuery,
    configuration: Any,
    *,
    candidate_mode: str = "exact-temporal",
) -> tuple[TimeDependentAStar, Any, CommittedRiskWindow]:
    candidate_mode = _validate_candidate_mode(candidate_mode)
    private = _private_window(current, query)
    from arctic_route_planning.contracts import HOURLY_RISK_INTERVAL
    from arctic_route_planning.risk import RiskSampler

    sampler = RiskSampler(private.frames, max_frame_gap=HOURLY_RISK_INTERVAL)
    grid = RegularGrid.from_risk_frame(
        private.frames[0],
        allow_diagonal=configuration.planner.connectivity == 8,
    )
    vessel_model = VesselPerformanceModel.from_configuration(configuration.vessel_model)
    common = {
        "grid": grid,
        "risk_sampler": sampler,
        "vessel_model": vessel_model,
        "planner_config": configuration.planner,
    }
    candidate_planner: Any
    if candidate_mode == "control-trace":
        # Keep a distinct control instance so its geometry counters and RSS
        # measurements cannot be mistaken for a shared planner/cache result.
        candidate_planner = TimeDependentAStar(**common)
    else:
        candidate_planner = TemporalLabelAStar(**common)
    return (
        TimeDependentAStar(**common),
        candidate_planner,
        private,
    )


def _run_track(
    *,
    planner: Any,
    request: ServicePlanningRequest,
    configuration: Any,
    planner_version: str,
) -> tuple[FourLayerPlanningOutcome, list[dict[str, Any]]]:
    adapter = planner
    if isinstance(planner, TemporalLabelAStar):
        raise TypeError("candidate must be wrapped by _CandidateAdapter")
    service = _service(
        adapter,
        request=request,
        configuration=configuration,
        planner_version=planner_version,
    )
    return service.execute(request), getattr(adapter, "records", [])


def _candidate_adapter(
    planner: Any,
    *,
    candidate_mode: str,
    window: CommittedRiskWindow,
    input_revision: int,
    generation_id: int,
    maximum_elapsed: timedelta,
) -> Any:
    candidate_mode = _validate_candidate_mode(candidate_mode)
    if candidate_mode == "control-trace":
        if not isinstance(planner, TimeDependentAStar):
            raise TypeError("control-trace requires an independent control planner")
        return _ControlTraceAdapter(
            planner,
            window=window,
            input_revision=input_revision,
            generation_id=generation_id,
        )
    if not isinstance(planner, TemporalLabelAStar):
        raise TypeError("exact-temporal requires the temporal candidate planner")
    return _CandidateAdapter(
        planner,
        window=window,
        input_revision=input_revision,
        maximum_elapsed=maximum_elapsed,
    )


def _resource_snapshot(
    started: float,
    *,
    swap_before: dict[str, int] | None = None,
    swap_after: dict[str, int] | None = None,
    process_swap_before_kib: int | None = None,
    process_swap_after_kib: int | None = None,
    cpu_pin_cpu: int | None = None,
    cpu_pin_succeeded: bool | None = None,
    usage_before: resource.struct_rusage | None = None,
) -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    process_swap_delta = _process_swap_delta(
        process_swap_before_kib,
        process_swap_after_kib,
    )
    kernel_measured = swap_before is not None and swap_after is not None
    process_measured = (
        process_swap_before_kib is not None and process_swap_after_kib is not None
    )
    cpu_affinity = _worker_cpu_affinity()
    rusage_fields = (
        "ru_utime",
        "ru_stime",
        "ru_nvcsw",
        "ru_nivcsw",
        "ru_minflt",
        "ru_majflt",
    )
    rusage = {
        field: getattr(usage, field)
        for field in rusage_fields
    }
    rusage_delta = None
    if usage_before is not None:
        rusage_delta = {
            field: getattr(usage, field) - getattr(usage_before, field)
            for field in rusage_fields
        }
    return {
        "wall_seconds": time.perf_counter() - started,
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "peak_rss_kib": usage.ru_maxrss,
        "rusage": rusage,
        "rusage_delta": rusage_delta,
        "swap_before": swap_before,
        "swap_after": swap_after,
        "swap_delta": _swap_delta(swap_before, swap_after),
        "process_swap_before_kib": process_swap_before_kib,
        "process_swap_after_kib": process_swap_after_kib,
        "process_swap_delta_kib": process_swap_delta,
        "pid": os.getpid(),
        "cpu_pin_cpu": cpu_pin_cpu,
        "cpu_pin_succeeded": cpu_pin_succeeded,
        "cpu_affinity": cpu_affinity,
        "cpu_measurement": {
            "status": _cpu_measurement_status(
                cpu_pin_cpu=cpu_pin_cpu,
                cpu_pin_succeeded=cpu_pin_succeeded,
                cpu_affinity=cpu_affinity,
            )
        },
        "swap_measurement": {
            "kernel_counters": kernel_measured,
            "process_vm_swap": process_measured,
            "status": _swap_measurement_status(
                swap_before=swap_before,
                swap_after=swap_after,
                process_swap_before_kib=process_swap_before_kib,
                process_swap_after_kib=process_swap_after_kib,
            ),
        },
    }


def _identity_check_for_case(prepared: _PreparedShadow, current: CommittedRiskWindow) -> None:
    current.assert_matches(prepared.query)
    if (
        current.commit_id != prepared.commit["commit_id"]
        or current.content_digest != prepared.commit["content_digest"]
    ):
        raise ValueError("committed RiskWindow identity changed before case")


def _track_configuration(
    *,
    control_planner: TimeDependentAStar,
    candidate_planner: Any,
    private: CommittedRiskWindow,
    prepared: _PreparedShadow,
    candidate_mode: str,
) -> dict[str, tuple[Any, str]]:
    candidate_adapter = _candidate_adapter(
        candidate_planner,
        window=private,
        input_revision=prepared.spec.input_revision,
        generation_id=prepared.spec.generation_id,
        maximum_elapsed=prepared.request.maximum_elapsed,
        candidate_mode=candidate_mode,
    )
    mode_metadata = _mode_metadata(candidate_mode)
    return {
        "control": (control_planner, "time-dependent-a-star.v1"),
        "candidate": (candidate_adapter, mode_metadata["candidate_algorithm"]),
    }


def _as_document(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _as_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_document(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _as_document(asdict(value))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _trace_certificate_allowed(record: Mapping[str, Any]) -> bool:
    """Return whether one observation is allowed to carry a trace cert."""

    status = str(record.get("reuse_status", ""))
    if status == _TRACE_CAPTURE_STATUS:
        return True
    return (
        status in _TRACE_HIT_STATUSES
        and bool(record.get("reuse_hit", record.get("reused", False)))
        and record.get("search_used", record.get("used_search")) is False
    )


def _trace_certificate_document(record: Mapping[str, Any]) -> dict[str, Any] | None:
    if not _trace_certificate_allowed(record):
        return None
    document: dict[str, Any] = {"status": "CERTIFIED_TRACE"}
    digest = record.get("trace_digest", record.get("digest"))
    if digest is not None:
        document["trace_digest"] = digest
    identity_digest = record.get("identity_digest")
    if identity_digest is not None:
        document["identity_digest"] = identity_digest
    return document


def _shadow_sidecar_records(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize C shadow observations for the runner's common sidecar schema."""

    if not metadata:
        return []
    records: list[dict[str, Any]] = []
    for observation in metadata.get("trace_observations", ()):
        if not isinstance(observation, dict):
            continue
        records.append(
            {
                **observation,
                "record_kind": "shadow_sidecar",
                "reuse_status": _TRACE_CAPTURE_STATUS,
                "reuse_lookup_status": "TRACE_CAPTURED",
                "reuse_hit": False,
                "search_used": True,
                "certificate": {"status": "CERTIFIED_TRACE"},
                "certificate_status": "CERTIFIED_TRACE",
            }
        )
    for observation in metadata.get("reuse_outcomes", ()):
        if not isinstance(observation, dict):
            continue
        status = str(observation.get("status", ""))
        certificate = _trace_certificate_document(
            {
                **observation,
                "reuse_status": status,
                "reuse_hit": bool(observation.get("reused", False)),
                "search_used": bool(observation.get("used_search", False)),
            }
        )
        records.append(
            {
                **observation,
                "record_kind": "shadow_sidecar",
                "reuse_status": status,
                "reuse_lookup_status": status,
                "reuse_hit": bool(observation.get("reused", False)),
                "search_used": bool(observation.get("used_search", False)),
                "certificate": certificate,
                "certificate_status": (
                    certificate.get("status") if certificate is not None else None
                ),
                "zero_search_metrics": (
                    {"expanded_states": 0, "edge_evaluations": 0}
                    if observation.get("reused") and not observation.get("used_search")
                    else None
                ),
            }
        )
    return records


def _prepared_shadow_track(
    *,
    prepared: _PreparedShadow,
    track: str,
    candidate_mode: str,
    cpu_pin_cpu: int | None = None,
    cpu_pin_succeeded: bool | None = None,
    diagnostic_profile: str = "baseline",
    warmup_runs: int = 0,
) -> tuple[Any, list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Execute exactly one formal-prepared shadow track.

    This is intentionally a strict protocol boundary.  The C side must expose
    ``execute_four_layer_temporal_shadow_track`` and return an object with an
    ``outcome`` and per-layer/objective ``timings``.  Falling back to the old
    combined shadow call would make isolated RSS and timing claims false.

    ``warmup_runs`` (M2K symmetric warm-up): when >0, the same track is run that
    many times *before* the timed run, discarding results and timings.  This
    removes the first-track cold-start bias (risk-frame/grid/sampler
    initialization paid by whichever track runs first) that the M2J
    ``order-gap`` gate observed as a large candidate-first regression paired
    with a negative control-first regression.  Both tracks are warmed before
    their timed measurement, making the comparison symmetric.
    """

    if candidate_mode != "control-trace":
        raise ValueError("the single-track formal shadow runner requires control-trace")
    if track not in {"control", "candidate"}:
        raise ValueError("track must be control or candidate")
    if warmup_runs < 0:
        raise ValueError("warmup_runs must be non-negative")
    method = getattr(prepared.prepared, "execute_four_layer_temporal_shadow_track", None)
    if not callable(method):
        raise RuntimeError(
            "PreparedRiskPlanning lacks the required single-track shadow API: "
            "execute_four_layer_temporal_shadow_track"
        )
    for _ in range(warmup_runs):
        warm = method(
            track=track,
            candidate_mode="control_trace",
            diagnostic_profile=diagnostic_profile,
        )
        # Warm-up run must still prove production isolation; discard timing.
        if getattr(warm, "outcome", None) is None:
            raise RuntimeError("shadow warm-up run produced no outcome")
        if bool(getattr(warm, "production_published", False)) or bool(
            getattr(getattr(warm, "outcome", None), "published", False)
        ):
            raise RuntimeError("shadow warm-up crossed the formal publication boundary")
    started = time.perf_counter()
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    swap_before = _swap_counters()
    process_swap_before_kib = _process_swap_kib()
    result = method(
        track=track,
        candidate_mode="control_trace",
        diagnostic_profile=diagnostic_profile,
    )
    elapsed_seconds = time.perf_counter() - started
    swap_after = _swap_counters()
    process_swap_after_kib = _process_swap_kib()
    outcome = getattr(result, "outcome", None)
    if outcome is None:
        raise RuntimeError("single-track shadow API returned no FourLayerPlanningOutcome")
    if bool(getattr(result, "production_published", False)) or bool(
        getattr(outcome, "published", False)
    ):
        raise RuntimeError("single-track shadow crossed the formal publication boundary")
    scratch_proof = _as_document(getattr(result, "scratch_proof", None))
    if not isinstance(scratch_proof, dict) or not all(
        bool(scratch_proof.get(field))
        for field in (
            "production_store_unchanged",
            "production_session_unchanged",
            "scratch_store_isolated",
        )
    ):
        raise RuntimeError("single-track shadow did not prove production-state isolation")
    if bool(scratch_proof.get("production_published", False)):
        raise RuntimeError("single-track shadow proof reports production publication")
    raw_timings = getattr(result, "timings", None)
    if raw_timings is None:
        raise RuntimeError("single-track shadow API returned no per-layer/objective timings")
    timings = _as_document(raw_timings)
    if not isinstance(timings, list):
        raise RuntimeError("single-track shadow timings must be a list")
    records = [item for item in timings if isinstance(item, dict)]
    if len(records) != len(timings):
        raise RuntimeError("single-track shadow timings contain a non-object record")
    resources = _resource_snapshot(
        started,
        swap_before=swap_before,
        swap_after=swap_after,
        process_swap_before_kib=process_swap_before_kib,
        process_swap_after_kib=process_swap_after_kib,
        cpu_pin_cpu=cpu_pin_cpu,
        cpu_pin_succeeded=cpu_pin_succeeded,
        usage_before=usage_before,
    )
    metadata = {
        "production_published": False,
        "scratch_published": bool(getattr(result, "scratch_published", False)),
        "reuse_outcomes": _as_document(getattr(result, "reuse_outcomes", ())),
        "trace_observations": _as_document(getattr(result, "trace_observations", ())),
        "status_counts": _as_document(getattr(result, "status_counts", {})),
        "identity_digests": _as_document(getattr(result, "identity_digests", ())),
        "scratch_proof": scratch_proof,
        "api": (
            "RiskSourcePlanningIngress.prepare/"
            "PreparedRiskPlanning.execute_four_layer_temporal_shadow_track"
        ),
        "track": track,
        "diagnostic_profile": diagnostic_profile,
        "elapsed_seconds": elapsed_seconds,
        "trace_lifecycle": _as_document(getattr(result, "trace_lifecycle", ())),
        "trace_normalization_ms": float(
            getattr(result, "trace_normalization_ms", 0.0)
        ),
    }
    return outcome, records, resources, metadata


def _run_in_process_case(
    *,
    prepared: _PreparedShadow,
    args: argparse.Namespace,
    execution_order: str,
) -> tuple[
    dict[str, FourLayerPlanningOutcome],
    dict[str, list[dict[str, Any]]],
    CommittedRiskWindow,
    dict[str, dict[str, Any]],
]:
    if args.candidate_mode == "control-trace":
        outcomes: dict[str, FourLayerPlanningOutcome] = {}
        records: dict[str, list[dict[str, Any]]] = {}
        resources: dict[str, dict[str, Any]] = {}
        order = (
            ("control", "candidate")
            if execution_order == "control-first"
            else ("candidate", "control")
        )
        for track in order:
            outcome, track_records, track_resources, metadata = _prepared_shadow_track(
                prepared=prepared,
                track=track,
                candidate_mode=args.candidate_mode,
                diagnostic_profile=getattr(args, "diagnostic_profile", "baseline"),
                warmup_runs=getattr(args, "warmup_runs", 0),
            )
            outcomes[track] = outcome
            records[track] = [
                *track_records,
                *_shadow_sidecar_records(metadata if track == "candidate" else None),
            ]
            resources[track] = {**track_resources, "shadow_metadata": metadata}
        return outcomes, records, prepared.window, resources
    with prepared.store.lease_committed_window(prepared.query) as current:
        _identity_check_for_case(prepared, current)
        control_planner, candidate_planner, private = _make_planners(
            current,
            prepared.query,
            prepared.configuration,
            candidate_mode=args.candidate_mode,
        )
        tracks = _track_configuration(
            control_planner=control_planner,
            candidate_planner=candidate_planner,
            private=private,
            prepared=prepared,
            candidate_mode=args.candidate_mode,
        )
        outcomes: dict[str, FourLayerPlanningOutcome] = {}
        records: dict[str, list[dict[str, Any]]] = {}
        resources: dict[str, dict[str, Any]] = {}
        order = (
            ("control", "candidate")
            if execution_order == "control-first"
            else ("candidate", "control")
        )
        for track in order:
            planner, planner_version = tracks[track]
            started = time.perf_counter()
            outcome, track_records = _run_track(
                planner=planner,
                request=prepared.request,
                configuration=prepared.configuration,
                planner_version=planner_version,
            )
            outcomes[track] = outcome
            records[track] = track_records
            resources[track] = _resource_snapshot(started)
        return outcomes, records, private, resources


def _worker_command(
    *,
    args: argparse.Namespace,
    track: str,
    result_path: Path,
) -> list[str]:
    # R1 (per-unit-phase isolation): the candidate track's `full_voyage` trace
    # capture must not pollute the later `rolling`/`executable` cold units.
    # A literal per-unit subprocess split is deferred because the
    # FourLayerPlanningService anchors MAIN_CORRIDOR/ROLLING/EXECUTABLE on the
    # FULL_VOYAGE recommended plan (layered.py: full_recommended at 129 / anchor
    # at 170), so a cold-phase subprocess would lack its anchor without passing
    # the FULL_VOYAGE plan data in.  The equivalent, safe, within-process
    # realization is to force the `trace-release-only` diagnostic profile on the
    # candidate track: after MAIN_CORRIDOR reuse (layer_index == 1) the retained
    # ControlTrace payload is cleared and gc.collect() runs before ROLLING/
    # EXECUTABLE, eliminating the trace-memory pollution.  Under `per-track`
    # (default) the explicit --diagnostic-profile is honored unchanged.
    isolation = getattr(args, "isolation", "per-track")
    if isolation == "per-unit-phase" and track == "candidate":
        effective_diagnostic_profile = "trace-release-only"
    else:
        effective_diagnostic_profile = getattr(args, "diagnostic_profile", "baseline")
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
        str(args.output_dir),
        "--candidate-mode",
        args.candidate_mode,
        "--rss-mode",
        "in-process",
        "--evidence-mode",
        getattr(args, "evidence_mode", "auto"),
        "--diagnostic-profile",
        effective_diagnostic_profile,
        "--warmup-runs",
        str(getattr(args, "warmup_runs", 0)),
        "--_track-worker",
        track,
        "--_worker-result",
        str(result_path),
    ]
    worker_timeout = getattr(args, "worker_timeout_seconds", None)
    if worker_timeout is not None:
        command.extend(["--worker-timeout-seconds", str(worker_timeout)])
    return command


def _worker_outcome(payload: dict[str, Any]) -> FourLayerPlanningOutcome:
    plan_set = four_layer_route_plan_set_from_dict(payload["plan_set"])
    return FourLayerPlanningOutcome(
        plan_set=plan_set,
        snapshot=LayeredStoreSnapshot(None, None, None, False),
        published=False,
    )


def _run_isolated_track(
    *,
    prepared: _PreparedShadow,
    args: argparse.Namespace,
    track: str,
) -> tuple[FourLayerPlanningOutcome, list[dict[str, Any]], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix=f"{track}-worker-", dir=args.output_dir) as tmp:
        result_path = Path(tmp) / "result.json"
        try:
            completed = subprocess.run(
                _worker_command(args=args, track=track, result_path=result_path),
                check=False,
                capture_output=True,
                text=True,
                timeout=float(args.worker_timeout_seconds),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{track} worker TIMEOUT after {args.worker_timeout_seconds:g}s"
            ) from exc
        if not result_path.exists():
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"{track} worker exited without a result"
                + (f": {detail[-1000:]}" if detail else "")
            )
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if completed.returncode != 0 or not payload.get("ok", False):
            error = payload.get("error", {})
            message = error.get("message") or completed.stderr.strip() or "worker failed"
            error_type = error.get("type", "RuntimeError")
            raise RuntimeError(f"{track} worker {error_type}: {message}")
        resources = dict(payload["resources"])
        if payload.get("shadow_metadata") is not None:
            resources["shadow_metadata"] = payload["shadow_metadata"]
        records = list(payload.get("records", []))
        records.extend(_shadow_sidecar_records(payload.get("shadow_metadata")))
        return (
            _worker_outcome(payload),
            records,
            resources,
        )


def _run_isolated_case(
    *,
    prepared: _PreparedShadow,
    args: argparse.Namespace,
    execution_order: str,
) -> tuple[
    dict[str, FourLayerPlanningOutcome],
    dict[str, list[dict[str, Any]]],
    CommittedRiskWindow,
    dict[str, dict[str, Any]],
]:
    outcomes: dict[str, FourLayerPlanningOutcome] = {}
    records: dict[str, list[dict[str, Any]]] = {}
    resources: dict[str, dict[str, Any]] = {}
    order = (
        ("control", "candidate") if execution_order == "control-first" else ("candidate", "control")
    )
    for track in order:
        outcome, track_records, track_resources = _run_isolated_track(
            prepared=prepared,
            args=args,
            track=track,
        )
        outcomes[track] = outcome
        records[track] = track_records
        resources[track] = track_resources
    # The child owns the long execution lease. Re-acquire it only to validate
    # the shared identity and provide the exact private frames for integrity.
    with prepared.store.lease_committed_window(prepared.query) as current:
        _identity_check_for_case(prepared, current)
        private = _private_window(current, prepared.query)
    return outcomes, records, private, resources


def _run_track_worker(args: argparse.Namespace) -> int:
    if args._worker_result is None or args._track_worker is None:
        raise ValueError("track worker requires --_worker-result and --_track-worker")
    cpu_pin_cpu = _pin_worker_cpu()
    cpu_pin_succeeded = cpu_pin_cpu is not None
    started = time.perf_counter()
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    swap_before = _swap_counters()
    process_swap_before_kib = _process_swap_kib()
    try:
        prepared = _prepare(args)
        if args.worker_timeout_seconds is None:
            args.worker_timeout_seconds = float(prepared.spec.per_stage_timeout_seconds)
        if args.candidate_mode == "control-trace":
            outcome, records, resources, shadow_metadata = _prepared_shadow_track(
                prepared=prepared,
                track=args._track_worker,
                candidate_mode=args.candidate_mode,
                diagnostic_profile=getattr(args, "diagnostic_profile", "baseline"),
                cpu_pin_cpu=cpu_pin_cpu,
                cpu_pin_succeeded=cpu_pin_succeeded,
                warmup_runs=getattr(args, "warmup_runs", 0),
            )
            payload = {
                "ok": True,
                "schema_version": "orchestrator.winter-p2-track-worker.v2",
                "track": args._track_worker,
                "candidate_mode": args.candidate_mode,
                "candidate_algorithm": _mode_metadata(args.candidate_mode)[
                    "candidate_algorithm"
                ],
                "published_in_scratch": outcome.published,
                "plan_set": _plan_set_document(outcome),
                "records": records,
                "resources": resources,
                "shadow_metadata": shadow_metadata,
            }
        else:
            with prepared.store.lease_committed_window(prepared.query) as current:
                _identity_check_for_case(prepared, current)
                control_planner, candidate_planner, private = _make_planners(
                    current,
                    prepared.query,
                    prepared.configuration,
                    candidate_mode=args.candidate_mode,
                )
                tracks = _track_configuration(
                    control_planner=control_planner,
                    candidate_planner=candidate_planner,
                    private=private,
                    prepared=prepared,
                    candidate_mode=args.candidate_mode,
                )
                planner, planner_version = tracks[args._track_worker]
                outcome, records = _run_track(
                    planner=planner,
                    request=prepared.request,
                    configuration=prepared.configuration,
                    planner_version=planner_version,
                )
                payload = {
                    "ok": True,
                    "schema_version": "orchestrator.winter-p2-track-worker.v1",
                    "track": args._track_worker,
                    "candidate_mode": args.candidate_mode,
                    "candidate_algorithm": _mode_metadata(args.candidate_mode)[
                        "candidate_algorithm"
                    ],
                    "published_in_scratch": outcome.published,
                    "plan_set": _plan_set_document(outcome),
                    "records": records,
                    "resources": _resource_snapshot(
                        started,
                        swap_before=swap_before,
                        swap_after=_swap_counters(),
                        process_swap_before_kib=process_swap_before_kib,
                        process_swap_after_kib=_process_swap_kib(),
                        cpu_pin_cpu=cpu_pin_cpu,
                        cpu_pin_succeeded=cpu_pin_succeeded,
                        usage_before=usage_before,
                    ),
                }
    except Exception as error:
        payload = {
            "ok": False,
            "schema_version": "orchestrator.winter-p2-track-worker.v2",
            "track": args._track_worker,
            "error": {"type": type(error).__name__, "message": str(error)},
            "resources": _resource_snapshot(
                started,
                swap_before=swap_before,
                swap_after=_swap_counters(),
                process_swap_before_kib=process_swap_before_kib,
                process_swap_after_kib=_process_swap_kib(),
                cpu_pin_cpu=cpu_pin_cpu,
                cpu_pin_succeeded=cpu_pin_succeeded,
                usage_before=usage_before,
            ),
        }
    _write_json(args._worker_result, payload)
    return 0 if payload["ok"] else 1


def _prepare(args: argparse.Namespace) -> _PreparedShadow:
    commit = json.loads(args.risk_commit.read_text(encoding="utf-8"))
    spec = ExecutionSpec.from_path(args.execution_spec)
    run_context = load_run_context(args.run_context)
    query = _risk_query(commit)
    _validate_identity(commit=commit, spec=spec, run_context=run_context, query=query)
    configuration = load_configuration(
        args.c_config_root,
        spec.scenario_id,
        shared_config_root=args.contracts_config_root,
    )
    store = PersistentRiskStore(args.risk_store_root)
    window = store.get_committed_window(query)
    if window.commit_id != commit["commit_id"] or window.content_digest != commit["content_digest"]:
        raise ValueError("loaded RiskFrame window differs from selected commit")
    endpoint_mapping = map_corridor_endpoints(
        configuration,
        window.frames[0],
        max_adjustment_km=spec.max_snap_km,
    )
    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=query.model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=spec.generation_id,
        input_revision=spec.input_revision,
        as_of_time=query.as_of,
        start_time=query.start,
        start=endpoint_mapping.start.node,
        goal=endpoint_mapping.goal.node,
        maximum_elapsed=query.end - query.start,
    )
    # Even the research runner must cross the formal B->C preparation fence.
    # The child track entrypoint below is deliberately narrower than the old
    # direct scratch-service path and requires the PreparedRiskPlanning
    # single-track shadow API.
    ingress = RiskSourcePlanningIngress(store, configuration=configuration)
    prepared = ingress.prepare(request)
    if (
        prepared.window.commit_id != window.commit_id
        or prepared.window.content_digest != window.content_digest
    ):
        raise ValueError("formal ingress preparation differs from selected RiskWindow commit")
    input_identity = {
        "run_context": run_context_to_dict(run_context),
        "execution_spec": spec.to_document(),
        "risk_commit_id": window.commit_id,
        "risk_content_digest": window.content_digest,
        "risk_frame_count": window.count,
        "risk_window_start": query.start.isoformat().replace("+00:00", "Z"),
        "risk_window_end": query.end.isoformat().replace("+00:00", "Z"),
        "risk_query": {
            "start": query.start.isoformat().replace("+00:00", "Z"),
            "end": query.end.isoformat().replace("+00:00", "Z"),
            "interval_seconds": int(query.interval.total_seconds()),
            "run_id": query.run_id,
            "scenario_id": query.scenario_id,
            "corridor_id": query.corridor_id,
            "generation_id": query.generation_id,
            "vessel_profile_id": query.vessel_profile_id,
            "config_digest": query.config_digest,
            "model_config_digest": query.model_config_digest,
            "as_of": query.as_of.isoformat().replace("+00:00", "Z"),
        },
        "input_revision": spec.input_revision,
        "planner_config_digest": configuration.planner_config_digest,
        "model_config_digest": query.model_config_digest,
        "endpoint_mapping": endpoint_mapping.to_document(),
    }
    return _PreparedShadow(
        spec=spec,
        commit=commit,
        commit_path=args.risk_commit,
        query=query,
        run_context=run_context,
        configuration=configuration,
        endpoint_mapping=endpoint_mapping,
        request=request,
        store=store,
        prepared=prepared,
        input_identity=input_identity,
    )


def _manifest(
    prepared: _PreparedShadow,
    args: argparse.Namespace,
    *,
    status: str,
) -> dict[str, Any]:
    mode_metadata = _mode_metadata(args.candidate_mode)
    orchestrator_root = Path(__file__).resolve().parents[1]
    work_package_c_root = _workspace_root() / "work_package_c"
    repositories = {
        "orchestrator": _git_environment(orchestrator_root),
        "work_package_c": _git_environment(work_package_c_root),
    }
    implementation_sha256 = {
        "winter_p2_shadow.py": _file_sha256(Path(__file__).resolve()),
        "work_package_c_ingress.py": _file_sha256(
            work_package_c_root / "src" / "arctic_route_planning" / "ingress.py"
        ),
        "work_package_c_control_trace_reuse.py": _file_sha256(
            work_package_c_root
            / "src"
            / "arctic_route_planning"
            / "planners"
            / "_archive"
            / "control_trace_reuse.py"
        ),
    }
    experiment_key = {
        "script_version": _SCRIPT_VERSION,
        "risk_content_digest": prepared.commit["content_digest"],
        "commit_id": prepared.commit["commit_id"],
        "run_id": prepared.spec.run_id,
        "generation_id": prepared.spec.generation_id,
        "input_revision": prepared.spec.input_revision,
        "screen_objective": args.screen_objective,
        "repetitions": args.repetitions,
        "execution_order": args.execution_order,
        "candidate_mode": args.candidate_mode,
        "rss_mode": args.rss_mode,
        "evidence_mode": getattr(args, "evidence_mode", "auto"),
        "evidence_mode_effective": getattr(
            args, "evidence_mode_effective", getattr(args, "evidence_mode", "auto")
        ),
        "diagnostic_profile": getattr(args, "diagnostic_profile", "baseline"),
        "implementation_sha256": implementation_sha256,
        "orchestrator_commit": repositories["orchestrator"].get("commit"),
        "work_package_c_commit": repositories["work_package_c"].get("commit"),
    }
    experiment_id = f"winter-p2-shadow-v4-{_canonical_digest(experiment_key)[:16]}"
    return {
        "schema_version": "orchestrator.winter-p2-shadow-manifest.v2",
        "experiment_id": experiment_id,
        "identity_kind": "experimental_shadow",
        "status": status,
        "script_version": _SCRIPT_VERSION,
        "candidate_mode": args.candidate_mode,
        "evidence_mode": getattr(args, "evidence_mode", "auto"),
        "evidence_mode_effective": getattr(
            args, "evidence_mode_effective", getattr(args, "evidence_mode", "auto")
        ),
        "diagnostic_profile": getattr(args, "diagnostic_profile", "baseline"),
        "candidate_algorithm": mode_metadata["candidate_algorithm"],
        "candidate_schema": mode_metadata["candidate_schema"],
        "sidecar_schema": mode_metadata["sidecar_schema"],
        "p2_reuse_claim": mode_metadata["p2_reuse_claim"],
        "p2_scope": (
            "same-goal shadow screening; no formal reuse publication"
            if args.candidate_mode == "exact-temporal"
            else "formal-prepared control-trace M2 shadow; separate tracks, no publication"
        ),
        "input_identity": prepared.input_identity,
        "input_files": {
            "risk_commit": str(prepared.commit_path),
            "risk_commit_sha256": _file_sha256(prepared.commit_path),
            "run_context": str(args.run_context),
            "run_context_sha256": _file_sha256(args.run_context),
            "execution_spec": str(args.execution_spec),
            "execution_spec_sha256": _file_sha256(args.execution_spec),
        },
        "repositories": {
            "orchestrator": repositories["orchestrator"],
            "work_package_c": repositories["work_package_c"],
        },
        "lock_sha256": {
            "orchestrator_uv_lock": _file_sha256(
                orchestrator_root / "uv.lock"
            ),
            "work_package_c_uv_lock": _file_sha256(work_package_c_root / "uv.lock"),
        },
        "implementation_sha256": implementation_sha256,
        "m2_policy": {
            "required_candidate_mode": "control-trace",
            "required_rss_mode": "isolated",
            "minimum_repetitions": _M2_MIN_REPETITIONS,
            "screening_repetitions": _M2_SCREENING_REPETITIONS,
            "screening_median_improvement_floor_percent": (
                _M2_SCREENING_IMPROVEMENT_FLOOR_PERCENT
            ),
            "overall_median_improvement_floor_percent": _M2_TOTAL_IMPROVEMENT_FLOOR_PERCENT,
            "per_layer_objective_regression_ceiling_percent": _M2_CELL_REGRESSION_CEILING_PERCENT,
            "full_voyage_trace_source_overhead_ceiling_percent": (
                _M2_TRACE_SOURCE_OVERHEAD_CEILING_PERCENT
            ),
            "overall_p95_regression_ceiling_percent": _M2_P95_REGRESSION_CEILING_PERCENT,
            "rss_ratio_ceiling": 1.10,
            "expected_trace_captured": _M2_EXPECTED_TRACE_COUNT,
            "expected_trace_hits": _M2_EXPECTED_HIT_COUNT,
            "expected_cold_control": _M2_EXPECTED_COLD_COUNT,
            "swap_required_zero": True,
            "percentile_method": "median exact; p95 nearest-rank ceil(0.95*n)-1",
        },
        "options": {
            "screen_objective": args.screen_objective,
            "screen_objective_semantics": (
                "reporting focus; all four layers and three objectives are retained"
            ),
            "repetitions": args.repetitions,
            "execution_order": args.execution_order,
            "prepare_only": args.prepare_only,
            "candidate_mode": args.candidate_mode,
            "rss_mode": args.rss_mode,
            "evidence_mode": getattr(args, "evidence_mode", "auto"),
            "evidence_mode_effective": getattr(
                args, "evidence_mode_effective", getattr(args, "evidence_mode", "auto")
            ),
            "diagnostic_profile": getattr(args, "diagnostic_profile", "baseline"),
            "warmup_runs": getattr(args, "warmup_runs", 0),
            "worker_timeout_seconds": args.worker_timeout_seconds,
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pid": os.getpid(),
        },
        "publication_boundary": {
            "formal_latest_store_written": False,
            "frozen_artifact_written": False,
            "candidate_presentation_published": False,
            "production_published": False,
            "shadow_store": "in-memory LayeredRoutePlanLatestStore only",
            "output_directory": str(args.output_dir),
        },
    }


def run(args: argparse.Namespace) -> int:
    args.execution_order = _validate_order(args.execution_order)
    args.candidate_mode = _validate_candidate_mode(
        getattr(args, "candidate_mode", "exact-temporal")
    )
    args.rss_mode = _validate_rss_mode(getattr(args, "rss_mode", "in-process"))
    args.evidence_mode = _validate_evidence_mode(
        getattr(args, "evidence_mode", "auto")
    )
    args.diagnostic_profile = _validate_diagnostic_profile(
        getattr(args, "diagnostic_profile", "baseline")
    )
    args.evidence_mode_effective = _effective_evidence_mode(args)
    if args.repetitions < 1:
        raise ValueError("repetitions must be positive")
    if args.candidate_mode == "control-trace" and args.rss_mode != "isolated":
        raise ValueError("control-trace M2 requires --rss-mode isolated")
    if args.diagnostic_profile != "baseline" and args.evidence_mode != "diagnostic":
        raise ValueError(
            "non-baseline diagnostic profiles require --evidence-mode diagnostic"
        )
    if args.evidence_mode == "diagnostic" and args.candidate_mode != "control-trace":
        raise ValueError("diagnostic evidence requires --candidate-mode control-trace")
    if (
        args.evidence_mode in {"diagnostic", "screening", "formal"}
        and args.candidate_mode != "control-trace"
    ):
        raise ValueError(
            "diagnostic, screening, and formal evidence require --candidate-mode control-trace"
        )
    _empty_output_dir(args.output_dir)
    prepared = _prepare(args)
    if args.worker_timeout_seconds is None:
        args.worker_timeout_seconds = float(prepared.spec.per_stage_timeout_seconds)
    if args.worker_timeout_seconds <= 0:
        raise ValueError("worker-timeout-seconds must be positive")
    manifest = _manifest(prepared, args, status="PREPARED")
    _write_json(args.output_dir / "manifest.json", manifest)
    _write_json(args.output_dir / "input-identity.json", prepared.input_identity)
    _write_json(args.output_dir / "endpoint-mapping.json", prepared.endpoint_mapping.to_document())
    if args.prepare_only:
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True), flush=True)
        return 0

    cases: list[dict[str, Any]] = []
    control_plan_sets: list[dict[str, Any]] = []
    candidate_plan_sets: list[dict[str, Any]] = []
    reuse_sidecars: list[dict[str, Any]] = []
    route_integrity: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    failures = 0
    cases_path = args.output_dir / "cases.jsonl"
    with cases_path.open("w", encoding="utf-8") as cases_file:
        for repetition in range(1, args.repetitions + 1):
            execution_order = _order_for(repetition, args.execution_order)
            started = time.perf_counter()
            mode_metadata = _mode_metadata(args.candidate_mode)
            case: dict[str, Any] = {
                "schema_version": "orchestrator.winter-p2-shadow-case.v2",
                "case_id": f"case-{repetition:03d}",
                "repetition": repetition,
                "execution_order": execution_order,
                "screen_objective": args.screen_objective,
                "candidate_mode": args.candidate_mode,
                "candidate_algorithm": mode_metadata["candidate_algorithm"],
                "candidate_schema": mode_metadata["candidate_schema"],
                "rss_mode": args.rss_mode,
                "evidence_mode": args.evidence_mode,
                "evidence_mode_effective": args.evidence_mode_effective,
                "diagnostic_profile": args.diagnostic_profile,
                "status": "PASS",
            }
            try:
                if args.rss_mode == "isolated":
                    outcomes, records, private, track_resources = _run_isolated_case(
                        prepared=prepared,
                        args=args,
                        execution_order=execution_order,
                    )
                else:
                    outcomes, records, private, track_resources = _run_in_process_case(
                        prepared=prepared,
                        args=args,
                        execution_order=execution_order,
                    )
                mode_metadata = _mode_metadata(args.candidate_mode)
                control_doc = _plan_set_document(outcomes["control"])
                candidate_doc = _plan_set_document(outcomes["candidate"])
                comparison = _plan_comparison(
                    outcomes["control"].plan_set,
                    outcomes["candidate"].plan_set,
                )
                integrity = {
                    "control": _integrity_document(
                        outcomes["control"].plan_set,
                        private.frames,
                        "control",
                    ),
                    "candidate": _integrity_document(
                        outcomes["candidate"].plan_set,
                        private.frames,
                        "candidate",
                    ),
                }
                candidate_sidecar_records = [
                    record
                    for record in records["candidate"]
                    if record.get("record_kind") == "shadow_sidecar"
                ]
                reuse = _reuse_sidecar(
                    candidate_records=candidate_sidecar_records or records["candidate"],
                    screen_objective=ObjectiveMode(args.screen_objective),
                    candidate_mode=args.candidate_mode,
                )
                case.update(
                    {
                        "rss_scope": (
                            "independent_child_process"
                            if args.rss_mode == "isolated"
                            else "combined_parent_process"
                        ),
                        "track_resources": track_resources,
                        "records": records,
                        "control": {
                            "published": outcomes["control"].published,
                            "scratch_published": bool(
                                track_resources.get("control", {})
                                .get("shadow_metadata", {})
                                .get("scratch_published", outcomes["control"].published)
                            ),
                            "production_published": False,
                            "plan_set": control_doc,
                            "plan_set_digest": _canonical_digest(
                                _runner_comparison_plan_set_document(control_doc)
                            ),
                            "planner_version": "time-dependent-a-star.v1",
                            "records": records["control"],
                        },
                        "candidate": {
                            "published": outcomes["candidate"].published,
                            "scratch_published": bool(
                                track_resources.get("candidate", {})
                                .get("shadow_metadata", {})
                                .get("scratch_published", outcomes["candidate"].published)
                            ),
                            "production_published": False,
                            "plan_set": candidate_doc,
                            "plan_set_digest": _canonical_digest(
                                _runner_comparison_plan_set_document(candidate_doc)
                            ),
                            "planner_version": mode_metadata["candidate_algorithm"],
                            "candidate_mode": args.candidate_mode,
                            "candidate_schema": mode_metadata["candidate_schema"],
                            "records": records["candidate"],
                        },
                        "publication_boundary": {
                            "formal_latest_store_written": False,
                            "frozen_artifact_written": False,
                            "candidate_presentation_published": False,
                            "production_published": False,
                            "control_scratch_published": bool(
                                track_resources.get("control", {})
                                .get("shadow_metadata", {})
                                .get("scratch_published", outcomes["control"].published)
                            ),
                            "candidate_scratch_published": bool(
                                track_resources.get("candidate", {})
                                .get("shadow_metadata", {})
                                .get("scratch_published", outcomes["candidate"].published)
                            ),
                        },
                        "reuse_sidecar": reuse,
                        "route_integrity": integrity,
                        "comparison": comparison,
                    }
                )
                semantic_ok = (
                    comparison["status"] == "PASS"
                    and comparison["pair_count"] == 12
                    and all(
                        item["status"] == "PASS" and item["route_count"] == 12
                        for item in integrity.values()
                    )
                )
                nonpublication_ok = all(
                    not bool(case_track.get("production_published", False))
                    for case_track in (case["control"], case["candidate"])
                )
                timing_ok = True
                if args.candidate_mode == "control-trace":
                    timing_ok = all(
                        len(_timing_rows(records[track])) == 12
                        for track in ("control", "candidate")
                    )
                case["status"] = (
                    "PASS" if semantic_ok and nonpublication_ok and timing_ok else "FAIL"
                )
            except Exception as error:  # record evidence and continue repetitions
                failures += 1
                case.update(
                    {
                        "status": "FAIL",
                        "error": {
                            "type": type(error).__name__,
                            "message": str(error),
                        },
                    }
                )
            case["wall_seconds"] = time.perf_counter() - started
            if case.get("track_resources"):
                case["peak_rss_kib"] = max(
                    int(item["peak_rss_kib"]) for item in case["track_resources"].values()
                )
            else:
                case["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            cases.append(case)
            if "control" in case and "candidate" in case:
                control_plan_sets.append(
                    {
                        "case_id": case["case_id"],
                        "plan_set": case["control"]["plan_set"],
                    }
                )
                candidate_plan_sets.append(
                    {
                        "case_id": case["case_id"],
                        "plan_set": case["candidate"]["plan_set"],
                    }
                )
                reuse_sidecars.append({"case_id": case["case_id"], **case["reuse_sidecar"]})
                route_integrity.append({"case_id": case["case_id"], **case["route_integrity"]})
                comparisons.append({"case_id": case["case_id"], **case["comparison"]})
            cases_file.write(
                json.dumps(case, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
            )
            # The exception path increments ``failures`` above; ordinary
            # comparison/integrity failures need the same accounting.
            if case["status"] != "PASS" and "error" not in case:
                failures += 1
    manifest["status"] = "PASS" if failures == 0 else "FAIL"
    manifest["case_count"] = len(cases)
    manifest["passed_cases"] = sum(case["status"] == "PASS" for case in cases)
    manifest["failed_cases"] = sum(case["status"] != "PASS" for case in cases)
    manifest["p2_reuse_claim"] = _mode_metadata(args.candidate_mode)["p2_reuse_claim"]
    if args.candidate_mode == "control-trace":
        if args.evidence_mode_effective == "diagnostic":
            m2_summary = None
            diagnostic_summary = _diagnostic_summary(
                cases,
                required_sample_count=args.repetitions,
                diagnostic_profile=args.diagnostic_profile,
            )
            manifest["diagnostic_summary"] = diagnostic_summary
            manifest["formal_gate_verdict"] = "NOT_APPLICABLE"
            manifest["m2_gate_verdict"] = "NOT_APPLICABLE"
            if diagnostic_summary["gate_verdict"] == "FAIL":
                failures += 1
                manifest["status"] = "FAIL"
        else:
            m2_summary = _m2_summary(cases)
            manifest["m2_summary"] = m2_summary
            screening_verdict = m2_summary["screening"]["gate_verdict"]
            manifest["m2_screening_gate_verdict"] = screening_verdict
            order_summary = _order_stratified_summary(cases)
            manifest["order_stratified_summary"] = order_summary
            order_gate = bool(order_summary.get("balanced")) and (
                order_summary.get("max_order_gap_percent_points") is not None
                and order_summary["max_order_gap_percent_points"]
                <= _DIAGNOSTIC_ORDER_GAP_CEILING_PERCENT_POINTS
            )
            manifest["order_consistency_gate"] = "PASS" if order_gate else "FAIL"
            if args.evidence_mode_effective == "screening":
                if screening_verdict == "FAIL" or not order_gate:
                    failures += 1
                    manifest["status"] = "FAIL"
                manifest["m2_gate_verdict"] = "NOT_APPLICABLE_SCREENING"
            elif args.evidence_mode_effective == "formal":
                if m2_summary["gate_verdict"] == "FAIL" or not order_gate:
                    failures += 1
                    manifest["status"] = "FAIL"
                manifest["m2_gate_verdict"] = (
                    "PASS" if m2_summary["gate_verdict"] == "PASS" and order_gate else "FAIL"
                )
            else:
                if (
                    args.repetitions == _M2_SCREENING_REPETITIONS
                    and screening_verdict == "FAIL"
                ):
                    failures += 1
                    manifest["status"] = "FAIL"
                if m2_summary["gate_verdict"] == "FAIL" and args.repetitions >= _M2_MIN_REPETITIONS:
                    failures += 1
                    manifest["status"] = "FAIL"
                manifest["m2_gate_verdict"] = m2_summary["gate_verdict"]
    else:
        m2_summary = None
    manifest["failed_cases"] = sum(case["status"] != "PASS" for case in cases)
    manifest["runner_failures"] = failures
    _write_json(args.output_dir / "manifest.json", manifest)
    _write_json(
        args.output_dir / "control-plan-sets.json",
        {
            "schema_version": "orchestrator.winter-p2-control-plan-sets.v1",
            "planner_algorithm": "time-dependent-a-star.v1",
            "cases": control_plan_sets,
        },
    )
    _write_json(
        args.output_dir / "candidate-plan-sets.json",
        {
            "schema_version": mode_metadata["candidate_schema"],
            "candidate_mode": args.candidate_mode,
            "candidate_algorithm": mode_metadata["candidate_algorithm"],
            "cases": candidate_plan_sets,
        },
    )
    _write_json(
        args.output_dir / "reuse-sidecars.json",
        {
            "schema_version": (
                "orchestrator.winter-p2-reuse-sidecars.v1"
                if args.candidate_mode == "exact-temporal"
                else "orchestrator.winter-p2-control-trace-sidecars.v1"
            ),
            "candidate_mode": args.candidate_mode,
            "cases": reuse_sidecars,
        },
    )
    _write_json(
        args.output_dir / "route-integrity.json",
        {"schema_version": "orchestrator.winter-p2-route-integrity.v1", "cases": route_integrity},
    )
    _write_json(
        args.output_dir / "comparisons.json",
        {"schema_version": "orchestrator.winter-p2-comparisons.v1", "cases": comparisons},
    )
    _write_json(
        args.output_dir / "comparison-summary.json",
        {
            "status": manifest["status"],
            "case_count": len(cases),
            "passed_cases": manifest["passed_cases"],
            "failed_cases": manifest["failed_cases"],
            "screen_objective": args.screen_objective,
            "candidate_mode": args.candidate_mode,
            "rss_mode": args.rss_mode,
            "m2_gate_verdict": manifest.get("m2_gate_verdict", "NOT_APPLICABLE"),
            "evidence_mode": args.evidence_mode,
            "evidence_mode_effective": args.evidence_mode_effective,
            "diagnostic_profile": args.diagnostic_profile,
            "m2_summary": m2_summary,
            "diagnostic_summary": manifest.get("diagnostic_summary"),
            "order_stratified_summary": manifest.get("order_stratified_summary"),
        },
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if failures == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="winter-p2-shadow")
    parser.add_argument("--risk-store-root", type=Path, required=True)
    parser.add_argument("--risk-commit", type=Path, required=True)
    parser.add_argument("--run-context", type=Path, required=True)
    parser.add_argument("--execution-spec", type=Path, required=True)
    parser.add_argument(
        "--c-config-root",
        type=Path,
        default=_workspace_root() / "work_package_c" / "configs",
    )
    parser.add_argument(
        "--contracts-config-root",
        type=Path,
        default=_workspace_root() / "arctic_route_contracts" / "configs",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--screen-objective",
        choices=tuple(mode.value for mode in ObjectiveMode),
        default=ObjectiveMode.RECOMMENDED.value,
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--warmup-runs",
        type=int,
        default=0,
        help=(
            "M2K symmetric warm-up: run each shadow track this many times before the "
            "timed run and discard results, removing first-track cold-start bias. "
            "Default 0 preserves legacy M2J behavior."
        ),
    )
    parser.add_argument(
        "--execution-order",
        default="alternate",
        help="control-first, candidate-first, alternate, or control,candidate",
    )
    parser.add_argument(
        "--candidate-mode",
        choices=_CANDIDATE_MODE_VALUES,
        default="exact-temporal",
        help="exact-temporal (legacy reproducible) or control-trace (P2.1)",
    )
    parser.add_argument(
        "--rss-mode",
        choices=_RSS_MODE_VALUES,
        default="in-process",
        help="combined in-process RSS or isolated per-track child-process RSS",
    )
    parser.add_argument(
        "--isolation",
        choices=_ISOLATION_VALUES,
        default="per-track",
        help=(
            "M2J candidate isolation granularity. 'per-track' (default) keeps the "
            "existing isolated per-track subprocess. 'per-unit-phase' forces the "
            "candidate track to the trace-release-only diagnostic profile so the "
            "full_voyage trace payload is gc-collected before rolling/executable "
            "(eliminating within-process trace-memory pollution); the literal "
            "per-unit subprocess split is deferred (layer anchor coupling)."
        ),
    )
    parser.add_argument(
        "--evidence-mode",
        choices=_EVIDENCE_MODE_VALUES,
        default="auto",
        help=(
            "auto preserves legacy repetition-based gates; diagnostic records "
            "full-track causal evidence without an M2 performance verdict"
        ),
    )
    parser.add_argument(
        "--diagnostic-profile",
        choices=_DIAGNOSTIC_PROFILE_VALUES,
        default="baseline",
        help="shadow-only diagnostic profile; valid only with --evidence-mode diagnostic",
    )
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        default=None,
        help="hard timeout for each isolated track worker; defaults to ExecutionSpec",
    )
    parser.add_argument(
        "--_track-worker",
        choices=("control", "candidate"),
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_worker-result",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args._track_worker is not None:
        return _run_track_worker(args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
