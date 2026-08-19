"""NavigationExecutionState, honest replan reasons, and validation (Strategy B)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from arctic_route_planning.domain import ReplanReason

from arctic_route_orchestrator.replay.digests import replay_semantic_digest
from arctic_route_orchestrator.replay.models import NavigationExecutionState, ReplayEvent
from arctic_route_orchestrator.replay.runner import (
    ReplayRunner,
    _honest_replan_reasons,
    merge_completed_track,
)
from arctic_route_orchestrator.replay.validation import validate_replay
from arctic_route_orchestrator.replay.vessel_motion import vessel_state_at


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
        current_edge_index=2,
        current_segment_start_eta="2026-08-15T12:00:00Z",
        current_segment_end_eta="2026-08-15T14:00:00Z",
        effective_speed_knots=14.2,
        speed_mps=7.3,
        speed_source="waypoint_eta_linear_interpolation",
        executed_distance_km=120.0,
        cumulative_travelled_km=130.0,
        planner_origin_node=(3, 4),
        planner_origin_adjustment_km=0.0,
        replan_decision_time="2026-08-15T12:00:00Z",
        effective_adoption_time="2026-08-15T12:30:00Z",
        adoption_status="PENDING",
        candidate_plan_revision=3,
        replan_physical_position={"longitude": 14.0, "latitude": 70.0},
        planner_origin_position={"longitude": 13.5, "latitude": 69.75},
        accepted_route={
            "distance_km": 221.5,
            "waypoints": [
                {"longitude": 12.0, "latitude": 69.0, "eta": "2026-08-15T10:00:00Z"},
                {"longitude": 13.5, "latitude": 69.75, "eta": "2026-08-15T12:00:00Z"},
            ],
        },
        pending_route={
            "distance_km": 180.0,
            "waypoints": [
                {"longitude": 13.5, "latitude": 69.75, "eta": "2026-08-15T12:00:00Z"},
                {"longitude": 15.0, "latitude": 70.5, "eta": "2026-08-15T14:00:00Z"},
            ],
        },
        superseded_route={
            "plan_revision": 2,
            "superseded_at": "2026-08-15T12:00:00Z",
            "route": {
                "distance_km": 200.0,
                "waypoints": [
                    {
                        "longitude": 13.5,
                        "latitude": 69.75,
                        "eta": "2026-08-15T11:00:00Z",
                    },
                    {
                        "longitude": 15.0,
                        "latitude": 70.5,
                        "eta": "2026-08-15T13:00:00Z",
                    },
                ],
            },
        },
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
    moving = _nav(revision=4).to_dict()
    moving["current_position"] = {"longitude": 15.0, "latitude": 70.5}
    moving["cumulative_travelled_km"] = 140.0
    snapshots[1]["ship_state"] = moving
    snapshots[1]["snapshot_digest"] = replay_semantic_digest(
        {
            key: value
            for key, value in snapshots[1].items()
            if key != "snapshot_digest"
        }
    )
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


def test_validate_replay_flags_stationary_vessel() -> None:
    first = _snapshot(0, "2026-08-15T10:00:00Z")
    second = _snapshot(1, "2026-08-15T11:00:00Z")
    moving = _nav(revision=4).to_dict()
    moving["cumulative_travelled_km"] = 130.0
    second["ship_state"] = moving
    second["snapshot_digest"] = replay_semantic_digest(
        {key: value for key, value in second.items() if key != "snapshot_digest"}
    )
    result = validate_replay([first, second])
    assert result["status"] == "FAIL"
    assert any(
        "stationary vessel while simulation time advanced" in item
        for item in result["violations"]
    )


def test_pre_planning_gate_respects_interval_and_data_change() -> None:
    start = datetime(2026, 8, 15, 10, tzinfo=UTC)
    plan = SimpleNamespace(
        start_time=start,
        waypoints=(
            SimpleNamespace(eta=start + timedelta(hours=2)),
            SimpleNamespace(eta=start + timedelta(hours=60)),
        ),
    )
    runner = ReplayRunner(
        replay_id="test",
        scenario_id="s",
        corridor_id="c",
        replay_start=start,
        replay_end=start + timedelta(hours=2),
        tick_cadence_hours=1,
        a_data_root=None,
        manifest_path=None,
        b_config_path=None,
        c_config_root=None,
        contracts_config_root=None,
        frozen_run_context_path=None,
        replan_min_interval_hours=2.0,
    )
    runner.current_plan = plan
    tick_young = start + timedelta(hours=1)
    assert runner._should_skip_replan(
        tick_young,
        [ReplayEvent(type="CLOCK_TICK", simulation_time=tick_young.isoformat())],
    ) is True
    tick_boundary = start + timedelta(hours=2)
    assert runner._should_skip_replan(
        tick_boundary,
        [ReplayEvent(type="CLOCK_TICK", simulation_time=tick_boundary.isoformat())],
    ) is False
    assert runner._should_skip_replan(
        tick_young,
        [
            ReplayEvent(
                type="DATA_REVISION_CHANGED",
                simulation_time=tick_young.isoformat(),
            )
        ],
    ) is False


def test_pre_planning_gate_waits_for_waypoint_alignment() -> None:
    start = datetime(2026, 8, 15, 10, tzinfo=UTC)
    plan = SimpleNamespace(
        start_time=start,
        waypoints=(
            SimpleNamespace(eta=start),
            SimpleNamespace(eta=start + timedelta(hours=2)),
            SimpleNamespace(eta=start + timedelta(hours=60)),
        ),
    )
    runner = ReplayRunner(
        replay_id="test",
        scenario_id="s",
        corridor_id="c",
        replay_start=start,
        replay_end=start + timedelta(hours=4),
        tick_cadence_hours=1,
        a_data_root=None,
        manifest_path=None,
        b_config_path=None,
        c_config_root=None,
        contracts_config_root=None,
        frozen_run_context_path=None,
        replan_min_interval_hours=1.0,
        replan_waypoint_aligned_only=True,
    )
    runner.current_plan = plan
    interior = start + timedelta(hours=1)
    assert runner._should_skip_replan(
        interior,
        [ReplayEvent(type="CLOCK_TICK", simulation_time=interior.isoformat())],
    ) is True
    waypoint = start + timedelta(hours=2)
    assert runner._should_skip_replan(
        waypoint,
        [ReplayEvent(type="CLOCK_TICK", simulation_time=waypoint.isoformat())],
    ) is False
    event_tick = interior + timedelta(hours=1)
    assert runner._should_skip_replan(
        event_tick,
        [
            ReplayEvent(
                type="RISK_CONTENT_UPDATED",
                simulation_time=event_tick.isoformat(),
            )
        ],
    ) is False


def test_deferred_replan_keeps_physical_position_until_adoption() -> None:
    start = datetime(2026, 8, 15, 10, tzinfo=UTC)
    waypoints = (
        SimpleNamespace(longitude=0.0, latitude=0.0, eta=start),
        SimpleNamespace(longitude=1.0, latitude=0.0, eta=start + timedelta(hours=1)),
        SimpleNamespace(longitude=2.0, latitude=0.0, eta=start + timedelta(hours=2)),
    )
    current_plan = SimpleNamespace(
        start_time=start,
        waypoints=waypoints,
        metrics=SimpleNamespace(distance_km=222.0),
    )
    runner = ReplayRunner(
        replay_id="test",
        scenario_id="s",
        corridor_id="c",
        replay_start=start,
        replay_end=start + timedelta(hours=3),
        tick_cadence_hours=1,
        a_data_root=None,
        manifest_path=None,
        b_config_path=None,
        c_config_root=None,
        contracts_config_root=None,
        frozen_run_context_path=None,
    )
    runner.current_plan = current_plan
    decision = start + timedelta(minutes=30)
    before = vessel_state_at(decision, waypoints)
    adoption_spec = {
        "mode": "NEXT_WAYPOINT_DEFERRED",
        "origin_node": (0, 1),
        "origin_adjustment_km": 0.0,
        "adoption_time": start + timedelta(hours=1),
    }
    runner._accept_published_replan(
        decision,
        adoption_spec,
        plan=SimpleNamespace(
            start_time=start,
            waypoints=waypoints,
            metrics=SimpleNamespace(distance_km=150.0),
        ),
        plan_set=None,
        plan_kind="v3_four_layer",
    )
    assert runner.pending_plan is not None
    assert runner.pending_adoption_time == start + timedelta(hours=1)
    assert runner.pending_revision == 1
    assert runner.current_plan is current_plan
    after = vessel_state_at(decision, waypoints)
    assert after.position == before.position
    assert after.speed_knots == before.speed_knots
    assert after.executed_distance_km == before.executed_distance_km
