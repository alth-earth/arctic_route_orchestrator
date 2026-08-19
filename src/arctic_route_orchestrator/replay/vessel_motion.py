"""Pure vessel kinematics for same-vessel causal replay (Strategy B).

This module owns the *physical* execution state of the ship:

* how ``simulation_time`` maps to a continuous position along the accepted
  route;
* what the effective segment speed is (derived from route ETA, never a
  front-end constant);
* navigation status (NOT_STARTED / UNDERWAY / ARRIVED);
* per-segment execution bookkeeping.

It deliberately does NOT know about grid nodes, planner origins, or snaps.
``PlannerOrigin`` is a separate concept handled by the replay runner; a snap
for the planner must never change the physical position returned here.

Interpolation contract: route edges are defined by C's grid as straight
lon/lat segments (``RegularGrid.edge_sample_points`` uses endpoint-inclusive
linear lon/lat interpolation), so ship motion follows the same contract to
stay consistent with route geometry and risk/integrity sampling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from arctic_route_planning.domain.models import GeoPoint
from arctic_route_planning.grid import (
    initial_bearing_degrees,
)

MPS_PER_KNOT = 0.5144444444444445
EARTH_RADIUS_KM = 6_371.0088
INTERPOLATION = "linear_lon_lat_consistent_with_grid_edge"


class InvalidRouteTimingError(ValueError):
    """A route whose ETA sequence cannot legally drive ship motion."""


@dataclass(frozen=True, slots=True)
class VesselState:
    status: str  # NOT_STARTED | UNDERWAY | ARRIVED
    position: dict[str, float]
    edge_index: int | None
    edge_progress: float | None
    segment_start_eta: str | None
    segment_end_eta: str | None
    speed_mps: float
    speed_knots: float
    executed_distance_km: float
    remaining_distance_km: float
    course_degrees: float | None
    interpolation: str = INTERPOLATION


def _haversine_km(start: GeoPoint, end: GeoPoint) -> float:
    from math import asin, cos, radians, sin, sqrt

    lat1 = radians(start.latitude)
    lat2 = radians(end.latitude)
    delta_lat = lat2 - lat1
    delta_lon = radians(end.longitude - start.longitude)
    haversine = (
        sin(delta_lat / 2.0) ** 2
        + cos(lat1) * cos(lat2) * sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(haversine)))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _ensure_utc(value: datetime, *, index: int) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidRouteTimingError(
            f"waypoint {index} ETA must be timezone-aware UTC"
        )
    return value.astimezone(UTC)


def _point(waypoint: Any) -> GeoPoint:
    return GeoPoint(
        longitude=float(waypoint.longitude),
        latitude=float(waypoint.latitude),
    )


def vessel_state_at(
    tick: datetime,
    waypoints: tuple[Any, ...],
    *,
    total_distance_km: float | None = None,
) -> VesselState:
    """Return the physical vessel state for one arbitrary simulation time.

    ``tick`` must be timezone-aware UTC. The route ETA sequence must be
    strictly increasing (duplicate / non-monotonic ETA is a fail-closed
    InvalidRouteTimingError).
    """

    if tick.tzinfo is None or tick.utcoffset() is None:
        raise InvalidRouteTimingError("simulation tick must be timezone-aware UTC")
    tick = tick.astimezone(UTC)
    if not waypoints:
        raise InvalidRouteTimingError("route must contain at least one waypoint")
    normalized = [
        (index, _ensure_utc(waypoint.eta, index=index), waypoint)
        for index, waypoint in enumerate(waypoints)
    ]
    for previous, current in pairwise(normalized):
        if current[1] <= previous[1]:
            raise InvalidRouteTimingError(
                "waypoint ETA sequence must be strictly increasing "
                f"({_iso(previous[1])} -> {_iso(current[1])})"
            )

    first_eta = normalized[0][1]
    last_eta = normalized[-1][1]
    if tick < first_eta:
        start = _point(waypoints[0])
        return VesselState(
            status="NOT_STARTED",
            position={"longitude": start.longitude, "latitude": start.latitude},
            edge_index=0,
            edge_progress=0.0,
            segment_start_eta=_iso(first_eta),
            segment_end_eta=_iso(normalized[1][1]) if len(normalized) > 1 else None,
            speed_mps=0.0,
            speed_knots=0.0,
            executed_distance_km=0.0,
            remaining_distance_km=float(
                sum(
                    _haversine_km(_point(waypoints[i]), _point(waypoints[i + 1]))
                    for i in range(len(waypoints) - 1)
                )
                if total_distance_km is None
                else total_distance_km
            ),
            course_degrees=None,
        )

    reached = -1
    for index, (_, eta, _) in enumerate(normalized):
        if eta <= tick:
            reached = index
        else:
            break

    if reached == len(normalized) - 1:
        goal = _point(waypoints[-1])
        executed = (
            sum(
                _haversine_km(_point(waypoints[i]), _point(waypoints[i + 1]))
                for i in range(len(waypoints) - 1)
            )
            if total_distance_km is None
            else float(total_distance_km)
        )
        return VesselState(
            status="ARRIVED",
            position={"longitude": goal.longitude, "latitude": goal.latitude},
            edge_index=max(0, len(normalized) - 2),
            edge_progress=1.0,
            segment_start_eta=_iso(normalized[-2][1])
            if len(normalized) > 1
            else _iso(last_eta),
            segment_end_eta=_iso(last_eta),
            speed_mps=0.0,
            speed_knots=0.0,
            executed_distance_km=executed,
            remaining_distance_km=0.0,
            course_degrees=None,
        )

    index = reached
    start_eta = normalized[index][1]
    end_eta = normalized[index + 1][1]
    span_seconds = (end_eta - start_eta).total_seconds()
    if span_seconds <= 0.0:
        raise InvalidRouteTimingError("zero-duration edge is not navigable")
    fraction = min(1.0, max(0.0, (tick - start_eta).total_seconds() / span_seconds))
    start = _point(waypoints[index])
    end = _point(waypoints[index + 1])
    position = {
        "longitude": start.longitude
        + (end.longitude - start.longitude) * fraction,
        "latitude": start.latitude + (end.latitude - start.latitude) * fraction,
    }
    segment_km = _haversine_km(start, end)
    segment_speed_mps = segment_km * 1000.0 / span_seconds
    executed = 0.0
    for waypoint_index in range(index):
        executed += _haversine_km(
            _point(waypoints[waypoint_index]),
            _point(waypoints[waypoint_index + 1]),
        )
    executed += _haversine_km(
        start,
        GeoPoint(longitude=position["longitude"], latitude=position["latitude"]),
    )
    total = (
        sum(
            _haversine_km(_point(waypoints[i]), _point(waypoints[i + 1]))
            for i in range(len(waypoints) - 1)
        )
        if total_distance_km is None
        else float(total_distance_km)
    )
    segment_course = initial_bearing_degrees(start, end)
    return VesselState(
        status="UNDERWAY",
        position=position,
        edge_index=index,
        edge_progress=fraction,
        segment_start_eta=_iso(start_eta),
        segment_end_eta=_iso(end_eta),
        speed_mps=segment_speed_mps,
        speed_knots=segment_speed_mps / MPS_PER_KNOT,
        executed_distance_km=executed,
        remaining_distance_km=max(0.0, total - executed),
        course_degrees=segment_course,
    )


def segment_speed_knots(
    waypoints: tuple[Any, ...],
) -> list[dict[str, Any]]:
    """Per-segment effective speed summary for audit output."""

    if len(waypoints) < 2:
        return []
    rows: list[dict[str, Any]] = []
    for index in range(len(waypoints) - 1):
        start = _point(waypoints[index])
        end = _point(waypoints[index + 1])
        start_eta = _ensure_utc(waypoints[index].eta, index=index)
        end_eta = _ensure_utc(waypoints[index + 1].eta, index=index + 1)
        span = (end_eta - start_eta).total_seconds()
        if span <= 0.0:
            raise InvalidRouteTimingError(
                f"segment {index} has non-positive duration"
            )
        distance_km = _haversine_km(start, end)
        speed_mps = distance_km * 1000.0 / span
        rows.append(
            {
                "segment": index,
                "start_eta": _iso(start_eta),
                "end_eta": _iso(end_eta),
                "distance_km": distance_km,
                "duration_hours": span / 3600.0,
                "speed_mps": speed_mps,
                "speed_knots": speed_mps / MPS_PER_KNOT,
                "course_degrees": initial_bearing_degrees(start, end),
            }
        )
    return rows
