"""Replay-local SimulationSnapshot / ReplayManifest models (Strategy B)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from arctic_route_orchestrator.replay.digests import replay_semantic_digest


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    type: str
    simulation_time: str
    revision: str | None = None
    description: str = ""
    observed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "simulation_time": self.simulation_time,
            "revision": self.revision,
            "description": self.description,
            "observed": self.observed,
        }


@dataclass(frozen=True, slots=True)
class RevisionState:
    data_revision: int
    b_input_revision: int
    risk_revision: int
    plan_revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_revision": self.data_revision,
            "b_input_revision": self.b_input_revision,
            "risk_revision": self.risk_revision,
            "plan_revision": self.plan_revision,
        }


@dataclass(frozen=True, slots=True)
class DataVisibilitySummary:
    max_source_issue_time: str | None
    visible_record_set_digest: str
    b_relevant_input_digest: str
    data_revision: int
    b_input_revision: int
    newly_visible_count: int = 0
    newly_visible_record_ids: tuple[str, ...] = ()
    quality_summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_source_issue_time": self.max_source_issue_time,
            "visible_record_set_digest": self.visible_record_set_digest,
            "b_relevant_input_digest": self.b_relevant_input_digest,
            "data_revision": self.data_revision,
            "b_input_revision": self.b_input_revision,
            "newly_visible_count": self.newly_visible_count,
            "newly_visible_record_ids": list(self.newly_visible_record_ids),
            "quality_summary": dict(self.quality_summary),
        }


@dataclass(frozen=True, slots=True)
class RiskStateSummary:
    risk_revision: int
    prediction_as_of: str
    risk_valid_start: str | None
    risk_valid_end: str | None
    resource_identity: str | None = None
    resource_digest: str | None = None
    presentation_horizons: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_revision": self.risk_revision,
            "prediction_as_of": self.prediction_as_of,
            "risk_valid_start": self.risk_valid_start,
            "risk_valid_end": self.risk_valid_end,
            "resource_identity": self.resource_identity,
            "resource_digest": self.resource_digest,
            "presentation_horizons": dict(self.presentation_horizons),
        }


@dataclass(frozen=True, slots=True)
class PlanningStateSummary:
    plan_revision: int
    planning_as_of: str
    departure_time: str
    supported_layers: tuple[str, ...] = ()
    unsupported_layers: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_revision": self.plan_revision,
            "planning_as_of": self.planning_as_of,
            "departure_time": self.departure_time,
            "supported_layers": list(self.supported_layers),
            "unsupported_layers": list(self.unsupported_layers),
            "blockers": list(self.blockers),
            "resources": self.resources,
        }


@dataclass(frozen=True, slots=True)
class ReadinessSummary:
    source_visibility: str
    b_input_ready: str
    risk_ready: str
    planning_ready: str
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_visibility": self.source_visibility,
            "b_input_ready": self.b_input_ready,
            "risk_ready": self.risk_ready,
            "planning_ready": self.planning_ready,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class SimulationSnapshot:
    schema_version: str
    replay_id: str
    scenario_id: str
    scenario_mode: str
    snapshot_index: int
    simulation_time: str
    knowledge_as_of: str
    visibility: DataVisibilitySummary
    risk: RiskStateSummary
    planning: PlanningStateSummary
    readiness: ReadinessSummary
    events: tuple[ReplayEvent, ...] = ()
    ship_state: dict[str, Any] = field(default_factory=lambda: {"status": "DEFERRED"})
    coverage: dict[str, Any] = field(default_factory=dict)
    hard_reason: dict[str, Any] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)
    snapshot_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "replay_id": self.replay_id,
            "scenario_id": self.scenario_id,
            "scenario_mode": self.scenario_mode,
            "snapshot_index": self.snapshot_index,
            "simulation_time": self.simulation_time,
            "knowledge_as_of": self.knowledge_as_of,
            "visibility": self.visibility.to_dict(),
            "risk": self.risk.to_dict(),
            "planning": self.planning.to_dict(),
            "readiness": self.readiness.to_dict(),
            "events": [event.to_dict() for event in self.events],
            "ship_state": self.ship_state,
            "coverage": self.coverage,
            "hard_reason": self.hard_reason,
            "data_quality": self.data_quality,
            "snapshot_digest": self.snapshot_digest,
        }

    def with_digest(self) -> SimulationSnapshot:
        document = self.to_dict()
        digest = replay_semantic_digest(
            {key: value for key, value in document.items() if key != "snapshot_digest"}
        )
        return replace(self, snapshot_digest=digest)


@dataclass(frozen=True, slots=True)
class ReplayManifest:
    schema_version: str
    replay_id: str
    scenario_id: str
    scenario_mode: str
    replay_start: str
    replay_end: str
    tick_cadence_hours: int
    snapshot_count: int
    snapshots: tuple[dict[str, Any], ...] = ()
    events: tuple[ReplayEvent, ...] = ()
    resources: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "replay_id": self.replay_id,
            "scenario_id": self.scenario_id,
            "scenario_mode": self.scenario_mode,
            "replay_start": self.replay_start,
            "replay_end": self.replay_end,
            "tick_cadence_hours": self.tick_cadence_hours,
            "snapshot_count": self.snapshot_count,
            "snapshots": list(self.snapshots),
            "events": [event.to_dict() for event in self.events],
            "resources": self.resources,
            "provenance": self.provenance,
            "semantic_digest": replay_semantic_digest(
                {
                    "schema_version": self.schema_version,
                    "replay_id": self.replay_id,
                    "scenario_id": self.scenario_id,
                    "scenario_mode": self.scenario_mode,
                    "replay_start": self.replay_start,
                    "replay_end": self.replay_end,
                    "tick_cadence_hours": self.tick_cadence_hours,
                    "snapshots": self.snapshots,
                    "events": [event.to_dict() for event in self.events],
                    "resources": self.resources,
                }
            ),
        }
