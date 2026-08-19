"""Presentation Adapter contract tests (Strategy B viewer foundation)."""

from __future__ import annotations

from typing import Any

import pytest

from arctic_route_orchestrator.replay.presentation import (
    PresentationAdapter,
    PresentationDataError,
)


def _route(waypoints: list[tuple[float, float, str]], distance_km: float = 100.0):
    return {
        "distance_km": distance_km,
        "waypoints": [
            {"longitude": lon, "latitude": lat, "eta": eta}
            for lon, lat, eta in waypoints
        ],
    }


BASE_ROUTE = _route(
    [
        (10.0, 60.0, "2026-08-15T10:00:00Z"),
        (11.0, 61.0, "2026-08-15T12:00:00Z"),
        (12.0, 62.0, "2026-08-15T14:00:00Z"),
    ],
    distance_km=333.0,
)

NEW_ROUTE = _route(
    [
        (10.5, 60.5, "2026-08-15T12:00:00Z"),
        (11.5, 61.8, "2026-08-15T14:00:00Z"),
        (12.5, 63.0, "2026-08-15T16:00:00Z"),
    ],
    distance_km=333.0,
)


def _ship(
    *,
    revision: int = 1,
    time: str,
    edge_index: int = 0,
    edge_progress: float = 0.5,
    status: str = "UNDERWAY",
    completed_track: list[dict[str, Any]] | None = None,
    accepted_route: dict[str, Any] | None = BASE_ROUTE,
    pending_route: dict[str, Any] | None = None,
    adoption_status: str = "NONE",
    replan_decision_time: str | None = None,
    effective_adoption_time: str | None = None,
    candidate_plan_revision: int | None = None,
    planner_origin_node: list[int] | None = None,
    planner_origin_position: dict[str, float] | None = None,
    snap_adjustment_km: float = 2.0,
    segment_start_eta: str = "2026-08-15T10:00:00Z",
    segment_end_eta: str = "2026-08-15T12:00:00Z",
) -> dict[str, Any]:
    return {
        "status": status,
        "navigation_state_revision": 1,
        "accepted_plan_revision": revision,
        "accepted_plan_digest": "abc",
        "executed_until": time,
        "current_position": {
            "longitude": 10.5 + edge_progress,
            "latitude": 60.5 + edge_progress,
        },
        "current_node": planner_origin_node or [0, 0],
        "edge_progress": edge_progress,
        "completed_track": (
            list(completed_track)
            if completed_track is not None
            else [
                {"longitude": 10.0, "latitude": 60.0, "eta": "2026-08-15T10:00:00Z"},
                {"longitude": 11.0, "latitude": 61.0, "eta": "2026-08-15T12:00:00Z"},
            ]
        ),
        "remaining_distance_km": 200.0,
        "snap_adjustment_km": snap_adjustment_km,
        "current_edge_index": edge_index,
        "current_segment_start_eta": segment_start_eta,
        "current_segment_end_eta": segment_end_eta,
        "effective_speed_knots": 9.65,
        "speed_mps": 4.96,
        "speed_source": "waypoint_eta_linear_interpolation",
        "executed_distance_km": 80.0,
        "cumulative_travelled_km": 80.0,
        "planner_origin_node": (
            tuple(planner_origin_node) if planner_origin_node else (0, 0)
        ),
        "planner_origin_adjustment_km": snap_adjustment_km,
        "planner_origin_position": planner_origin_position or {
            "longitude": 10.5,
            "latitude": 60.5,
        },
        "replan_decision_time": replan_decision_time,
        "effective_adoption_time": effective_adoption_time,
        "adoption_status": adoption_status,
        "candidate_plan_revision": candidate_plan_revision,
        "replan_physical_position": None,
        "accepted_route": accepted_route,
        "pending_route": pending_route,
        "superseded_route": None,
    }


