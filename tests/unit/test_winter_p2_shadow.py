"""Focused contract tests for the isolated Winter P2 shadow runner."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from arctic_route_planning.domain import ObjectiveMode
from arctic_route_planning.planners import PlanningRequest

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "winter_p2_shadow.py"
_SCRIPT_SPEC = importlib.util.spec_from_file_location("winter_p2_shadow", _SCRIPT_PATH)
assert _SCRIPT_SPEC is not None and _SCRIPT_SPEC.loader is not None
shadow = importlib.util.module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = shadow
_SCRIPT_SPEC.loader.exec_module(shadow)


def test_execution_order_aliases_and_alternation() -> None:
    assert shadow._validate_order("control,candidate") == "control-first"
    assert shadow._validate_order("candidate,control") == "candidate-first"
    assert shadow._order_for(1, "alternate") == "control-first"
    assert shadow._order_for(2, "alternate") == "candidate-first"
    with pytest.raises(ValueError, match="execution-order"):
        shadow._validate_order("random")


def test_candidate_mode_and_rss_mode_are_explicit() -> None:
    assert shadow._validate_candidate_mode("exact_temporal") == "exact-temporal"
    assert shadow._validate_candidate_mode("control-trace") == "control-trace"
    assert shadow._validate_rss_mode("isolated") == "isolated"
    exact = shadow._mode_metadata("exact-temporal")
    trace = shadow._mode_metadata("control-trace")
    assert exact["candidate_algorithm"] != trace["candidate_algorithm"]
    assert exact["candidate_schema"] != trace["candidate_schema"]
    assert exact["sidecar_schema"] != trace["sidecar_schema"]
    with pytest.raises(ValueError, match="candidate-mode"):
        shadow._validate_candidate_mode("beam")
    with pytest.raises(ValueError, match="rss-mode"):
        shadow._validate_rss_mode("shared")


def test_parser_preserves_exact_temporal_default_and_accepts_control_trace() -> None:
    required = [
        "--risk-store-root",
        "/tmp/risk",
        "--risk-commit",
        "/tmp/commit.json",
        "--run-context",
        "/tmp/context.json",
        "--execution-spec",
        "/tmp/spec.json",
        "--output-dir",
        "/tmp/output",
    ]
    exact = shadow.build_parser().parse_args(required)
    assert exact.candidate_mode == "exact-temporal"
    assert exact.rss_mode == "in-process"
    trace = shadow.build_parser().parse_args(
        [*required, "--candidate-mode", "control-trace", "--rss-mode", "isolated"]
    )
    assert trace.candidate_mode == "control-trace"
    assert trace.rss_mode == "isolated"


def test_parser_accepts_explicit_worker_timeout() -> None:
    required = [
        "--risk-store-root",
        "/tmp/risk",
        "--risk-commit",
        "/tmp/commit.json",
        "--run-context",
        "/tmp/context.json",
        "--execution-spec",
        "/tmp/spec.json",
        "--output-dir",
        "/tmp/output",
    ]
    args = shadow.build_parser().parse_args(
        [
            *required,
            "--candidate-mode",
            "control-trace",
            "--rss-mode",
            "isolated",
            "--worker-timeout-seconds",
            "42",
        ]
    )
    assert args.worker_timeout_seconds == 42.0


def test_control_trace_refuses_shared_process_m2_measurement() -> None:
    required = [
        "--risk-store-root",
        "/tmp/risk",
        "--risk-commit",
        "/tmp/commit.json",
        "--run-context",
        "/tmp/context.json",
        "--execution-spec",
        "/tmp/spec.json",
        "--output-dir",
        "/tmp/output",
    ]
    args = shadow.build_parser().parse_args(
        [*required, "--candidate-mode", "control-trace"]
    )
    with pytest.raises(ValueError, match="requires --rss-mode isolated"):
        shadow.run(args)


def test_isolated_worker_command_has_explicit_track_boundary(tmp_path: Path) -> None:
    args = SimpleNamespace(
        risk_store_root=tmp_path / "risk",
        risk_commit=tmp_path / "commit.json",
        run_context=tmp_path / "context.json",
        execution_spec=tmp_path / "spec.json",
        c_config_root=tmp_path / "c-config",
        contracts_config_root=tmp_path / "contracts-config",
        output_dir=tmp_path / "output",
        candidate_mode="control-trace",
        worker_timeout_seconds=42.0,
    )
    command = shadow._worker_command(
        args=args,
        track="candidate",
        result_path=tmp_path / "worker-result.json",
    )
    assert "--_track-worker" in command
    assert command[command.index("--_track-worker") + 1] == "candidate"
    assert command[command.index("--candidate-mode") + 1] == "control-trace"
    assert command[command.index("--rss-mode") + 1] == "in-process"
    assert command[command.index("--worker-timeout-seconds") + 1] == "42.0"


def test_shadow_requires_empty_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "experiment"
    shadow._empty_output_dir(output)
    assert output.is_dir()
    (output / "existing.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="empty experiment directory"):
        shadow._empty_output_dir(output)


def test_reuse_sidecar_reports_real_hits_and_fallbacks() -> None:
    records = [
        {
            "reuse_hit": True,
            "reuse_status": "HIT_EXACT",
            "certificate": {"status": "CERTIFIED_REUSABLE"},
        },
        {"reuse_hit": False, "reuse_status": "FALLBACK_CONTROL", "certificate": None},
    ]
    sidecar = shadow._reuse_sidecar(
        candidate_records=records,
        screen_objective=ObjectiveMode.RECOMMENDED,
    )
    assert sidecar["status"] == "PASS"
    assert sidecar["certificate_status"] == "CERTIFIED_REUSABLE"
    assert sidecar["reuse_hit_count"] == 1
    assert sidecar["p2_same_goal_reuse_attempted"] is True


def test_reuse_sidecar_keeps_cold_candidate_distinct_from_control_fallback() -> None:
    sidecar = shadow._reuse_sidecar(
        candidate_records=[
            {
                "reuse_hit": False,
                "reuse_status": "COLD_CANDIDATE",
                "reuse_lookup_status": "MISS_INCOMPATIBLE",
                "certificate": None,
            }
        ],
        screen_objective=ObjectiveMode.RECOMMENDED,
    )
    assert sidecar["status"] == "MISS_COLD_CANDIDATE"
    assert sidecar["fallback"] == "cold_candidate"
    assert sidecar["reuse_statuses"] == ["COLD_CANDIDATE"]
    assert sidecar["reuse_lookup_statuses"] == ["MISS_INCOMPATIBLE"]


def test_control_trace_sidecar_reports_real_hits_and_misses() -> None:
    sidecar = shadow._reuse_sidecar(
        candidate_records=[
            {
                "candidate_mode": "control-trace",
                "reuse_status": "TRACE_CAPTURED",
                "reuse_lookup_status": "TRACE_CAPTURED",
                "reuse_hit": False,
                "search_used": True,
                "certificate": {"status": "CERTIFIED_TRACE"},
            },
            {
                "candidate_mode": "control-trace",
                "reuse_status": "HIT_TRACE_EQUIVALENT",
                "reuse_lookup_status": "HIT_TRACE_EQUIVALENT",
                "reuse_hit": True,
                "search_used": False,
                "certificate": {"status": "CERTIFIED_TRACE"},
            },
            {
                "candidate_mode": "control-trace",
                "reuse_status": "FALLBACK_CONTROL",
                "reuse_lookup_status": "MISS_INCOMPATIBLE",
                "reuse_hit": False,
                "search_used": True,
                "certificate": {"status": "CERTIFIED_TRACE"},
            },
            {
                "candidate_mode": "control-trace",
                "reuse_status": "COLD_CONTROL",
                "reuse_lookup_status": "NOT_ATTEMPTED",
                "reuse_hit": False,
                "search_used": True,
                "certificate": {"status": "CERTIFIED_TRACE"},
            },
        ],
        screen_objective=ObjectiveMode.RECOMMENDED,
        candidate_mode="control-trace",
    )
    assert sidecar["schema_version"] == "orchestrator.winter-p2-control-trace-sidecar.v1"
    assert sidecar["status"] == "PASS"
    assert sidecar["certificate_status"] == "CERTIFIED_TRACE"
    assert sidecar["p2_reuse_claim"] == "SHADOW_ONLY_CONTROL_TRACE_REUSE"
    assert sidecar["p2_same_goal_reuse_attempted"] is True
    assert sidecar["reuse_hit_count"] == 1
    assert sidecar["zero_search_hit_count"] == 1
    assert sidecar["reuse_miss_count"] == 2
    assert sidecar["reuse_lookup_miss_count"] == 1
    assert sidecar["candidate_sessions"][2]["certificate"] is None
    assert sidecar["candidate_sessions"][3]["certificate"] is None
    assert sidecar["reuse_statuses"] == [
        "COLD_CONTROL",
        "FALLBACK_CONTROL",
        "HIT_TRACE_EQUIVALENT",
        "TRACE_CAPTURED",
    ]
    assert "TRACE_ONLY" not in str(sidecar)


def test_shadow_sidecar_records_normalize_trace_and_reuse_counts() -> None:
    records = shadow._shadow_sidecar_records(
        {
            "trace_observations": [
                {"objective": value}
                for value in ("fastest", "low_risk", "recommended")
            ],
            "reuse_outcomes": [
                {
                    "objective": "recommended",
                    "status": "HIT_TRACE_EQUIVALENT",
                    "reused": True,
                    "used_search": False,
                }
                for _ in range(3)
            ]
            + [
                {
                    "objective": "recommended",
                    "status": "COLD_CONTROL",
                    "reused": False,
                    "used_search": True,
                }
                for _ in range(6)
            ],
        }
    )
    assert records[0]["certificate"] == {"status": "CERTIFIED_TRACE"}
    assert records[3]["certificate"]["status"] == "CERTIFIED_TRACE"
    assert records[6]["certificate"] is None
    assert records[6]["certificate_status"] is None
    assert shadow._trace_counts(records) == {
        "trace_captured": 3,
        "trace_hits": 3,
        "cold_control": 6,
        "fallback_control": 0,
        "record_count": 12,
    }

    combined = [
        {
            "layer": "full_voyage",
            "objective": "recommended",
            "wall_ms": 1.0,
            "reuse_status": "TRACE_CAPTURED",
        },
        *records,
    ]
    assert shadow._trace_counts(combined) == shadow._trace_counts(records)


def test_m2_summary_enforces_12_routes_timing_reuse_rss_and_swap() -> None:
    cells = [
        (layer, objective)
        for layer in shadow._CONTROL_TRACE_LAYER_NAMES
        for objective in ("fastest", "low_risk", "recommended")
    ]
    timing_control = [
        {
            "layer": layer,
            "objective": objective,
            "wall_ms": 100.0,
            "expanded": 7,
            "edge": 11,
            "search_used": True,
            "reuse_status": "CONTROL_SEARCH",
        }
        for layer, objective in cells
    ]
    timing_candidate = []
    for layer, objective in cells:
        status = (
            "TRACE_CAPTURED"
            if layer == "full_voyage"
            else "HIT_TRACE_EQUIVALENT"
            if layer == "main_corridor_24_72h"
            else "COLD_CONTROL"
        )
        hit = status == "HIT_TRACE_EQUIVALENT"
        timing_candidate.append(
            {
                "layer": layer,
                "objective": objective,
                "wall_ms": 70.0,
                "expanded": 0 if hit else 7,
                "edge": 0 if hit else 11,
                "search_used": not hit,
                "trace_status": "TRACE_CAPTURED" if status == "TRACE_CAPTURED" else None,
                "reuse_status": status,
            }
        )
    sidecar = shadow._shadow_sidecar_records(
        {
            "trace_observations": [
                {"objective": value}
                for value in ("fastest", "low_risk", "recommended")
            ],
            "reuse_outcomes": [
                {
                    "objective": "recommended",
                    "status": "HIT_TRACE_EQUIVALENT",
                    "reused": True,
                    "used_search": False,
                }
                for _ in range(3)
            ]
            + [
                {
                    "objective": "recommended",
                    "status": "COLD_CONTROL",
                    "reused": False,
                    "used_search": True,
                }
                for _ in range(6)
            ],
        }
    )
    pairs = [
        {
            "layer": layer,
            "objective": objective,
            "status": "PASS",
            "control_route_digest": f"route-{index}",
            "candidate_route_digest": f"route-{index}",
        }
        for index, (layer, objective) in enumerate(cells)
    ]
    cases = [
        {
            "case_id": f"case-{index}",
            "status": "PASS",
            "rss_scope": "independent_child_process",
            "records": {
                "control": timing_control,
                "candidate": [*timing_candidate, *sidecar],
            },
            "track_resources": {
                "control": {
                    "wall_seconds": 1.0,
                    "peak_rss_kib": 100,
                    "swap_delta": {"pswpin": 0, "pswpout": 0},
                },
                "candidate": {
                    "wall_seconds": 0.7,
                    "peak_rss_kib": 105,
                    "swap_delta": {"pswpin": 0, "pswpout": 0},
                },
            },
            "comparison": {"status": "PASS", "pair_count": 12, "pairs": pairs},
            "route_integrity": {
                "control": {"status": "PASS", "route_count": 12},
                "candidate": {"status": "PASS", "route_count": 12},
            },
        }
        for index in range(1, 4)
    ]
    summary = shadow._m2_summary(cases)
    assert summary["gate_verdict"] == "PASS"
    assert summary["overall"]["median_improvement_percent"] == pytest.approx(30.0)
    assert summary["overall"]["p95_gate"] == "PASS"
    assert summary["rss"]["median_ratio"] == pytest.approx(1.05)
    assert summary["swap"]["gate"] == "PASS"
    assert summary["reuse_timing_gate"] == "PASS"
    assert summary["trace_source_overhead"]["gate"] == "PASS"
    assert all(
        item["gate"] == "PASS"
        for item in summary["trace_source_overhead"]["objectives"]
    )
    assert summary["screening"]["trace_source_overhead"]["gate"] == "PASS"
    assert all(item["gate"] == "PASS" for item in summary["cells"])
    assert summary["screening"]["gate_verdict"] == "PASS"
    assert shadow._m2_summary(cases[:2])["screening"]["gate_verdict"] == "PASS"
    slow_candidate = [
        {**record, "wall_ms": 110.0}
        if record.get("layer") == "full_voyage"
        else record
        for record in timing_candidate
    ]
    slow_case = {
        **cases[0],
        "records": {
            **cases[0]["records"],
            "candidate": [*slow_candidate, *sidecar],
        },
    }
    slow_summary = shadow._m2_summary([slow_case] * 3)
    assert slow_summary["trace_source_overhead"]["gate"] == "FAIL"
    assert slow_summary["gate_verdict"] == "FAIL"
    bad_candidate = [
        {**record, "expanded": 1}
        if record.get("reuse_status") == "HIT_TRACE_EQUIVALENT"
        else record
        for record in timing_candidate
    ]
    bad_case = {
        **cases[0],
        "records": {
            **cases[0]["records"],
            "candidate": [*bad_candidate, *sidecar],
        },
    }
    bad_summary = shadow._m2_summary([bad_case] * 3)
    assert bad_summary["reuse_timing_gate"] == "FAIL"
    assert bad_summary["gate_verdict"] == "FAIL"


def test_prepared_shadow_track_requires_strict_single_track_api() -> None:
    prepared = SimpleNamespace(prepared=SimpleNamespace())
    with pytest.raises(RuntimeError, match="single-track shadow API"):
        shadow._prepared_shadow_track(
            prepared=prepared,
            track="control",
            candidate_mode="control-trace",
        )


def test_prepared_shadow_track_rejects_missing_production_isolation_proof() -> None:
    result = SimpleNamespace(
        outcome=SimpleNamespace(published=False),
        production_published=False,
        scratch_proof=SimpleNamespace(
            production_published=False,
            production_store_unchanged=True,
            production_session_unchanged=False,
            scratch_store_isolated=True,
        ),
    )
    prepared = SimpleNamespace(
        prepared=SimpleNamespace(
            execute_four_layer_temporal_shadow_track=lambda **_: result
        )
    )
    with pytest.raises(RuntimeError, match="production-state isolation"):
        shadow._prepared_shadow_track(
            prepared=prepared,
            track="candidate",
            candidate_mode="control-trace",
        )


@dataclass(frozen=True)
class _Metrics:
    expanded_states: int = 7
    edge_evaluations: int = 0


@dataclass(frozen=True)
class _Diagnostics:
    expanded_labels: int = 7
    edge_evaluations: int = 11


@dataclass(frozen=True)
class _Constraints:
    maximum_elapsed_seconds: float | None = 3600.0
    maximum_risk: float | None = None


def test_control_trace_adapter_traces_full_and_reuses_only_same_goal_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planning_result = SimpleNamespace(metrics=_Metrics())
    trace_identity = SimpleNamespace(goal=(1, 1), digest="i" * 64)
    trace = SimpleNamespace(
        identity=trace_identity,
        digest="d" * 64,
        write_count=5,
        replacement_count=1,
        maximum_inserted_elapsed=1800.0,
        maximum_inserted_path_edge_risk=0.2,
        source_route_digest="r" * 64,
        termination="FIRST_GOAL_POP",
        route_elapsed_seconds=1200.0,
        route_max_edge_risk=0.1,
    )
    trace_calls: list[dict[str, object]] = []
    reuse_calls: list[dict[str, object]] = []

    class FakePlanner:
        risk_identity = SimpleNamespace()
        planner_config = SimpleNamespace()
        risk_as_of_times = ()

        def plan(self, request):
            assert request.objective is ObjectiveMode.RECOMMENDED
            return planning_result

    def fake_trace_plan(planner, request, *, identity):
        trace_calls.append(identity)
        return planning_result, trace

    def fake_try_reuse(trace_arg, planner, request, *, identity):
        reuse_calls.append(identity)
        return SimpleNamespace(
            hit=True,
            status=SimpleNamespace(value="HIT_TRACE_EQUIVALENT"),
            reason=SimpleNamespace(value="HIT"),
            result=planning_result,
            trace=trace_arg,
        )

    monkeypatch.setattr(shadow, "control_trace_plan", fake_trace_plan)
    monkeypatch.setattr(shadow, "try_control_trace_reuse", fake_try_reuse)
    planner = FakePlanner()
    adapter = shadow._ControlTraceAdapter(
        planner,
        window=SimpleNamespace(
            commit_id="risk-window-commit",
            content_digest="c" * 64,
        ),
        input_revision=7,
        generation_id=11,
    )
    request = PlanningRequest(
        start=(0, 0),
        goal=(1, 1),
        departure_time=datetime(2026, 8, 25, tzinfo=UTC),
        maximum_elapsed=timedelta(hours=1),
    )
    full_results = adapter.plan_candidates(request, (ObjectiveMode.RECOMMENDED,))
    main_results = adapter.plan_candidates(
        replace(request, maximum_elapsed=timedelta(minutes=30)),
        (ObjectiveMode.RECOMMENDED,),
    )
    rolling_results = adapter.plan_candidates(
        replace(request, maximum_elapsed=timedelta(minutes=20)),
        (ObjectiveMode.RECOMMENDED,),
    )
    other_goal_results = adapter.plan_candidates(
        replace(request, goal=(2, 2), maximum_elapsed=timedelta(minutes=20)),
        (ObjectiveMode.RECOMMENDED,),
    )
    assert full_results[ObjectiveMode.RECOMMENDED] is planning_result
    assert main_results[ObjectiveMode.RECOMMENDED] is planning_result
    assert rolling_results[ObjectiveMode.RECOMMENDED] is planning_result
    assert other_goal_results[ObjectiveMode.RECOMMENDED] is planning_result
    assert len(trace_calls) == 1
    assert len(reuse_calls) == 1
    assert trace_calls[0]["risk_window_commit_id"] == "risk-window-commit"
    assert trace_calls[0]["risk_window_content_digest"] == "c" * 64
    assert trace_calls[0]["input_revision"] == 7
    assert trace_calls[0]["generation_id"] == 11
    assert reuse_calls[0]["planner_request_identity"]["goal"] == (1, 1)
    assert adapter.records[0]["candidate_algorithm"] == shadow._CONTROL_TRACE_VERSION
    assert adapter.records[0]["reuse_status"] == "TRACE_CAPTURED"
    assert adapter.records[0]["search_used"] is True
    assert adapter.records[1]["reuse_status"] == "HIT_TRACE_EQUIVALENT"
    assert adapter.records[1]["search_used"] is False
    assert adapter.records[1]["zero_search_metrics"] == {
        "expanded_states": 0,
        "edge_evaluations": 0,
    }
    assert adapter.records[2]["reuse_status"] == "COLD_CONTROL"
    assert adapter.records[2]["reuse_lookup_status"] == "NOT_ATTEMPTED"
    assert adapter.records[3]["reuse_status"] == "COLD_CONTROL"
    assert adapter.records[3]["reuse_lookup_status"] == "NOT_ATTEMPTED"


def test_candidate_adapter_records_zero_search_on_p2_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planning_result = SimpleNamespace(metrics=_Metrics())
    candidate_result = SimpleNamespace(
        planning_result=planning_result,
        diagnostics=_Diagnostics(),
    )
    source_result = SimpleNamespace(diagnostics=_Diagnostics())
    proof = SimpleNamespace(
        certificate_status="CERTIFIED_REUSABLE",
        U=1.0,
        LB=1.0,
        epsilon=1e-12,
        open_termination=SimpleNamespace(value="OPEN_BOUND"),
        state_digest="s" * 64,
        route_digest="r" * 64,
        source_constraints=_Constraints(),
    )
    certificate = SimpleNamespace(
        certificate=proof,
        checkpoint=SimpleNamespace(identity=SimpleNamespace(session_id="source-session")),
        result=source_result,
    )
    outcome = SimpleNamespace(
        hit=True,
        status=SimpleNamespace(value="HIT_MONOTONIC"),
        fallback_reason="MONOTONIC_TIGHTENING",
        result=candidate_result,
    )

    class FakePlanner:
        risk_identity = SimpleNamespace()
        planner_config = SimpleNamespace()
        risk_as_of_times = ()

        def __init__(self) -> None:
            self.created = 0

        def create_session(self, request, *, identity):
            self.created += 1
            return SimpleNamespace(
                session_id="source-session",
                state=SimpleNamespace(value="GOAL_CERTIFIED"),
            )

        def advance_session(self, session):
            return source_result

    planner = FakePlanner()
    adapter = shadow._CandidateAdapter(
        planner,
        window=SimpleNamespace(content_digest="c" * 64, commit_id="risk-window-sha256-" + "c" * 64),
        input_revision=1,
        maximum_elapsed=timedelta(hours=2),
    )
    adapter._identity = lambda request: SimpleNamespace(session_id="source-session")
    monkeypatch.setattr(shadow, "certify_session", lambda session: certificate)
    monkeypatch.setattr(shadow, "try_reuse", lambda *args, **kwargs: outcome)
    request = PlanningRequest(
        start=(0, 0),
        goal=(1, 1),
        departure_time=datetime(2026, 8, 25, tzinfo=UTC),
        maximum_elapsed=timedelta(hours=1),
    )

    results = adapter.plan_candidates(request, (ObjectiveMode.RECOMMENDED,))

    assert results[ObjectiveMode.RECOMMENDED] is planning_result
    assert planner.created == 1
    assert adapter.records[0]["reuse_status"] == "HIT_MONOTONIC"
    assert adapter.records[0]["search_used"] is False
    assert adapter.records[0]["zero_search_metrics"] == {
        "expanded_labels": 0,
        "edge_evaluations": 0,
    }


def test_run_track_passes_the_service_request(monkeypatch: pytest.MonkeyPatch) -> None:
    request = SimpleNamespace()
    observed: list[object] = []

    class FakeService:
        def execute(self, actual_request):
            observed.append(actual_request)
            return "outcome"

    monkeypatch.setattr(shadow, "_service", lambda *args, **kwargs: FakeService())
    outcome, records = shadow._run_track(
        planner=SimpleNamespace(),
        request=request,
        configuration=SimpleNamespace(),
        planner_version="test",
    )

    assert outcome == "outcome"
    assert records == []
    assert observed == [request]
