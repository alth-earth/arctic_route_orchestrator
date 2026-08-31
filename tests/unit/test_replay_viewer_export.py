import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

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


def test_viewer_presentation_declares_formal_motion_and_raw_fallback_policies() -> None:
    assert _viewer_presentation["schema_version"] == "presentation.viewer-presentation.v1"
    assert _viewer_presentation["risk_rendering"]["geometry_policy"] == (
        "exact_authoritative_cells_no_interpolation"
    )
    assert _viewer_presentation["risk_rendering"]["hard_reason_policy"] == (
        "separate_exact_cells_fail_closed"
    )
    assert _viewer_presentation["route_rendering"]["geometry_policy"] == (
        "producer_motion_samples_when_formally_bound"
    )
    assert _viewer_presentation["route_rendering"]["fallback_policy"] == (
        "authoritative_route_waypoints"
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


def _winter_plan() -> dict:
    return {
        "plan_id": "route-v3-sha256-" + "a" * 64,
        "planning_layer": "full_voyage",
        "objective_mode": "recommended",
        "start_time": "2026-02-15T00:00:00Z",
        "metrics": {
            "distance_km": 10.0,
            "eta_hours": 1.0,
            "avg_risk": 0.1,
            "max_risk": 0.2,
            "integrated_risk_hours": 0.1,
            "minimum_confidence": 0.8,
        },
        "waypoints": [
            {
                "longitude": 18.0,
                "latitude": 70.0,
                "eta": "2026-02-15T00:00:00Z",
                "recommended_speed_mps": 5.0,
            },
            {
                "longitude": 18.0,
                "latitude": 71.0,
                "eta": "2026-02-15T01:00:00Z",
                "recommended_speed_mps": 5.0,
            },
        ],
    }


def _v2_sidecar_for_route(route: dict) -> dict:
    coordinates = [[point["lon"], point["lat"]] for point in route["waypoints"]]
    route_digest = _EXPORTER._canonical_sha256(coordinates)
    samples = [
        {
            "lon": point["lon"],
            "lat": point["lat"],
            "eta": point["eta"],
            "course_degrees": 90.0,
            "speed_knots": 10.0 if index < len(route["waypoints"]) - 1 else 0.0,
        }
        for index, point in enumerate(route["waypoints"])
    ]
    sidecar = {
        "schema_version": "c.research-route-smoothing-sidecar.v2",
        "status": "ACCEPTED",
        "applied": True,
        "research_only": True,
        "research_eligible": True,
        "production_qualified": False,
        "calibration_status": "NOT_CALIBRATED",
        "manoeuvring_qualification": "SYNTHETIC_ASSUMPTION_ONLY",
        "route_id": route["route_id"],
        "raw_route_digest": route_digest,
        "curve_digest": "c" * 64,
        "route_identity": {
            "route_id": route["route_id"],
            "route_digest": route_digest,
        },
        "authoritative_route": {
            "route_id": route["route_id"],
            "route_digest": route_digest,
            "waypoints": route["waypoints"],
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
        "motion_samples": samples,
    }
    sidecar["same_geometry_motion_digest"] = _EXPORTER._canonical_sha256(
        {
            "curve_digest": sidecar["curve_digest"],
            "motion_samples": samples,
        }
    )
    sidecar["same_geometry_motion_evidence"] = {
        "same_geometry_motion_digest": sidecar["same_geometry_motion_digest"],
        "sample_count": len(samples),
    }
    sidecar["sidecar_digest"] = _EXPORTER._canonical_sha256(sidecar)
    return sidecar


def _winter_identity_documents() -> tuple[dict, ...]:
    plan = _winter_plan()
    candidate_ids = [plan["plan_id"]] + [
        f"route-v3-sha256-{index:064x}" for index in range(1, 12)
    ]
    selected = {
        "candidate_id": plan["plan_id"],
        "geometry": {
            "coordinates": [[18.0, 70.0], [18.0, 71.0]],
        },
        "distance_km": 10.0,
        "travel_hours": 1.0,
        "risk_metrics": {
            "average_risk": 0.1,
            "maximum_risk": 0.2,
            "integrated_risk_hours": 0.1,
        },
        "provenance": {"source_risk_ids": ["risk-1"]},
    }
    candidates = [selected] + [
        {
            "candidate_id": candidate_id,
            "provenance": {"source_risk_ids": ["risk-1"]},
        }
        for candidate_id in candidate_ids[1:]
    ]
    bundle = {
        "schema_version": "a.dataset-bundle.v2",
        "bundle_id": "a-bundle-test",
        "bundle_digest": "b" * 64,
        "minimum_required_end": "2026-02-21T00:00:00Z",
    }
    run_context = {
        "schema_version": "run-context.v2",
        "run_id": "run-winter",
        "scenario_id": "winter",
        "corridor_id": "corridor",
        "vessel_profile_id": "vessel",
        "dataset_bundle_id": "a-bundle-test",
        "dataset_bundle_digest": "b" * 64,
        "simulation_start": "2026-02-15T00:00:00Z",
        "simulation_end": "2026-02-21T00:00:00Z",
    }
    risk_index = {
        "status": "FORMAL_VALIDATED",
        "frame_schema": "bc.risk-frame.v2",
        "run_id": "run-winter",
        "scenario_id": "winter",
        "dataset_bundle_id": "a-bundle-test",
        "dataset_bundle_digest": "b" * 64,
        "commit_id": "risk-window",
        "content_digest": "c" * 64,
        "frame_ids": ["risk-1", "risk-2"],
    }
    risk_commit = {
        "schema_version": "bc.risk-window-commit.v1",
        "commit_id": "risk-window",
        "content_digest": "c" * 64,
        "run_id": "run-winter",
        "scenario_id": "winter",
        "count": 2,
        "interval_seconds": 3600,
        "start": "2026-02-15T00:00:00Z",
        "end": "2026-02-21T00:00:00Z",
        "frames": [{"risk_id": "risk-1"}, {"risk_id": "risk-2"}],
    }
    plan_set = {
        "schema_version": "cd.four-layer-route-plan-set.v3",
        "run_id": "run-winter",
        "scenario_id": "winter",
        "layer_set_id": "layer-set",
        "layers": [
            {"planning_layer": "full_voyage", "plans": {"recommended": plan}}
        ],
    }
    route_candidates = {
        "selected_candidate_id": plan["plan_id"],
        "candidate_set_id": "candidate-set",
        "provenance": {"source_run_id": "run-winter"},
        "candidates": candidates,
    }
    integrity = [
        {
            "route_id": candidate_id,
            "status": "PASS",
            "land_intersections": 0,
            "data_unavailable_violations": 0,
            "edge_hard_violations": 0,
        }
        for candidate_id in candidate_ids
    ]
    return bundle, run_context, risk_index, risk_commit, plan_set, route_candidates, integrity


def test_winter_identity_rejects_cross_scenario_sources() -> None:
    documents = list(_winter_identity_documents())
    documents[3]["scenario_id"] = "summer"

    with pytest.raises(ValueError, match="RiskWindow scenario"):
        _EXPORTER._validate_winter_identity(
            dataset_bundle=documents[0],
            run_context=documents[1],
            risk_index=documents[2],
            risk_commit=documents[3],
            plan_set=documents[4],
            route_candidates=documents[5],
            route_integrity=documents[6],
        )


def test_winter_identity_binds_a_b_c_and_presentation_sources() -> None:
    documents = _winter_identity_documents()

    identity = _EXPORTER._validate_winter_identity(
        dataset_bundle=documents[0],
        run_context=documents[1],
        risk_index=documents[2],
        risk_commit=documents[3],
        plan_set=documents[4],
        route_candidates=documents[5],
        route_integrity=documents[6],
    )

    assert identity["dataset_bundle_id"] == "a-bundle-test"
    assert identity["run_id"] == "run-winter"
    assert identity["risk_frame_count"] == 2
    assert identity["selected_candidate_id"] == _winter_plan()["plan_id"]


def test_winter_vessel_timeline_uses_waypoint_eta_and_linear_lon_lat() -> None:
    timeline = _EXPORTER._winter_vessel_timeline(
        _winter_plan(),
        cadence_seconds=1800,
    )

    assert [entry["t"] for entry in timeline] == [
        "2026-02-15T00:00:00Z",
        "2026-02-15T00:30:00Z",
        "2026-02-15T01:00:00Z",
    ]
    assert timeline[1]["v"]["lon"] == 18.0
    assert timeline[1]["v"]["lat"] == 70.5
    assert timeline[1]["v"]["status"] == "UNDERWAY"
    assert timeline[-1]["v"]["status"] == "ARRIVED"
    assert timeline[-1]["v"]["kn"] == 0.0
    assert timeline[-1]["ctl"] == 2


def test_optional_route_smoothing_sidecar_is_bound_to_the_authoritative_route(
    tmp_path: Path,
) -> None:
    route = _EXPORTER._winter_route_meta(_winter_plan())
    sidecar = {
        "schema_version": "c.research-route-smoothing-sidecar.v1",
        "policy": "authoritative_waypoints_adaptive_local_cubic_bspline_motion_research_only",
        "status": "ACCEPTED",
        "applied": True,
        "research_only": True,
        "research_eligible": True,
        "plan_revision": route["revision"],
        "adoption_time": route["effective_adoption_time"],
        "validation": {
            "research_gate_passed": True,
            "risk_rechecked": True,
            "hard_mask_rechecked": True,
            "coverage_complete": True,
            "eta_recomputed": True,
            "speed_checked": True,
        },
        "route_id": route["route_id"],
        "raw_route_digest": _EXPORTER._canonical_sha256(
            [[point["lon"], point["lat"]] for point in route["waypoints"]]
        ),
        "authoritative_route": {
            "route_digest": None,
            "waypoints": route["waypoints"],
        },
        "motion_samples": route["waypoints"],
    }
    sidecar["authoritative_route"]["route_digest"] = sidecar["raw_route_digest"]
    sidecar["sidecar_digest"] = _EXPORTER._canonical_sha256(sidecar)
    path = tmp_path / "route-smoothing.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    loaded = _EXPORTER._load_route_smoothing_sidecar(path, route=route)

    assert loaded == sidecar


def test_optional_v2_route_smoothing_sidecar_is_validated_and_bound(
    tmp_path: Path,
) -> None:
    route = _EXPORTER._winter_route_meta(_winter_plan())
    sidecar = _v2_sidecar_for_route(route)
    path = tmp_path / "route-smoothing-v2.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    loaded = _EXPORTER._load_route_smoothing_sidecar(path, route=route)

    assert loaded == sidecar
    assert loaded["schema_version"] == "c.research-route-smoothing-sidecar.v2"


def test_optional_v2_route_smoothing_sidecar_rejects_route_digest_mismatch(
    tmp_path: Path,
) -> None:
    route = _EXPORTER._winter_route_meta(_winter_plan())
    sidecar = _v2_sidecar_for_route(route)
    sidecar["raw_route_digest"] = "c" * 64
    sidecar["route_identity"]["route_digest"] = "c" * 64
    sidecar["authoritative_route"]["route_digest"] = "c" * 64
    sidecar["sidecar_digest"] = _EXPORTER._canonical_sha256(sidecar)
    path = tmp_path / "route-smoothing-v2-mismatch.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    with pytest.raises(ValueError, match="validation failed"):
        _EXPORTER._load_route_smoothing_sidecar(path, route=route)


def test_optional_route_smoothing_sidecar_rejects_tampered_digest(tmp_path: Path) -> None:
    route = _EXPORTER._winter_route_meta(_winter_plan())
    sidecar = {
        "schema_version": "c.research-route-smoothing-sidecar.v1",
        "status": "ACCEPTED",
        "applied": True,
        "research_only": True,
        "research_eligible": True,
        "plan_revision": route["revision"],
        "adoption_time": route["effective_adoption_time"],
        "validation": {
            "research_gate_passed": True,
            "risk_rechecked": True,
            "hard_mask_rechecked": True,
            "coverage_complete": True,
            "eta_recomputed": True,
            "speed_checked": True,
        },
        "route_id": route["route_id"],
        "raw_route_digest": "a" * 64,
        "authoritative_route": {"route_digest": "a" * 64},
        "motion_samples": route["waypoints"],
        "sidecar_digest": "b" * 64,
    }
    path = tmp_path / "tampered-route-smoothing.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    with pytest.raises(ValueError, match="sidecar digest is invalid"):
        _EXPORTER._load_route_smoothing_sidecar(path, route=route)


def test_optional_route_smoothing_sidecar_rejects_authoritative_waypoint_drift(
    tmp_path: Path,
) -> None:
    route = _EXPORTER._winter_route_meta(_winter_plan())
    sidecar = {
        "schema_version": "c.research-route-smoothing-sidecar.v1",
        "status": "ACCEPTED",
        "applied": True,
        "research_only": True,
        "research_eligible": True,
        "plan_revision": route["revision"],
        "adoption_time": route["effective_adoption_time"],
        "validation": {
            "research_gate_passed": True,
            "risk_rechecked": True,
            "hard_mask_rechecked": True,
            "coverage_complete": True,
            "eta_recomputed": True,
            "speed_checked": True,
        },
        "route_id": route["route_id"],
        "raw_route_digest": _EXPORTER._canonical_sha256(
            [[point["lon"], point["lat"]] for point in route["waypoints"]]
        ),
        "authoritative_route": {
            "route_digest": None,
            "waypoints": [dict(point) for point in route["waypoints"]],
        },
        "motion_samples": route["waypoints"],
    }
    sidecar["authoritative_route"]["route_digest"] = sidecar["raw_route_digest"]
    sidecar["authoritative_route"]["waypoints"][1]["eta"] = "2026-02-15T00:00:01Z"
    sidecar["sidecar_digest"] = _EXPORTER._canonical_sha256(sidecar)
    path = tmp_path / "drifted-route-smoothing.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    with pytest.raises(ValueError, match="authoritative waypoint differs"):
        _EXPORTER._load_route_smoothing_sidecar(path, route=route)


def test_optional_motion_artifacts_change_combined_presentation_identity() -> None:
    identity = {
        "scenario_id": "winter",
        "dataset_bundle_id": "dataset",
        "run_id": "run",
        "risk_window_id": "risk",
    }
    route = {"waypoints": [{"eta": "2026-01-01T00:00:00Z"}, {"eta": "2026-01-01T01:00:00Z"}]}

    _, baseline_digest = _EXPORTER._winter_combined_identity(
        identity,
        route=route,
        cadence_seconds=60,
    )
    _, sidecar_digest = _EXPORTER._winter_combined_identity(
        identity,
        route=route,
        cadence_seconds=60,
        route_smoothing_sidecar_digest="a" * 64,
    )
    _, formal_motion_digest = _EXPORTER._winter_combined_identity(
        identity,
        route=route,
        cadence_seconds=60,
        route_motion_set_ids=[f"route-motion-set-sha256-{'b' * 64}"],
    )

    assert sidecar_digest != baseline_digest
    assert formal_motion_digest != baseline_digest
    assert formal_motion_digest != sidecar_digest


def test_production_winter_export_requires_formal_motion_artifact() -> None:
    args = SimpleNamespace(
        output_dir=Path("/root/my_project/work_package_d/viewer"),
        require_route_motion=False,
        route_motion_set=None,
    )

    with pytest.raises(ValueError, match="requires --route-motion-set"):
        _EXPORTER._export_winter_combined(args)


def test_viewer_presentation_declares_formal_motion_fail_closed_policy() -> None:
    route_rendering = _EXPORTER.VIEWER_PRESENTATION["route_rendering"]
    assert route_rendering["formal_motion_required_for_production_default"] is True
    assert route_rendering["formal_motion_failure_policy"] == "RAW_WAYPOINT_TIMELINE_FAIL_CLOSED"
