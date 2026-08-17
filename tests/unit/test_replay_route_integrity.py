"""Causal route integrity audit tests (synthetic RiskFrames)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import xarray as xr
from arctic_route_planning.contracts import (
    ProvenanceKind,
    RiskFrame,
    SourceReference,
)

from arctic_route_orchestrator.replay.route_integrity import audit_route

RUN_ID = "run-00000000-0000-4000-8000-000000000001"
DIGEST = "0" * 64
T0 = datetime(2026, 8, 15, 10, tzinfo=UTC)


def _frame(
    valid: datetime,
    hard: list[list[bool]],
    reasons: list[list[str]],
) -> RiskFrame:
    hard_array = np.asarray(hard, dtype=bool)
    payload = xr.Dataset(
        data_vars={
            "risk_score": (
                ("latitude", "longitude"),
                np.full(hard_array.shape, 0.1, dtype=float),
            ),
            "risk_level": (
                ("latitude", "longitude"),
                np.full(hard_array.shape, 1, dtype=np.uint8),
            ),
            "hard_mask": (("latitude", "longitude"), hard_array),
            "confidence": (
                ("latitude", "longitude"),
                np.full(hard_array.shape, 1.0, dtype=float),
            ),
            "environment_speed_factor": (
                ("latitude", "longitude"),
                np.full(hard_array.shape, 0.9, dtype=float),
            ),
            "hard_reason": (
                ("latitude", "longitude"),
                np.asarray(reasons, dtype="U32"),
            ),
        },
        coords={
            "latitude": np.array([50.0, 51.0, 52.0], dtype=float),
            "longitude": np.array([10.0, 11.0, 12.0], dtype=float),
        },
        attrs={
            "crs": "EPSG:4326",
            "grid_id": "synthetic-grid",
            "calibration_status": "demo_unvalidated",
        },
    )
    return RiskFrame(
        schema_version="bc.risk-frame.v2",
        risk_id=f"risk-{valid.isoformat()}",
        run_id=RUN_ID,
        scenario_id="synthetic",
        corridor_id="synthetic",
        vessel_profile_id="vessel",
        config_digest=DIGEST,
        model_config_digest=DIGEST,
        generation_id=0,
        valid_time=valid,
        as_of_time=T0,
        generated_at=T0,
        model_version="test",
        payload=payload,
        source_summary=(
            SourceReference(
                source_id="synthetic",
                data_id="synthetic-frame",
                issue_time=T0,
                valid_time=valid,
                version="1",
                quality_flag="good",
            ),
        ),
        provenance=ProvenanceKind.SYNTHETIC,
    )


@dataclass(frozen=True)
class _Waypoint:
    longitude: float
    latitude: float
    eta: datetime


@dataclass(frozen=True)
class _Route:
    waypoints: tuple[_Waypoint, ...]
    plan_id: str = "route-synthetic"


def _frames(hard, reasons) -> tuple[RiskFrame, RiskFrame]:
    return (
        _frame(T0, hard, reasons),
        _frame(T0 + timedelta(hours=1), hard, reasons),
    )


def _clean() -> tuple[list[list[bool]], list[list[str]]]:
    hard = [[False] * 3 for _ in range(3)]
    reasons = [["NONE"] * 3 for _ in range(3)]
    return hard, reasons


def test_pass_route_has_no_violations() -> None:
    hard, reasons = _clean()
    route = _Route(
        (
            _Waypoint(10.0, 50.0, T0),
            _Waypoint(11.0, 51.0, T0 + timedelta(hours=1)),
        )
    )
    result = audit_route(route, _frames(hard, reasons))
    assert result["status"] == "PASS"
    assert result["waypoint_hard_violations"] == 0
    assert result["edge_hard_violations"] == 0
    assert result["corner_cutting_violations"] == 0


def test_waypoint_on_land_detected() -> None:
    hard, reasons = _clean()
    hard[1][1] = True
    reasons[1][1] = "LAND"
    route = _Route(
        (
            _Waypoint(10.0, 50.0, T0),
            _Waypoint(11.0, 51.0, T0 + timedelta(hours=1)),
        )
    )
    result = audit_route(route, _frames(hard, reasons))
    assert result["status"] == "FAIL"
    assert result["waypoint_hard_violations"] >= 1
    assert result["land_intersections"] >= 1


def test_diagonal_corner_cut_detected() -> None:
    hard, reasons = _clean()
    hard[0][1] = True
    reasons[0][1] = "DATA_UNAVAILABLE"
    route = _Route(
        (
            _Waypoint(10.0, 50.0, T0),
            _Waypoint(11.0, 51.0, T0 + timedelta(hours=1)),
        )
    )
    result = audit_route(route, _frames(hard, reasons))
    assert result["corner_cutting_violations"] >= 1
    assert result["data_unavailable_violations"] >= 1
