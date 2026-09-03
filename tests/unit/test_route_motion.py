from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from arctic_route_planning.motion import (
    build_route_motion_candidate_set,
    build_route_motion_candidate_set_with_evidence,
    build_route_motion_set,
    build_route_motion_set_with_evidence,
)
from arctic_route_planning.publishing import (
    four_layer_route_plan_set_to_dict,
    route_motion_candidate_set_to_dict,
    route_motion_set_to_dict,
)

from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.output import publish_output_directory
from arctic_route_orchestrator.route_motion import (
    load_bound_route_motion_candidate_set,
    load_bound_route_motion_set,
    validate_route_motion_context,
)
from arctic_route_orchestrator.route_presentation import project_runtime_route_candidates

_HELPER_PATH = Path(__file__).with_name("test_route_presentation.py")
_SPEC = importlib.util.spec_from_file_location("route_presentation_test_helper", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_HELPER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPER)
_plan_set = _HELPER._plan_set


def _artifact(tmp_path):
    plan_set = _plan_set()
    motion_set = build_route_motion_set(
        plan_set,
        risk_window_id="risk-window-fixture",
        risk_window_digest="2" * 64,
        vessel_profile_digest="3" * 64,
        producer_digest="4" * 64,
        generated_at=plan_set.generated_at,
    )
    document = route_motion_set_to_dict(motion_set)
    path = tmp_path / "route-motion-set.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    recommended = plan_set.recommended
    replay_route = {
        "route_id": recommended.plan_id,
        "effective_adoption_time": recommended.waypoints[0].eta.isoformat().replace(
            "+00:00", "Z"
        ),
        "waypoints": [
            {
                "lon": waypoint.longitude,
                "lat": waypoint.latitude,
                "eta": waypoint.eta.isoformat().replace("+00:00", "Z"),
            }
            for waypoint in recommended.waypoints
        ],
    }
    return path, plan_set, document, replay_route


def _artifact_with_evidence(tmp_path):
    plan_set = _plan_set()
    motion_set, evidence = build_route_motion_set_with_evidence(
        plan_set,
        risk_window_id="risk-window-fixture",
        risk_window_digest="2" * 64,
        vessel_profile_digest="3" * 64,
        producer_digest="4" * 64,
        generated_at=plan_set.generated_at,
    )
    document = route_motion_set_to_dict(motion_set)
    path = tmp_path / "route-motion-set.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    (tmp_path / "route-motion-qualification-evidence.json").write_text(
        json.dumps(evidence), encoding="utf-8"
    )
    recommended = plan_set.recommended
    replay_route = {
        "route_id": recommended.plan_id,
        "effective_adoption_time": recommended.waypoints[0].eta.isoformat().replace(
            "+00:00", "Z"
        ),
        "waypoints": [
            {
                "lon": waypoint.longitude,
                "lat": waypoint.latitude,
                "eta": waypoint.eta.isoformat().replace("+00:00", "Z"),
            }
            for waypoint in recommended.waypoints
        ],
    }
    return path, plan_set, document, replay_route, evidence


def test_formal_motion_is_bound_to_four_recommended_routes_and_adoption(tmp_path) -> None:
    path, plan_set, document, replay_route = _artifact(tmp_path)

    loaded = load_bound_route_motion_set(
        path,
        plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
        replay_routes=[replay_route],
    )

    assert loaded == document
    assert len(loaded["records"]) == 4
    assert loaded["records"][0]["plan_id"] == replay_route["route_id"]


def test_formal_motion_validates_adjacent_qualification_evidence(tmp_path) -> None:
    path, plan_set, _document, replay_route, evidence = _artifact_with_evidence(tmp_path)
    loaded = load_bound_route_motion_set(
        path,
        plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
        replay_routes=[replay_route],
    )
    assert loaded["records"]
    assert evidence["motion_set_id"] == loaded["motion_set_id"]

    tampered = json.loads(json.dumps(evidence))
    tampered["records"][0]["details"]["fallback_reason"] = "tampered"
    (tmp_path / "route-motion-qualification-evidence.json").write_text(
        json.dumps(tampered), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="evidence digest"):
        load_bound_route_motion_set(
            path,
            plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
            replay_routes=[replay_route],
        )

    tampered = json.loads(json.dumps(evidence))
    tampered["records"][0]["diagnostics"]["qualification_result"] = "tampered"
    tampered_body = dict(tampered)
    tampered_body.pop("evidence_id")
    from arctic_route_planning.publishing import canonical_route_motion_sha256

    tampered["evidence_id"] = (
        "route-motion-qualification-evidence-sha256-"
        + canonical_route_motion_sha256(tampered_body)
    )
    (tmp_path / "route-motion-qualification-evidence.json").write_text(
        json.dumps(tampered), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="diagnostics differ"):
        load_bound_route_motion_set(
            path,
            plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
            replay_routes=[replay_route],
        )