def _snapshot(
    index: int,
    time: str,
    ship: dict[str, Any],
    *,
    events: list[dict[str, Any]] | None = None,
    risk_revision: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": "orchestrator.replay-snapshot.v1",
        "replay_id": "test",
        "scenario_id": "scenario",
        "scenario_mode": "causal_replay",
        "snapshot_index": index,
        "simulation_time": time,
        "knowledge_as_of": time,
        "visibility": {
            "max_source_issue_time": time,
            "visible_record_set_digest": "digest",
            "b_relevant_input_digest": "digest",
            "data_revision": 1,
            "b_input_revision": 1,
        },
        "risk": {
            "risk_revision": risk_revision,
            "risk_content_revision": risk_revision,
            "risk_window_revision": 1,
            "prediction_as_of": time,
            "risk_valid_start": "2026-08-15T10:00:00Z",
            "risk_valid_end": "2026-08-18T15:00:00Z",
            "resource_identity": "risk-window-sha256-test",
            "resource_digest": "risk-digest",
            "risk_semantic_digest": "risk-semantic",
            "presentation_horizons": {
                "0h": time,
                "+6h": "2026-08-15T16:00:00Z",
                "+12h": "2026-08-15T22:00:00Z",
                "+24h": "2026-08-16T10:00:00Z",
            },
        },
        "planning": {
            "plan_revision": ship["accepted_plan_revision"],
            "planning_as_of": time,
            "departure_time": time,
        },
        "readiness": {
            "source_visibility": "READY",
            "b_input_ready": "READY",
            "risk_ready": "READY",
            "planning_ready": "READY",
        },
        "events": events or [],
        "ship_state": ship,
        "coverage": {},
        "hard_reason": {"identity": "land_sea_mask"},
        "data_quality": {},
        "snapshot_digest": "0000",
    }


def _manifest(events: list[dict[str, Any]], snapshot_count: int) -> dict[str, Any]:
    return {
        "schema_version": "orchestrator.replay-manifest.v1",
        "replay_id": "test",
        "scenario_id": "scenario",
        "scenario_mode": "causal_replay",
        "replay_start": "2026-08-15T10:00:00Z",
        "replay_end": "2026-08-15T16:00:00Z",
        "tick_cadence_hours": 1,
        "snapshot_count": snapshot_count,
        "snapshots": [{"index": i, "digest": "0000"} for i in range(snapshot_count)],
        "events": events,
        "resources": {},
        "provenance": {},
        "semantic_digest": "0000",
    }


def test_t1_snapshot_to_presentation_state_field_mapping() -> None:
    snapshots = [
        _snapshot(
            0,
            "2026-08-15T10:00:00Z",
            _ship(time="2026-08-15T10:00:00Z", status="UNDERWAY"),
        )
    ]
    adapter = PresentationAdapter(_manifest([], 1), snapshots)
    state = adapter.state_at("2026-08-15T10:00:00Z")
    assert state.scenario_mode == "causal_replay"
    assert state.knowledge_as_of == "2026-08-15T10:00:00Z"
    assert state.vessel.status == "UNDERWAY"
    assert state.vessel.physical_position_source.startswith(
        "accepted_route_eta"
    )
    assert state.plan.accepted_plan_revision == 1
    assert len(state.plan.accepted_future_route) == 3
    assert state.plan.current_authoritative_segment.index == 0
    assert state.plan.current_authoritative_segment.start_eta == "2026-08-15T10:00:00Z"
    assert state.plan.current_authoritative_segment.end_eta == "2026-08-15T12:00:00Z"
    assert state.risk.risk_content_revision == 1
    assert state.risk.current_resource == "risk-window-sha256-test"
    assert state.risk.presentation_horizons["+6h"] == "2026-08-15T16:00:00Z"
    assert state.risk.hard_reason_resource == "land_sea_mask"


