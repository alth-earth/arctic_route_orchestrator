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
import time
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
from arctic_route_planning.endpoints import map_corridor_endpoints
from arctic_route_planning.ingress import RiskSourcePlanningIngress
from arctic_route_planning.service import ServicePlanningRequest

from arctic_route_orchestrator.replay.digests import (
    b_relevant_input_digest,
    visible_record_set_digest,
)
from arctic_route_orchestrator.replay.models import (
    DataVisibilitySummary,
    PlanningStateSummary,
    ReadinessSummary,
    ReplayEvent,
    ReplayManifest,
    RiskStateSummary,
    SimulationSnapshot,
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
    risk_forecast_end: datetime | None = None
    tick_cadence_hours: int
    a_data_root: Path
    manifest_path: Path
    b_config_path: Path
    c_config_root: Path
    contracts_config_root: Path
    frozen_run_context_path: Path
    max_snap_km: float = 30.0
    cache_memory_mb: float = 2048.0
    c_attempt_timeout_seconds: int = 900

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

    risk_commit: Any = None
    window_commit: Any = None
    risk_valid_start: datetime | None = None
    risk_valid_end: datetime | None = None
    prediction_as_of: datetime | None = None
    current_plan: Any = None
    current_plan_set: Any = None
    supported_layers: tuple[str, ...] = ()
    unsupported_layers: tuple[str, ...] = (
        "executable_0_6h",
        "rolling_0_24h",
        "main_corridor_24_72h",
        "full_voyage",
    )
    planning_blockers: tuple[str, ...] = ()
    _route_integrity: dict[str, Any] | None = None
    events: list[ReplayEvent] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    _model_config_digest: str = ""
    _last_visible_digest: str = ""
    _last_relevant_digest: str = ""
    _last_data_revision: int = 0
    _initial_plan_attempted: bool = False

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
        self.configuration = _replay_configuration(
            self.configuration,
            replay_start=self.replay_start,
            forecast_end=self.risk_forecast_end,
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
            self._last_visible_digest = visible_digest
            self._last_relevant_digest = relevant_digest
            self._last_data_revision = self.data_revision

            planning = self._planning_tick(tick, index, events, progress=progress)

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
            "plan_revision": self.plan_revision,
            "risk_commit_id": self.risk_commit.commit_id if self.risk_commit else None,
            "risk_valid_start": _iso(self.risk_valid_start) if self.risk_valid_start else None,
            "risk_valid_end": _iso(self.risk_valid_end) if self.risk_valid_end else None,
            "supported_layers": list(self.supported_layers),
            "unsupported_layers": list(self.unsupported_layers),
            "planning_blockers": list(self.planning_blockers),
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
        self.run_context = base_context
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
                    _iso(self.risk_forecast_end)
                    if self.risk_forecast_end
                    else None
                ),
                unsupported_layers=self.unsupported_layers,
                blockers=("RISK_NOT_READY",),
            )
        if (
            self.current_plan is None
            and self.plan_revision == 0
            and not self._initial_plan_attempted
        ):
            self._initial_plan_attempted = True
            if progress is not None:
                progress({"stage": "C initial planning attempt", "tick": _iso(tick)})
            self._attempt_initial_plan(tick, events)
        elif self.current_plan is not None:
            self._attempt_replan(tick, events)
        return PlanningStateSummary(
            plan_revision=self.plan_revision,
            planning_as_of=_iso(tick),
            departure_time=_iso(tick),
            planning_valid_start=_iso(tick),
            planning_valid_end=(
                _iso(self.risk_forecast_end)
                if self.risk_forecast_end
                else None
            ),
            supported_layers=self.supported_layers,
            unsupported_layers=self.unsupported_layers,
            blockers=self.planning_blockers,
        )

    def _attempt_initial_plan(self, tick: datetime, events: list[ReplayEvent]) -> None:
        request = self._planning_request(tick, input_revision=0)
        try:
            prepared = self.ingress.prepare(request)
            outcome = prepared.execute_four_layer()
            self.current_plan_set = outcome.plan_set
            self.current_plan = outcome.plan_set.recommended
            self._audit_and_attach_route()
            self.plan_revision = 1
            self.supported_layers = (
                "executable_0_6h",
                "rolling_0_24h",
                "main_corridor_24_72h",
                "full_voyage",
            )
            self.unsupported_layers = ()
            self.planning_blockers = ()
            events.append(
                ReplayEvent(
                    type="PLAN_COMPUTED",
                    simulation_time=_iso(tick),
                    revision=str(self.plan_revision),
                    observed=True,
                )
            )
        except Exception as exc:  # fail-closed: classify, never fake success
            v2_blocker = self._probe_v2(tick)
            self.planning_blockers = (
                f"{type(exc).__name__}: {exc}",
                *(v2_blocker,),
            )
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
                    description=self.planning_blockers[0][:300],
                    observed=True,
                )
            )

    def _attempt_replan(self, tick: datetime, events: list[ReplayEvent]) -> None:
        request = self._planning_request(tick, input_revision=self.plan_revision)
        observation = _observation(
            tick=tick,
            data_revision=self.data_revision,
            risk_revision=self.risk_commit.commit_id,
            plan=self.current_plan,
        )
        try:
            prepared = self.ingress.prepare(request)
            outcome = prepared.replan_four_layer_if_needed(request, observation)
            if outcome.decision.triggered and outcome.outcome is not None:
                self.plan_revision += 1
                self.current_plan_set = outcome.outcome.plan_set
                self.current_plan = outcome.outcome.plan_set.recommended
                self._audit_and_attach_route()
                events.append(
                    ReplayEvent(
                        type="REPLAN_TRIGGERED",
                        simulation_time=_iso(tick),
                        revision=str(self.plan_revision),
                        description=",".join(
                            reason.value for reason in outcome.decision.reasons
                        ),
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
                        type="PLAN_REUSED",
                        simulation_time=_iso(tick),
                        revision=str(self.plan_revision),
                        observed=True,
                    )
                )
        except Exception as exc:
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

    def _probe_v2(self, tick: datetime) -> str | None:
        """Independent second C-API probe (v2 batch) for honest evidence."""

        try:
            request = self._planning_request(tick, input_revision=0)
            prepared = self.ingress.prepare(request)
            prepared.execute()
            return None
        except Exception as exc:
            return f"v2 probe: {type(exc).__name__}: {exc}"

    def _audit_and_attach_route(self) -> None:
        from arctic_route_orchestrator.replay.route_integrity import audit_route

        plans = []
        plan_set = getattr(self, "current_plan_set", None)
        if plan_set is not None:
            for bundle in plan_set.layers:
                for _objective, plan in bundle.plans.items():
                    plans.append(plan)
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

    def _planning_request(self, tick: datetime, *, input_revision: int) -> ServicePlanningRequest:
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
            start=self.endpoint_mapping.start.node,
            goal=self.endpoint_mapping.goal.node,
            maximum_elapsed=None,
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
            prediction_as_of=_iso(self.prediction_as_of) if self.prediction_as_of else "",
            risk_valid_start=_iso(self.window_commit.start) if self.window_commit else None,
            risk_valid_end=_iso(self.window_commit.end) if self.window_commit else None,
            resource_identity=self.window_commit.commit_id if self.window_commit else None,
            resource_digest=self.window_commit.content_digest if self.window_commit else None,
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
                "plan_revision": self.plan_revision,
                "risk_commit_id": self.risk_commit.commit_id if self.risk_commit else None,
            },
        )

    def _restore(self, checkpoint: dict[str, Any]) -> None:
        self.data_revision = int(checkpoint.get("data_revision", 0))
        self.b_input_revision = int(checkpoint.get("b_input_revision", 0))
        self.risk_revision = int(checkpoint.get("risk_revision", 0))
        self.plan_revision = int(checkpoint.get("plan_revision", 0))
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


def _replay_configuration(configuration, *, replay_start: datetime, forecast_end: datetime):
    hours = int((forecast_end - replay_start).total_seconds() // 3600)
    if (forecast_end - replay_start) != timedelta(hours=hours):
        raise ValueError("risk forecast end must align to whole hours from replay start")
    scenario = replace(
        configuration.scenario,
        simulation_start=replay_start,
        simulation_end=forecast_end,
        horizon_hours=hours,
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
