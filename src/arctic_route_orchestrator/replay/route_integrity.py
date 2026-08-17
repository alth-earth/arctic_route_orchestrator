"""Route Geospatial Integrity for newly generated causal routes.

Uses C's own RiskSampler / grid semantics (the same code the planner uses),
so the audit is authoritative for what the planner could legally traverse:

* waypoint hard at its exact ETA;
* edge hard along C's 3-sample pattern plus a dense ~10 km pattern;
* diagonal corner cutting through hard orthogonal side cells;
* hard_reason classification (LAND / DATA_UNAVAILABLE / OTHER).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from arctic_route_planning.contracts import RiskFrame
from arctic_route_planning.domain.models import GeoPoint
from arctic_route_planning.grid import RegularGrid, haversine_km
from arctic_route_planning.risk import RiskSampler


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _axis_weights(coordinates: tuple[float, ...], target: float):
    if len(coordinates) < 2:
        return ((0, 1.0),) if abs(coordinates[0] - target) <= 1e-10 else ()
    ascending = coordinates[0] < coordinates[-1]
    ordered = coordinates if ascending else tuple(reversed(coordinates))
    if target < ordered[0] - 1e-10 or target > ordered[-1] + 1e-10:
        return ()
    exact = [
        index
        for index, value in enumerate(ordered)
        if abs(value - target) <= 1e-10
    ]
    if exact:
        index = exact[0]
        return ((index if ascending else len(coordinates) - 1 - index, 1.0),)
    upper = 0
    while upper < len(ordered) and ordered[upper] <= target:
        upper += 1
    lower = upper - 1
    if lower < 0 or upper >= len(ordered):
        return ()
    fraction = (target - ordered[lower]) / (ordered[upper] - ordered[lower])
    lower_index = lower if ascending else len(coordinates) - 1 - lower
    upper_index = upper if ascending else len(coordinates) - 1 - upper
    return ((lower_index, 1.0 - fraction), (upper_index, fraction))


def _hard_reasons_at(
    frame: RiskFrame,
    longitude: float,
    latitude: float,
) -> frozenset[str]:
    payload = frame.payload
    latitudes = tuple(float(value) for value in payload.coords["latitude"].values)
    longitudes = tuple(float(value) for value in payload.coords["longitude"].values)
    hard = payload["hard_mask"].values
    reasons = payload["hard_reason"].values
    lat_weights = _axis_weights(latitudes, float(latitude))
    lon_weights = _axis_weights(longitudes, float(longitude))
    result: set[str] = set()
    for lat_index, lat_weight in lat_weights:
        for lon_index, lon_weight in lon_weights:
            if lat_weight * lon_weight <= 0.0:
                continue
            if bool(hard[lat_index, lon_index]):
                result.add(str(reasons[lat_index, lon_index]))
    return frozenset(result)


def _sample_hard_reasons(
    frames: tuple[RiskFrame, ...],
    sampler: RiskSampler,
    longitude: float,
    latitude: float,
    sampled_at: datetime,
) -> tuple[bool, frozenset[str]]:
    sample = sampler.sample(sampled_at, float(longitude), float(latitude))
    if not sample.hard_mask:
        return False, frozenset()
    lower = max(
        (frame for frame in frames if frame.valid_time <= sampled_at),
        key=lambda frame: frame.valid_time,
        default=None,
    )
    upper = min(
        (frame for frame in frames if frame.valid_time >= sampled_at),
        key=lambda frame: frame.valid_time,
        default=None,
    )
    reasons: set[str] = set()
    if lower is not None:
        reasons |= _hard_reasons_at(lower, longitude, latitude)
    if upper is not None and upper is not lower:
        reasons |= _hard_reasons_at(upper, longitude, latitude)
    return True, frozenset(reasons)


def audit_route(
    route: Any,
    frames: tuple[RiskFrame, ...],
) -> dict[str, Any]:
    """Audit one RoutePlan / RoutePlanV3 against committed risk frames."""

    sampler = RiskSampler(frames, max_frame_gap=timedelta(minutes=180))
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=True)
    waypoints = tuple(route.waypoints)
    violations: list[dict[str, Any]] = []
    waypoint_hard = 0
    edge_hard = 0
    land = 0
    data_unavailable = 0
    other = 0
    corner_cutting = 0

    def node_of(longitude: float, latitude: float):
        rows = [
            row
            for row, value in enumerate(grid.latitudes)
            if abs(value - float(latitude)) <= 1e-9
        ]
        columns = [
            column
            for column, value in enumerate(grid.longitudes)
            if abs(value - float(longitude)) <= 1e-9
        ]
        if len(rows) == 1 and len(columns) == 1:
            return rows[0], columns[0]
        return None

    nodes = [
        node_of(waypoint.longitude, waypoint.latitude) for waypoint in waypoints
    ]
    for index, (waypoint, node) in enumerate(zip(waypoints, nodes, strict=True)):
        if node is None:
            violations.append(
                {
                    "type": "ROUTE_REPRESENTATION_MISMATCH",
                    "waypoint": index,
                    "detail": "waypoint is not an exact grid node",
                }
            )
            continue
        hard, reasons = _sample_hard_reasons(
            frames,
            sampler,
            waypoint.longitude,
            waypoint.latitude,
            waypoint.eta,
        )
        if hard:
            waypoint_hard += 1
            reason = sorted(reasons)[0] if reasons else "OTHER"
            land += reason == "LAND"
            data_unavailable += reason == "DATA_UNAVAILABLE"
            other += reason not in ("LAND", "DATA_UNAVAILABLE")
            violations.append(
                {
                    "type": "WAYPOINT_HARD",
                    "waypoint": index,
                    "hard_reason": reason,
                    "lon": waypoint.longitude,
                    "lat": waypoint.latitude,
                    "eta": waypoint.eta.isoformat(),
                }
            )

    for index in range(len(waypoints) - 1):
        start = waypoints[index]
        end = waypoints[index + 1]
        start_node = nodes[index]
        end_node = nodes[index + 1]
        if start_node is None or end_node is None:
            continue
        row_delta = abs(end_node[0] - start_node[0])
        column_delta = abs(end_node[1] - start_node[1])
        if row_delta > 1 or column_delta > 1 or (row_delta + column_delta) == 0:
            violations.append(
                {
                    "type": "ROUTE_REPRESENTATION_MISMATCH",
                    "edge": index,
                    "detail": "consecutive waypoints are not 8-neighbors",
                }
            )
            continue
        diagonal = row_delta == 1 and column_delta == 1
        distance = haversine_km(
            GeoPoint(longitude=start.longitude, latitude=start.latitude),
            GeoPoint(longitude=end.longitude, latitude=end.latitude),
        )
        count = max(3, math.ceil(distance / 10.0) + 1)
        for sample_index in range(count):
            fraction = sample_index / (count - 1)
            longitude = start.longitude + (end.longitude - start.longitude) * fraction
            latitude = start.latitude + (end.latitude - start.latitude) * fraction
            sampled_at = start.eta + (end.eta - start.eta) * fraction
            hard, reasons = _sample_hard_reasons(
                frames, sampler, longitude, latitude, sampled_at
            )
            if hard:
                edge_hard += 1
                reason = sorted(reasons)[0] if reasons else "OTHER"
                land += reason == "LAND"
                data_unavailable += reason == "DATA_UNAVAILABLE"
                other += reason not in ("LAND", "DATA_UNAVAILABLE")
                violations.append(
                    {
                        "type": "EDGE_HARD",
                        "edge": index,
                        "sample": sample_index,
                        "hard_reason": reason,
                        "lon": round(longitude, 6),
                        "lat": round(latitude, 6),
                    }
                )
        if diagonal:
            row_low = min(start_node[0], end_node[0])
            row_high = max(start_node[0], end_node[0])
            column_low = min(start_node[1], end_node[1])
            column_high = max(start_node[1], end_node[1])
            mid_eta = start.eta + (end.eta - start.eta) * 0.5
            for side_node in ((row_low, column_high), (row_high, column_low)):
                side_lat = grid.latitudes[side_node[0]]
                side_lon = grid.longitudes[side_node[1]]
                hard, reasons = _sample_hard_reasons(
                    frames, sampler, side_lon, side_lat, mid_eta
                )
                if hard:
                    corner_cutting += 1
                    violations.append(
                        {
                            "type": "DIAGONAL_CORNER_CUT",
                            "edge": index,
                            "side_node": list(side_node),
                            "hard_reason": (
                                sorted(reasons)[0] if reasons else "OTHER"
                            ),
                        }
                    )

    return {
        "route_id": getattr(route, "plan_id", ""),
        "waypoint_count": len(waypoints),
        "edge_count": max(0, len(waypoints) - 1),
        "waypoint_hard_violations": waypoint_hard,
        "edge_hard_violations": edge_hard,
        "land_intersections": land,
        "data_unavailable_violations": data_unavailable,
        "other_hard_violations": other,
        "corner_cutting_violations": corner_cutting,
        "status": "PASS" if not violations else "FAIL",
        "violations": violations,
    }
