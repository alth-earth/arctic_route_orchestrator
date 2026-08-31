"""Tests for explicit, non-production sidecar motion interpolation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arctic_route_orchestrator.replay.research_route_motion import (
    _canonical_digest,
    normalize_research_route_sidecar,
    research_motion_at,
    validate_research_route_sidecar,
)


def _sidecar() -> dict:
    sidecar = {
        "schema_version": "c.research-route-smoothing-sidecar.v1",
        "status": "ACCEPTED",
        "applied": True,
        "research_only": True,
        "research_eligible": True,
        "validation": {
            "research_gate_passed": True,
            "risk_rechecked": True,
            "hard_mask_rechecked": True,
            "coverage_complete": True,
            "eta_recomputed": True,
            "speed_checked": True,
        },
        "raw_route_digest": "a" * 64,
        "authoritative_route": {"route_digest": "a" * 64},
        "motion_samples": [
            {"lon": 0.0, "lat": 0.0, "eta": "2026-01-01T00:00:00Z"},
            {"lon": 1.0, "lat": 0.0, "eta": "2026-01-01T01:00:00Z"},
            {"lon": 1.0, "lat": 1.0, "eta": "2026-01-01T02:00:00Z"},
        ],
    }
    sidecar["sidecar_digest"] = _canonical_digest(sidecar)
    return sidecar


def _v2_sidecar() -> dict:
    route_digest = "b" * 64
    sidecar = {
        "schema_version": "c.research-route-smoothing-sidecar.v2",
        "status": "ACCEPTED",
        "applied": True,
        "research_only": True,
        "research_eligible": True,
        "production_qualified": False,
        "calibration_status": "SYNTHETIC_UNCALIBRATED",
        "manoeuvring_qualification": "SYNTHETIC_ONLY",
        "route_id": "route-v2",
        "raw_route_digest": route_digest,
        "curve_digest": "c" * 64,
        "route_identity": {
            "route_id": "route-v2",
            "route_digest": route_digest,
        },
        "authoritative_route": {
            "route_id": "route-v2",
            "route_digest": route_digest,
        },
        "validation": {
            "research_gate_passed": True,
            "risk_rechecked": True,
            "hard_mask_rechecked": True,
            "coverage_complete": True,
            "eta_recomputed": True,
            "speed_checked": True,
            "curvature_checked": True,
            "corridor_checked": True,
            "kinematics_checked": True,
        },
        "motion_samples": [
            {
                "lon": 0.0,
                "lat": 0.0,
                "eta": "2026-01-01T00:00:00Z",
                "course_degrees": 90.0,
                "speed_knots": 10.0,
            },
            {
                "lon": 1.0,
                "lat": 0.0,
                "eta": "2026-01-01T01:00:00Z",
                "course_degrees": 90.0,
                "speed_knots": 10.0,
            },
            {
                "lon": 1.0,
                "lat": 1.0,
                "eta": "2026-01-01T02:00:00Z",
                "course_degrees": 0.0,
                "speed_knots": 0.0,
            },
        ],
    }
    sidecar["same_geometry_motion_digest"] = _canonical_digest(
        {
            "curve_digest": sidecar["curve_digest"],
            "motion_samples": sidecar["motion_samples"],
        }
    )
    sidecar["same_geometry_motion_evidence"] = {
        "same_geometry_motion_digest": sidecar["same_geometry_motion_digest"],
        "sample_count": len(sidecar["motion_samples"]),
    }
    sidecar["sidecar_digest"] = _canonical_digest(sidecar)
    return sidecar


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


def test_valid_v2_sidecar_dispatches_and_normalizes_motion_view() -> None:
    sidecar = _v2_sidecar()

    validation = validate_research_route_sidecar(sidecar)
    normalized = normalize_research_route_sidecar(sidecar)

    assert validation.valid is True
    assert validation.sample_count == 3
    assert normalized is not None
    assert normalized["schema_version"] == "c.research-route-smoothing-sidecar.v2"
    assert normalized["motion_samples"][0]["course_degrees"] == 90.0
    assert normalized["motion_samples"][0]["speed_knots"] == 10.0
    assert normalized["same_geometry_motion_digest"] == sidecar[
        "same_geometry_motion_digest"
    ]
    state = research_motion_at(datetime(2026, 1, 1, 0, 30, tzinfo=UTC), sidecar)
    assert state.valid is True
    assert state.status == "UNDERWAY"
    assert state.position == {"longitude": 0.5, "latitude": 0.0}
    assert state.course_degrees == 90.0
    assert state.speed_knots == 10.0


def test_unknown_v2_schema_is_rejected_by_strict_dispatcher() -> None:
    sidecar = _v2_sidecar()
    sidecar["schema_version"] = "c.research-route-smoothing-sidecar.v3"

    validation = validate_research_route_sidecar(sidecar)

    assert validation.valid is False
    assert validation.reason == "unsupported_sidecar_schema"


def test_v2_route_identity_and_expected_digest_mismatch_falls_back() -> None:
    sidecar = _v2_sidecar()

    assert validate_research_route_sidecar(
        sidecar,
        expected_route_digest="c" * 64,
    ).reason == "authoritative_route_digest_mismatch"

    sidecar["route_identity"]["route_digest"] = "c" * 64
    sidecar["sidecar_digest"] = _canonical_digest(sidecar)
    validation = validate_research_route_sidecar(sidecar)
    assert validation.valid is False
    assert validation.reason == "route_identity_digest_mismatch"


def test_v2_non_finite_kinematics_is_rejected() -> None:
    sidecar = _v2_sidecar()
    sidecar["motion_samples"][1]["speed_knots"] = float("nan")
    sidecar["sidecar_digest"] = _canonical_digest(sidecar)

    validation = validate_research_route_sidecar(sidecar)

    assert validation.valid is False
    assert validation.reason == "invalid_motion_kinematics"
