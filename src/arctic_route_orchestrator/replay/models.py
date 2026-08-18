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
    risk_content_revision: int
    plan_revision: int
    risk_window_revision: int = 0
    observation_sequence: int = 0
    navigation_state_revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_revision": self.data_revision,
            "b_input_revision": self.b_input_revision,
            "risk_content_revision": self.risk_content_revision,
            "risk_window_revision": self.risk_window_revision,
            "observation_sequence": self.observation_sequence,
            "plan_revision": self.plan_revision,
            "navigation_state_revision": self.navigation_state_revision,
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
    risk_content_revision: int = 0
    risk_window_revision: int = 0
    risk_semantic_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_revision": self.risk_revision,
            "risk_content_revision": self.risk_content_revision,
            "risk_window_revision": self.risk_window_revision,
            "prediction_as_of": self.prediction_as_of,
            "risk_valid_start": self.risk_valid_start,
            "risk_valid_end": self.risk_valid_end,
            "resource_identity": self.resource_identity,
            "resource_digest": self.resource_digest,
            "risk_semantic_digest": self.risk_semantic_digest,
            "presentation_horizons": dict(self.presentation_horizons),
        }


@dataclass(frozen=True, slots=True)
class PlanningStateSummary:
    plan_revision: int
    planning_as_of: str
    departure_time: str
    planning_valid_start: str | None = None
    planning_valid_end: str | None = None
    supported_layers: tuple[str, ...] = ()
    unsupported_layers: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    observation_sequence: int = 0
    replan_reasons: tuple[str, ...] = ()
    route_semantic_digests: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_revision": self.plan_revision,
            "planning_as_of": self.planning_as_of,
            "departure_time": self.departure_time,
            "planning_valid_start": self.planning_valid_start,
            "planning_valid_end": self.planning_valid_end,
            "supported_layers": list(self.supported_layers),
            "unsupported_layers": list(self.unsupported_layers),
            "blockers": list(self.blockers),
            "resources": self.resources,
            "observation_sequence": self.observation_sequence,
            "replan_reasons": list(self.replan_reasons),
            "route_semantic_digests": {
                layer: dict(objectives)
                for layer, objectives in self.route_semantic_digests.items()
            },
        }


@dataclass(frozen=True, slots=True)
class NavigationExecutionState:
    """Executed vessel state for same-vessel causal replay (v1, replay-local).

    v1 deliberately uses node-aligned replanning: the origin for a new plan is
    the last accepted-route waypoint reached at or before simulation time,
    snapped to the nearest navigable grid node within an explicit tolerance.
    Edge interpolation is reported for presentation/provenance but never used
    to teleport the replan origin.
    """

    status: str
    navigation_state_revision: int
    accepted_plan_revision: int
    accepted_plan_digest: str
    executed_until: str | None
    current_position: dict[str, float] | None
    current_node: tuple[int, int] | None
    edge_progress: float | None
    completed_track: tuple[dict[str, Any], ...] = ()
    remaining_distance_km: float | None = None
    snap_adjustment_km: float | None = None
    last_distance_delta_km: float | None = None
    expected_travel_km: float | None = None
    current_edge_index: int | None = None
    current_segment_start_eta: str | None = None
    current_segment_end_eta: str | None = None
    effective_speed_knots: float | None = None
    speed_source: str = "waypoint_eta_linear_interpolation"
    executed_distance_km: float | None = None
    cumulative_travelled_km: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "navigation_state_revision": self.navigation_state_revision,
            "accepted_plan_revision": self.accepted_plan_revision,
            "accepted_plan_digest": self.accepted_plan_digest,
            "executed_until": self.executed_until,
            "current_position": dict(self.current_position)
            if self.current_position
            else None,
            "current_node": list(self.current_node) if self.current_node else None,
            "edge_progress": self.edge_progress,
            "completed_track": list(self.completed_track),
            "remaining_distance_km": self.remaining_distance_km,
            "snap_adjustment_km": self.snap_adjustment_km,
            "last_distance_delta_km": self.last_distance_delta_km,
            "expected_travel_km": self.expected_travel_km,
            "current_edge_index": self.current_edge_index,
            "current_segment_start_eta": self.current_segment_start_eta,
            "current_segment_end_eta": self.current_segment_end_eta,
            "effective_speed_knots": self.effective_speed_knots,
            "speed_source": self.speed_source,
            "executed_distance_km": self.executed_distance_km,
            "cumulative_travelled_km": self.cumulative_travelled_km,
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> NavigationExecutionState:
        node = document.get("current_node")
        return cls(
            status=str(document.get("status", "DEFERRED")),
            navigation_state_revision=int(
                document.get("navigation_state_revision", 0)
            ),
            accepted_plan_revision=int(document.get("accepted_plan_revision", 0)),
            accepted_plan_digest=str(document.get("accepted_plan_digest", "")),
            executed_until=document.get("executed_until"),
            current_position=(
                {
                    "longitude": float(document["current_position"]["longitude"]),
                    "latitude": float(document["current_position"]["latitude"]),
                }
                if document.get("current_position")
                else None
            ),
            current_node=(int(node[0]), int(node[1])) if node else None,
            edge_progress=(
                float(document["edge_progress"])
                if document.get("edge_progress") is not None
                else None
            ),
            completed_track=tuple(
                dict(item) for item in document.get("completed_track", ())
            ),
            remaining_distance_km=document.get("remaining_distance_km"),
            snap_adjustment_km=document.get("snap_adjustment_km"),
            last_distance_delta_km=document.get("last_distance_delta_km"),
            expected_travel_km=document.get("expected_travel_km"),
            current_edge_index=(
                int(document["current_edge_index"])
                if document.get("current_edge_index") is not None
                else None
            ),
            current_segment_start_eta=document.get("current_segment_start_eta"),
            current_segment_end_eta=document.get("current_segment_end_eta"),
            effective_speed_knots=(
                float(document["effective_speed_knots"])
                if document.get("effective_speed_knots") is not None
                else None
            ),
            speed_source=str(
                document.get(
                    "speed_source", "waypoint_eta_linear_interpolation"
                )
            ),
            executed_distance_km=document.get("executed_distance_km"),
            cumulative_travelled_km=document.get("cumulative_travelled_km"),
        )


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
