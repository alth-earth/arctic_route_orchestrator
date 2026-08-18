"""Machine validation of replay snapshots / manifests (Strategy B)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from arctic_route_orchestrator.replay.digests import replay_semantic_digest


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def validate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Check causal invariants for one replay snapshot document."""

    violations: list[str] = []
    simulation_time = _parse_utc(snapshot["simulation_time"])
    knowledge_as_of = _parse_utc(snapshot["knowledge_as_of"])
    visibility = snapshot["visibility"]
    risk = snapshot["risk"]
    planning = snapshot["planning"]
    if knowledge_as_of != simulation_time:
        violations.append("knowledge_as_of != simulation_time")
    max_issue = visibility.get("max_source_issue_time")
    if max_issue and _parse_utc(max_issue) > knowledge_as_of:
        violations.append("max_source_issue_time > knowledge_as_of")
    prediction_as_of = risk.get("prediction_as_of")
    if prediction_as_of and _parse_utc(prediction_as_of) > knowledge_as_of:
        violations.append("prediction_as_of > knowledge_as_of")
    planning_as_of = planning.get("planning_as_of")
    if planning_as_of and _parse_utc(planning_as_of) > knowledge_as_of:
        violations.append("planning_as_of > knowledge_as_of")
    if snapshot["scenario_mode"] != "causal_replay":
        violations.append("scenario_mode != causal_replay")
    ship_status = snapshot["ship_state"].get("status")
    if ship_status not in ("DEFERRED", "ACTIVE", "ARRIVED"):
        violations.append("ship_state.status is not DEFERRED/ACTIVE/ARRIVED")
    if ship_status != "DEFERRED":
        if "navigation_state_revision" not in snapshot["ship_state"]:
            violations.append("active ship_state lacks navigation_state_revision")
        if "accepted_plan_revision" not in snapshot["ship_state"]:
            violations.append("active ship_state lacks accepted_plan_revision")
        if "executed_until" not in snapshot["ship_state"]:
            violations.append("active ship_state lacks executed_until")
    expected = replay_semantic_digest(
        {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
    )
    if expected != snapshot.get("snapshot_digest"):
        violations.append("snapshot_digest does not match semantic content")
    return {
        "snapshot_index": snapshot.get("snapshot_index"),
        "simulation_time": snapshot.get("simulation_time"),
        "status": "PASS" if not violations else "FAIL",
        "violations": violations,
    }


def validate_replay(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate a full replay sequence (monotonicity + per-snapshot checks)."""

    violations: list[str] = []
    previous_time: datetime | None = None
    previous_index: int | None = None
    previous_revisions: dict[str, int] = {}
    previous_nav: dict[str, Any] | None = None
    previous_position: tuple[float, float] | None = None
    for snapshot in snapshots:
        result = validate_snapshot(snapshot)
        violations.extend(
            f"snapshot {snapshot.get('snapshot_index')}: {item}"
            for item in result["violations"]
        )
        current_time = _parse_utc(snapshot["simulation_time"])
        if previous_time is not None and current_time <= previous_time:
            violations.append("simulation_time not strictly monotonic")
        if previous_index is not None and snapshot["snapshot_index"] != previous_index + 1:
            violations.append("snapshot_index not monotonic")
        revisions = {
            "data": snapshot["visibility"]["data_revision"],
            "b_input": snapshot["visibility"]["b_input_revision"],
            "risk": snapshot["risk"]["risk_revision"],
            "plan": snapshot["planning"]["plan_revision"],
            "risk_window": snapshot["risk"].get("risk_window_revision", 0),
            "observation_sequence": snapshot["planning"].get(
                "observation_sequence", 0
            ),
            "navigation": snapshot["ship_state"].get(
                "navigation_state_revision", 0
            ),
        }
        for name, value in revisions.items():
            if (
                name in previous_revisions
                and value < previous_revisions[name]
            ):
                violations.append(f"{name}_revision moved backwards")
        previous_revisions = revisions
        ship = snapshot["ship_state"]
        if ship.get("status") != "DEFERRED":
            if (
                previous_nav is not None
                and ship.get("accepted_plan_revision", 0)
                < previous_nav.get("accepted_plan_revision", 0)
            ):
                violations.append("accepted_plan_revision moved backwards")
            previous_count = (
                len(previous_nav.get("completed_track", ()))
                if previous_nav is not None
                else 0
            )
            current_count = len(ship.get("completed_track", ()))
            if current_count < previous_count:
                violations.append("completed_track shrank (history rewrote)")
            position = ship.get("current_position")
            if position and previous_position:
                delta_km = _haversine_km(
                    previous_position,
                    (position["longitude"], position["latitude"]),
                )
                if delta_km > 80.0:
                    violations.append(
                        f"navigation teleport candidate: {delta_km:.1f} km in one tick"
                    )
        if ship.get("status") != "DEFERRED":
            previous_position = (
                (ship["current_position"]["longitude"], ship["current_position"]["latitude"])
                if ship.get("current_position")
                else None
            )
            previous_nav = ship
        else:
            previous_nav = None
            previous_position = None
        previous_time = current_time
        previous_index = snapshot["snapshot_index"]
    return {
        "snapshot_count": len(snapshots),
        "status": "PASS" if not violations else "FAIL",
        "violations": violations,
    }


def validate_manifest(manifest: dict[str, Any], snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Check manifest references match the actual snapshot sequence."""

    violations: list[str] = []
    if manifest.get("snapshot_count") != len(snapshots):
        violations.append("manifest snapshot_count mismatch")
    if manifest.get("scenario_mode") != "causal_replay":
        violations.append("manifest scenario_mode != causal_replay")
    actual_indexes = [snapshot["snapshot_index"] for snapshot in snapshots]
    expected_indexes = list(range(len(snapshots)))
    if actual_indexes != expected_indexes:
        violations.append("snapshot indexes not contiguous")
    for entry in manifest.get("snapshots", []):
        index = entry["index"]
        if index >= len(snapshots):
            violations.append(f"manifest references missing snapshot {index}")
            continue
        if snapshots[index]["snapshot_digest"] != entry.get("digest"):
            violations.append(f"manifest digest mismatch for snapshot {index}")
    return {
        "status": "PASS" if not violations else "FAIL",
        "violations": violations,
    }


def _haversine_km(start: tuple[float, float], end: tuple[float, float]) -> float:
    import math

    lon1, lat1 = (math.radians(value) for value in start)
    lon2, lat2 = (math.radians(value) for value in end)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    a = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * 6_371.0088 * math.asin(min(1.0, math.sqrt(a)))