def test_t2_continuous_vessel_motion_between_snapshots() -> None:
    snapshots = [
        _snapshot(
            0,
            "2026-08-15T10:00:00Z",
            _ship(time="2026-08-15T10:00:00Z"),
        ),
        _snapshot(
            1,
            "2026-08-15T11:00:00Z",
            _ship(time="2026-08-15T11:00:00Z", edge_progress=0.5),
        ),
    ]
    adapter = PresentationAdapter(_manifest([], 2), snapshots)
    first = adapter.state_at("2026-08-15T10:05:00Z")
    second = adapter.state_at("2026-08-15T10:10:00Z")
    assert first.vessel.latitude != second.vessel.latitude
    assert second.vessel.executed_distance_km > first.vessel.executed_distance_km
    assert first.vessel.current_edge_index == second.vessel.current_edge_index


def test_t3_stationary_regression_under_way_must_move() -> None:
    snapshots = [
        _snapshot(
            0,
            "2026-08-15T10:00:00Z",
            _ship(time="2026-08-15T10:00:00Z", status="UNDERWAY"),
        )
    ]
    adapter = PresentationAdapter(_manifest([], 1), snapshots)
    a = adapter.vessel_at("2026-08-15T10:30:00Z")
    b = adapter.vessel_at("2026-08-15T10:35:00Z")
    assert a["status"] == "UNDERWAY"
    assert a["speed_mps"] > 0
    assert (a["longitude"], a["latitude"]) != (b["longitude"], b["latitude"])


def test_t4_arrival_is_stationary_at_goal() -> None:
    snapshots = [
        _snapshot(
            0,
            "2026-08-15T10:00:00Z",
            _ship(time="2026-08-15T10:00:00Z", status="UNDERWAY"),
        )
    ]
    adapter = PresentationAdapter(_manifest([], 1), snapshots)
    arrived = adapter.state_at("2026-08-15T14:00:00Z")
    assert arrived.vessel.status == "ARRIVED"
    assert arrived.vessel.longitude == 12.0
    assert arrived.vessel.latitude == 62.0
    assert arrived.vessel.speed_mps == 0.0
    assert arrived.vessel.remaining_distance_km == 0.0


def test_t5_completed_track_preserves_history_across_plan() -> None:
    completed_before = [
        {"longitude": 10.0, "latitude": 60.0, "eta": "2026-08-15T10:00:00Z"},
    ]
    completed_after = [
        *completed_before,
        {"longitude": 11.0, "latitude": 61.0, "eta": "2026-08-15T12:00:00Z"},
    ]
    snapshots = [
        _snapshot(
            0,
            "2026-08-15T10:00:00Z",
            _ship(
                time="2026-08-15T10:00:00Z",
                revision=1,
                completed_track=completed_before,
                accepted_route=BASE_ROUTE,
            ),
        ),
        _snapshot(
            1,
            "2026-08-15T13:00:00Z",
            _ship(
                time="2026-08-15T13:00:00Z",
                revision=2,
                completed_track=completed_after,
                accepted_route=NEW_ROUTE,
                edge_index=1,
                snap_adjustment_km=0.0,
            ),
        ),
    ]
    events = [
        {
            "type": "REPLAN_TRIGGERED",
            "simulation_time": "2026-08-15T12:00:00Z",
            "revision": "2",
        },
        {
            "type": "ROUTE_CHANGED",
            "simulation_time": "2026-08-15T12:00:00Z",
            "revision": "2",
        },
    ]
    adapter = PresentationAdapter(_manifest(events, 2), snapshots)
    state = adapter.state_at("2026-08-15T13:00:00Z")
    assert state.plan.completed_track == completed_after
    assert state.plan.completed_track[:2] == completed_after


