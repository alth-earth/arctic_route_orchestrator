"""Strict presentation projection for C's atomic four-layer route set.

The projection copies C-owned route semantics into a D-consumable package.  It
does not rank candidates, recompute metrics, or alter route geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.contracts import FourLayerRoutePlanSet, PlanLayer
from arctic_route_planning.domain import ObjectiveMode

SCHEMA_VERSION = "presentation.route-candidates.v1"
SOURCE_SCHEMA = "cd.four-layer-route-plan-set.v3"
RUNTIME_SCHEMA_VERSION = "presentation.runtime-route-candidates.v1"
RUNTIME_SOURCE_SCHEMA = "cd.four-layer-route-plan-set.v3"
RUNTIME_ID_PATTERN = re.compile(r"^runtime-route-candidates-sha256-[0-9a-f]{64}$")


def project_route_candidates(plan_set: FourLayerRoutePlanSet) -> dict[str, Any]:
    """Project one complete C v3 plan set without changing its semantics."""

    candidates: list[dict[str, Any]] = []
    for bundle in plan_set.layers:
        for objective in ObjectiveMode:
            plan = bundle.plans[objective]
            candidates.append(
                {
                    "candidate_id": plan.plan_id,
                    "layer": plan.planning_layer.value,
                    "objective": plan.objective_mode.value,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [waypoint.longitude, waypoint.latitude]
                            for waypoint in plan.waypoints
                        ],
                    },
                    "distance_km": plan.metrics.distance_km,
                    "arrival_eta": _iso(plan.waypoints[-1].eta),
                    "travel_hours": plan.metrics.eta_hours,
                    "risk_metrics": {
                        "average_risk": plan.metrics.avg_risk,
                        "maximum_risk": plan.metrics.max_risk,
                        "integrated_risk_hours": plan.metrics.integrated_risk_hours,
                        "minimum_confidence": plan.metrics.minimum_confidence,
                        "hard_violation_count": plan.metrics.hard_constraint_violations,
                    },
                    "provenance": {
                        "source_schema": plan.schema_version,
                        "source_plan_id": plan.plan_id,
                        "run_id": plan.run_id,
                        "scenario_id": plan.scenario_id,
                        "corridor_id": plan.corridor_id,
                        "vessel_profile_id": plan.vessel_profile_id,
                        "config_digest": plan.config_digest,
                        "model_config_digest": plan.model_config_digest,
                        "planner_config_digest": plan.planner_config_digest,
                        "generation_id": plan.generation_id,
                        "input_revision": plan.input_revision,
                        "source_risk_ids": list(plan.source_risk_ids),
                    },
                }
            )
    identity = {
        "layer_set_id": plan_set.layer_set_id,
        "decision_time": _iso(plan_set.start_time),
        "selected_candidate_id": plan_set.recommended.plan_id,
        "candidates": candidates,
    }
    candidate_set_id = "route-candidates-sha256-" + _digest(identity)
    document = {
        "schema_version": SCHEMA_VERSION,
        "status": "PUBLISHED",
        "candidate_set_id": candidate_set_id,
        "layer_set_id": plan_set.layer_set_id,
        "decision_time": identity["decision_time"],
        "selected_candidate_id": identity["selected_candidate_id"],
        "provenance": {
            "source_schema": SOURCE_SCHEMA,
            "source_layer_set_id": plan_set.layer_set_id,
            "source_run_id": plan_set.run_id,
            "source_generation_id": plan_set.generation_id,
            "source_input_revision": plan_set.input_revision,
            "projection_owner": "arctic_route_orchestrator",
        },
        "candidates": candidates,
    }
    validate_route_candidates(document)
    return document


def project_runtime_route_candidates(
    plan_set: FourLayerRoutePlanSet,
) -> dict[str, Any]:
    """Project C's complete 4x3 plans with authoritative waypoint ETAs.

    This additive package is intentionally separate from
    ``presentation.route-candidates.v1``.  The latter remains a compact
    display contract; this package is the only presentation input that may
    drive a pre-run full-voyage route choice.
    """

    candidates: list[dict[str, Any]] = []
    for bundle in plan_set.layers:
        for objective in ObjectiveMode:
            plan = bundle.plans[objective]
            waypoints = [
                {
                    "longitude": waypoint.longitude,
                    "latitude": waypoint.latitude,
                    "eta": _iso(waypoint.eta),
                    "recommended_speed_mps": waypoint.recommended_speed_mps,
                }
                for waypoint in plan.waypoints
            ]
            candidates.append(
                {
                    "candidate_id": plan.plan_id,
                    "layer": plan.planning_layer.value,
                    "objective": plan.objective_mode.value,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [waypoint["longitude"], waypoint["latitude"]]
                            for waypoint in waypoints
                        ],
                    },
                    "waypoints": waypoints,
                    "distance_km": plan.metrics.distance_km,
                    "arrival_eta": _iso(plan.waypoints[-1].eta),
                    "travel_hours": plan.metrics.eta_hours,
                    "risk_metrics": {
                        "average_risk": plan.metrics.avg_risk,
                        "maximum_risk": plan.metrics.max_risk,
                        "integrated_risk_hours": plan.metrics.integrated_risk_hours,
                        "minimum_confidence": plan.metrics.minimum_confidence,
                        "hard_violation_count": plan.metrics.hard_constraint_violations,
                    },
                    "provenance": {
                        "source_schema": plan.schema_version,
                        "source_plan_id": plan.plan_id,
                        "run_id": plan.run_id,
                        "scenario_id": plan.scenario_id,
                        "corridor_id": plan.corridor_id,
                        "vessel_profile_id": plan.vessel_profile_id,
                        "config_digest": plan.config_digest,
                        "model_config_digest": plan.model_config_digest,
                        "planner_config_digest": plan.planner_config_digest,
                        "generation_id": plan.generation_id,
                        "input_revision": plan.input_revision,
                        "source_risk_ids": list(plan.source_risk_ids),
                    },
                }
            )
    identity = {
        "layer_set_id": plan_set.layer_set_id,
        "decision_time": _iso(plan_set.start_time),
        "selected_candidate_id": plan_set.recommended.plan_id,
        "candidates": candidates,
    }
    document = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "status": "PUBLISHED",
        "runtime_candidate_set_id": "runtime-route-candidates-sha256-" + _digest(identity),
        "layer_set_id": plan_set.layer_set_id,
        "decision_time": identity["decision_time"],
        "selected_candidate_id": identity["selected_candidate_id"],
        "provenance": {
            "source_schema": RUNTIME_SOURCE_SCHEMA,
            "source_layer_set_id": plan_set.layer_set_id,
            "source_run_id": plan_set.run_id,
            "source_generation_id": plan_set.generation_id,
            "source_input_revision": plan_set.input_revision,
            "projection_owner": "arctic_route_orchestrator",
        },
        "candidates": candidates,
    }
    validate_runtime_route_candidates(document)
    return document


def load_route_candidates(path: str | Path) -> dict[str, Any]:
    """Load and strictly validate a published route-candidate package."""

    location = Path(path)
    value = json.loads(
        location.read_text(encoding="utf-8"),
        object_pairs_hook=_object_without_duplicate_keys,
        parse_constant=_reject_non_finite_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("route candidate document must be an object")
    validate_route_candidates(value)
    return value


def validate_route_candidates(document: Mapping[str, Any]) -> None:
    """Enforce the atomic 4 x 3 presentation publication invariants."""

    required = {
        "schema_version",
        "status",
        "candidate_set_id",
        "layer_set_id",
        "decision_time",
        "selected_candidate_id",
        "provenance",
        "candidates",
    }
    if set(document) != required:
        raise ValueError("route candidate package fields differ from v1")
    if document["schema_version"] != SCHEMA_VERSION or document["status"] != "PUBLISHED":
        raise ValueError("route candidate package must be published v1")
    candidates = document["candidates"]
    if not isinstance(candidates, list) or len(candidates) != 12:
        raise ValueError("published route candidate package must contain exactly 12 routes")
    expected_order = [
        (layer.value, objective.value)
        for layer in PlanLayer
        for objective in ObjectiveMode
    ]
    expected_pairs = set(expected_order)
    actual_pairs: list[tuple[str, str]] = []
    candidate_ids: list[str] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            raise ValueError(f"candidate {index} must be an object")
        _validate_candidate(candidate, index=index)
        actual_pairs.append((candidate["layer"], candidate["objective"]))
        candidate_ids.append(candidate["candidate_id"])
    if Counter(actual_pairs) != Counter(expected_pairs):
        raise ValueError("published route candidates must contain each layer/objective once")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate_id values must be unique")
    selected = document["selected_candidate_id"]
    if selected not in candidate_ids:
        raise ValueError("selected_candidate_id must reference a published candidate")
    selected_item = candidates[candidate_ids.index(selected)]
    if (selected_item["layer"], selected_item["objective"]) != (
        PlanLayer.FULL_VOYAGE.value,
        ObjectiveMode.RECOMMENDED.value,
    ):
        raise ValueError("selected candidate must be C's full-voyage recommended route")
    provenance = document["provenance"]
    if not isinstance(provenance, Mapping) or provenance.get("source_schema") != SOURCE_SCHEMA:
        raise ValueError("route candidate package must preserve C v3 provenance")
    if provenance.get("source_layer_set_id") != document["layer_set_id"]:
        raise ValueError("route candidate layer identity does not match provenance")
    identity = {
        "layer_set_id": document["layer_set_id"],
        "decision_time": document["decision_time"],
        "selected_candidate_id": selected,
        "candidates": candidates,
    }
    expected_id = "route-candidates-sha256-" + _digest(identity)
    if document["candidate_set_id"] != expected_id:
        raise ValueError("candidate_set_id does not match canonical presentation content")


def load_runtime_route_candidates(path: str | Path) -> dict[str, Any]:
    """Load and strictly validate the waypoint/ETA runtime candidate package."""

    location = Path(path)
    value = json.loads(
        location.read_text(encoding="utf-8"),
        object_pairs_hook=_object_without_duplicate_keys,
        parse_constant=_reject_non_finite_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("runtime route candidate document must be an object")
    validate_runtime_route_candidates(value)
    return value


def validate_runtime_route_candidates(document: Mapping[str, Any]) -> None:
    """Enforce identity, cardinality, and C waypoint/ETA invariants."""

    required = {
        "schema_version", "status", "runtime_candidate_set_id", "layer_set_id",
        "decision_time", "selected_candidate_id", "provenance", "candidates",
    }
    if set(document) != required:
        raise ValueError("runtime route candidate package fields differ from v1")
    if document["schema_version"] != RUNTIME_SCHEMA_VERSION or document["status"] != "PUBLISHED":
        raise ValueError("runtime route candidate package must be published v1")
    runtime_id = document["runtime_candidate_set_id"]
    if not isinstance(runtime_id, str) or RUNTIME_ID_PATTERN.fullmatch(runtime_id) is None:
        raise ValueError("runtime candidate set identity is invalid")
    if not isinstance(document["layer_set_id"], str) or not document["layer_set_id"].startswith(
        "layer-set-sha256-"
    ):
        raise ValueError("runtime candidate layer identity is invalid")
    if (
        not isinstance(document["decision_time"], str)
        or not document["decision_time"].endswith("Z")
    ):
        raise ValueError("runtime candidate decision_time is invalid")
    try:
        decision_time = datetime.fromisoformat(document["decision_time"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("runtime candidate decision_time is invalid") from exc
    decision_time = decision_time.astimezone(UTC)
    candidates = document["candidates"]
    if not isinstance(candidates, list) or len(candidates) != 12:
        raise ValueError("runtime route candidate package must contain exactly 12 routes")
    expected_order = [
        (layer.value, objective.value)
        for layer in PlanLayer
        for objective in ObjectiveMode
    ]
    expected_pairs = set(expected_order)
    actual_pairs: set[tuple[str, str]] = set()
    candidate_ids: set[str] = set()
    for index, candidate in enumerate(candidates):
        _validate_runtime_candidate(candidate, index=index, decision_time=decision_time)
        pair = (candidate["layer"], candidate["objective"])
        if pair != expected_order[index] or pair in actual_pairs:
            raise ValueError("runtime candidate layer/objective coverage is invalid")
        actual_pairs.add(pair)
        if candidate["candidate_id"] in candidate_ids:
            raise ValueError("runtime candidate IDs must be unique")
        candidate_ids.add(candidate["candidate_id"])
    if actual_pairs != expected_pairs:
        raise ValueError("runtime candidates must contain each layer/objective once")
    selected_id = document["selected_candidate_id"]
    if selected_id not in candidate_ids:
        raise ValueError("runtime selected_candidate_id must reference a candidate")
    selected = next(item for item in candidates if item["candidate_id"] == selected_id)
    if (selected["layer"], selected["objective"]) != (
        PlanLayer.FULL_VOYAGE.value,
        ObjectiveMode.RECOMMENDED.value,
    ):
        raise ValueError("runtime selected candidate must be C's full-voyage recommendation")
    provenance = document["provenance"]
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "source_schema", "source_layer_set_id", "source_run_id", "source_generation_id",
        "source_input_revision", "projection_owner",
    }:
        raise ValueError("runtime candidate provenance is malformed")
    if provenance.get("source_schema") != RUNTIME_SOURCE_SCHEMA or provenance.get(
        "source_layer_set_id"
    ) != document["layer_set_id"] or provenance.get("projection_owner") != (
        "arctic_route_orchestrator"
    ):
        raise ValueError("runtime candidate provenance identity is invalid")
    identity = {
        "layer_set_id": document["layer_set_id"],
        "decision_time": document["decision_time"],
        "selected_candidate_id": selected_id,
        "candidates": candidates,
    }
    expected_id = "runtime-route-candidates-sha256-" + _digest(identity)
    if runtime_id != expected_id:
        raise ValueError("runtime_candidate_set_id does not match canonical content")


def _validate_candidate(candidate: Mapping[str, Any], *, index: int) -> None:
    required = {
        "candidate_id",
        "layer",
        "objective",
        "geometry",
        "distance_km",
        "arrival_eta",
        "travel_hours",
        "risk_metrics",
        "provenance",
    }
    if set(candidate) != required:
        raise ValueError(f"candidate {index} fields differ from v1")
    geometry = candidate["geometry"]
    if not isinstance(geometry, Mapping) or set(geometry) != {"type", "coordinates"}:
        raise ValueError(f"candidate {index} geometry is malformed")
    coordinates = geometry["coordinates"]
    if (
        geometry["type"] != "LineString"
        or not isinstance(coordinates, list)
        or len(coordinates) < 2
    ):
        raise ValueError(f"candidate {index} geometry must be a LineString")
    for coordinate in coordinates:
        if (
            not isinstance(coordinate, list)
            or len(coordinate) != 2
            or any(not _finite_number(value) for value in coordinate)
        ):
            raise ValueError(f"candidate {index} contains an invalid coordinate")
    for name in ("distance_km", "travel_hours"):
        if not _finite_number(candidate[name]) or candidate[name] < 0:
            raise ValueError(f"candidate {index} {name} must be finite and non-negative")
    metrics = candidate["risk_metrics"]
    metric_names = {
        "average_risk",
        "maximum_risk",
        "integrated_risk_hours",
        "minimum_confidence",
        "hard_violation_count",
    }
    if not isinstance(metrics, Mapping) or set(metrics) != metric_names:
        raise ValueError(f"candidate {index} risk_metrics is malformed")
    for name in metric_names - {"hard_violation_count"}:
        if not _finite_number(metrics[name]) or metrics[name] < 0:
            raise ValueError(f"candidate {index} risk metric {name} is invalid")
    if metrics["hard_violation_count"] != 0:
        raise ValueError(f"candidate {index} contains a hard constraint violation")
    provenance = candidate["provenance"]
    if not isinstance(provenance, Mapping):
        raise ValueError(f"candidate {index} provenance is malformed")
    if provenance.get("source_schema") != "cd.route-plan.v3":
        raise ValueError(f"candidate {index} source schema is not cd.route-plan.v3")
    if provenance.get("source_plan_id") != candidate["candidate_id"]:
        raise ValueError(f"candidate {index} source plan identity is inconsistent")


def _validate_runtime_candidate(
    candidate: Mapping[str, Any],
    *,
    index: int,
    decision_time: datetime,
) -> None:
    required = {
        "candidate_id", "layer", "objective", "geometry", "waypoints", "distance_km",
        "arrival_eta", "travel_hours", "risk_metrics", "provenance",
    }
    if not isinstance(candidate, Mapping) or set(candidate) != required:
        raise ValueError(f"runtime candidate {index} fields differ from v1")
    _validate_candidate(
        {key: value for key, value in candidate.items() if key != "waypoints"},
        index=index,
    )
    waypoints = candidate["waypoints"]
    if not isinstance(waypoints, list) or len(waypoints) < 2:
        raise ValueError(f"runtime candidate {index} waypoints are invalid")
    previous = None
    for waypoint in waypoints:
        if not isinstance(waypoint, Mapping) or set(waypoint) != {
            "longitude", "latitude", "eta", "recommended_speed_mps"
        }:
            raise ValueError(f"runtime candidate {index} waypoint is malformed")
        if not _finite_number(waypoint["longitude"]) or not -180 <= waypoint["longitude"] <= 180:
            raise ValueError(f"runtime candidate {index} longitude is invalid")
        if not _finite_number(waypoint["latitude"]) or not -90 <= waypoint["latitude"] <= 90:
            raise ValueError(f"runtime candidate {index} latitude is invalid")
        if not _finite_number(waypoint["recommended_speed_mps"]) or waypoint[
            "recommended_speed_mps"
        ] <= 0:
            raise ValueError(f"runtime candidate {index} speed is invalid")
        if not isinstance(waypoint["eta"], str) or not waypoint["eta"].endswith("Z"):
            raise ValueError(f"runtime candidate {index} waypoint ETA is invalid")
        try:
            current = datetime.fromisoformat(waypoint["eta"].replace("Z", "+00:00")).astimezone(UTC)
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"runtime candidate {index} waypoint ETA is invalid") from exc
        if previous is not None and current <= previous:
            raise ValueError(f"runtime candidate {index} waypoint ETA is not increasing")
        previous = current
    if (not isinstance(candidate["arrival_eta"], str) or
        not candidate["arrival_eta"].endswith("Z")):
        raise ValueError(f"runtime candidate {index} arrival ETA is invalid")
    if previous is None or datetime.fromisoformat(
        candidate["arrival_eta"].replace("Z", "+00:00")
    ).astimezone(UTC) != previous:
        raise ValueError(f"runtime candidate {index} arrival ETA differs from waypoint ETA")
    first_eta = datetime.fromisoformat(waypoints[0]["eta"].replace("Z", "+00:00")).astimezone(UTC)
    if first_eta < decision_time:
        raise ValueError(f"runtime candidate {index} starts before decision time")
    coordinates = candidate["geometry"]["coordinates"]
    waypoint_coordinates = [
        [waypoint["longitude"], waypoint["latitude"]] for waypoint in waypoints
    ]
    if coordinates != waypoint_coordinates:
        raise ValueError(f"runtime candidate {index} geometry differs from C waypoints")


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("presentation timestamps must be UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


__all__ = [
    "RUNTIME_ID_PATTERN",
    "RUNTIME_SCHEMA_VERSION",
    "load_route_candidates",
    "load_runtime_route_candidates",
    "project_route_candidates",
    "project_runtime_route_candidates",
    "validate_route_candidates",
    "validate_runtime_route_candidates",
]
