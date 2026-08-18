"""Deterministic replay digests (business semantics only, no wall clock)."""

from __future__ import annotations

import hashlib
import json
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
        "commit_id",
        "content_digest",
        "revision",
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