def test_t6_deferred_adoption_keeps_old_segment_then_switches() -> None:
    pending_ship = _ship(
        time="2026-08-15T11:00:00Z",
        revision=1,
        edge_progress=0.5,
        accepted_route=BASE_ROUTE,
        pending_route=NEW_ROUTE,
        adoption_status="PENDING",
        replan_decision_time="2026-08-15T11:00:00Z",
        effective_adoption_time="2026-08-15T12:00:00Z",
        candidate_plan_revision=2,
    )
    adopted_ship = _ship(
        time="2026-08-15T12:00:00Z",
        revision=2,
        edge_progress=0.0,
        accepted_route=NEW_ROUTE,
        adoption_status="DEFERRED",
        snap_adjustment_km=0.0,
        segment_start_eta="2026-08-15T12:00:00Z",
        segment_end_eta="2026-08-15T14:00:00Z",
    )
    snapshots = [
        _snapshot(
            0,
            "2026-08-15T10:00:00Z",
            _ship(time="2026-08-15T10:00:00Z", revision=1),
        ),
        _snapshot(1, "2026-08-15T11:00:00Z", pending_ship),
        _snapshot(2, "2026-08-15T12:00:00Z", adopted_ship),
    ]
    events = [
        {
            "type": "REPLAN_DECIDED",
            "simulation_time": "2026-08-15T11:00:00Z",
            "revision": "2",
        },
        {
            "type": "REPLAN_ADOPTED",
            "simulation_time": "2026-08-15T12:00:00Z",
            "revision": "2",
        },
        {
            "type": "ROUTE_CHANGED",
            "simulation_time": "2026-08-15T12:00:00Z",
            "revision": "2",
        },
    ]
    adapter = PresentationAdapter(_manifest(events, 3), snapshots)
    before = adapter.state_at("2026-08-15T11:30:00Z")
    assert before.plan.current_authoritative_segment.start_eta == "2026-08-15T10:00:00Z"
    assert before.plan.current_authoritative_segment.end_eta == "2026-08-15T12:00:00Z"
    assert before.plan.pending_adoption == {
        "mode": "NEXT_WAYPOINT_DEFERRED",
        "decision_time": "2026-08-15T11:00:00Z",
        "effective_adoption_time": "2026-08-15T12:00:00Z",
    }
    assert before.plan.pending_candidate is not None
    assert before.plan.pending_candidate["plan_revision"] == 2

    after_route = adapter.state_at("2026-08-15T12:30:00Z")
    assert after_route.plan.accepted_plan_revision == 2
    assert after_route.plan.current_authoritative_segment.start_eta == "2026-08-15T12:00:00Z"
    assert after_route.plan.current_authoritative_segment.end_eta == "2026-08-15T14:00:00Z"
    assert after_route.plan.pending_candidate is None
    assert after_route.vessel.status == "UNDERWAY"


def test_t7_replan_skipped_and_plan_reused_do_not_produce_route_change() -> None:
    snapshots = [
        _snapshot(
            i,
            f"2026-08-15T1{index}:00:00Z",
            _ship(time=f"2026-08-15T1{index}:00:00Z", revision=1),
        )
        for index, i in ((0, 0), (1, 1))
    ]
    events = [
        {
            "type": "REPLAN_SKIPPED",
            "simulation_time": "2026-08-15T11:00:00Z",
            "revision": "1",
        },
        {
            "type": "PLAN_REUSED",
            "simulation_time": "2026-08-15T11:00:00Z",
            "revision": "1",
        },
    ]
    adapter = PresentationAdapter(_manifest(events, 2), snapshots)
    assert adapter._route_changes == []
    audit = adapter.adoption_audit()
    assert audit["summary"]["accepted_replan_count"] == 0
    state = adapter.state_at("2026-08-15T11:00:00Z")
    assert state.plan.accepted_plan_revision == 1


