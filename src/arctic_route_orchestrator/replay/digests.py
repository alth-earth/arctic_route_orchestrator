"""Deterministic replay digests (business semantics only, no wall clock)."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from arctic_route_data.causal_replay import (
    REQUIRED_FORMAL_DATA_TYPES,
    STATIC_TYPES,
    SourceRecord,
)

_QUALITY_RANK = {"good": 3, "suspect": 2, "degraded": 1, "missing": 0}

_WALL_CLOCK_FIELDS = frozenset(
    {
        "generated_at",
        "created_at",
        "published_at",
        "heartbeat_at",
        "elapsed_seconds",
        "compute_ms",
        "duration_seconds",
        # Provenance-derived identities that embed wall-clock (e.g. B frame
        # generated_at) must not leak into the semantic digest.
        "resource_identity",
        "resource_digest",
        "replay_id",
        "commit_id",
        "content_digest",
        "revision",
        # Route plan IDs are content-addressed identities that may embed the
        # wall-clock generated_at; business route content stays in the digest.
        "route_id",
    }
)


def _revision_rank(record: SourceRecord) -> tuple[Any, ...]:
    return (
        _QUALITY_RANK.get(record.quality_flag, 3),
        record.issue_time,
        record.ingest_time or datetime.min.replace(tzinfo=UTC),
        record.version or "",
        record.data_id,
    )


def visible_record_set_digest(
    records: Iterable[SourceRecord],
) -> str:
    payload = "\n".join(sorted(record.data_id for record in records))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bbox_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> bool:
    west, south, east, north = outer
    target_west, target_south, target_east, target_north = inner
    tolerance = 1e-9
    return (
        west <= target_west + tolerance
        and south <= target_south + tolerance
        and east >= target_east - tolerance
        and north >= target_north - tolerance
    )


def _best_per_valid_time(
    records: Iterable[SourceRecord],
) -> dict[datetime, SourceRecord]:
    best: dict[datetime, SourceRecord] = {}
    for record in records:
        current = best.get(record.valid_time)
        if current is None or _revision_rank(record) > _revision_rank(current):
            best[record.valid_time] = record
    return best


def b_relevant_input_digest(
    visible: Iterable[SourceRecord],
    *,
    window_start: datetime,
    window_end: datetime,
    target_bbox: tuple[float, float, float, float],
    required_types: frozenset[str] = REQUIRED_FORMAL_DATA_TYPES,
) -> str:
    """Identity of the exact record set B would consume for one window.

    Selection mirrors A's revision ranking (quality, issue, ingest, version,
    data_id).  Per required type we keep the best revision per valid time
    inside the requested window plus the immediate lower/upper bracketing
    neighbours, and always the applicable static version.  Records outside
    the corridor bbox or outside the requested range do not change B input.
    """

    selected: list[tuple[Any, ...]] = []
    for data_type in sorted(required_types):
        type_records = [
            record
            for record in visible
            if record.data_type == data_type
            and _bbox_contains(record.bbox, target_bbox)
        ]
        if data_type in STATIC_TYPES:
            if type_records:
                best = max(type_records, key=_revision_rank)
                selected.append(
                    (
                        data_type,
                        "static",
                        best.data_id,
                        best.issue_time.isoformat(),
                        best.quality_flag,
                        best.version or "",
                        best.checksum or "",
                    )
                )
            continue
        in_window = _best_per_valid_time(
            record
            for record in type_records
            if window_start <= record.valid_time <= window_end
        )
        lower = [
            record
            for record in type_records
            if record.valid_time < window_start
        ]
        upper = [
            record
            for record in type_records
            if record.valid_time > window_end
        ]
        bracketing: dict[datetime, SourceRecord] = dict(in_window)
        if lower:
            nearest_lower_valid = max(record.valid_time for record in lower)
            nearest_lower = max(
                (
                    record
                    for record in lower
                    if record.valid_time == nearest_lower_valid
                ),
                key=_revision_rank,
            )
            bracketing[nearest_lower.valid_time] = nearest_lower
        if upper:
            nearest_upper_valid = min(record.valid_time for record in upper)
            nearest_upper = max(
                (
                    record
                    for record in upper
                    if record.valid_time == nearest_upper_valid
                ),
                key=_revision_rank,
            )
            bracketing[nearest_upper.valid_time] = nearest_upper
        for record in sorted(bracketing.values(), key=lambda item: item.valid_time):
            selected.append(
                (
                    data_type,
                    record.valid_time.isoformat(),
                    record.data_id,
                    record.issue_time.isoformat(),
                    record.quality_flag,
                    record.version or "",
                    record.checksum or "",
                )
            )
    payload = "\n".join(json.dumps(item, sort_keys=True) for item in selected)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def replay_semantic_digest(document: dict[str, Any]) -> str:
    """Deterministic digest over business fields; wall-clock excluded."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: clean(item)
                for key, item in value.items()
                if key not in _WALL_CLOCK_FIELDS
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    encoded = json.dumps(
        clean(document),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def risk_semantic_digest(frames: Iterable[Any]) -> str:
    """Business-only digest of committed risk frames.

    Includes every business field the planner can legally consume
    (valid_time, as_of_time, model_version, provenance, source summaries,
    coordinates and risk/confidence/hard payloads) and excludes wall-clock
    provenance such as generated_at and risk_id (content-addressed identities
    that embed generated_at).
    """

    payloads: list[Any] = []
    for frame in frames:
        document = _frame_business_document(frame)
        payloads.append(document)
    encoded = json.dumps(
        _json_safe(payloads),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_safe(value: Any) -> Any:
    """Deterministically encode NaN/Inf floats (JSON cannot hold them)."""

    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _frame_business_document(frame: Any) -> dict[str, Any]:
    payload = frame.payload
    variables = {}
    for name in (
        "risk_score",
        "risk_level",
        "confidence",
        "hard_mask",
        "hard_reason",
        "environment_speed_factor",
    ):
        try:
            values = payload[name].values
        except (KeyError, AttributeError):
            continue
        variables[name] = values.tolist()
    sources = []
    for source in getattr(frame, "source_summary", ()) or ():
        sources.append(
            {
                "data_id": getattr(source, "data_id", ""),
                "data_type": getattr(source, "data_type", ""),
                "issue_time": _iso_datetime(getattr(source, "issue_time", None)),
                "valid_time": _iso_datetime(getattr(source, "valid_time", None)),
                "quality": getattr(source, "quality_flag", ""),
                "version": getattr(source, "version", ""),
                "checksum": getattr(source, "checksum", ""),
            }
        )
    return {
        "valid_time": _iso_datetime(frame.valid_time),
        "as_of_time": _iso_datetime(frame.as_of_time),
        "model_version": getattr(frame, "model_version", ""),
        "provenance": getattr(frame, "provenance", "").value
        if hasattr(getattr(frame, "provenance", ""), "value")
        else getattr(frame, "provenance", ""),
        "variables": variables,
        "sources": sources,
    }


def _iso_datetime(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "astimezone"):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def route_semantic_digest(plan: Any) -> str:
    """Business-only digest of one RoutePlan / RoutePlanV3.

    Waypoints (lon/lat/eta), objective metrics, plan kind, replan reasons,
    layer identity and focus windows are business semantics; generated_at,
    plan_id, planning_request_id and other wall-clock identities are excluded.
    """

    metrics = getattr(plan, "metrics", None)
    document = {
        "objective": getattr(getattr(plan, "objective_mode", None), "value", None)
        if hasattr(getattr(plan, "objective_mode", None), "value")
        else getattr(plan, "objective_mode", None),
        "plan_kind": getattr(getattr(plan, "plan_kind", None), "value", None)
        if hasattr(getattr(plan, "plan_kind", None), "value")
        else getattr(plan, "plan_kind", None),
        "start_time": _iso_datetime(getattr(plan, "start_time", None)),
        "as_of_time": _iso_datetime(getattr(plan, "as_of_time", None)),
        "planning_layer": getattr(
            getattr(plan, "planning_layer", None), "value", None
        )
        if hasattr(getattr(plan, "planning_layer", None), "value")
        else getattr(plan, "planning_layer", None),
        "focus_start_time": _iso_datetime(
            getattr(plan, "focus_start_time", None)
        ),
        "focus_end_time": _iso_datetime(getattr(plan, "focus_end_time", None)),
        "destination_reached": getattr(plan, "destination_reached", None),
        "replan_reasons": [
            reason.value if hasattr(reason, "value") else reason
            for reason in getattr(plan, "replan_reasons", ())
        ],
        "waypoints": [
            {
                "longitude": float(waypoint.longitude),
                "latitude": float(waypoint.latitude),
                "eta": _iso_datetime(waypoint.eta),
            }
            for waypoint in getattr(plan, "waypoints", ())
        ],
        "metrics": {
            "distance_km": float(metrics.distance_km),
            "eta_hours": float(metrics.eta_hours),
            "avg_risk": float(metrics.avg_risk),
            "max_risk": float(metrics.max_risk),
            "integrated_risk_hours": float(metrics.integrated_risk_hours),
            "minimum_confidence": float(metrics.minimum_confidence),
            "hard_constraint_violations": int(
                metrics.hard_constraint_violations
            ),
            "turn_count": int(metrics.turn_count),
            "expanded_nodes": int(metrics.expanded_nodes),
            "objective_cost": float(metrics.objective_cost),
        }
        if metrics is not None
        else None,
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
