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


def validate_initial_candidates_and_adopted_motion(
    *,
    plan_sets_by_revision: Mapping[int, Mapping[str, Any]],
    replay_routes: Sequence[Mapping[str, Any]],
    route_motion_sets: Sequence[Mapping[str, Any]],
    route_motion_candidate_sets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate the Viewer ``initial-candidates-and-adopted`` motion strategy.

    C emits two sibling transports with deliberately different scopes:

    * the initial revision exposes all three full-voyage objectives so the
      operator can choose a route before playback; and
    * every replay revision that can become authoritative must have its
      four-layer recommended ``RouteMotionSet``.

    Every replay revision carries a candidate set.  Candidate sets for later
    revisions are useful for the research comparison panel even though the
    runtime chooser starts from the initial revision; their recommended record
    must agree with the corresponding formal full-voyage record.  This prevents
    a Viewer from showing one motion mode for a candidate card and silently
    using a different motion source after adoption.

    This function intentionally checks the producer's declared motion policy,
    not a new safety policy: ``CURVE`` must be fully qualified by the C
    contract, while ``RAW_PASSTHROUGH`` must retain an explicit producer
    fallback reason.  RAW therefore remains truthful and usable as the
    approved pre-production fallback; it is never upgraded to CURVE here.
    """

    if not plan_sets_by_revision:
        raise ValueError("motion strategy has no v3 plan revisions")

    revision_to_layer_set: dict[int, str] = {}
    layer_set_to_revision: dict[str, int] = {}
    for revision, plan_set in plan_sets_by_revision.items():
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("motion strategy plan revision is invalid")
        if not isinstance(plan_set, Mapping):
            raise ValueError("motion strategy plan set is malformed")
        if plan_set.get("schema_version") != "cd.four-layer-route-plan-set.v3":
            raise ValueError("motion strategy plan set is not C v3")
        layer_set_id = plan_set.get("layer_set_id")
        if not isinstance(layer_set_id, str) or not layer_set_id:
            raise ValueError("motion strategy plan layer_set_id is missing")
        if revision in revision_to_layer_set or layer_set_id in layer_set_to_revision:
            raise ValueError("motion strategy plan revisions or layer bindings are duplicated")
        revision_to_layer_set[revision] = layer_set_id
        layer_set_to_revision[layer_set_id] = revision

    route_by_revision: dict[int, Mapping[str, Any]] = {}
    for route in replay_routes:
        if not isinstance(route, Mapping):
            raise ValueError("motion strategy replay route is malformed")
        revision = route.get("revision")
        route_id = route.get("route_id")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(route_id, str)
            or not route_id
        ):
            raise ValueError("motion strategy replay route identity is invalid")
        if revision in route_by_revision:
            raise ValueError("motion strategy replay route revisions are duplicated")
        if revision not in revision_to_layer_set:
            raise ValueError(f"motion strategy replay route revision is not published: {revision}")
        route_by_revision[revision] = route
    if not route_by_revision:
        raise ValueError("motion strategy has no replay route revisions")

    def _index_sets(
        values: Sequence[Mapping[str, Any]],
        *,
        identity_field: str,
        label: str,
    ) -> dict[str, Mapping[str, Any]]:
        indexed: dict[str, Mapping[str, Any]] = {}
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError(f"{label} is malformed")
            layer_set_id = value.get("layer_set_id")
            artifact_id = value.get(identity_field)
            if (
                not isinstance(layer_set_id, str)
                or layer_set_id not in layer_set_to_revision
                or not isinstance(artifact_id, str)
                or not artifact_id
            ):
                raise ValueError(f"{label} is not bound to a published plan revision")
            if layer_set_id in indexed or artifact_id in {
                item.get(identity_field) for item in indexed.values()
            }:
                raise ValueError(f"{label} identities or layer bindings are duplicated")
            indexed[layer_set_id] = value
        return indexed

    motion_by_layer_set = _index_sets(
        route_motion_sets,
        identity_field="motion_set_id",
        label="formal route motion set",
    )
    candidate_by_layer_set = _index_sets(
        route_motion_candidate_sets,
        identity_field="motion_candidate_set_id",
        label="route motion candidate set",
    )

    expected_revisions = sorted(route_by_revision)
    unexpected_motion_revisions = sorted(
        layer_set_to_revision[layer_set_id]
        for layer_set_id in motion_by_layer_set
        if layer_set_to_revision[layer_set_id] not in expected_revisions
    )
    if unexpected_motion_revisions:
        raise ValueError(
            "formal route motion set has an unconsumed replay revision: "
            + ", ".join(str(item) for item in unexpected_motion_revisions)
        )
    unexpected_candidate_revisions = sorted(
        layer_set_to_revision[layer_set_id]
        for layer_set_id in candidate_by_layer_set
        if layer_set_to_revision[layer_set_id] not in expected_revisions
    )
    if unexpected_candidate_revisions:
        raise ValueError(
            "route motion candidate set has an unconsumed replay revision: "
            + ", ".join(str(item) for item in unexpected_candidate_revisions)
        )
    missing_candidate_revisions = sorted(
        set(expected_revisions)
        - {layer_set_to_revision[layer_set_id] for layer_set_id in candidate_by_layer_set}
    )
    if missing_candidate_revisions:
        missing_label = (
            "initial route motion candidate set"
            if min(expected_revisions) in missing_candidate_revisions
            else "route motion candidate set"
        )
        raise ValueError(
            f"{missing_label} is missing for replay revision: "
            + ", ".join(str(item) for item in missing_candidate_revisions)
        )

    def _records(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        records = value.get("records")
        if not isinstance(records, list):
            raise ValueError("motion artifact records are missing")
        if any(not isinstance(record, Mapping) for record in records):
            raise ValueError("motion artifact records are malformed")
        return list(records)

    def _assert_declared_motion_policy(record: Mapping[str, Any], *, label: str) -> str:
        mode = record.get("mode")
        fallback_reason = record.get("fallback_reason")
        qualification = record.get("qualification")
        if not isinstance(qualification, Mapping):
            raise ValueError(f"{label} qualification is missing")
        result = qualification.get("result")
        if mode == "CURVE":
            if fallback_reason is not None or result != "QUALIFIED_ENGINEERING_REFERENCE":
                raise ValueError(f"{label} CURVE qualification is incomplete")
        elif mode == "RAW_PASSTHROUGH":
            if not isinstance(fallback_reason, str) or not fallback_reason.strip():
                raise ValueError(f"{label} RAW_PASSTHROUGH fallback reason is missing")
            if result != "RAW_FALLBACK":
                raise ValueError(f"{label} RAW_PASSTHROUGH qualification is inconsistent")
        else:
            raise ValueError(f"{label} has unsupported motion mode")
        return str(mode)

    def _record_for_layer(
        value: Mapping[str, Any],
        *,
        planning_layer: str,
        label: str,
    ) -> Mapping[str, Any]:
        matches = [
            record
            for record in _records(value)
            if record.get("planning_layer") == planning_layer
        ]
        if len(matches) != 1:
            raise ValueError(f"{label} must contain exactly one {planning_layer} record")
        return matches[0]

    def _candidate_record(
        value: Mapping[str, Any], *, objective: str, label: str
    ) -> Mapping[str, Any]:
        matches = [
            item.get("record")
            for item in _records(value)
            if item.get("objective_mode") == objective
            and isinstance(item.get("record"), Mapping)
        ]
        if len(matches) != 1:
            raise ValueError(f"{label} must contain exactly one {objective} record")
        return matches[0]

    # The initial candidate transport is the only source used by the runtime
    # route chooser before playback.  Make its presence and revision explicit;
    # a candidate set from an adopted revision must never become the initial
    # chooser merely because it happened to be listed first.
    initial_revision = min(expected_revisions)
    initial_layer_set_id = revision_to_layer_set[initial_revision]
    initial_candidates = candidate_by_layer_set.get(initial_layer_set_id)
    if initial_candidates is None:
        raise ValueError(
            "initial route motion candidate set is missing for replay revision "
            f"{initial_revision}"
        )
    initial_motion = motion_by_layer_set.get(initial_layer_set_id)
    if initial_motion is None:
        raise ValueError(
            "initial formal route motion set is missing for replay revision "
            f"{initial_revision}"
        )

    candidate_modes: dict[str, str] = {}
    for objective in ("fastest", "low_risk", "recommended"):
        record = _candidate_record(
            initial_candidates,
            objective=objective,
            label="initial route motion candidate set",
        )
        candidate_modes[objective] = _assert_declared_motion_policy(
            record,
            label=f"initial candidate {objective}",
        )

    def _assert_recommended_pair(
        motion_set: Mapping[str, Any],
        candidate_set: Mapping[str, Any],
        *,
        revision: int,
    ) -> None:
        formal_record = _record_for_layer(
            motion_set,
            planning_layer="full_voyage",
            label=f"formal route motion set R{revision}",
        )
        candidate_record = _candidate_record(
            candidate_set,
            objective="recommended",
            label=f"route motion candidate set R{revision}",
        )
        _assert_declared_motion_policy(
            formal_record,
            label=f"adopted formal motion R{revision}",
        )
        _assert_declared_motion_policy(
            candidate_record,
            label=f"candidate recommended motion R{revision}",
        )
        for field in (
            "plan_id",
            "raw_route_digest",
            "mode",
            "fallback_reason",
            "curve_digest",
            "motion_digest",
        ):
            if formal_record.get(field) != candidate_record.get(field):
                raise ValueError(
                    f"R{revision} formal and candidate recommended motion differ: {field}"
                )

    _assert_recommended_pair(initial_motion, initial_candidates, revision=initial_revision)

    adopted_motion: list[dict[str, Any]] = []
    for revision in expected_revisions:
        layer_set_id = revision_to_layer_set[revision]
        motion_set = motion_by_layer_set.get(layer_set_id)
        if motion_set is None:
            raise ValueError(
                "formal route motion set is missing for replay revision "
                f"{revision}"
            )
        route = route_by_revision[revision]
        route_id = route["route_id"]
        records = _records(motion_set)
        matching = [record for record in records if record.get("plan_id") == route_id]
        if len(matching) != 1:
            raise ValueError(
                f"formal route motion set R{revision} does not bind replay route {route_id}"
            )
        mode = _assert_declared_motion_policy(
            matching[0],
            label=f"adopted formal motion R{revision}",
        )
        if revision != initial_revision:
            adopted_motion.append(
                {
                    "revision": revision,
                    "layer_set_id": layer_set_id,
                    "plan_id": route_id,
                    "mode": mode,
                    "fallback_reason": matching[0].get("fallback_reason"),
                }
            )

        candidate_set = candidate_by_layer_set[layer_set_id]
        _assert_recommended_pair(
            candidate_set=candidate_set,
            motion_set=motion_set,
            revision=revision,
        )

    return {
        "strategy": "initial_candidates_and_adopted_revisions",
        "initial_revision": initial_revision,
        "initial_layer_set_id": initial_layer_set_id,
        "initial_candidate_set_id": initial_candidates.get("motion_candidate_set_id"),
        "candidate_revisions": sorted(
            layer_set_to_revision[layer_set_id] for layer_set_id in candidate_by_layer_set
        ),
        "motion_revisions": expected_revisions,
        "candidate_modes": candidate_modes,
        "initial_motion_mode": _record_for_layer(
            initial_motion,
            planning_layer="full_voyage",
            label=f"formal route motion set R{initial_revision}",
        ).get("mode"),
        "adopted_motion": adopted_motion,
    }


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
    "validate_initial_candidates_and_adopted_motion",
    "validate_route_motion_context",
]