def test_adoption_audit_immediate() -> None:
    snapshots = [
        _snapshot(
            0,
            "2026-08-15T10:00:00Z",
            _ship(time="2026-08-15T10:00:00Z", revision=1),
        ),
        _snapshot(
            1,
            "2026-08-15T11:00:00Z",
            _ship(time="2026-08-15T11:00:00Z", revision=1),
        ),
        _snapshot(
            2,
            "2026-08-15T12:00:00Z",
            _ship(time="2026-08-15T12:00:00Z", revision=2),
        ),
    ]
    events = [
        {
            "type": "REPLAN_TRIGGERED",
            "simulation_time": "2026-08-15T12:00:00Z",
            "revision": "2",
        },
        {
            "type": "ROUTE_CHANGED",
            "simulation_time": "2026-08-15T12:00:00Z",
            "revision": "2",
        },
    ]
    adapter = PresentationAdapter(_manifest(events, 3), snapshots)
    audit = adapter.adoption_audit()
    assert audit["summary"]["accepted_replan_count"] == 1
    assert audit["summary"]["IMMEDIATE"] == 1
    assert audit["summary"]["NEXT_WAYPOINT_DEFERRED"] == 0
    entry = audit["entries"][0]
    assert entry["adoption_mode"] == "IMMEDIATE"
    assert entry["plan_revision_before"] == 1
    assert entry["plan_revision_after"] == 2
    assert entry["effective_adoption_time"] == "2026-08-15T12:00:00Z"
    assert entry["route_changed_time"] == "2026-08-15T12:00:00Z"
    assert entry["physical_at_waypoint"] is False
    assert audit["summary"]["snap_adjustment_km"]["median"] == 2.0


def test_adoption_audit_deferred() -> None:
    snapshots = [
        _snapshot(
            0,
            "2026-08-15T10:00:00Z",
            _ship(time="2026-08-15T10:00:00Z", revision=1),
        ),
        _snapshot(
            1,
            "2026-08-15T11:00:00Z",
            _ship(
                time="2026-08-15T11:00:00Z",
                revision=1,
                pending_route=NEW_ROUTE,
                adoption_status="PENDING",
                replan_decision_time="2026-08-15T11:00:00Z",
                effective_adoption_time="2026-08-15T12:00:00Z",
                candidate_plan_revision=2,
            ),
        ),
        _snapshot(
            2,
            "2026-08-15T12:00:00Z",
            _ship(time="2026-08-15T12:00:00Z", revision=2),
        ),
    ]
    events = [
        {
            "type": "REPLAN_DECIDED",
            "simulation_time": "2026-08-15T11:00:00Z",
            "revision": "2",
        },
        {
            "type": "REPLAN_ADOPTED",
            "simulation_time": "2026-08-15T12:00:00Z",
            "revision": "2",
        },
        {
            "type": "ROUTE_CHANGED",
            "simulation_time": "2026-08-15T12:00:00Z",
            "revision": "2",
        },
    ]
    adapter = PresentationAdapter(_manifest(events, 3), snapshots)
    audit = adapter.adoption_audit()
    assert audit["summary"]["accepted_replan_count"] == 1
    assert audit["summary"]["NEXT_WAYPOINT_DEFERRED"] == 1
    entry = audit["entries"][0]
    assert entry["adoption_mode"] == "NEXT_WAYPOINT_DEFERRED"
    assert entry["decision_time"] == "2026-08-15T11:00:00Z"
    assert entry["effective_adoption_time"] == "2026-08-15T12:00:00Z"
    assert entry["route_changed_time"] == "2026-08-15T12:00:00Z"
    assert entry["physical_at_waypoint"] is False
    assert entry["planner_origin_node"] == [0, 0]


def test_arbitrary_time_motion_refuses_old_schema() -> None:
    ship = _ship(time="2026-08-15T10:00:00Z")
    ship["accepted_route"] = None
    snapshots = [
        _snapshot(0, "2026-08-15T10:00:00Z", ship),
    ]
    adapter = PresentationAdapter(_manifest([], 1), snapshots)
    with pytest.raises(PresentationDataError):
        adapter.state_at("2026-08-15T10:05:00Z")
