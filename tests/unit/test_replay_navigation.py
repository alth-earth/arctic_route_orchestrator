"""NavigationExecutionState, honest replan reasons, and validation (Strategy B)."""

from __future__ import annotations

from datetime import UTC, datetime

from arctic_route_planning.domain import ReplanReason

from arctic_route_orchestrator.replay.digests import replay_semantic_digest
from arctic_route_orchestrator.replay.models import NavigationExecutionState, ReplayEvent
from arctic_route_orchestrator.replay.runner import (
    _honest_replan_reasons,
    merge_completed_track,
)
from arctic_route_orchestrator.replay.validation import validate_replay


def _nav(status: str = "ACTIVE", revision: int = 3) -> NavigationExecutionState:
    return NavigationExecutionState(
        status=status,
        navigation_state_revision=revision,
        accepted_plan_revision=2,
        accepted_plan_digest="abc",
        executed_until="2026-08-15T13:00:00Z",
        current_position={"longitude": 14.0, "latitude": 70.0},
        current_node=(3, 4),
        edge_progress=0.5,
        completed_track=(
            {"longitude": 12.0, "latitude": 69.0, "eta": "2026-08-15T10:00:00Z"},
        ),
        remaining_distance_km=80.0,
        snap_adjustment_km=0.0,
        last_distance_delta_km=8.0,
        expected_travel_km=9.0,
    )


def test_navigation_state_roundtrip() -> None:
    state = _nav()
    restored = NavigationExecutionState.from_dict(state.to_dict())
    assert restored == state


def test_honest_replan_reasons_drop_fake_data_trigger() -> None:
    events = [
        ReplayEvent(
            type="CLOCK_TICK",
            simulation_time="2026-08-15T11:00:00Z",
            revision="1",
        ),
        ReplayEvent(
            type="RISK_WINDOW_ADVANCED",
            simulation_time="2026-08-15T11:00:00Z",
            revision="2",
        ),
    ]
    reasons = _honest_replan_reasons((ReplanReason.TIME, ReplanReason.DATA), events)
    assert reasons == ("time",)


def test_honest_replan_reasons_keep_real_data_trigger() -> None:
    events = [
        ReplayEvent(
            type="DATA_REVISION_CHANGED",
            simulation_time="2026-08-15T12:00:00Z",
            revision="2",
        ),
    ]
    reasons = _honest_replan_reasons((ReplanReason.TIME, ReplanReason.DATA), events)
    assert set(reasons) == {"time", "data"}


class _Waypoint:
    def __init__(self, longitude, latitude, eta) -> None:
        self.longitude = longitude
        self.latitude = latitude
        self.eta = eta


def test_completed_track_is_append_only_across_plan_adoption() -> None:
    previous = (
        {"longitude": 12.0, "latitude": 69.0, "eta": "2026-08-15T10:00:00Z"},
        {"longitude": 13.0, "latitude": 69.5, "eta": "2026-08-15T12:00:00Z"},
    )
    new_plan_waypoints = (
        _Waypoint(14.0, 70.0, datetime(2026, 8, 15, 14, tzinfo=UTC)),
        _Waypoint(15.0, 70.5, datetime(2026, 8, 15, 16, tzinfo=UTC)),
    )
    merged = merge_completed_track(previous, new_plan_waypoints)
    assert len(merged) == 4
    assert merged[:2] == previous
    assert merged[2]["eta"] == "2026-08-15T14:00:00Z"
    assert merged[3]["eta"] == "2026-08-15T16:00:00Z"


def _snapshot(index: int, simulation_time: str, **overrides):
    ship = overrides.pop("ship_state", _nav(revision=index + 3).to_dict())
    document = {
        "schema_version": "orchestrator.replay-snapshot.v1",
        "replay_id": "test",
        "scenario_id": "s",
        "scenario_mode": "causal_replay",
        "snapshot_index": index,
        "simulation_time": simulation_time,
        "knowledge_as_of": simulation_time,
        "visibility": {
            "max_source_issue_time": "2026-08-15T09:37:00Z",
            "visible_record_set_digest": "v",
            "b_relevant_input_digest": "b",
            "data_revision": 1,
            "b_input_revision": 1,
            "newly_visible_count": 0,
            "newly_visible_record_ids": [],
            "quality_summary": {"good": 1},
        },
        "risk": {
            "risk_revision": 1,
            "risk_content_revision": 1,
            "risk_window_revision": index + 1,
            "prediction_as_of": "2026-08-15T10:00:00Z",
            "risk_valid_start": "2026-08-15T10:00:00Z",
            "risk_valid_end": "2026-08-15T11:00:00Z",
            "resource_identity": "w",
            "resource_digest": "d",
            "risk_semantic_digest": "r",
            "presentation_horizons": {},
        },
        "planning": {
            "plan_revision": 2,
            "planning_as_of": simulation_time,
            "departure_time": simulation_time,
            "planning_valid_start": simulation_time,
            "planning_valid_end": "2026-08-18T15:00:00Z",
            "supported_layers": ["full_voyage_complete_route"],
            "unsupported_layers": [],
            "blockers": [],
            "resources": {},
            "observation_sequence": index + 1,
            "replan_reasons": ["time"],
            "route_semantic_digests": {},
        },
        "readiness": {
            "source_visibility": "READY",
            "b_input_ready": "READY",
            "risk_ready": "READY",
            "planning_ready": "READY",
            "blockers": [],
        },
        "events": [],
        "ship_state": ship,
        "coverage": {},
        "hard_reason": {},
        "data_quality": {},
        "snapshot_digest": "x",
    }
    document["snapshot_digest"] = replay_semantic_digest(
        {key: value for key, value in document.items() if key != "snapshot_digest"}
    )
    return document


def test_validate_replay_accepts_active_navigation_and_flags_rewind() -> None:
    snapshots = [
        _snapshot(0, "2026-08-15T10:00:00Z"),
        _snapshot(1, "2026-08-15T11:00:00Z"),
    ]
    result = validate_replay(snapshots)
    assert result["status"] == "PASS"

    bad = _snapshot(1, "2026-08-15T11:00:00Z")
    bad["ship_state"] = _nav(revision=1).to_dict()  # revision moved backwards
    bad["snapshot_digest"] = replay_semantic_digest(
        {key: value for key, value in bad.items() if key != "snapshot_digest"}
    )
    result = validate_replay([snapshots[0], bad])
    assert result["status"] == "FAIL"
    assert any("navigation_revision moved backwards" in item for item in result["violations"])
