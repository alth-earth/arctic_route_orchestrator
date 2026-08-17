"""Replay digest and visibility semantics (Strategy B, synthetic)."""

from __future__ import annotations

from datetime import UTC, datetime

from arctic_route_data.causal_replay import SourceRecord

from arctic_route_orchestrator.replay.digests import (
    b_relevant_input_digest,
    replay_semantic_digest,
    visible_record_set_digest,
)

BBOX = (10.0, 68.5, 22.0, 79.5)
WINDOW_START = datetime(2026, 8, 15, 10, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 17, 6, tzinfo=UTC)


def _record(
    data_id: str,
    data_type: str,
    issue: str,
    valid: str,
    *,
    quality: str = "good",
    version: str = "v1",
    checksum: str | None = None,
) -> SourceRecord:
    return SourceRecord(
        data_id=data_id,
        data_type=data_type,
        category="dynamic",
        issue_time=datetime.fromisoformat(issue.replace("Z", "+00:00")).astimezone(UTC),
        valid_time=datetime.fromisoformat(valid.replace("Z", "+00:00")).astimezone(UTC),
        quality_flag=quality,
        bbox=BBOX,
        evidence_authoritative=True,
        evidence_method="explicit_catalog",
        ingest_time=datetime(2026, 8, 15, 9, tzinfo=UTC),
        version=version,
        checksum=checksum or data_id,
    )


def _visible(records, tick: datetime):
    return tuple(record for record in records if record.issue_time <= tick)


def test_visible_digest_changes_with_new_record() -> None:
    base = (_record("a", "wind_field", "2026-08-15T08:00:00Z", "2026-08-15T12:00:00Z"),)
    tick = datetime(2026, 8, 15, 10, tzinfo=UTC)
    before = visible_record_set_digest(_visible(base, tick))
    after = visible_record_set_digest(
        _visible(
            (
                *base,
                _record("b", "wind_field", "2026-08-15T09:30:00Z", "2026-08-15T15:00:00Z"),
            ),
            tick,
        )
    )
    assert before != after


def test_future_issue_time_is_not_visible() -> None:
    future = _record("f", "wind_field", "2026-08-15T11:00:00Z", "2026-08-15T12:00:00Z")
    tick = datetime(2026, 8, 15, 10, tzinfo=UTC)
    visible = _visible((future,), tick)
    assert visible == ()


def test_relevant_digest_ignores_irrelevant_visible_records() -> None:
    base = (
        _record("w1", "wind_field", "2026-08-15T08:00:00Z", "2026-08-15T12:00:00Z"),
        _record("w2", "wind_field", "2026-08-15T08:00:00Z", "2026-08-16T12:00:00Z"),
        _record("w3", "wind_field", "2026-08-15T08:00:00Z", "2026-08-17T00:00:00Z"),
        _record("w_hi", "wind_field", "2026-08-15T08:00:00Z", "2026-08-18T12:00:00Z"),
    )
    tick = datetime(2026, 8, 15, 10, tzinfo=UTC)
    before = b_relevant_input_digest(
        _visible(base, tick),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        target_bbox=BBOX,
    )
    irrelevant = (
        *base,
        _record("x1", "not_required", "2026-08-15T09:00:00Z", "2026-08-15T12:00:00Z"),
        # farther upper neighbor than w_hi -> does not change B input identity
        _record("x2", "wind_field", "2026-08-15T09:00:00Z", "2026-08-20T12:00:00Z"),
    )
    after_irrelevant = b_relevant_input_digest(
        _visible(irrelevant, tick),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        target_bbox=BBOX,
    )
    assert before == after_irrelevant

    relevant = (
        *base,
        _record("w4", "wind_field", "2026-08-15T09:00:00Z", "2026-08-15T12:00:00Z"),
    )
    after_relevant = b_relevant_input_digest(
        _visible(relevant, tick),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        target_bbox=BBOX,
    )
    assert before != after_relevant


def test_relevant_digest_prefers_better_revision_but_not_worse() -> None:
    older_better = _record(
        "good",
        "wind_field",
        "2026-08-15T08:00:00Z",
        "2026-08-15T12:00:00Z",
        quality="good",
    )
    newer_worse = _record(
        "bad",
        "wind_field",
        "2026-08-15T09:00:00Z",
        "2026-08-15T12:00:00Z",
        quality="suspect",
    )
    tick = datetime(2026, 8, 15, 10, tzinfo=UTC)
    only_worse = b_relevant_input_digest(
        _visible((older_better,), tick),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        target_bbox=BBOX,
    )
    with_worse = b_relevant_input_digest(
        _visible((older_better, newer_worse), tick),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        target_bbox=BBOX,
    )
    assert only_worse == with_worse


def test_semantic_digest_excludes_wall_clock() -> None:
    base = {"simulation_time": "2026-08-15T10:00:00Z", "plan": "abc"}
    other = {"simulation_time": "2026-08-15T10:00:00Z", "plan": "abc", "generated_at": "x"}
    assert replay_semantic_digest(base) == replay_semantic_digest(other)
    changed = {"simulation_time": "2026-08-15T11:00:00Z", "plan": "abc"}
    assert replay_semantic_digest(base) != replay_semantic_digest(changed)
