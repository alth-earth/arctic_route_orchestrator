"""Strict transport binding for C's formal route-motion sibling artifact."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctic_route_planning.contracts import PlanLayer
from arctic_route_planning.domain import ObjectiveMode
from arctic_route_planning.publishing import (
    canonical_route_motion_sha256,
    four_layer_route_plan_set_from_dict,
    route_motion_candidate_set_from_dict,
    route_motion_candidate_set_to_dict,
    route_motion_set_from_dict,
    route_motion_set_to_dict,
)

from arctic_route_orchestrator.route_presentation import validate_runtime_route_candidates


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
    _validate_optional_qualification_evidence(
        location,
        artifact_kind="motion_set",
        artifact_id=motion_set.motion_set_id,
        producer_digest=motion_set.producer_digest,
        risk_window_digest=motion_set.risk_window_digest,
        records=motion_set.records,
    )
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


def load_bound_route_motion_candidate_set(
    path: str | Path,
    *,
    plan_set_document: Mapping[str, Any],
    runtime_candidates_document: Mapping[str, Any],
) -> dict[str, Any]:
    """Load the additive C motion artifact for the three full-voyage objectives.

    The candidate motion set is deliberately bound to both the immutable C
    plan set and the orchestrator's waypoint/ETA presentation projection.  A
    D consumer therefore cannot silently pair a curve sample with a different
    objective, revision, or timestamp.
    """

    location = Path(path)
    try:
        document = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read route motion candidate set {location}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("route motion candidate set must be a JSON object")
    candidate_set = route_motion_candidate_set_from_dict(document)
    _validate_optional_qualification_evidence(
        location,
        artifact_kind="motion_candidate_set",
        artifact_id=candidate_set.motion_candidate_set_id,
        producer_digest=candidate_set.producer_digest,
        risk_window_digest=candidate_set.risk_window_digest,
        records=candidate_set.records,
    )
    plan_set = four_layer_route_plan_set_from_dict(plan_set_document)
    validate_runtime_route_candidates(runtime_candidates_document)
    if candidate_set.layer_set_id != plan_set.layer_set_id:
        raise ValueError("route motion candidate set differs from plan layer_set_id")
    if runtime_candidates_document.get("layer_set_id") != plan_set.layer_set_id:
        raise ValueError("runtime route candidates differ from plan layer_set_id")
    for name in (
        "run_id", "scenario_id", "corridor_id", "generation_id", "input_revision",
        "vessel_profile_id", "config_digest", "model_config_digest",
        "planner_config_digest",
    ):
        if getattr(candidate_set, name) != getattr(plan_set, name):
            raise ValueError(f"route motion candidate set differs from plan set: {name}")
    candidates = {
        item["objective"]: item
        for item in runtime_candidates_document["candidates"]
        if item["layer"] == "full_voyage"
    }
    full = plan_set.bundle_for(PlanLayer.FULL_VOYAGE)
    for item in candidate_set.records:
        objective = item.objective_mode.value
        plan = full.plans[item.objective_mode]
        candidate = candidates.get(objective)
        if candidate is None or candidate["candidate_id"] != plan.plan_id:
            raise ValueError("route motion candidate objective is not bound to C plan")
        waypoint_payload = [
            {
                "longitude": waypoint.longitude,
                "latitude": waypoint.latitude,
                "eta": waypoint.eta.isoformat().replace("+00:00", "Z"),
                "recommended_speed_mps": waypoint.recommended_speed_mps,
            }
            for waypoint in plan.waypoints
        ]
        candidate_payload = [
            {
                "longitude": waypoint["longitude"],
                "latitude": waypoint["latitude"],
                "eta": waypoint["eta"],
                "recommended_speed_mps": waypoint["recommended_speed_mps"],
            }
            for waypoint in candidate["waypoints"]
        ]
        if candidate_payload != waypoint_payload:
            raise ValueError("runtime candidate waypoints differ from C plan")
        if item.record.plan_id != plan.plan_id or item.record.raw_route_digest != (
            canonical_route_motion_sha256(waypoint_payload)
        ):
            raise ValueError("route motion candidate raw waypoint digest differs from C plan")
    return route_motion_candidate_set_to_dict(candidate_set)


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


def _validate_optional_qualification_evidence(
    motion_location: Path,
    *,
    artifact_kind: str,
    artifact_id: str,
    producer_digest: str,
    risk_window_digest: str,
    records: Sequence[Any],
) -> None:
    """Validate a new C evidence sidecar while keeping old v1 artifacts readable."""

    location = motion_location.with_name("route-motion-qualification-evidence.json")
    if not location.exists():
        return
    try:
        evidence = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"cannot read route motion qualification evidence {location}: {exc}"
        ) from exc
    if not isinstance(evidence, Mapping):
        raise ValueError("route motion qualification evidence must be an object")
    if evidence.get("schema_version") != "c.route-motion-qualification-evidence.v1":
        raise ValueError("unsupported route motion qualification evidence schema")
    evidence_id = evidence.get("evidence_id")
    if (
        not isinstance(evidence_id, str)
        or not evidence_id.startswith("route-motion-qualification-evidence-sha256-")
    ):
        raise ValueError("route motion qualification evidence identity is invalid")
    evidence_body = dict(evidence)
    evidence_body.pop("evidence_id", None)
    expected_evidence_id = (
        "route-motion-qualification-evidence-sha256-"
        + canonical_route_motion_sha256(evidence_body)
    )
    if evidence_id != expected_evidence_id:
        raise ValueError("route motion qualification evidence digest does not match content")
    if evidence.get("producer_digest") != producer_digest:
        raise ValueError("route motion qualification evidence producer digest differs")
    if evidence.get("risk_window_digest") != risk_window_digest:
        raise ValueError("route motion qualification evidence risk window digest differs")
    identity_field = (
        "motion_set_id" if artifact_kind == "motion_set" else "motion_candidate_set_id"
    )
    if evidence.get(identity_field) != artifact_id:
        raise ValueError("route motion qualification evidence artifact identity differs")
    entries = evidence.get("records")
    if not isinstance(entries, list):
        raise ValueError("route motion qualification evidence records must be an array")
    matching = [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("artifact_kind") == artifact_kind
    ]
    if len(matching) != len(records):
        raise ValueError("route motion qualification evidence record cardinality differs")
    expected_records = {
        (item.record if hasattr(item, "record") else item).plan_id: item
        for item in records
    }
    if len(matching) != len({entry.get("plan_id") for entry in matching}):
        raise ValueError("route motion qualification evidence plan ids are not unique")
    for entry in matching:
        plan_id = entry.get("plan_id")
        item = expected_records.get(plan_id)
        if item is None:
            raise ValueError("route motion qualification evidence plan id differs")
        record = item.record if hasattr(item, "record") else item
        expected_objective = (
            item.objective_mode.value if hasattr(item, "objective_mode") else None
        )
        if (
            entry.get("artifact_id") != artifact_id
            or entry.get("objective_mode") != expected_objective
            or entry.get("planning_layer") != record.planning_layer.value
            or entry.get("mode") != record.mode.value
            or entry.get("fallback_reason") != record.fallback_reason
            or entry.get("raw_route_digest") != record.raw_route_digest
            or entry.get("details_digest") != record.qualification.details_digest
        ):
            raise ValueError("route motion qualification evidence record differs")
        details = entry.get("details")
        if not isinstance(details, Mapping):
            raise ValueError("route motion qualification evidence details must be an object")
        if canonical_route_motion_sha256(details) != record.qualification.details_digest:
            raise ValueError("route motion qualification evidence details digest differs")
        diagnostics = entry.get("diagnostics")
        if not isinstance(diagnostics, Mapping):
            raise ValueError("route motion qualification evidence diagnostics are missing")
        if (
            diagnostics.get("schema_version")
            != "c.route-motion-qualification-evidence.v1"
            or diagnostics.get("details_digest") != record.qualification.details_digest
            or diagnostics.get("qualification_result") != record.qualification.result
        ):
            raise ValueError("route motion qualification evidence diagnostics differ")


def _shift_iso(value: object, offset_seconds: float) -> str | None:
    if not isinstance(value, str):
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return (moment + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


__all__ = [
    "load_bound_route_motion_candidate_set",
    "load_bound_route_motion_set",
    "validate_route_motion_context",
]
