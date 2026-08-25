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
from arctic_route_planning.planners.control_trace_reuse import (
    trace_plan as control_trace_plan,
)
from arctic_route_planning.planners.control_trace_reuse import (
    try_reuse as try_control_trace_reuse,
)
from arctic_route_planning.planners.temporal_label_astar import TemporalLabelAStar
from arctic_route_planning.planners.temporal_reuse import (
    TemporalCertifiedGoal,
    TemporalReuseOutcome,
    certify_session,
    try_reuse,
)
from arctic_route_planning.planners.temporal_session import TemporalSessionIdentity
from arctic_route_planning.publishing import (
    four_layer_route_plan_set_to_dict,
    route_plan_v3_semantic_digest,
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

_SCRIPT_VERSION = "winter-p2-shadow.v2"
_CANDIDATE_VERSION = "temporal-label-astar.shadow.v1"
_CONTROL_TRACE_VERSION = "time-dependent-a-star.control-trace.v1"
_ORDER_VALUES = ("control-first", "candidate-first", "alternate")
_CANDIDATE_MODE_VALUES = ("exact-temporal", "control-trace")
_RSS_MODE_VALUES = ("in-process", "isolated")
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
            name, _, value = line.partition(" ")
            if name in {"pswpin", "pswpout"}:
                counters[name] = int(value.strip())
    except (OSError, ValueError):
        return None
    return counters if counters else None


def _swap_delta(
    before: dict[str, int] | None,
    after: dict[str, int] | None,
) -> dict[str, int] | None:
    if before is None or after is None:
        return None
    return {
        name: max(0, int(after.get(name, 0)) - int(before.get(name, 0)))
        for name in ("pswpin", "pswpout")
    }


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
        control_route_digest = route_plan_v3_semantic_digest(control_plan)
        candidate_route_digest = route_plan_v3_semantic_digest(candidate_plan)
        route_equal = (
            _waypoint_signature(control_plan) == _waypoint_signature(candidate_plan)
            and max(speed_deltas, default=0.0) <= _NUMERIC_TOLERANCE
            and control_plan.source_risk_ids == candidate_plan.source_risk_ids
            and control_plan.destination_reached == candidate_plan.destination_reached
            and control_plan.layer_goal_reached == candidate_plan.layer_goal_reached
            and control_metrics.hard_constraint_violations
            == candidate_metrics.hard_constraint_violations
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
        "control_plan_set_digest": _canonical_digest(control_doc),
        "candidate_plan_set_digest": _canonical_digest(candidate_doc),
        "comparison_policy": {
            "eta_tolerance_seconds": _ETA_TOLERANCE_SECONDS,
            "numeric_tolerance": _NUMERIC_TOLERANCE,
            "compute_and_expansion_differences_are_diagnostic_only": True,
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
        rows.append(
            {
                "layer": layer,
                "objective": objective,
                "wall_ms": float(wall_ms),
            }
        )
    return rows


def _timing_cell_map(records: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    rows = _timing_rows(records)
    return {(row["layer"], row["objective"]): row["wall_ms"] for row in rows}


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
    semantic_pass = []
    determinism_inputs: dict[str, list[Any]] = {"control": [], "candidate": []}
    rss_ratios: list[float] = []
    swap_deltas: list[dict[str, int]] = []
    for case in cases:
        counts = _trace_counts(case.get("records", {}).get("candidate", []))
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
        for track_resources in resources.values():
            if isinstance(track_resources, dict) and isinstance(
                track_resources.get("swap_delta"), dict
            ):
                swap_deltas.append(
                    {
                        name: int(track_resources["swap_delta"].get(name, 0))
                        for name in ("pswpin", "pswpout")
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
    resource_gate = independent_rss and (rss_median is not None and rss_median <= 1.10)
    swap_gate = bool(swap_deltas) and all(
        delta["pswpin"] == 0 and delta["pswpout"] == 0 for delta in swap_deltas
    )
    gate_verdict = (
        "PASS"
        if sufficient
        and semantic_gate
        and reuse_gate
        and deterministic
        and overall_gate == "PASS"
        and cell_gate
        and resource_gate
        and swap_gate
        else "NOT_EVALUATED_INSUFFICIENT_REPETITIONS"
        if not sufficient
        else "FAIL"
    )
    screening_gate = (
        "PASS"
        if screening_sufficient
        and semantic_gate
        and reuse_gate
        and deterministic
        and improvement is not None
        and improvement >= _M2_SCREENING_IMPROVEMENT_FLOOR_PERCENT
        and bool(cell_summary)
        and all(item["gate"] == "PASS" for item in cell_summary)
        and resource_gate
        and swap_gate
        else "NOT_EVALUATED_INSUFFICIENT_REPETITIONS"
        if not screening_sufficient
        else "FAIL"
    )
    return {
        "schema_version": "orchestrator.winter-p2-m2-summary.v1",
        "sample_count": sample_count,
        "minimum_repetitions": _M2_MIN_REPETITIONS,
        "screening": {
            "minimum_repetitions": _M2_SCREENING_REPETITIONS,
            "median_improvement_floor_percent": (
                _M2_SCREENING_IMPROVEMENT_FLOOR_PERCENT
            ),
            "gate_verdict": screening_gate,
        },
        "semantic_gate": "PASS" if semantic_gate else "FAIL",
        "reuse_matrix_gate": "PASS" if reuse_gate else "FAIL",
        "determinism_gate": "PASS" if deterministic else "FAIL",
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
            "gate": "PASS" if swap_gate else "FAIL" if sufficient else "NOT_MEASURED",
        },
        "expected_trace_hit_cold": {
            "trace_captured": _M2_EXPECTED_TRACE_COUNT,
            "trace_hits": _M2_EXPECTED_HIT_COUNT,
            "cold_control": _M2_EXPECTED_COLD_COUNT,
        },
        "gate_verdict": gate_verdict,
        "percentile_method": "median exact; p95 nearest-rank ceil(0.95*n)-1",
    }


def _reuse_sidecar(
    *,
    candidate_records: list[dict[str, Any]],
    screen_objective: ObjectiveMode,
    candidate_mode: str = "exact-temporal",
) -> dict[str, Any]:
    mode_metadata = _mode_metadata(candidate_mode)
    if candidate_mode == "control-trace":
        hits = [record for record in candidate_records if record.get("reuse_hit")]
        trace_records = [
            record
            for record in candidate_records
            if (record.get("certificate") or {}).get("status") == "CERTIFIED_TRACE"
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
) -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "wall_seconds": time.perf_counter() - started,
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "peak_rss_kib": usage.ru_maxrss,
        "swap_before": swap_before,
        "swap_after": swap_after,
        "swap_delta": _swap_delta(swap_before, swap_after),
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
                "reuse_status": "TRACE_CAPTURED",
                "reuse_lookup_status": "TRACE_CAPTURED",
                "reuse_hit": False,
                "search_used": True,
                "certificate": {"status": "CERTIFIED_TRACE"},
            }
        )
    for observation in metadata.get("reuse_outcomes", ()):
        if not isinstance(observation, dict):
            continue
        status = str(observation.get("status", ""))
        records.append(
            {
                **observation,
                "record_kind": "shadow_sidecar",
                "reuse_status": status,
                "reuse_lookup_status": status,
                "reuse_hit": bool(observation.get("reused", False)),
                "search_used": bool(observation.get("used_search", False)),
                "certificate": {"status": "CERTIFIED_TRACE"},
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
) -> tuple[Any, list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Execute exactly one formal-prepared shadow track.

    This is intentionally a strict protocol boundary.  The C side must expose
    ``execute_four_layer_temporal_shadow_track`` and return an object with an
    ``outcome`` and per-layer/objective ``timings``.  Falling back to the old
    combined shadow call would make isolated RSS and timing claims false.
    """

    if candidate_mode != "control-trace":
        raise ValueError("the single-track formal shadow runner requires control-trace")
    if track not in {"control", "candidate"}:
        raise ValueError("track must be control or candidate")
    method = getattr(prepared.prepared, "execute_four_layer_temporal_shadow_track", None)
    if not callable(method):
        raise RuntimeError(
            "PreparedRiskPlanning lacks the required single-track shadow API: "
            "execute_four_layer_temporal_shadow_track"
        )
    started = time.perf_counter()
    swap_before = _swap_counters()
    result = method(track=track, candidate_mode="control_trace")
    elapsed_seconds = time.perf_counter() - started
    swap_after = _swap_counters()
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
    )
    metadata = {
        "production_published": False,
        "scratch_published": bool(getattr(result, "scratch_published", False)),
        "reuse_outcomes": _as_document(getattr(result, "reuse_outcomes", ())),
        "trace_observations": _as_document(getattr(result, "trace_observations", ())),
        "scratch_proof": scratch_proof,
        "api": (
            "RiskSourcePlanningIngress.prepare/"
            "PreparedRiskPlanning.execute_four_layer_temporal_shadow_track"
        ),
        "track": track,
        "elapsed_seconds": elapsed_seconds,
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
    started = time.perf_counter()
    swap_before = _swap_counters()
    try:
        prepared = _prepare(args)
        if args.worker_timeout_seconds is None:
            args.worker_timeout_seconds = float(prepared.spec.per_stage_timeout_seconds)
        if args.candidate_mode == "control-trace":
            outcome, records, resources, shadow_metadata = _prepared_shadow_track(
                prepared=prepared,
                track=args._track_worker,
                candidate_mode=args.candidate_mode,
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
    }
    experiment_id = f"winter-p2-shadow-v2-{_canonical_digest(experiment_key)[:16]}"
    return {
        "schema_version": "orchestrator.winter-p2-shadow-manifest.v2",
        "experiment_id": experiment_id,
        "identity_kind": "experimental_shadow",
        "status": status,
        "script_version": _SCRIPT_VERSION,
        "candidate_mode": args.candidate_mode,
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
            "orchestrator": _git_environment(Path(__file__).resolve().parents[1]),
            "work_package_c": _git_environment(_workspace_root() / "work_package_c"),
        },
        "lock_sha256": {
            "orchestrator_uv_lock": _file_sha256(
                Path(__file__).resolve().parents[1] / "uv.lock"
            ),
            "work_package_c_uv_lock": _file_sha256(
                _workspace_root() / "work_package_c" / "uv.lock"
            ),
        },
        "implementation_sha256": {
            "winter_p2_shadow.py": _file_sha256(Path(__file__).resolve()),
            "work_package_c_ingress.py": _file_sha256(
                _workspace_root()
                / "work_package_c"
                / "src"
                / "arctic_route_planning"
                / "ingress.py"
            ),
            "work_package_c_control_trace_reuse.py": _file_sha256(
                _workspace_root()
                / "work_package_c"
                / "src"
                / "arctic_route_planning"
                / "planners"
                / "control_trace_reuse.py"
            ),
        },
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
    if args.repetitions < 1:
        raise ValueError("repetitions must be positive")
    if args.candidate_mode == "control-trace" and args.rss_mode != "isolated":
        raise ValueError("control-trace M2 requires --rss-mode isolated")
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
                            "plan_set_digest": _canonical_digest(control_doc),
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
                            "plan_set_digest": _canonical_digest(candidate_doc),
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
        m2_summary = _m2_summary(cases)
        manifest["m2_summary"] = m2_summary
        screening_verdict = m2_summary["screening"]["gate_verdict"]
        manifest["m2_screening_gate_verdict"] = screening_verdict
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
            "m2_summary": m2_summary,
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
