import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "replay_viewer_export.py"
_SPEC = importlib.util.spec_from_file_location("replay_viewer_export", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_EXPORTER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_EXPORTER)

_risk_horizon_selections = _EXPORTER._risk_horizon_selections
_select_risk_horizon = _EXPORTER._select_risk_horizon
_viewer_presentation = _EXPORTER.VIEWER_PRESENTATION
_risk_forecast_summary = _EXPORTER._risk_forecast_summary


def _frames() -> list[dict]:
    return [
        {"valid_time": f"2026-08-15T{hour:02d}:00:00Z", "risk_id": f"risk-{hour}"}
        for hour in (10, 16, 22)
    ]


def test_current_horizon_uses_latest_frame_at_or_before_simulation_time() -> None:
    selection = _select_risk_horizon(
        _frames(),
        simulation_time=datetime(2026, 8, 15, 10, 30, tzinfo=UTC),
        horizon_hours=0,
    )

    assert selection["availability"] == "AVAILABLE"
    assert selection["actual_valid_time"] == "2026-08-15T10:00:00Z"
    assert selection["actual_horizon_seconds"] == -1800
    assert selection["selection_method"] == "latest_valid_time_at_or_before_simulation_time"


def test_future_horizon_reports_floor_actual_time() -> None:
    selection = _select_risk_horizon(
        _frames(),
        simulation_time=datetime(2026, 8, 15, 10, 30, tzinfo=UTC),
        horizon_hours=6,
    )

    assert selection["availability"] == "AVAILABLE"
    assert selection["requested_valid_time"] == "2026-08-15T16:30:00Z"
    assert selection["actual_valid_time"] == "2026-08-15T16:00:00Z"
    assert selection["actual_horizon_seconds"] == 19800
    assert selection["selection_method"] == "floor_valid_time_at_or_before_requested_valid_time"


def test_future_horizon_after_frame_range_is_unavailable_without_stale_fallback() -> None:
    selection = _select_risk_horizon(
        _frames(),
        simulation_time=datetime(2026, 8, 15, 10, 30, tzinfo=UTC),
        horizon_hours=12,
    )

    assert selection["availability"] == "UNAVAILABLE"
    assert selection["reason"] == "requested_valid_time_after_available_range"
    assert selection["actual_valid_time"] is None
    assert selection["frame_index"] is None
    assert selection["risk_id"] is None


def test_horizon_index_keeps_simulation_time_and_available_keys() -> None:
    entries = _risk_horizon_selections(
        _frames(),
        [datetime(2026, 8, 15, 10, 0, tzinfo=UTC)],
    )

    assert entries[0]["simulation_time"] == "2026-08-15T10:00:00Z"
    assert entries[0]["available_horizons"] == ["current", "+6h", "+12h"]
    assert entries[0]["selections"]["+24h"]["availability"] == "UNAVAILABLE"


def test_viewer_presentation_declares_display_only_geometry_policies() -> None:
    assert _viewer_presentation["schema_version"] == "presentation.viewer-presentation.v1"
    assert _viewer_presentation["risk_rendering"]["geometry_policy"] == (
        "exact_authoritative_cells_no_interpolation"
    )
    assert _viewer_presentation["risk_rendering"]["hard_reason_policy"] == (
        "separate_exact_cells_fail_closed"
    )
    assert _viewer_presentation["route_rendering"]["authoritative_semantics_unchanged"]
    assert _viewer_presentation["vessel_rendering"]["pixel_motion"] == "none"


def test_risk_frame_summary_is_presentation_only_and_counts_published_values() -> None:
    summary = _EXPORTER._risk_frame_summary(
        {
            "risk_levels": [1, 1, 5],
            "risk_scores": [0.1, 0.2, None],
            "hard_reasons": ["NONE", "NONE", "LAND"],
        }
    )

    assert summary["risk_level_counts"] == {"1": 2, "2": 0, "3": 0, "4": 0, "5": 1}
    assert summary["hard_reason_counts"] == {"LAND": 1, "NONE": 2}
    assert abs(summary["risk_score_mean"] - 0.15) < 1e-12
    assert summary["land_count"] == 1
    assert summary["data_unavailable_count"] == 0
    assert summary["hard_cell_count"] == 1


def test_risk_forecast_summary_reports_presentation_trend_only() -> None:
    frames = [
        {
            "valid_time": "2026-08-15T10:00:00Z",
            "summary": {"risk_score_mean": 0.2},
        },
        {
            "valid_time": "2026-08-15T11:00:00Z",
            "summary": {"risk_score_mean": 0.1},
        },
    ]

    summary = _risk_forecast_summary(frames)

    assert summary["status"] == "PASS"
    assert summary["trend"] == "decreasing"
    assert summary["trend_method"] == "first_to_last_finite_mean_score"
    assert summary["mean_score_delta"] == -0.1


def test_exporter_accepts_only_validated_optional_route_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    expected = {"schema_version": "presentation.route-candidates.v1", "status": "PUBLISHED"}
    path = tmp_path / "route-candidates.json"
    path.write_text(json.dumps(expected), encoding="utf-8")
    observed: list[Path] = []

    def fake_loader(location: Path) -> dict:
        observed.append(location)
        return expected

    monkeypatch.setattr(_EXPORTER, "load_route_candidates", fake_loader)

    assert _EXPORTER._route_candidates_package(path) == expected
    assert observed == [path]


def test_exporter_preserves_not_published_fallback_without_candidate_artifact() -> None:
    assert _EXPORTER._route_candidates_package(None) == _EXPORTER.ROUTE_CANDIDATES_PACKAGE


def test_exporter_rejects_candidate_sidecar_from_another_scenario(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "route-candidates.json"
    path.write_text("{}", encoding="utf-8")
    document = {
        "status": "PUBLISHED",
        "candidates": [{"provenance": {"scenario_id": "winter"}}],
    }
    monkeypatch.setattr(_EXPORTER, "load_route_candidates", lambda _path: document)

    with pytest.raises(ValueError, match="does not match"):
        _EXPORTER._route_candidates_package(path, scenario_id="summer")
