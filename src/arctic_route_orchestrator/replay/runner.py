"""Event-driven causal replay runner (Strategy B).

Lifecycle per tick:

    advance simulation clock
    -> knowledge_as_of = simulation_time
    -> resolve A visibility (issue_time <= knowledge_as_of)
    -> visible / B-relevant digests
    -> decide B reuse vs recompute
    -> evaluate replan policy
    -> decide C reuse vs replan
    -> publish SimulationSnapshot (atomic) + checkpoint

The runner is replay-local: it never mutates the frozen retrospective path,
never lowers the issue-time gate, and publishes only inside the configured
output root.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctic_route_contracts import (
    RunContext,
    canonical_sha256,
    configuration_digest,
    load_run_context,
)
from arctic_route_data import (
    PartitionedABCache,
    SimulationClock,
    WorkPackageA,
)
from arctic_route_data.causal_replay import (
    REQUIRED_FORMAL_DATA_TYPES,
    STATIC_TYPES,
    SourceRecord,
    load_manifest_records,
)
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import ProvenanceKind
from arctic_route_planning.domain import ReplanReason
from arctic_route_planning.endpoints import map_corridor_endpoints
from arctic_route_planning.grid import RegularGrid, haversine_km
from arctic_route_planning.ingress import RiskSourcePlanningIngress
from arctic_route_planning.service import ServicePlanningRequest

from arctic_route_orchestrator.replay import parallel as replay_parallel
from arctic_route_orchestrator.replay.digests import (
    b_relevant_input_digest,
    risk_semantic_digest,
    route_semantic_digest,
    visible_record_set_digest,
)
from arctic_route_orchestrator.replay.models import (
    DataVisibilitySummary,
    NavigationExecutionState,
    PlanningStateSummary,
    ReadinessSummary,
    ReplayEvent,
    ReplayManifest,
    RiskStateSummary,
    SimulationSnapshot,
)
from arctic_route_orchestrator.replay.vessel_motion import (
    InvalidRouteTimingError,
    VesselState,
    vessel_state_at,
)

EXPECTED_INTERVAL_HOURS: dict[str, float | None] = {
    "land_sea_mask": None,
    "ocean_current": 1.0,
    "sea_ice_concentration": 1.0,
    "sea_ice_drift": 1.0,
    "sea_ice_edge": 1.0,
    "sea_ice_thickness": 1.0,
    "sea_ice_type": 1.0,
    "temperature": 3.0,
    "visibility": 3.0,
    "water_level": 1.0,
    "wave": 3.0,
    "wind_field": 3.0,
}


@dataclass(frozen=True, slots=True)
class ReplayPaths:
    output_root: Path
    risk_store_root: Path
    snapshots_dir: Path
    logs_dir: Path
    heartbeat_path: Path
    checkpoint_path: Path
    manifest_path: Path
    summary_path: Path

    @classmethod
    def create(cls, output_root: str | Path, replay_id: str) -> ReplayPaths:
        root = Path(output_root)
        paths = ReplayPaths(
            output_root=root,
            risk_store_root=root / "risk-store",
            snapshots_dir=root / "snapshots",
            logs_dir=root / "logs",
            heartbeat_path=root / "heartbeat.json",
            checkpoint_path=root / "checkpoint.json",
            manifest_path=root / "causal-replay-manifest.json",
            summary_path=root / "replay-summary.json",
        )
        for directory in (
            root,
            paths.risk_store_root,
            paths.snapshots_dir,
            paths.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return paths


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(path: Path, document: Any) -> None:
    _atomic_write(
        path,
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def _heartbeat(path: Path, stage: str, tick: int, elapsed: float, rss_mb: float) -> None:
    with suppress(OSError):
        _write_json(
            path,
            {
                "stage": stage,
                "tick": tick,
                "elapsed_seconds": round(elapsed, 1),
                "rss_mb": round(rss_mb, 1),
                "updated_at": _iso(datetime.now(UTC)),
            },
        )


def _rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        return 0.0
    return 0.0


@dataclass(slots=True)
class ReplayRunner:
    """One Scenario B causal replay execution."""

    replay_id: str
    scenario_id: str
    corridor_id: str
    replay_start: datetime
    replay_end: datetime
    tick_cadence_hours: int
    a_data_root: Path
    manifest_path: Path
    b_config_path: Path
    c_config_root: Path
    contracts_config_root: Path
    frozen_run_context_path: Path
    risk_forecast_end: datetime | None = None
    planning_horizon_hours: int | None = None
    v2_only: bool = False
    max_snap_km: float = 30.0
    cache_memory_mb: float = 2048.0
    c_attempt_timeout_seconds: int = 900
    planning_workers: int = 1
    replan_min_interval_hours: float | None = None
    replan_waypoint_aligned_only: bool = False
    parallel_pool_mode: str = "percall"
    pending_plan: Any = None
    pending_plan_set: Any = None
    pending_plan_kind: str = ""
    pending_decision_time: datetime | None = None
    pending_adoption_time: datetime | None = None
    pending_revision: int = 0
    pending_origin_node: tuple[int, int] | None = None
    pending_origin_adjustment_km: float | None = None
    pending_decision_position: dict[str, float] | None = None
    active_plan_time_offset: timedelta = field(default_factory=timedelta)
    superseded_route_payload: dict[str, Any] | None = None

    records: tuple[SourceRecord, ...] = ()
    run_context: RunContext | None = None
    paths: ReplayPaths | None = None
    risk_store: Any = None
    ingress: Any = None
    endpoint_mapping: Any = None
    configuration: Any = None

    # revision state
    data_revision: int = 0
    b_input_revision: int = 0
    risk_revision: int = 0
    plan_revision: int = 0
    input_revision: int = 0
    observation_sequence: int = 0
    risk_window_revision: int = 0
    navigation_state_revision: int = 0
    pre_planning_skips: int = 0
    replan_candidate_computations: int = 0
    replan_candidates_accepted: int = 0
    replan_candidates_rejected: int = 0
    planning_elapsed_seconds: float = 0.0
    tick_performance: list[dict[str, Any]] = field(default_factory=list)
    risk_semantic_digest: str = ""
    route_semantic_digests: dict[str, dict[str, str]] = field(default_factory=dict)
    last_replan_reasons: tuple[str, ...] = ()

    risk_commit: Any = None
    window_commit: Any = None
    risk_valid_start: datetime | None = None
    risk_valid_end: datetime | None = None
    prediction_as_of: datetime | None = None
    current_plan: Any = None
    current_plan_set: Any = None
    current_batch: Any = None
    plan_kind: str = ""
    supported_layers: tuple[str, ...] = ()
    unsupported_layers: tuple[str, ...] = (
        "executable_0_6h",
        "rolling_0_24h",
        "main_corridor_24_72h",
        "full_voyage",
    )
    planning_blockers: tuple[str, ...] = ()
    planning_valid_end: datetime | None = None
    v2_probe_eta_hours: float | None = None
    _route_integrity: dict[str, Any] | None = None
    events: list[ReplayEvent] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    _model_config_digest: str = ""
    _last_visible_digest: str = ""
    _last_relevant_digest: str = ""
    _last_data_revision: int = 0
    _last_window_identity: str = ""
    _initial_plan_attempted: bool = False
    nav_state: NavigationExecutionState | None = None
    _grid: Any = None
    _hard_mask: Any = None

    def run(
        self,
        *,
        output_root: str | Path | None = None,
        resume: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        self.paths = ReplayPaths.create(
            output_root or _default_output_root(self.replay_id),
            self.replay_id,
        )
        self.records = load_manifest_records(str(self.manifest_path), self.corridor_id)
        self.configuration = load_configuration(
            self.c_config_root,
            self.scenario_id,
            shared_config_root=self.contracts_config_root,
        )
        self.risk_forecast_end = self.risk_forecast_end or _common_causal_valid_end(
            self.records,
            self.replay_start,
        )
        forecast_hours = int(
            (self.risk_forecast_end - self.replay_start).total_seconds() // 3600
        )
        self.planning_horizon_hours = self.planning_horizon_hours or (
            _aligned_planning_hours(forecast_hours)
        )
        # Effective planning end is bounded by the causally-visible risk
        # forecast; the aligned scenario horizon only satisfies the corridor
        # contract (C's explicit maximum_elapsed stays inside risk coverage).
        self.planning_valid_end = self.risk_forecast_end
        self.configuration = _replay_configuration(
            self.configuration,
            replay_start=self.replay_start,
            planning_horizon_hours=self.planning_horizon_hours,
        )
        start_index = 0
        if resume and self.paths.checkpoint_path.is_file():
            checkpoint = json.loads(self.paths.checkpoint_path.read_text(encoding="utf-8"))
            start_index = int(checkpoint.get("last_completed_tick", -1)) + 1
            self._restore(checkpoint)
        tick = self.replay_start + timedelta(hours=start_index * self.tick_cadence_hours)
        index = start_index
        previous_visible: tuple[SourceRecord, ...] = ()
        while tick <= self.replay_end:
            tick_started = time.perf_counter()
            visible = tuple(
                record for record in self.records if record.issue_time <= tick
            )
            newly_visible = tuple(
                record
                for record in visible
                if record not in previous_visible
            )
            visible_digest = visible_record_set_digest(visible)
            relevant_digest = b_relevant_input_digest(
                visible,
                window_start=self.replay_start,
                window_end=self.replay_end,
                target_bbox=_corridor_bbox(self.configuration),
            )
            if index == 0 or visible_digest != self._last_visible_digest:
                self.data_revision += 1
            if self.risk_commit is None or relevant_digest != self._last_relevant_digest:
                self.b_input_revision += 1
            events: list[ReplayEvent] = [
                ReplayEvent(
                    type="CLOCK_TICK",
                    simulation_time=_iso(tick),
                    revision=str(self.data_revision),
                    observed=True,
                )
            ]
            if newly_visible:
                events.append(
                    ReplayEvent(
                        type="DATA_BECAME_VISIBLE",
                        simulation_time=_iso(tick),
                        revision=str(self.data_revision),
                        description=f"{len(newly_visible)} records",
                        observed=False,
                    )
                )
            if self.data_revision != self._last_data_revision:
                events.append(
                    ReplayEvent(
                        type="DATA_REVISION_CHANGED",
                        simulation_time=_iso(tick),
                        revision=str(self.data_revision),
                        observed=True,
                    )
                )
            if self.risk_commit is None or relevant_digest != self._last_relevant_digest:
                self._build_risk_window(tick, progress=progress)
                events.append(
                    ReplayEvent(
                        type="B_UPDATED",
                        simulation_time=_iso(tick),
                        revision=self.risk_commit.commit_id,
                        observed=True,
                    )
                )
                events.append(
                    ReplayEvent(
                        type="RISK_CONTENT_UPDATED",
                        simulation_time=_iso(tick),
                        revision=str(self.risk_revision),
                        observed=True,
                    )
                )
                events.append(
                    ReplayEvent(
                        type="RISK_REVISION_CHANGED",
                        simulation_time=_iso(tick),
                        revision=str(self.risk_revision),
                        observed=True,
                    )
                )
            else:
                events.append(
                    ReplayEvent(
                        type="B_REUSED",
                        simulation_time=_iso(tick),
                        revision=self._window_identity(tick).commit_id,
                        observed=True,
                    )
                )
            self.window_commit = self._window_identity(tick)
            if (
                self.window_commit is not None
                and self.window_commit.commit_id != self._last_window_identity
            ):
                self.risk_window_revision += 1
                events.append(
                    ReplayEvent(
                        type="RISK_WINDOW_ADVANCED",
                        simulation_time=_iso(tick),
                        revision=str(self.risk_window_revision),
                        description="suffix window advanced",
                        observed=False,
                    )
                )
                self._last_window_identity = self.window_commit.commit_id
            self._last_visible_digest = visible_digest
            self._last_relevant_digest = relevant_digest
            self._last_data_revision = self.data_revision

            planning_started = time.perf_counter()
            planning = self._planning_tick(tick, index, events, progress=progress)
            planning_seconds = time.perf_counter() - planning_started
            self.planning_elapsed_seconds += planning_seconds
            replan_triggered = any(
                event.type == "REPLAN_TRIGGERED" for event in events
            )
            replan_decided = any(
                event.type == "REPLAN_DECIDED" for event in events
            )
            plan_computed = any(
                event.type == "PLAN_COMPUTED" for event in events
            )
            plan_skipped = any(
                event.type == "REPLAN_SKIPPED" for event in events
            )
            plan_reused = any(
                event.type == "PLAN_REUSED" for event in events
            )
            candidate_computed = (
                plan_computed or replan_triggered
                or replan_decided
                or (plan_reused and not plan_skipped)
            )
            candidate_accepted = (
                plan_computed or replan_triggered or replan_decided
            )
            candidate_rejected = candidate_computed and not candidate_accepted
            if candidate_computed:
                self.replan_candidate_computations += 1
                if candidate_accepted:
                    self.replan_candidates_accepted += 1
                else:
                    self.replan_candidates_rejected += 1
            if plan_skipped:
                self.pre_planning_skips += 1
            self.tick_performance.append(
                {
                    "tick": index,
                    "simulation_time": _iso(tick),
                    "planning_seconds": round(planning_seconds, 2),
                    "candidate_computed": candidate_computed,
                    "candidate_accepted": candidate_accepted,
                    "candidate_rejected": candidate_rejected,
                    "pre_planning_skip": plan_skipped,
                }
            )
            self._update_navigation(tick)

            snapshot = self._snapshot(
                index=index,
                tick=tick,
                visible=visible,
                newly_visible=newly_visible,
                visible_digest=visible_digest,
                relevant_digest=relevant_digest,
                events=events,
                planning=planning,
            )
            self.snapshots.append(
                {
                    "index": index,
                    "simulation_time": _iso(tick),
                    "resource": f"snapshots/{index:04d}.json",
                    "digest": snapshot.snapshot_digest,
                }
            )
            _write_json(
                self.paths.snapshots_dir / f"{index:04d}.json",
                snapshot.to_dict(),
            )
            self._checkpoint(index, tick)
            _heartbeat(
                self.paths.heartbeat_path,
                "tick_completed",
                index,
                time.perf_counter() - started,
                _rss_mb(),
            )
            if progress is not None:
                progress(
                    {
                        "tick": index,
                        "simulation_time": _iso(tick),
                        "data_revision": self.data_revision,
                        "b_input_revision": self.b_input_revision,
                        "risk_revision": self.risk_revision,
                        "plan_revision": self.plan_revision,
                        "tick_seconds": round(time.perf_counter() - tick_started, 1),
                        "planning_seconds": round(planning_seconds, 2),
                    }
                )
            previous_visible = visible
            index += 1
            tick += timedelta(hours=self.tick_cadence_hours)

        manifest = ReplayManifest(
            schema_version="orchestrator.replay-manifest.v1",
            replay_id=self.replay_id,
            scenario_id=self.scenario_id,
            scenario_mode="causal_replay",
            replay_start=_iso(self.replay_start),
            replay_end=_iso(self.replay_end),
            tick_cadence_hours=self.tick_cadence_hours,
            snapshot_count=len(self.snapshots),
            snapshots=tuple(self.snapshots),
            events=tuple(self.events),
            resources={
                "risk_store": "risk-store",
                "snapshots": "snapshots",
            },
            provenance={
                "runner": "arctic_route_orchestrator.replay.runner",
                "replay_start": _iso(self.replay_start),
                "replay_end": _iso(self.replay_end),
                "tick_cadence_hours": self.tick_cadence_hours,
                "scenario_mode": "causal_replay",
            },
        )
        _write_json(self.paths.manifest_path, manifest.to_dict())
        summary = {
            "replay_id": self.replay_id,
            "scenario_id": self.scenario_id,
            "scenario_mode": "causal_replay",
            "snapshot_count": len(self.snapshots),
            "data_revision": self.data_revision,
            "b_input_revision": self.b_input_revision,
            "risk_revision": self.risk_revision,
            "risk_content_revision": self.risk_revision,
            "risk_window_revision": self.risk_window_revision,
            "plan_revision": self.plan_revision,
            "observation_sequence": self.observation_sequence,
            "navigation_state_revision": self.navigation_state_revision,
            "replan_candidate_computations": self.replan_candidate_computations,
            "replan_candidates_accepted": self.replan_candidates_accepted,
            "replan_candidates_rejected": self.replan_candidates_rejected,
            "pre_planning_skips": self.pre_planning_skips,
            "planning_elapsed_seconds": round(self.planning_elapsed_seconds, 1),
            "tick_performance": self.tick_performance,
            "replan_min_interval_hours": self.replan_min_interval_hours,
            "risk_commit_id": self.risk_commit.commit_id if self.risk_commit else None,
            "risk_valid_start": _iso(self.risk_valid_start) if self.risk_valid_start else None,
            "risk_valid_end": _iso(self.risk_valid_end) if self.risk_valid_end else None,
            "risk_semantic_digest": self.risk_semantic_digest,
            "supported_layers": list(self.supported_layers),
            "unsupported_layers": list(self.unsupported_layers),
            "planning_blockers": list(self.planning_blockers),
            "v2_probe_eta_hours": self.v2_probe_eta_hours,
            "planning_workers": self.planning_workers,
            "parallel_pool_mode": self.parallel_pool_mode,
            "route_semantic_digests": self.route_semantic_digests,
            "events": [event.to_dict() for event in self.events],
            "total_elapsed_seconds": round(time.perf_counter() - started, 1),
            "peak_rss_mb": round(_rss_mb(), 1),
        }
        _write_json(self.paths.summary_path, summary)
        return summary

    def _build_risk_window(self, tick: datetime, *, progress: Callable | None = None) -> None:
        from arctic_route_data.sources import LocalArchiveSource
        from arctic_route_risk.config import load_risk_build_configuration
        from arctic_route_risk.context import BInputEnvelope
        from arctic_route_risk.publishing.store import PersistentRiskStore
        from arctic_route_risk.service import RiskBuildRequest, RiskBuildService

        if progress is not None:
            progress({"stage": "A causal resolution", "tick": _iso(tick)})
        source = LocalArchiveSource(self.a_data_root)
        clock = SimulationClock(tick)
        cache = PartitionedABCache(max_memory_mb=self.cache_memory_mb)
        work = WorkPackageA(source=source, clock=clock, cache=cache)
        forecast_end = self.risk_forecast_end or self.replay_end
        horizon_hours = int((forecast_end - tick).total_seconds() // 3600)
        prepared = work.prepare_window_for_b(
            route_id=self.corridor_id,
            data_types=sorted(REQUIRED_FORMAL_DATA_TYPES),
            start_time=tick,
            target_horizon_hours=horizon_hours,
            minimum_complete_horizon_hours=horizon_hours,
            expected_interval_hours=EXPECTED_INTERVAL_HOURS,
            knowledge_as_of=tick,
        )
        if progress is not None:
            progress({"stage": "B build", "tick": _iso(tick)})
        base_context = self._replay_run_context(prepared.dataset_bundle)
        envelope = BInputEnvelope.from_prepared_window(
            run_context=base_context,
            prepared_window=prepared,
            generation_id=0,
            knowledge_as_of=tick,
            requested_start=tick,
            requested_end=forecast_end,
        )
        risk_configuration = load_risk_build_configuration(str(self.b_config_path))
        request = RiskBuildRequest(
            envelope=envelope,
            target_bbox=_corridor_bbox(self.configuration),
            grid_config=risk_configuration.grid_config,
            model_config=risk_configuration.model_config,
        )
        self._model_config_digest = request.model_config_digest
        frames = RiskBuildService(utc_now=lambda: datetime.now(UTC)).build_window(request)
        store = PersistentRiskStore(self.paths.risk_store_root)
        store.bind_generation_authority(_replay_run_id(self.replay_id), SimulationClock(tick))
        self.risk_commit = store.publish_window(frames)
        self.window_commit = self.risk_commit
        self.risk_store = store
        self.risk_valid_start = frames[0].valid_time
        self.risk_valid_end = frames[-1].valid_time
        self.prediction_as_of = tick
        self.risk_revision += 1
        self.risk_window_revision += 1
        self.risk_semantic_digest = risk_semantic_digest(frames)
        self._last_window_identity = self.risk_commit.commit_id
        self.run_context = self._c_run_context(base_context)
        self.ingress = RiskSourcePlanningIngress(
            source=store,
            configuration=self.configuration,
        )
        self.endpoint_mapping = map_corridor_endpoints(
            self.configuration,
            frames[0],
            max_adjustment_km=self.max_snap_km,
        )

    def _replay_run_context(self, bundle) -> RunContext:
        base = load_run_context(self.frozen_run_context_path)
        scenario = self.configuration.scenario
        digest = configuration_digest(
            scenario,
            self.configuration.corridor,
            self.configuration.vessel,
            dataset_bundle_id=bundle.bundle_id,
            dataset_bundle_digest=bundle.bundle_digest,
        )
        return replace(
            base,
            run_id=_replay_run_id(self.replay_id),
            created_at=datetime.now(UTC),
            scenario_mode=base.scenario_mode,
            simulation_start=self.replay_start,
            simulation_end=self.risk_forecast_end or self.replay_end,
            scenario_digest=canonical_sha256(scenario),
            dataset_bundle_id=bundle.bundle_id,
            dataset_bundle_digest=bundle.bundle_digest,
            config_digest=digest,
        )

    def _c_run_context(self, b_context: RunContext) -> RunContext:
        """C requires the RunContext window to equal the replay scenario
        (planning) window; B uses the narrower risk forecast window."""

        return replace(
            b_context,
            simulation_end=(
                self.replay_start + timedelta(hours=self.planning_horizon_hours)
            ),
        )

    def _window_identity(self, tick: datetime):
        if tick == self.replay_start or self.risk_commit is None:
            return self.risk_commit
        if self.window_commit is not None and self.window_commit.start == tick:
            return self.window_commit
        return self.risk_store.publish_suffix_window(
            self.risk_commit.frames,
            start=tick,
        )

    def _planning_tick(
        self,
        tick: datetime,
        index: int,
        events: list[ReplayEvent],
        *,
        progress: Callable | None = None,
    ) -> PlanningStateSummary:
        if self.risk_commit is None:
            return PlanningStateSummary(
                plan_revision=self.plan_revision,
                planning_as_of=_iso(tick),
                departure_time=_iso(tick),
                planning_valid_start=_iso(tick),
                planning_valid_end=(
                    _iso(self.planning_valid_end)
                    if self.planning_valid_end
                    else None
                ),
                unsupported_layers=self.unsupported_layers,
                blockers=("RISK_NOT_READY",),
            )
        self._adopt_pending_if_due(tick, events)
        if (
            self.current_plan is None
            and self.plan_revision == 0
            and not self._initial_plan_attempted
        ):
            self._initial_plan_attempted = True
            if progress is not None:
                progress({"stage": "C initial planning attempt", "tick": _iso(tick)})
            self._run_planning(self._attempt_initial_plan, tick, events)
        elif self.current_plan is not None:
            if self._should_skip_replan(tick, events):
                self.last_replan_reasons = ()
                events.append(
                    ReplayEvent(
                        type="REPLAN_SKIPPED",
                        simulation_time=_iso(tick),
                        revision=str(self.plan_revision),
                        description=(
                            "pre-planning gate: no data/risk-content change "
                            "and accepted plan is younger than "
                            f"{self.replan_min_interval_hours:g}h"
                        ),
                        observed=True,
                    )
                )
                return PlanningStateSummary(
                    plan_revision=self.plan_revision,
                    planning_as_of=_iso(tick),
                    departure_time=_iso(tick),
                    planning_valid_start=_iso(tick),
                    planning_valid_end=(
                        _iso(self.planning_valid_end)
                        if self.planning_valid_end
                        else None
                    ),
                    supported_layers=self.supported_layers,
                    unsupported_layers=self.unsupported_layers,
                    blockers=self.planning_blockers,
                    resources=(
                        {"route_integrity": self._route_integrity}
                        if self._route_integrity is not None
                        else {}
                    ),
                    observation_sequence=self.observation_sequence,
                    replan_reasons=self.last_replan_reasons,
                    route_semantic_digests=dict(self.route_semantic_digests),
                )
            self._run_planning(self._attempt_replan, tick, events)
        return PlanningStateSummary(
            plan_revision=self.plan_revision,
            planning_as_of=_iso(tick),
            departure_time=_iso(tick),
            planning_valid_start=_iso(tick),
            planning_valid_end=(
                _iso(self.planning_valid_end)
                if self.planning_valid_end
                else None
            ),
            supported_layers=self.supported_layers,
            unsupported_layers=self.unsupported_layers,
            blockers=self.planning_blockers,
            resources=(
                {"route_integrity": self._route_integrity}
                if self._route_integrity is not None
                else {}
            ),
            observation_sequence=self.observation_sequence,
            replan_reasons=self.last_replan_reasons,
            route_semantic_digests=dict(self.route_semantic_digests),
        )

    def _should_skip_replan(
        self,
        tick: datetime,
        events: list[ReplayEvent],
    ) -> bool:
        """Cheap pre-planning gate (never replaces the Switch Gate).

        A replan can be skipped before paying for the expensive C candidate
        only when all of the following are true:

        * the policy interval is configured;
        * the accepted plan is younger than the interval;
        * no real A data or B risk-content change occurred this tick
          (window suffix advances alone are not a content change);
        * the accepted route still has at least one interval of remaining
          planning horizon (fail-closed near arrival).
        """

        if self.current_plan is None:
            return False
        if (
            self.replan_min_interval_hours is None
            and not self.replan_waypoint_aligned_only
        ):
            return False
        content_changed = any(
            event.type
            in (
                "DATA_REVISION_CHANGED",
                "DATA_BECAME_VISIBLE",
                "RISK_CONTENT_UPDATED",
                "RISK_REVISION_CHANGED",
            )
            for event in events
        )
        if content_changed:
            return False
        if self.replan_waypoint_aligned_only:
            edge_progress = self._route_edge_progress(tick, self.current_plan)
            if edge_progress is None or abs(edge_progress) > 1e-6:
                return True
        if self.replan_min_interval_hours is None:
            return False
        if self.replan_min_interval_hours <= 0.0:
            return False
        start_time = getattr(self.current_plan, "start_time", None)
        if start_time is None:
            return False
        elapsed = tick - start_time
        if elapsed < timedelta(hours=self.replan_min_interval_hours):
            return True
        waypoints = tuple(getattr(self.current_plan, "waypoints", ()))
        if not waypoints:
            return False
        remaining = waypoints[-1].eta - tick
        if remaining < timedelta(hours=self.replan_min_interval_hours):
            return False
        return False

    def _route_edge_progress(self, tick: datetime, plan: Any) -> float | None:
        waypoints = tuple(getattr(plan, "waypoints", ()))
        if not waypoints:
            return None
        reached = -1
        for index, waypoint in enumerate(waypoints):
            if waypoint.eta <= tick:
                reached = index
            else:
                break
        if reached == len(waypoints) - 1:
            return 1.0
        start = waypoints[max(0, reached)]
        end = waypoints[reached + 1]
        span = (end.eta - start.eta).total_seconds()
        if span <= 0.0:
            return 0.0
        return max(0.0, min(1.0, (tick - start.eta).total_seconds() / span))
    def _run_planning(self, operation, tick: datetime, events: list[ReplayEvent]) -> None:
        if self.planning_workers <= 1:
            operation(tick, events)
            return
        with replay_parallel.install(
            workers=self.planning_workers,
            risk_store_root=self.paths.risk_store_root,
            c_config_root=self.c_config_root,
            contracts_config_root=self.contracts_config_root,
            max_snap_km=self.max_snap_km,
            timeout_seconds=self.c_attempt_timeout_seconds,
            pool_mode=self.parallel_pool_mode,
        ):
            operation(tick, events)

    def _attempt_initial_plan(self, tick: datetime, events: list[ReplayEvent]) -> None:
        if self.v2_only:
            self._run_v2_initial(tick, events, v3_blocker="v2-only mode")
            return
        request = self._planning_request(tick, input_revision=0)
        try:
            prepared = self.ingress.prepare(request)
            outcome = prepared.execute_four_layer()
            self.current_plan_set = outcome.plan_set
            self.current_plan = outcome.plan_set.recommended
            self._audit_and_attach_route()
            self._attach_route_digests()
            self.plan_revision = 1
            self.supported_layers = (
                "executable_0_6h",
                "rolling_0_24h",
                "main_corridor_24_72h",
                "full_voyage",
            )
            self.unsupported_layers = ()
            self.planning_blockers = ()
            self.plan_kind = "v3_four_layer"
            events.append(
                ReplayEvent(
                    type="PLAN_COMPUTED",
                    simulation_time=_iso(tick),
                    revision=str(self.plan_revision),
                    observed=True,
                )
            )
        except Exception as exc:  # fail-closed: classify, never fake success
            print(
                "[replay] v3 initial planning failed:\n"
                + traceback.format_exc(limit=10),
                file=sys.stderr,
                flush=True,
            )
            v3_blocker = f"{type(exc).__name__}: {exc}"
            self._run_v2_initial(tick, events, v3_blocker=v3_blocker)

    def _run_v2_initial(
        self,
        tick: datetime,
        events: list[ReplayEvent],
        *,
        v3_blocker: str,
    ) -> None:
        try:
            request_v2 = self._planning_request(tick, input_revision=0)
            prepared_v2 = self.ingress.prepare(request_v2)
            batch = prepared_v2.execute()
            self.current_plan = batch.selected
            self.current_batch = batch
            self.current_plan_set = None
            self.plan_revision = 1
            self.plan_kind = "v2_complete_route_fallback"
            self._audit_and_attach_route()
            self._attach_route_digests()
            self.supported_layers = ("full_voyage_complete_route",)
            self.unsupported_layers = (
                "executable_0_6h",
                "rolling_0_24h",
                "main_corridor_24_72h",
                "full_voyage_v3_four_layer",
            )
            self.planning_blockers = (
                "v3 four-layer blocked: " + v3_blocker,
                "v2 complete-route fallback engaged",
            )
            events.append(
                ReplayEvent(
                    type="PLAN_COMPUTED",
                    simulation_time=_iso(tick),
                    revision=str(self.plan_revision),
                    description="v2 complete-route fallback",
                    observed=True,
                )
            )
        except Exception as v2_exc:
            print(
                "[replay] v2 initial fallback failed:\n"
                + traceback.format_exc(limit=6),
                file=sys.stderr,
                flush=True,
            )
            self.planning_blockers = (v3_blocker, f"v2: {v2_exc}")
            self.unsupported_layers = (
                "executable_0_6h",
                "rolling_0_24h",
                "main_corridor_24_72h",
                "full_voyage",
            )
            events.append(
                ReplayEvent(
                    type="PLANNING_NOT_READY",
                    simulation_time=_iso(tick),
                    revision="0",
                    description=v3_blocker[:300],
                    observed=True,
                )
            )

    def _attempt_replan(self, tick: datetime, events: list[ReplayEvent]) -> None:
        adoption_spec = self._plan_adoption_spec(tick, self.current_plan)
        if adoption_spec is None:
            events.append(
                ReplayEvent(
                    type="PLAN_REUSED",
                    simulation_time=_iso(tick),
                    revision=str(self.plan_revision),
                    description="vessel arrived; no further replan origin",
                    observed=True,
                )
            )
            self.last_replan_reasons = ()
            return
        origin = adoption_spec["origin_node"]
        self.observation_sequence += 1
        self.input_revision = self.observation_sequence
        request = self._planning_request(
            tick,
            input_revision=self.observation_sequence,
            start=origin,
        )
        observation = _observation(
            tick=tick,
            data_revision=self.observation_sequence,
            risk_revision=self.window_commit.commit_id,
            plan=self.current_plan,
        )
        try:
            prepared = self.ingress.prepare(request)
            if self.plan_kind == "v2_complete_route_fallback":
                outcome = prepared.replan_if_needed(observation)
                policy_triggered = (
                    outcome.decision.triggered and outcome.batch is not None
                )
                published = policy_triggered and outcome.batch.published
                if published:
                    self._accept_published_replan(
                        tick,
                        adoption_spec,
                        plan=outcome.batch.selected,
                        plan_set=None,
                        plan_kind="v2_complete_route_fallback",
                    )
            else:
                outcome = prepared.replan_four_layer_if_needed(observation)
                policy_triggered = (
                    outcome.decision.triggered and outcome.outcome is not None
                )
                published = policy_triggered and outcome.outcome.published
                if published:
                    self._accept_published_replan(
                        tick,
                        adoption_spec,
                        plan=outcome.outcome.plan_set.recommended,
                        plan_set=outcome.outcome.plan_set,
                        plan_kind="v3_four_layer",
                    )
            triggered = published
            self.last_replan_reasons = _honest_replan_reasons(
                outcome.decision.reasons, events
            )
            if triggered:
                if adoption_spec["mode"] == "IMMEDIATE":
                    events.append(
                        ReplayEvent(
                            type="REPLAN_TRIGGERED",
                            simulation_time=_iso(tick),
                            revision=str(self.plan_revision),
                            description=",".join(self.last_replan_reasons),
                            observed=True,
                        )
                    )
                    events.append(
                        ReplayEvent(
                            type="ROUTE_CHANGED",
                            simulation_time=_iso(tick),
                            revision=str(self.plan_revision),
                            observed=True,
                        )
                    )
                else:
                    events.append(
                        ReplayEvent(
                            type="REPLAN_DECIDED",
                            simulation_time=_iso(tick),
                            revision=str(self.pending_revision),
                            description=(
                                f"deferred adoption at {_iso(self.pending_adoption_time)}; "
                                "physical vessel stays on current accepted segment"
                            ),
                            observed=True,
                        )
                    )
            else:
                events.append(
                    ReplayEvent(
                        type="PLAN_REUSED",
                        simulation_time=_iso(tick),
                        revision=str(self.plan_revision),
                        description=(
                            "policy triggered; switch gate rejected candidate"
                            if policy_triggered
                            else ",".join(self.last_replan_reasons)
                        ),
                        observed=True,
                    )
                )
        except Exception as exc:
            print(
                "[replay] v3 replan failed:\n"
                + traceback.format_exc(limit=10),
                file=sys.stderr,
                flush=True,
            )
            self.planning_blockers = (f"{type(exc).__name__}: {exc}",)
            events.append(
                ReplayEvent(
                    type="PLANNING_NOT_READY",
                    simulation_time=_iso(tick),
                    revision=str(self.plan_revision),
                    description=str(exc)[:300],
                    observed=True,
                )
            )

    def _accept_published_replan(
        self,
        tick: datetime,
        adoption_spec: dict[str, Any],
        *,
        plan: Any,
        plan_set: Any,
        plan_kind: str,
    ) -> None:
        """Store a published candidate as the accepted plan.

        At an exact accepted-route waypoint the plan can be adopted
        immediately.  Mid-edge decisions are deferred: the physical vessel
        stays on the current accepted segment and the new plan becomes the
        accepted route only when the vessel reaches the planner-origin node.
        """

        if adoption_spec["mode"] == "IMMEDIATE":
            self.superseded_route_payload = self._capture_superseded(tick)
            self.current_plan = plan
            self.current_plan_set = plan_set
            self.plan_kind = plan_kind
            self.plan_revision += 1
            self.active_plan_time_offset = timedelta()
            self.pending_plan = None
            self.pending_plan_set = None
            self.pending_plan_kind = ""
            self.pending_decision_time = None
            self.pending_adoption_time = None
            self.pending_revision = 0
            self.pending_origin_node = None
            self.pending_origin_adjustment_km = None
            self.pending_decision_position = None
            self._audit_and_attach_route()
            self._attach_route_digests()
            return
        self.pending_plan = plan
        self.pending_plan_set = plan_set
        self.pending_plan_kind = plan_kind
        self.pending_decision_time = tick
        self.pending_adoption_time = adoption_spec["adoption_time"]
        self.pending_revision = self.plan_revision + 1
        self.pending_origin_node = adoption_spec["origin_node"]
        self.pending_origin_adjustment_km = adoption_spec[
            "origin_adjustment_km"
        ]
        self.pending_decision_position = self._physical_state_at(
            tick, self.current_plan
        ).position

    def _adopt_pending_if_due(
        self,
        tick: datetime,
        events: list[ReplayEvent],
    ) -> None:
        """Switch a deferred plan into the accepted route at adoption time."""

        if (
            self.pending_plan is None
            or self.pending_adoption_time is None
            or tick < self.pending_adoption_time
        ):
            return
        self.superseded_route_payload = self._capture_superseded(tick)
        self.current_plan = self.pending_plan
        self.current_plan_set = self.pending_plan_set
        self.plan_kind = self.pending_plan_kind
        self.plan_revision = self.pending_revision
        self.active_plan_time_offset = (
            self.pending_adoption_time - self.pending_decision_time
            if self.pending_decision_time is not None
            else timedelta()
        )
        self._audit_and_attach_route()
        self._attach_route_digests()
        events.append(
            ReplayEvent(
                type="REPLAN_ADOPTED",
                simulation_time=_iso(tick),
                revision=str(self.plan_revision),
                description="deferred plan adopted at execution node",
                observed=True,
            )
        )
        events.append(
            ReplayEvent(
                type="ROUTE_CHANGED",
                simulation_time=_iso(tick),
                revision=str(self.plan_revision),
                description="deferred adoption",
                observed=True,
            )
        )
        self.pending_plan = None
        self.pending_plan_set = None
        self.pending_plan_kind = ""
        self.pending_decision_time = None
        self.pending_adoption_time = None
        self.pending_revision = 0
        self.pending_origin_node = None
        self.pending_origin_adjustment_km = None
        self.pending_decision_position = None

    def _plan_adoption_spec(
        self,
        tick: datetime,
        plan: Any,
    ) -> dict[str, Any] | None:
        """Build the planner-origin / effective-adoption tuple for a replan.

        The planner may only start from a grid node.  This does NOT move the
        physical ship: the returned node is either the current accepted-route
        waypoint (immediate) or the next waypoint on the accepted route
        (deferred until the vessel physically reaches it).
        """

        waypoints = tuple(getattr(plan, "waypoints", ()))
        if not waypoints:
            return None
        state = self._physical_state_at(tick, plan)
        if state.status == "ARRIVED":
            return None
        offset = (
            self.active_plan_time_offset
            if plan is self.current_plan
            else timedelta()
        )
        if state.status == "NOT_STARTED":
            waypoint = waypoints[0]
            node, adjustment = self._snap_position(
                waypoint.longitude, waypoint.latitude, tick
            )
            return {
                "origin_node": node,
                "origin_adjustment_km": adjustment,
                "adoption_time": waypoint.eta + offset,
                "mode": "IMMEDIATE",
                "origin_position": {
                    "longitude": waypoint.longitude,
                    "latitude": waypoint.latitude,
                },
            }
        index = int(state.edge_index)
        if state.edge_progress == 0.0 and index >= 0:
            origin_waypoint = waypoints[index]
            mode = "IMMEDIATE"
            adoption_time = origin_waypoint.eta + offset
        else:
            origin_waypoint = waypoints[index + 1]
            mode = "NEXT_WAYPOINT_DEFERRED"
            adoption_time = origin_waypoint.eta + offset
        node, adjustment = self._snap_position(
            origin_waypoint.longitude, origin_waypoint.latitude, tick
        )
        return {
            "origin_node": node,
            "origin_adjustment_km": adjustment,
            "adoption_time": adoption_time,
            "mode": mode,
            "origin_position": {
                "longitude": origin_waypoint.longitude,
                "latitude": origin_waypoint.latitude,
            },
        }

    def _physical_state_at(self, tick: datetime, plan: Any) -> VesselState:
        """Pure physical kinematics for the accepted route at ``tick``."""

        offset = (
            self.active_plan_time_offset
            if plan is self.current_plan
            else timedelta()
        )
        motion_tick = tick - offset
        total_distance_km = getattr(
            getattr(plan, "metrics", None), "distance_km", None
        )
        return vessel_state_at(
            motion_tick,
            tuple(getattr(plan, "waypoints", ())),
            total_distance_km=(
                float(total_distance_km)
                if total_distance_km is not None
                else None
            ),
        )

    def _route_payload(
        self,
        plan: Any,
        *,
        offset: timedelta = timedelta(),
    ) -> dict[str, Any] | None:
        """Serialize accepted/pending route waypoints for the viewer.

        ETAs are shifted by ``offset`` so they are expressed in physical
        simulation-clock time.  ``vessel_state_at(tick, payload)`` then
        reproduces the runner's physical motion exactly without replanning.
        """

        waypoints = tuple(getattr(plan, "waypoints", ()))
        if not waypoints:
            return None
        rows = [
            {
                "longitude": float(waypoint.longitude),
                "latitude": float(waypoint.latitude),
                "eta": _iso(waypoint.eta + offset),
            }
            for waypoint in waypoints
        ]
        distance = getattr(getattr(plan, "metrics", None), "distance_km", None)
        return {
            "distance_km": float(distance) if distance is not None else None,
            "waypoints": rows,
        }

    def _capture_superseded(self, tick: datetime) -> dict[str, Any] | None:
        """Snapshot the route being replaced (physical-clock ETAs)."""

        if self.current_plan is None:
            return None
        return {
            "plan_revision": self.plan_revision,
            "superseded_at": _iso(tick),
            "route": self._route_payload(
                self.current_plan,
                offset=self.active_plan_time_offset,
            ),
        }
    def _probe_v2(self, tick: datetime) -> str | None:
        """Independent second C-API probe (v2 batch) for honest evidence."""

        try:
            request = self._planning_request(tick, input_revision=0)
            prepared = self.ingress.prepare(request)
            batch = prepared.execute()
            self.v2_probe_eta_hours = float(batch.selected.metrics.eta_hours)
            return None
        except Exception as exc:
            print(
                "[replay] v2 probe failed:\n"
                + traceback.format_exc(limit=6),
                file=sys.stderr,
                flush=True,
            )
            return f"v2 probe: {type(exc).__name__}: {exc}"

    def _audit_and_attach_route(self) -> None:
        from arctic_route_orchestrator.replay.route_integrity import audit_route

        plans = []
        plan_set = getattr(self, "current_plan_set", None)
        if plan_set is not None:
            for bundle in plan_set.layers:
                for _objective, plan in bundle.plans.items():
                    plans.append(plan)
        elif self.plan_kind == "v2_complete_route_fallback":
            batch = getattr(self, "current_batch", None)
            if batch is not None:
                plans.extend(batch.plans.values())
            else:
                plans.append(self.current_plan)
        else:
            plans.append(self.current_plan)
        audits = [audit_route(plan, self.risk_commit.frames) for plan in plans]
        overall = "PASS" if all(item["status"] == "PASS" for item in audits) else "FAIL"
        self._route_integrity = {
            "status": overall,
            "routes": audits,
        }
        if overall != "PASS":
            self.planning_blockers = (
                *self.planning_blockers,
                "route integrity FAIL: "
                f"{[a['status'] for a in audits if a['status'] != 'PASS'][:3]}",
            )

    def _planning_request(
        self,
        tick: datetime,
        *,
        input_revision: int,
        start: tuple[int, int] | None = None,
    ) -> ServicePlanningRequest:
        origin = start if start is not None else self._origin_for_tick(tick)
        if origin is None:
            origin = self.endpoint_mapping.start.node
        return ServicePlanningRequest(
            run_context=self.run_context,
            scenario=self.configuration.scenario,
            corridor=self.configuration.corridor,
            vessel=self.configuration.vessel,
            vessel_model=self.configuration.vessel_model,
            model_config_digest=self._model_config_digest,
            planner_config_digest=self.configuration.planner_config_digest,
            risk_provenance=ProvenanceKind.FORMAL,
            generation_id=0,
            input_revision=input_revision,
            as_of_time=self.prediction_as_of or tick,
            start_time=tick,
            start=origin,
            goal=self.endpoint_mapping.goal.node,
            maximum_elapsed=self.risk_forecast_end - tick,
        )

    def _attach_route_digests(self) -> None:
        digests: dict[str, dict[str, str]] = {}
        if self.current_plan_set is not None:
            for bundle in self.current_plan_set.layers:
                layer = getattr(bundle.planning_layer, "value", None) or str(
                    bundle.planning_layer
                )
                digests[layer] = {
                    getattr(objective, "value", None) or str(objective): (
                        route_semantic_digest(plan)
                    )
                    for objective, plan in bundle.plans.items()
                }
        elif self.current_batch is not None:
            digests["full_voyage_complete_route"] = {
                getattr(objective, "value", None) or str(objective): (
                    route_semantic_digest(plan)
                )
                for objective, plan in self.current_batch.plans.items()
            }
        elif self.current_plan is not None:
            digests["full_voyage_complete_route"] = {
                "recommended": route_semantic_digest(self.current_plan)
            }
        self.route_semantic_digests = digests

    def _origin_for_tick(self, tick: datetime) -> tuple[int, int] | None:
        """Replan origin for the current simulation tick.

        Same-vessel rule (v1): the origin is the last accepted-route waypoint
        reached at or before ``tick``, snapped to the nearest navigable grid
        node.  No arbitrary lon/lat start and no silent nearest-node teleport:
        every snap is bounded by ``max_snap_km`` and reported in ship state.
        Returns None when the vessel has arrived (no further replan origin).
        """

        if self.current_plan is None:
            return self.endpoint_mapping.start.node
        adoption_spec = self._plan_adoption_spec(tick, self.current_plan)
        if adoption_spec is None:
            return None
        node = adoption_spec["origin_node"]
        if node == self.endpoint_mapping.goal.node:
            return None
        return node

    def _position_at(self, tick: datetime, plan: Any) -> dict[str, Any]:
        """Backward-compatible wrapper: pure physical state + planner snap."""

        try:
            state = self._physical_state_at(tick, plan)
        except InvalidRouteTimingError:
            raise
        position = state.position
        node, adjustment = self._snap_position(
            position["longitude"], position["latitude"], tick
        )
        return {
            "position": position,
            "current_node": node,
            "edge_progress": state.edge_progress,
            "snap_adjustment_km": adjustment,
            "arrived": state.status == "ARRIVED",
            "reached_index": int(state.edge_index or 0),
            "current_edge_index": state.edge_index,
            "current_segment_start_eta": state.segment_start_eta,
            "current_segment_end_eta": state.segment_end_eta,
            "effective_speed_knots": state.speed_knots,
            "speed_mps": state.speed_mps,
            "executed_distance_km": state.executed_distance_km,
        }

    def _snap_position(
        self,
        longitude: float,
        latitude: float,
        tick: datetime,
    ) -> tuple[tuple[int, int], float]:
        from arctic_route_planning.domain import GeoPoint

        grid = self._ensure_grid()
        frame = self._frame_at(tick)
        hard_mask = frame.payload["hard_mask"].values
        point = GeoPoint(longitude=longitude, latitude=latitude)
        nearest = grid.nearest_node(point)
        if grid.contains(nearest) and not bool(hard_mask[nearest]):
            return nearest, haversine_km(point, grid.point(nearest))
        cell_km = max(
            1.0,
            haversine_km(
                grid.point((0, 0)),
                grid.point((1, 0)),
            )
            if grid.shape[0] > 1
            else 1.0,
        )
        max_rings = int(max(1.0, self.max_snap_km / cell_km) + 2)
        best: tuple[float, tuple[int, int]] | None = None
        for radius in range(1, max_rings + 1):
            for row_offset in range(-radius, radius + 1):
                for column_offset in range(-radius, radius + 1):
                    if max(abs(row_offset), abs(column_offset)) != radius:
                        continue
                    candidate = (
                        nearest[0] + row_offset,
                        nearest[1] + column_offset,
                    )
                    if not grid.contains(candidate) or bool(hard_mask[candidate]):
                        continue
                    distance = haversine_km(point, grid.point(candidate))
                    if distance <= self.max_snap_km and (
                        best is None or distance < best[0]
                    ):
                        best = (distance, candidate)
            if best is not None:
                break
        if best is None:
            raise ValueError(
                "nav_snap_failed: no navigable grid node within "
                f"{self.max_snap_km:.1f} km of ({longitude}, {latitude})"
            )
        return best[1], best[0]

    def _ensure_grid(self) -> RegularGrid:
        if self._grid is None:
            if self.risk_commit is None or not self.risk_commit.frames:
                raise RuntimeError("risk window is required before navigation state")
            frame = self.risk_commit.frames[0]
            self._grid = RegularGrid.from_risk_frame(
                frame,
                allow_diagonal=self.configuration.planner.connectivity == 8,
            )
        return self._grid

    def _frame_at(self, tick: datetime):
        frames = self.risk_commit.frames
        candidates = [frame for frame in frames if frame.valid_time <= tick]
        return candidates[-1] if candidates else frames[0]

    def _update_navigation(self, tick: datetime) -> None:
        if self.current_plan is None:
            self.nav_state = NavigationExecutionState(
                status="DEFERRED",
                navigation_state_revision=self.navigation_state_revision,
                accepted_plan_revision=self.plan_revision,
                accepted_plan_digest="",
                executed_until=None,
                current_position=None,
                current_node=None,
                edge_progress=None,
            )
            return
        plan = self.current_plan
        state = self._physical_state_at(tick, plan)
        waypoints = tuple(plan.waypoints)
        if state.status == "ARRIVED":
            completed_count = len(waypoints)
        elif state.status == "NOT_STARTED":
            completed_count = 1
        else:
            completed_count = int(state.edge_index) + 1
        # Completed track is append-only execution history.  When a replan
        # adopts a new route, only the future portion changes; waypoints
        # already executed under the previous accepted plan stay immutable.
        previous_track = (
            self.nav_state.completed_track if self.nav_state is not None else ()
        )
        completed_track = merge_completed_track(
            previous_track,
            waypoints[:completed_count],
        )
        position = state.position
        remaining = state.remaining_distance_km
        previous = self.nav_state
        delta = None
        expected = None
        if previous is not None and previous.current_position is not None and position:
            from arctic_route_planning.domain import GeoPoint

            previous_position = GeoPoint(
                longitude=previous.current_position["longitude"],
                latitude=previous.current_position["latitude"],
            )
            current_position = GeoPoint(
                longitude=position["longitude"],
                latitude=position["latitude"],
            )
            delta = haversine_km(previous_position, current_position)
            previous_until = previous.executed_until
            if previous_until and previous.accepted_plan_revision == self.plan_revision:
                expected = self._route_travel_km(
                    plan,
                    datetime.fromisoformat(
                        previous_until.replace("Z", "+00:00")
                    ).astimezone(UTC),
                    tick,
                )
        cumulative = None
        if previous is not None and previous.cumulative_travelled_km is not None:
            cumulative = float(previous.cumulative_travelled_km)
            if delta is not None:
                cumulative += max(delta, 0.0)
        else:
            cumulative = float(state.executed_distance_km or 0.0)
        arrived = state.status == "ARRIVED"
        status = state.status
        executed_until = (
            _iso(waypoints[-1].eta) if arrived else _iso(tick)
        )
        adoption_spec = self._plan_adoption_spec(tick, plan)
        planner_origin_node = (
            adoption_spec["origin_node"] if adoption_spec else None
        )
        planner_origin_adjustment = (
            adoption_spec["origin_adjustment_km"] if adoption_spec else None
        )
        planner_origin_position = (
            adoption_spec["origin_position"] if adoption_spec else None
        )
        if self.pending_plan is not None:
            adoption_status = "PENDING"
        elif self.active_plan_time_offset.total_seconds() > 0.0:
            adoption_status = "DEFERRED"
        elif self.plan_revision > 1:
            adoption_status = "IMMEDIATE"
        else:
            adoption_status = "NONE"
        self.navigation_state_revision += 1
        self.nav_state = NavigationExecutionState(
            status=status,
            navigation_state_revision=self.navigation_state_revision,
            accepted_plan_revision=self.plan_revision,
            accepted_plan_digest=self._accepted_plan_digest(),
            executed_until=executed_until,
            current_position=position,
            current_node=planner_origin_node,
            edge_progress=state.edge_progress,
            completed_track=completed_track,
            remaining_distance_km=remaining,
            snap_adjustment_km=planner_origin_adjustment,
            last_distance_delta_km=delta,
            expected_travel_km=expected,
            current_edge_index=state.edge_index,
            current_segment_start_eta=state.segment_start_eta,
            current_segment_end_eta=state.segment_end_eta,
            effective_speed_knots=state.speed_knots,
            speed_mps=state.speed_mps,
            speed_source=state.interpolation,
            executed_distance_km=state.executed_distance_km,
            cumulative_travelled_km=cumulative,
            planner_origin_node=planner_origin_node,
            planner_origin_adjustment_km=planner_origin_adjustment,
            planner_origin_position=planner_origin_position,
            accepted_route=self._route_payload(
                plan,
                offset=self.active_plan_time_offset,
            ),
            pending_route=(
                self._route_payload(self.pending_plan)
                if self.pending_plan is not None
                else None
            ),
            superseded_route=self.superseded_route_payload,
            replan_decision_time=(
                _iso(self.pending_decision_time)
                if self.pending_decision_time is not None
                else None
            ),
            effective_adoption_time=(
                _iso(self.pending_adoption_time)
                if self.pending_adoption_time is not None
                else None
            ),
            adoption_status=adoption_status,
            candidate_plan_revision=(
                self.pending_revision if self.pending_plan is not None else None
            ),
            replan_physical_position=(
                dict(self.pending_decision_position)
                if self.pending_decision_position
                else None
            ),
        )

    def _remaining_distance_km(
        self,
        plan: Any,
        tick: datetime,
        position: dict[str, float] | None,
    ) -> float:
        if position is None:
            return float(getattr(plan.metrics, "distance_km", 0.0))
        from arctic_route_planning.domain import GeoPoint

        remaining = 0.0
        current = GeoPoint(
            longitude=position["longitude"],
            latitude=position["latitude"],
        )
        for waypoint in plan.waypoints:
            if waypoint.eta <= tick:
                continue
            target = GeoPoint(
                longitude=waypoint.longitude,
                latitude=waypoint.latitude,
            )
            remaining += haversine_km(current, target)
            current = target
        return remaining

    def _route_travel_km(
        self,
        plan: Any,
        start_tick: datetime,
        end_tick: datetime,
    ) -> float:
        from arctic_route_planning.domain import GeoPoint

        if end_tick <= start_tick:
            return 0.0
        waypoints = tuple(plan.waypoints)
        if not waypoints:
            return 0.0

        def point_at(tick: datetime):
            reached = -1
            for index, waypoint in enumerate(waypoints):
                if waypoint.eta <= tick:
                    reached = index
                else:
                    break
            if reached < 0:
                return (
                    waypoints[0].longitude,
                    waypoints[0].latitude,
                )
            if reached == len(waypoints) - 1:
                return (
                    waypoints[-1].longitude,
                    waypoints[-1].latitude,
                )
            start = waypoints[reached]
            end = waypoints[reached + 1]
            span = (end.eta - start.eta).total_seconds()
            fraction = (
                0.0
                if span <= 0.0
                else max(0.0, min(1.0, (tick - start.eta).total_seconds() / span))
            )
            return (
                start.longitude + (end.longitude - start.longitude) * fraction,
                start.latitude + (end.latitude - start.latitude) * fraction,
            )

        a_lon, a_lat = point_at(start_tick)
        b_lon, b_lat = point_at(end_tick)
        start_index = -1
        end_index = -1
        for index, waypoint in enumerate(waypoints):
            if waypoint.eta <= start_tick:
                start_index = index
            if waypoint.eta <= end_tick:
                end_index = index
            else:
                break
        if start_index == end_index:
            return haversine_km(
                GeoPoint(longitude=a_lon, latitude=a_lat),
                GeoPoint(longitude=b_lon, latitude=b_lat),
            )
        total = haversine_km(
            GeoPoint(longitude=a_lon, latitude=a_lat),
            GeoPoint(
                longitude=waypoints[start_index + 1].longitude,
                latitude=waypoints[start_index + 1].latitude,
            ),
        )
        for index in range(start_index + 1, end_index):
            total += haversine_km(
                GeoPoint(
                    longitude=waypoints[index].longitude,
                    latitude=waypoints[index].latitude,
                ),
                GeoPoint(
                    longitude=waypoints[index + 1].longitude,
                    latitude=waypoints[index + 1].latitude,
                ),
            )
        total += haversine_km(
            GeoPoint(
                longitude=waypoints[end_index].longitude,
                latitude=waypoints[end_index].latitude,
            ),
            GeoPoint(longitude=b_lon, latitude=b_lat),
        )
        return total

    def _accepted_plan_digest(self) -> str:
        if self.plan_kind == "v2_complete_route_fallback":
            return self.route_semantic_digests.get(
                "full_voyage_complete_route", {}
            ).get("recommended", "")
        return self.route_semantic_digests.get("full_voyage", {}).get(
            "recommended", ""
        )

    def _snapshot(
        self,
        *,
        index,
        tick,
        visible,
        newly_visible,
        visible_digest,
        relevant_digest,
        events,
        planning,
    ) -> SimulationSnapshot:
        quality = Counter(record.quality_flag for record in visible)
        visibility = DataVisibilitySummary(
            max_source_issue_time=(
                _iso(max(record.issue_time for record in visible)) if visible else None
            ),
            visible_record_set_digest=visible_digest,
            b_relevant_input_digest=relevant_digest,
            data_revision=self.data_revision,
            b_input_revision=self.b_input_revision,
            newly_visible_count=len(newly_visible),
            newly_visible_record_ids=tuple(
                record.data_id for record in newly_visible[:8]
            ),
            quality_summary=dict(sorted(quality.items())),
        )
        risk_ready = "READY" if self.risk_commit is not None else "NOT_READY"
        planning_ready = "READY" if self.current_plan is not None else "NOT_READY"
        risk = RiskStateSummary(
            risk_revision=self.risk_revision,
            risk_content_revision=self.risk_revision,
            risk_window_revision=self.risk_window_revision,
            prediction_as_of=_iso(self.prediction_as_of) if self.prediction_as_of else "",
            risk_valid_start=_iso(self.window_commit.start) if self.window_commit else None,
            risk_valid_end=_iso(self.window_commit.end) if self.window_commit else None,
            resource_identity=self.window_commit.commit_id if self.window_commit else None,
            resource_digest=self.window_commit.content_digest if self.window_commit else None,
            risk_semantic_digest=self.risk_semantic_digest,
            presentation_horizons={
                "0h": _iso(tick),
                "+6h": _iso(tick + timedelta(hours=6)),
                "+12h": _iso(tick + timedelta(hours=12)),
                "+24h": _iso(tick + timedelta(hours=24)),
            },
        )
        readiness = ReadinessSummary(
            source_visibility="READY" if visible else "NOT_READY",
            b_input_ready="READY" if self.risk_commit is not None else "NOT_READY",
            risk_ready=risk_ready,
            planning_ready=planning_ready,
            blockers=self.planning_blockers,
        )
        ship_state = (
            self.nav_state.to_dict()
            if self.nav_state is not None
            else {"status": "DEFERRED"}
        )
        snapshot = SimulationSnapshot(
            schema_version="orchestrator.replay-snapshot.v1",
            replay_id=self.replay_id,
            scenario_id=self.scenario_id,
            scenario_mode="causal_replay",
            snapshot_index=index,
            simulation_time=_iso(tick),
            knowledge_as_of=_iso(tick),
            visibility=visibility,
            risk=risk,
            planning=planning,
            readiness=readiness,
            events=tuple(events),
            ship_state=ship_state,
        ).with_digest()
        self.events.extend(events)
        return snapshot

    def _checkpoint(self, index: int, tick: datetime) -> None:
        _write_json(
            self.paths.checkpoint_path,
            {
                "replay_id": self.replay_id,
                "last_completed_tick": index,
                "last_simulation_time": _iso(tick),
                "data_revision": self.data_revision,
                "b_input_revision": self.b_input_revision,
                "risk_revision": self.risk_revision,
                "risk_content_revision": self.risk_revision,
                "risk_window_revision": self.risk_window_revision,
                "plan_revision": self.plan_revision,
                "observation_sequence": self.observation_sequence,
                "navigation_state_revision": self.navigation_state_revision,
                "pre_planning_skips": self.pre_planning_skips,
                "replan_candidate_computations": self.replan_candidate_computations,
                "replan_candidates_accepted": self.replan_candidates_accepted,
                "replan_candidates_rejected": self.replan_candidates_rejected,
                "planning_elapsed_seconds": self.planning_elapsed_seconds,
                "tick_performance": self.tick_performance,
                "input_revision": self.input_revision,
                "risk_commit_id": self.risk_commit.commit_id if self.risk_commit else None,
                "risk_semantic_digest": self.risk_semantic_digest,
                "last_window_identity": self._last_window_identity,
                "route_semantic_digests": self.route_semantic_digests,
                "last_replan_reasons": list(self.last_replan_reasons),
                "nav_state": self.nav_state.to_dict() if self.nav_state else None,
                "active_plan_time_offset_seconds": (
                    self.active_plan_time_offset.total_seconds()
                ),
                "superseded_route_payload": self.superseded_route_payload,
            },
        )

    def _restore(self, checkpoint: dict[str, Any]) -> None:
        self.data_revision = int(checkpoint.get("data_revision", 0))
        self.b_input_revision = int(checkpoint.get("b_input_revision", 0))
        self.risk_revision = int(checkpoint.get("risk_revision", 0))
        self.plan_revision = int(checkpoint.get("plan_revision", 0))
        self.input_revision = int(checkpoint.get("input_revision", 0))
        self.observation_sequence = int(checkpoint.get("observation_sequence", 0))
        self.risk_window_revision = int(checkpoint.get("risk_window_revision", 0))
        self.navigation_state_revision = int(
            checkpoint.get("navigation_state_revision", 0)
        )
        self.pre_planning_skips = int(checkpoint.get("pre_planning_skips", 0))
        self.replan_candidate_computations = int(
            checkpoint.get("replan_candidate_computations", 0)
        )
        self.replan_candidates_accepted = int(
            checkpoint.get("replan_candidates_accepted", 0)
        )
        self.replan_candidates_rejected = int(
            checkpoint.get("replan_candidates_rejected", 0)
        )
        self.planning_elapsed_seconds = float(
            checkpoint.get("planning_elapsed_seconds", 0.0)
        )
        self.tick_performance = list(checkpoint.get("tick_performance", []))
        self.risk_semantic_digest = str(checkpoint.get("risk_semantic_digest", ""))
        self._last_window_identity = str(checkpoint.get("last_window_identity", ""))
        self.route_semantic_digests = dict(
            checkpoint.get("route_semantic_digests", {})
        )
        self.last_replan_reasons = tuple(
            checkpoint.get("last_replan_reasons", ())
        )
        nav = checkpoint.get("nav_state")
        self.nav_state = NavigationExecutionState.from_dict(nav) if nav else None
        self.active_plan_time_offset = timedelta(
            seconds=float(
                checkpoint.get("active_plan_time_offset_seconds", 0.0)
            )
        )
        superseded = checkpoint.get("superseded_route_payload")
        self.superseded_route_payload = (
            dict(superseded) if superseded is not None else None
        )
        self.pending_plan = None
        self.pending_plan_set = None
        self.pending_plan_kind = ""
        self.pending_decision_time = None
        self.pending_adoption_time = None
        self.pending_revision = 0
        self.pending_origin_node = None
        self.pending_origin_adjustment_km = None
        self.pending_decision_position = None
        self._last_visible_digest = ""
        self._last_relevant_digest = ""
        self._last_data_revision = 0
        self._initial_plan_attempted = int(checkpoint.get("plan_revision", 0)) > 0
        self._restore_runtime(checkpoint)

    def _restore_runtime(self, checkpoint: dict[str, Any]) -> None:
        """Re-attach committed risk store and C endpoints after a restart."""

        from arctic_route_risk.publishing.store import PersistentRiskStore

        commit_id = checkpoint.get("risk_commit_id")
        if not commit_id:
            return
        document = json.loads(
            (
                self.paths.risk_store_root / "commits" / f"{commit_id}.json"
            ).read_text(encoding="utf-8")
        )
        query = _query_from_commit_document(document)
        store = PersistentRiskStore(self.paths.risk_store_root)
        store.bind_generation_authority(
            _replay_run_id(self.replay_id), SimulationClock(query.start)
        )
        window = store.get_committed_window(query)
        self.risk_store = store
        self.risk_commit = window
        self.window_commit = window
        self.risk_valid_start = window.start
        self.risk_valid_end = window.end
        self.prediction_as_of = window.as_of
        self.ingress = RiskSourcePlanningIngress(
            source=store,
            configuration=self.configuration,
        )
        self.endpoint_mapping = map_corridor_endpoints(
            self.configuration,
            window.frames[0],
            max_adjustment_km=self.max_snap_km,
        )


def _observation(*, tick, data_revision, risk_revision, plan):
    from arctic_route_planning.replanning import ReplanObservation

    return ReplanObservation(
        observed_at=tick,
        risk_valid_time=tick,
        data_revision=data_revision,
        risk_revision=risk_revision,
        route_avg_risk=plan.metrics.avg_risk,
        route_max_risk=plan.metrics.max_risk,
    )


def _honest_replan_reasons(
    reasons: tuple[ReplanReason, ...],
    events: list[ReplayEvent],
) -> tuple[str, ...]:
    """Translate C's internal trigger reasons into replay-honest reasons.

    C's ReplanTriggerEvaluator treats the monotonic planning observation
    sequence and the per-tick suffix-window identity as ``data`` revisions.
    Replay semantics distinguish:

    * real A data revision (visible record set changed);
    * real B risk content revision (risk frames rebuilt);
    * window advance (suffix identity changed, content reused).

    A ``DATA`` trigger caused only by the observation sequence or window
    identity is therefore not surfaced as a data change.
    """

    data_changed = any(
        event.type in ("DATA_REVISION_CHANGED", "DATA_BECAME_VISIBLE")
        for event in events
    )
    content_changed = any(
        event.type in ("RISK_REVISION_CHANGED", "RISK_CONTENT_UPDATED")
        for event in events
    )
    mapped: list[str] = []
    for reason in reasons:
        value = getattr(reason, "value", None) or str(reason)
        if value == "data" and not (data_changed or content_changed):
            continue
        if value not in mapped:
            mapped.append(value)
    return tuple(mapped)


def merge_completed_track(
    previous: tuple[dict[str, Any], ...],
    waypoints: tuple[Any, ...],
) -> tuple[dict[str, Any], ...]:
    """Append-only executed-history merge across plan adoptions."""

    existing_keys = {
        (item["longitude"], item["latitude"], item["eta"])
        for item in previous
    }
    merged = list(previous)
    for waypoint in waypoints:
        key = (
            waypoint.longitude,
            waypoint.latitude,
            _iso(waypoint.eta),
        )
        if key in existing_keys:
            continue
        existing_keys.add(key)
        merged.append(
            {
                "longitude": waypoint.longitude,
                "latitude": waypoint.latitude,
                "eta": _iso(waypoint.eta),
            }
        )
    return tuple(merged)


def _default_output_root(replay_id: str) -> Path:
    return (
        Path("/root/my_project/work_package_a/data/output/rc2-smoke")
        / "causal-replay-mvp"
        / replay_id
    )


def _replay_run_id(replay_id: str) -> str:
    suffix = hashlib.sha256(replay_id.encode("utf-8")).hexdigest()[:12]
    return f"run-00000000-0000-4000-8000-{suffix}"


def _corridor_bbox(configuration) -> tuple[float, float, float, float]:
    box = configuration.corridor.data_bbox
    return (float(box.west), float(box.south), float(box.east), float(box.north))


def _common_causal_valid_end(
    records: tuple[SourceRecord, ...],
    knowledge_as_of: datetime,
) -> datetime:
    """Largest valid end supported by every required dynamic type at the
    knowledge cutoff (static layers use prior-support and never cap)."""

    visible = tuple(
        record for record in records if record.issue_time <= knowledge_as_of
    )
    dynamic_types = REQUIRED_FORMAL_DATA_TYPES - STATIC_TYPES
    ends: list[datetime] = []
    for data_type in sorted(dynamic_types):
        valid_times = [
            record.valid_time
            for record in visible
            if record.data_type == data_type
        ]
        if valid_times:
            ends.append(max(valid_times))
    return min(ends) if ends else knowledge_as_of


def _aligned_planning_hours(forecast_hours: int) -> int:
    """Smallest corridor-aligned horizon (24h multiple, >=72) covering the
    forecast hours, within the corridor policy bounds."""

    hours = max(72, ((forecast_hours + 23) // 24) * 24)
    if hours > 144:
        raise ValueError("causal forecast horizon exceeds corridor policy maximum")
    return hours


def _replay_configuration(
    configuration,
    *,
    replay_start: datetime,
    planning_horizon_hours: int,
):
    scenario = replace(
        configuration.scenario,
        simulation_start=replay_start,
        simulation_end=replay_start + timedelta(hours=planning_horizon_hours),
        horizon_hours=planning_horizon_hours,
    )
    return replace(configuration, scenario=scenario)


def _query_from_commit_document(document: dict[str, Any]):
    from arctic_route_planning.contracts import RiskWindowQuery

    return RiskWindowQuery(
        start=datetime.fromisoformat(
            document["start"].replace("Z", "+00:00")
        ).astimezone(UTC),
        end=datetime.fromisoformat(document["end"].replace("Z", "+00:00")).astimezone(
            UTC
        ),
        interval=timedelta(seconds=int(document["interval_seconds"])),
        run_id=str(document["run_id"]),
        scenario_id=str(document["scenario_id"]),
        corridor_id=str(document["corridor_id"]),
        generation_id=int(document["generation_id"]),
        vessel_profile_id=str(document["vessel_profile_id"]),
        config_digest=str(document["config_digest"]),
        model_config_digest=str(document["model_config_digest"]),
        as_of=datetime.fromisoformat(
            document["as_of"].replace("Z", "+00:00")
        ).astimezone(UTC),
    )