def test_candidate_motion_is_bound_to_all_full_voyage_objectives(tmp_path) -> None:
    plan_set = _plan_set()
    motion_set = build_route_motion_candidate_set(
        plan_set,
        risk_window_id="risk-window-fixture",
        risk_window_digest="2" * 64,
        vessel_profile_digest="3" * 64,
        producer_digest="4" * 64,
        generated_at=plan_set.generated_at,
    )
    document = route_motion_candidate_set_to_dict(motion_set)
    path = tmp_path / "route-motion-candidate-set.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    runtime = project_runtime_route_candidates(plan_set)

    loaded = load_bound_route_motion_candidate_set(
        path,
        plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
        runtime_candidates_document=runtime,
    )

    assert loaded == document
    assert [item["objective_mode"] for item in loaded["records"]] == [
        "fastest", "low_risk", "recommended"
    ]
    tampered = json.loads(json.dumps(runtime))
    tampered["candidates"][0]["waypoints"][1]["longitude"] += 0.01
    with pytest.raises(ValueError, match="geometry differs"):
        load_bound_route_motion_candidate_set(
            path,
            plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
            runtime_candidates_document=tampered,
        )


def test_candidate_motion_validates_its_qualification_evidence(tmp_path) -> None:
    plan_set = _plan_set()
    candidate_set, evidence = build_route_motion_candidate_set_with_evidence(
        plan_set,
        risk_window_id="risk-window-fixture",
        risk_window_digest="2" * 64,
        vessel_profile_digest="3" * 64,
        producer_digest="4" * 64,
        generated_at=plan_set.generated_at,
    )
    path = tmp_path / "route-motion-candidate-set.json"
    path.write_text(
        json.dumps(route_motion_candidate_set_to_dict(candidate_set)), encoding="utf-8"
    )
    (tmp_path / "route-motion-qualification-evidence.json").write_text(
        json.dumps(evidence), encoding="utf-8"
    )
    runtime = project_runtime_route_candidates(plan_set)
    loaded = load_bound_route_motion_candidate_set(
        path,
        plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
        runtime_candidates_document=runtime,
    )
    assert loaded["motion_candidate_set_id"] == candidate_set.motion_candidate_set_id


def test_formal_motion_accepts_declared_uniform_deferred_adoption_offset(tmp_path) -> None:
    path, plan_set, document, replay_route = _artifact(tmp_path)
    offset = timedelta(minutes=17, seconds=5)
    replay_route["motion_time_offset_seconds"] = offset.total_seconds()
    for waypoint in replay_route["waypoints"]:
        moment = datetime.fromisoformat(waypoint["eta"].replace("Z", "+00:00"))
        waypoint["eta"] = (moment + offset).astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        )
    replay_route["effective_adoption_time"] = replay_route["waypoints"][0]["eta"]

    loaded = load_bound_route_motion_set(
        path,
        plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
        replay_routes=[replay_route],
    )

    assert loaded == document


def test_formal_motion_rejects_stale_plan_and_adoption_teleport(tmp_path) -> None:
    path, plan_set, _document, replay_route = _artifact(tmp_path)
    stale_plan = four_layer_route_plan_set_to_dict(plan_set)
    stale_plan["input_revision"] += 1

    with pytest.raises(ValueError, match="input_revision"):
        load_bound_route_motion_set(
            path,
            plan_set_document=stale_plan,
            replay_routes=[replay_route],
        )

    replay_route.update({"revision": 2, "adoption_mode": "REPLAN"})
    load_bound_route_motion_set(
        path,
        plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
        replay_routes=[replay_route],
    )
    replay_route["effective_adoption_time"] = "2026-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="teleport"):
        load_bound_route_motion_set(
            path,
            plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
            replay_routes=[replay_route],
        )


def test_formal_motion_rejects_canonical_tampering(tmp_path) -> None:
    path, plan_set, document, replay_route = _artifact(tmp_path)
    document["records"][0]["motion_samples"][0]["lon"] += 0.01
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match=r"digest|motion_set_id"):
        load_bound_route_motion_set(
            path,
            plan_set_document=four_layer_route_plan_set_to_dict(plan_set),
            replay_routes=[replay_route],
        )


def test_formal_motion_binds_risk_window_and_vessel_context(tmp_path) -> None:
    _path, _plan_set_value, document, _replay_route = _artifact(tmp_path)
    expected = {
        "risk_window_id": document["risk_window_id"],
        "risk_window_digest": document["risk_window_digest"],
        "vessel_profile_id": document["vessel_profile_id"],
        "vessel_profile_version": document["vessel_profile_version"],
        "vessel_profile_digest": document["vessel_profile_digest"],
    }

    validate_route_motion_context(document, **expected)
    with pytest.raises(ValueError, match="risk_window_digest"):
        validate_route_motion_context(
            document,
            **{**expected, "risk_window_digest": "f" * 64},
        )
    with pytest.raises(ValueError, match="vessel_profile_version"):
        validate_route_motion_context(
            document,
            **{**expected, "vessel_profile_version": "stale"},
        )


def test_plan_and_motion_publish_atomically_with_checksums(tmp_path) -> None:
    _path, plan_set, document, _replay_route = _artifact(tmp_path)
    target = tmp_path / "immutable-output"

    output, checksums = publish_output_directory(
        target,
        {
            "four-layer-route-plan-set.json": four_layer_route_plan_set_to_dict(plan_set),
            "route-motion-set.json": document,
        },
    )

    assert output == target.resolve()
    assert set(checksums) == {
        "four-layer-route-plan-set.json", "route-motion-set.json",
    }
    checksum_manifest = json.loads((output / "checksums.json").read_text())
    assert checksum_manifest["files"] == checksums
    with pytest.raises(OrchestrationError, match="already exists"):
        publish_output_directory(target, {"route-motion-set.json": document})
