"""SimulationSnapshot / ReplayManifest model invariants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from arctic_route_orchestrator.replay.models import (
    DataVisibilitySummary,
    PlanningStateSummary,
    ReadinessSummary,
    ReplayEvent,
    ReplayManifest,
    RiskStateSummary,
    SimulationSnapshot,
)
from arctic_route_orchestrator.replay.runner import (
    ReplayRunner,
    _configuration_audit,
)


def _snapshot(simulation_time: str = "2026-08-15T10:00:00Z", index: int = 0):
    return SimulationSnapshot(
        schema_version="orchestrator.replay-snapshot.v1",
        replay_id="replay-001",
        scenario_id="tromso_isfjorden_august_2026_demo_v1",
        scenario_mode="causal_replay",
        snapshot_index=index,
        simulation_time=simulation_time,
        knowledge_as_of=simulation_time,
        visibility=DataVisibilitySummary(
            max_source_issue_time="2026-08-15T09:37:34Z",
            visible_record_set_digest="v",
            b_relevant_input_digest="r",
            data_revision=1,
            b_input_revision=1,
        ),
        risk=RiskStateSummary(
            risk_revision=1,
            prediction_as_of="2026-08-15T10:00:00Z",
            risk_valid_start="2026-08-15T10:00:00Z",
            risk_valid_end="2026-08-17T06:00:00Z",
        ),
        planning=PlanningStateSummary(
            plan_revision=0,
            planning_as_of="2026-08-15T10:00:00Z",
            departure_time="2026-08-15T10:00:00Z",
        ),
        readiness=ReadinessSummary(
            source_visibility="READY",
            b_input_ready="READY",
            risk_ready="READY",
            planning_ready="NOT_READY",
            blockers=("PLANNING_HORIZON_UNSUPPORTED",),
        ),
        events=(ReplayEvent(type="CLOCK_TICK", simulation_time=simulation_time),),
    ).with_digest()


def test_snapshot_invariants() -> None:
    snapshot = _snapshot()
    assert snapshot.knowledge_as_of == snapshot.simulation_time
    assert snapshot.visibility.max_source_issue_time <= snapshot.knowledge_as_of
    assert snapshot.risk.prediction_as_of <= snapshot.knowledge_as_of
    assert snapshot.planning.planning_as_of <= snapshot.knowledge_as_of
    assert snapshot.ship_state["status"] == "DEFERRED"
    assert snapshot.snapshot_digest


def test_snapshot_digest_is_deterministic_and_time_sensitive() -> None:
    first = _snapshot()
    second = _snapshot()
    assert first.snapshot_digest == second.snapshot_digest
    later = _snapshot(simulation_time="2026-08-15T11:00:00Z", index=1)
    assert first.snapshot_digest != later.snapshot_digest


def test_manifest_shape() -> None:
    manifest = ReplayManifest(
        schema_version="orchestrator.replay-manifest.v1",
        replay_id="replay-001",
        scenario_id="tromso_isfjorden_august_2026_demo_v1",
        scenario_mode="causal_replay",
        replay_start="2026-08-15T10:00:00Z",
        replay_end="2026-08-15T22:00:00Z",
        tick_cadence_hours=1,
        snapshot_count=1,
        snapshots=(
            {"index": 0, "simulation_time": "2026-08-15T10:00:00Z"},
        ),
        events=(ReplayEvent(type="REPLAN_TRIGGERED", simulation_time="x"),),
        resources={"risk_store": "path"},
    )
    document = manifest.to_dict()
    assert document["semantic_digest"]
    assert document["snapshot_count"] == 1


@dataclass(frozen=True)
class _PlannerProfile:
    schema_version: str = "planner-config.v1"
    operational_speed_reserve_fraction: float = 0.05


@dataclass(frozen=True)
class _ReplanningProfile:
    schema_version: str = "replanning-config.v1"
    route_switch_gain_threshold: float = 0.01


@dataclass(frozen=True)
class _Configuration:
    planner_config_digest: str = "a" * 64
    planner: _PlannerProfile = _PlannerProfile()
    replanning: _ReplanningProfile = _ReplanningProfile()


def test_configuration_audit_contains_named_profiles_and_no_paths() -> None:
    document = _configuration_audit(
        _Configuration(),
        planner_name="winter_motion_reserve_5pct",
        replanning_name="winter_viewer_dynamic",
    )

    assert document == {
        "planner_name": "winter_motion_reserve_5pct",
        "replanning_name": "winter_viewer_dynamic",
        "planner_config_digest": "a" * 64,
        "planner": {
            "schema_version": "planner-config.v1",
            "operational_speed_reserve_fraction": 0.05,
        },
        "replanning": {
            "schema_version": "replanning-config.v1",
            "route_switch_gain_threshold": 0.01,
        },
    }
    assert not any("/" in str(value) for value in document.values())


def test_checkpoint_configuration_mismatch_is_fail_closed(tmp_path) -> None:
    runner = ReplayRunner(
        replay_id="test",
        scenario_id="s",
        corridor_id="c",
        replay_start=datetime(2026, 2, 15, tzinfo=UTC),
        replay_end=datetime(2026, 2, 15, 1, tzinfo=UTC),
        tick_cadence_hours=1,
        a_data_root=tmp_path,
        manifest_path=tmp_path / "manifest.sqlite3",
        b_config_path=tmp_path / "b.json",
        c_config_root=tmp_path / "c-config",
        contracts_config_root=tmp_path / "contracts-config",
        frozen_run_context_path=tmp_path / "run-context.json",
        planner_name="winter_motion_reserve_5pct",
        replanning_name="winter_viewer_dynamic",
    )
    runner.configuration = _Configuration()

    try:
        runner._validate_checkpoint_configuration(
            {
                "planner_name": "default",
                "replanning_name": "winter_viewer_dynamic",
                "planner_config_digest": "a" * 64,
            }
        )
    except ValueError as exc:
        assert "planner_name" in str(exc)
    else:  # pragma: no cover - assertion makes a silent acceptance impossible
        raise AssertionError("configuration mismatch was accepted")
