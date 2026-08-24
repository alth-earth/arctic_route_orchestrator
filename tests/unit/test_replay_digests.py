"""Replay digest and visibility semantics (Strategy B, synthetic)."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arctic_route_data.causal_replay import SourceRecord
from arctic_route_planning.adapters.fixture import FixtureRiskSource
from arctic_route_planning.config import load_configuration
from arctic_route_planning.development import create_development_run_context

from arctic_route_orchestrator.replay.digests import (
    b_relevant_input_digest,
    replay_semantic_digest,
    risk_semantic_digest,
    route_semantic_digest,
    visible_record_set_digest,
)


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


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
    different_run = {
        "simulation_time": "2026-08-15T10:00:00Z",
        "plan": "abc",
        "replay_id": "run-b",
    }
    assert replay_semantic_digest(base) == replay_semantic_digest(different_run)
    changed = {"simulation_time": "2026-08-15T11:00:00Z", "plan": "abc"}
    assert replay_semantic_digest(base) != replay_semantic_digest(changed)


def test_semantic_digest_distinguishes_business_window_revision_not_identity() -> None:
    base = {
        "simulation_time": "2026-08-15T11:00:00Z",
        "risk_window_revision": 2,
        "events": [
            {
                "type": "RISK_WINDOW_ADVANCED",
                "simulation_time": "2026-08-15T11:00:00Z",
                "revision": "commit-a",
                "description": "suffix window advanced",
            }
        ],
    }
    same_business = {
        "simulation_time": "2026-08-15T11:00:00Z",
        "risk_window_revision": 2,
        "events": [
            {
                "type": "RISK_WINDOW_ADVANCED",
                "simulation_time": "2026-08-15T11:00:00Z",
                "revision": "commit-b",
                "description": "suffix window advanced",
            }
        ],
    }
    changed_business = {
        "simulation_time": "2026-08-15T11:00:00Z",
        "risk_window_revision": 3,
        "events": [
            {
                "type": "RISK_WINDOW_ADVANCED",
                "simulation_time": "2026-08-15T11:00:00Z",
                "revision": "commit-b",
                "description": "suffix window advanced",
            }
        ],
    }
    assert replay_semantic_digest(base) == replay_semantic_digest(same_business)
    assert replay_semantic_digest(base) != replay_semantic_digest(changed_business)


def _synthetic_frames():
    config = load_configuration(
        str(_workspace_root() / "work_package_c" / "configs"),
        "tromso_isfjorden_july_2026_retrospective_v1",
    )
    fixture = FixtureRiskSource(
        scenario=config.scenario,
        corridor=config.corridor,
        vessel=config.vessel,
        run_context=create_development_run_context(config, source_kind="synthetic"),
        frame_count=2,
        shape=(5, 7),
    )
    return tuple(fixture.frames)


def test_risk_semantic_digest_changes_with_business_mutation() -> None:
    frames = _synthetic_frames()
    original = risk_semantic_digest(frames)
    mutated = replace(
        frames[0],
        risk_id=f"{frames[0].risk_id}-revision",
        generated_at=frames[0].generated_at + timedelta(hours=1),
    )
    payload = dict(mutated.payload.data_vars)
    payload["confidence"] = (
        ("latitude", "longitude"),
        (
            mutated.payload["confidence"].values - 0.05
        ).astype("float32"),
    )
    from xarray import Dataset

    mutated = replace(
        mutated,
        payload=Dataset(
            payload,
            coords=mutated.payload.coords,
            attrs=mutated.payload.attrs,
        ),
    )
    # wall-clock generated_at alone must not change the digest
    wall_clock_only = replace(frames[0], generated_at=frames[0].generated_at + timedelta(hours=2))
    assert risk_semantic_digest((wall_clock_only, *frames[1:])) == original
    assert risk_semantic_digest((mutated, *frames[1:])) != original


class _MetricNamespace:
    def __init__(self, **values) -> None:
        self.__dict__.update(values)


class _WaypointNamespace:
    def __init__(self, longitude, latitude, eta) -> None:
        self.longitude = longitude
        self.latitude = latitude
        self.eta = eta


class _RouteNamespace:
    def __init__(self, waypoints, metrics, **values) -> None:
        self.waypoints = waypoints
        self.metrics = metrics
        self.__dict__.update(values)


def _sample_route(*, waypoint_latitude=70.0):
    eta0 = datetime(2026, 8, 15, 10, tzinfo=UTC)
    eta1 = datetime(2026, 8, 15, 12, tzinfo=UTC)
    metrics = _MetricNamespace(
        distance_km=120.0,
        eta_hours=2.0,
        avg_risk=0.3,
        max_risk=0.5,
        integrated_risk_hours=0.6,
        minimum_confidence=0.8,
        hard_constraint_violations=0,
        turn_count=2,
        expanded_nodes=100,
        objective_cost=3.2,
    )
    return _RouteNamespace(
        (
            _WaypointNamespace(12.0, 69.0, eta0),
            _WaypointNamespace(14.0, waypoint_latitude, eta1),
        ),
        metrics,
        objective_mode="recommended",
        plan_kind="initial",
        start_time=eta0,
        as_of_time=eta0,
        planning_layer="full_voyage",
        destination_reached=True,
        replan_reasons=(),
    )


def test_route_semantic_digest_mutation_sensitivity() -> None:
    base = _sample_route()
    wall_clock_identity = _RouteNamespace(
        base.waypoints,
        base.metrics,
        objective_mode="recommended",
        plan_kind="initial",
        start_time=base.start_time,
        as_of_time=base.as_of_time,
        planning_layer="full_voyage",
        destination_reached=True,
        replan_reasons=(),
        generated_at="wall-clock-A",
        plan_id="route-id-A",
    )
    assert route_semantic_digest(base) == route_semantic_digest(wall_clock_identity)
    mutated = _sample_route(waypoint_latitude=70.5)
    assert route_semantic_digest(base) != route_semantic_digest(mutated)
