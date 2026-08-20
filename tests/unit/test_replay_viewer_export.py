import importlib.util
from datetime import UTC, datetime
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "replay_viewer_export.py"
_SPEC = importlib.util.spec_from_file_location("replay_viewer_export", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_EXPORTER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_EXPORTER)

_risk_horizon_selections = _EXPORTER._risk_horizon_selections
_select_risk_horizon = _EXPORTER._select_risk_horizon


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
