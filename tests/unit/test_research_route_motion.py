"""Tests for explicit, non-production sidecar motion interpolation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arctic_route_orchestrator.replay.research_route_motion import (
    research_motion_at,
    validate_research_route_sidecar,
)


def _sidecar() -> dict:
    return {
        "schema_version": "c.research-route-smoothing-sidecar.v1",
        "status": "ACCEPTED",
        "applied": True,
        "research_only": True,
        "raw_route_digest": "a" * 64,
        "authoritative_route": {"route_digest": "a" * 64},
        "motion_samples": [
            {"lon": 0.0, "lat": 0.0, "eta": "2026-01-01T00:00:00Z"},
            {"lon": 1.0, "lat": 0.0, "eta": "2026-01-01T01:00:00Z"},
            {"lon": 1.0, "lat": 1.0, "eta": "2026-01-01T02:00:00Z"},
        ],
    }


def test_valid_sidecar_interpolates_and_keeps_explicit_statuses() -> None:
    sidecar = _sidecar()
    assert validate_research_route_sidecar(sidecar).valid is True

    before = research_motion_at(datetime(2025, 12, 31, 23, tzinfo=UTC), sidecar)
    middle = research_motion_at(datetime(2026, 1, 1, 0, 30, tzinfo=UTC), sidecar)
    arrived = research_motion_at(datetime(2026, 1, 1, 2, tzinfo=UTC), sidecar)

    assert before.status == "NOT_STARTED"
    assert middle.status == "UNDERWAY"
    assert middle.position == {"longitude": 0.5, "latitude": 0.0}
    assert middle.course_degrees == pytest.approx(90.0)
    assert arrived.status == "ARRIVED"


def test_digest_mismatch_and_fallback_sidecar_never_produce_motion() -> None:
    sidecar = _sidecar()
    assert validate_research_route_sidecar(sidecar, expected_route_digest="b" * 64).reason == (
        "authoritative_route_digest_mismatch"
    )
    result = research_motion_at(
        datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
        {**sidecar, "status": "FALLBACK", "applied": False, "fallback_reason": "unknown"},
    )
    assert result.valid is False
    assert result.status == "FALLBACK"
    assert result.position is None
    assert result.fallback_reason == "unknown"


def test_non_monotonic_eta_is_rejected() -> None:
    sidecar = _sidecar()
    sidecar["motion_samples"][1]["eta"] = "2026-01-01T00:00:00Z"
    validation = validate_research_route_sidecar(sidecar)
    assert validation.valid is False
    assert validation.reason == "non_monotonic_motion_sample_eta"
