"""Strict transport binding for C's formal route-motion sibling artifact."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctic_route_planning.domain import ObjectiveMode
from arctic_route_planning.publishing import (
    canonical_route_motion_sha256,
    four_layer_route_plan_set_from_dict,
    route_motion_set_from_dict,
    route_motion_set_to_dict,
)


def load_bound_route_motion_set(
    path: str | Path,
    *,
    plan_set_document: Mapping[str, Any],
    replay_routes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Load, validate, and bind one motion set to one C plan set and replay."""

    location = Path(path)
    try:
        document = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read route motion set {location}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("route motion set must be a JSON object")
    motion_set = route_motion_set_from_dict(document)
    plan_set = four_layer_route_plan_set_from_dict(plan_set_document)
    for name in (
        "layer_set_id", "run_id", "scenario_id", "corridor_id", "generation_id",
        "input_revision", "vessel_profile_id", "config_digest", "model_config_digest",
        "planner_config_digest",
    ):
        if getattr(motion_set, name) != getattr(plan_set, name):
            raise ValueError(f"route motion set differs from plan set: {name}")
    recommended = tuple(
        bundle.plans[ObjectiveMode.RECOMMENDED] for bundle in plan_set.layers
    )
    for record, plan in zip(motion_set.records, recommended, strict=True):
        if record.plan_id != plan.plan_id or record.planning_layer is not plan.planning_layer:
            raise ValueError("route motion records do not match four recommended plans")
        waypoint_payload = [
            {
                "longitude": waypoint.longitude,
                "latitude": waypoint.latitude,
                "eta": waypoint.eta.isoformat().replace("+00:00", "Z"),
                "recommended_speed_mps": waypoint.recommended_speed_mps,
            }
            for waypoint in plan.waypoints
        ]
        if record.raw_route_digest != canonical_route_motion_sha256(waypoint_payload):
            raise ValueError("route motion raw waypoint digest differs from C plan")
    normalized = route_motion_set_to_dict(motion_set)
    _validate_replay_adoption(normalized, replay_routes)
    return normalized


def validate_route_motion_context(
    motion_set: Mapping[str, Any],
    *,
    risk_window_id: str,
    risk_window_digest: str,
    vessel_profile_id: str,
    vessel_profile_version: str,
    vessel_profile_digest: str,
) -> None:
    """Bind a normalized set to the B commit and shared RunContext vessel."""

    expected = {
        "risk_window_id": risk_window_id,
        "risk_window_digest": risk_window_digest,
        "vessel_profile_id": vessel_profile_id,
        "vessel_profile_version": vessel_profile_version,
        "vessel_profile_digest": vessel_profile_digest,
    }
    for name, value in expected.items():
        if motion_set.get(name) != value:
            raise ValueError(f"route motion set differs from publication context: {name}")


def _validate_replay_adoption(
    motion_set: Mapping[str, Any],
    replay_routes: Sequence[Mapping[str, Any]],
) -> None:
    records = {
        record["plan_id"]: record
        for record in motion_set.get("records", [])
        if isinstance(record, Mapping)
    }
    matched = 0
    for route in replay_routes:
        record = records.get(route.get("route_id"))
        if record is None:
            continue
        matched += 1
        waypoints = route.get("waypoints")
        samples = record.get("motion_samples")
        if not isinstance(waypoints, list) or not waypoints:
            raise ValueError("replay route has no authoritative waypoints")
        if not isinstance(samples, list) or len(samples) < 2:
            raise ValueError("route motion record has no samples")
        first_waypoint = waypoints[0]
        first_sample = samples[0]
        offset_seconds = float(route.get("motion_time_offset_seconds", 0.0))
        first_sample_eta = _shift_iso(first_sample.get("eta"), offset_seconds)
        if (
            first_sample.get("lon") != first_waypoint.get("lon")
            or first_sample.get("lat") != first_waypoint.get("lat")
            or first_sample_eta != first_waypoint.get("eta")
            or first_sample_eta != route.get("effective_adoption_time")
        ):
            raise ValueError("route motion adoption would teleport from physical route start")
        last_waypoint = waypoints[-1]
        last_sample = samples[-1]
        if (
            last_sample.get("lon") != last_waypoint.get("lon")
            or last_sample.get("lat") != last_waypoint.get("lat")
            or _shift_iso(last_sample.get("eta"), offset_seconds)
            != last_waypoint.get("eta")
        ):
            raise ValueError("route motion endpoint differs from authoritative route")
    if matched == 0:
        raise ValueError("route motion set does not cover any replay route revision")


def _shift_iso(value: object, offset_seconds: float) -> str | None:
    if not isinstance(value, str):
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return (moment + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


__all__ = ["load_bound_route_motion_set", "validate_route_motion_context"]
