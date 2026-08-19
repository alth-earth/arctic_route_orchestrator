"""Pure vessel kinematics tests (T1-T6, ship-motion round)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from arctic_route_orchestrator.replay.vessel_motion import (
    InvalidRouteTimingError,
    vessel_state_at,
)


def _waypoint(longitude: float, latitude: float, eta_str: str):
    return SimpleNamespace(
        longitude=longitude,
        latitude=latitude,
        eta=datetime.fromisoformat(eta_str.replace("Z", "+00:00")).astimezone(UTC),
    )


def _route():
    return (
        _waypoint(0.0, 0.0, "2026-08-15T10:00:00Z"),
        _waypoint(1.0, 0.0, "2026-08-15T11:00:00Z"),
        _waypoint(2.0, 0.0, "2026-08-15T12:00:00Z"),
    )


def test_t1_segment_speed_is_distance_over_duration() -> None:
    state = vessel_state_at(
        datetime(2026, 8, 15, 10, 30, tzinfo=UTC),
        _route(),
    )
    # ~111.19 km along 1deg of longitude at the equator over 1h.
    expected_knots = (
        state.executed_distance_km - 0.0
    ) / 0.5 / 1.852
    assert state.status == "UNDERWAY"
    assert state.speed_knots == pytest.approx(expected_knots, rel=1e-3)
    assert state.speed_mps > 0.0


def test_t2_exact_waypoint_is_exact() -> None:
    state = vessel_state_at(
        datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
        _route(),
    )
    assert state.position == {"longitude": 0.0, "latitude": 0.0}
    state2 = vessel_state_at(
        datetime(2026, 8, 15, 11, 0, tzinfo=UTC),
        _route(),
    )
    assert state2.position == {"longitude": 1.0, "latitude": 0.0}
    assert state2.edge_progress == 0.0


def test_t3_mid_edge_is_not_endpoint() -> None:
    state = vessel_state_at(
        datetime(2026, 8, 15, 10, 30, tzinfo=UTC),
        _route(),
    )
    assert state.edge_index == 0
    assert 0.25 < state.edge_progress < 0.75
    assert state.position != {"longitude": 0.0, "latitude": 0.0}
    assert state.position != {"longitude": 1.0, "latitude": 0.0}


def test_t4_continuous_progress_without_replan() -> None:
    route = _route()
    times = [
        datetime(2026, 8, 15, 10, 15, tzinfo=UTC),
        datetime(2026, 8, 15, 10, 30, tzinfo=UTC),
        datetime(2026, 8, 15, 10, 45, tzinfo=UTC),
    ]
    distances = [vessel_state_at(t, route).executed_distance_km for t in times]
    assert distances[0] < distances[1] < distances[2]


def test_t5_arrival_is_stationary_at_goal() -> None:
    state = vessel_state_at(
        datetime(2026, 8, 15, 13, 0, tzinfo=UTC),
        _route(),
    )
    assert state.status == "ARRIVED"
    assert state.position == {"longitude": 2.0, "latitude": 0.0}
    assert state.speed_knots == 0.0
    assert state.speed_mps == 0.0


def test_t6_invalid_eta_fails_closed() -> None:
    route = (
        _waypoint(0.0, 0.0, "2026-08-15T10:00:00Z"),
        _waypoint(1.0, 0.0, "2026-08-15T09:00:00Z"),
    )
    with pytest.raises(InvalidRouteTimingError):
        vessel_state_at(datetime(2026, 8, 15, 10, 0, tzinfo=UTC), route)

    duplicate = (
        _waypoint(0.0, 0.0, "2026-08-15T10:00:00Z"),
        _waypoint(1.0, 0.0, "2026-08-15T10:00:00Z"),
    )
    with pytest.raises(InvalidRouteTimingError):
        vessel_state_at(datetime(2026, 8, 15, 10, 0, tzinfo=UTC), duplicate)


def test_high_frequency_sampling_continuously_progresses() -> None:
    route = _route()
    previous: tuple[float, float] | None = None
    previous_distance = -1.0
    start = datetime(2026, 8, 15, 10, tzinfo=UTC)
    for index in range(13):
        state = vessel_state_at(
            start + timedelta(minutes=5 * index),
            route,
        )
        assert state.executed_distance_km > previous_distance
        assert state.speed_knots > 0.0
        current = (state.position["longitude"], state.position["latitude"])
        if previous is not None:
            assert current != previous
        previous = current
        previous_distance = state.executed_distance_km
