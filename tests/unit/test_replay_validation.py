"""Replay validation invariants (synthetic)."""

from __future__ import annotations

from arctic_route_orchestrator.replay.digests import replay_semantic_digest
from arctic_route_orchestrator.replay.validation import (
    validate_manifest,
    validate_replay,
    validate_snapshot,
)


def _snapshot_document(simulation_time: str, index: int) -> dict:
    from arctic_route_orchestrator.replay.models import (
        DataVisibilitySummary,
        PlanningStateSummary,
        ReadinessSummary,
        RiskStateSummary,
        SimulationSnapshot,
    )

    snapshot = SimulationSnapshot(
        schema_version="orchestrator.replay-snapshot.v1",
        replay_id="replay-test",
        scenario_id="scenario",
        scenario_mode="causal_replay",
        snapshot_index=index,
        simulation_time=simulation_time,
        knowledge_as_of=simulation_time,
        visibility=DataVisibilitySummary(
            max_source_issue_time=simulation_time,
            visible_record_set_digest="v",
            b_relevant_input_digest="r",
            data_revision=1,
            b_input_revision=1,
        ),
        risk=RiskStateSummary(
            risk_revision=1,
            prediction_as_of=simulation_time,
            risk_valid_start=simulation_time,
            risk_valid_end=simulation_time,
        ),
        planning=PlanningStateSummary(
            plan_revision=0,
            planning_as_of=simulation_time,
            departure_time=simulation_time,
        ),
        readiness=ReadinessSummary(
            source_visibility="READY",
            b_input_ready="READY",
            risk_ready="READY",
            planning_ready="NOT_READY",
        ),
    ).with_digest()
    return snapshot.to_dict()


def test_valid_snapshot_passes() -> None:
    document = _snapshot_document("2026-08-15T10:00:00Z", 0)
    result = validate_snapshot(document)
    assert result["status"] == "PASS"
    assert result["violations"] == []


def test_future_max_issue_fails() -> None:
    document = _snapshot_document("2026-08-15T10:00:00Z", 0)
    document["visibility"]["max_source_issue_time"] = "2026-08-15T11:00:00Z"
    result = validate_snapshot(document)
    assert result["status"] == "FAIL"
    assert any("max_source_issue_time" in item for item in result["violations"])


def test_retrospective_snapshot_allows_later_fixed_knowledge_cutoff() -> None:
    document = _snapshot_document("2026-02-15T10:00:00Z", 0)
    document["scenario_mode"] = "retrospective_dynamic_replay"
    document["knowledge_as_of"] = "2026-08-25T16:08:12Z"
    document["snapshot_digest"] = replay_semantic_digest(
        {key: value for key, value in document.items() if key != "snapshot_digest"}
    )

    assert validate_snapshot(document)["status"] == "PASS"

    document["knowledge_as_of"] = "2026-02-14T23:59:59Z"
    document["snapshot_digest"] = replay_semantic_digest(
        {key: value for key, value in document.items() if key != "snapshot_digest"}
    )
    result = validate_snapshot(document)
    assert result["status"] == "FAIL"
    assert any("retrospective knowledge_as_of" in item for item in result["violations"])


def test_retrospective_snapshot_allows_posthoc_prediction_timestamp() -> None:
    document = _snapshot_document("2026-02-15T10:00:00Z", 0)
    document["scenario_mode"] = "retrospective_dynamic_replay"
    document["knowledge_as_of"] = "2026-09-02T17:20:41.878201Z"
    document["risk"]["prediction_as_of"] = "2026-09-03T00:00:00Z"
    document["snapshot_digest"] = replay_semantic_digest(
        {key: value for key, value in document.items() if key != "snapshot_digest"}
    )

    result = validate_snapshot(document)

    assert result["status"] == "PASS"
    assert result["violations"] == []


def test_monotonic_sequence_passes_and_backwards_fails() -> None:
    documents = [
        _snapshot_document("2026-08-15T10:00:00Z", 0),
        _snapshot_document("2026-08-15T11:00:00Z", 1),
    ]
    assert validate_replay(documents)["status"] == "PASS"
    documents[1]["simulation_time"] = "2026-08-15T09:00:00Z"
    assert validate_replay(documents)["status"] == "FAIL"


def test_manifest_digest_reference_check() -> None:
    documents = [_snapshot_document("2026-08-15T10:00:00Z", 0)]
    manifest = {
        "snapshot_count": 1,
        "scenario_mode": "causal_replay",
        "snapshots": [
            {"index": 0, "digest": documents[0]["snapshot_digest"]},
        ],
    }
    assert validate_manifest(manifest, documents)["status"] == "PASS"
    manifest["snapshots"][0]["digest"] = "0" * 64
    assert validate_manifest(manifest, documents)["status"] == "FAIL"
