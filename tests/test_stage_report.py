from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.service import (
    _append_stage_record,
    _check_stage_timeout,
    _write_stage_report,
)


def _spec(*, timeout: float = 900.0) -> ExecutionSpec:
    return ExecutionSpec(
        schema_version="orchestrator.execution-spec.v1",
        run_id="run-00000000-0000-4000-8000-000000000009",
        scenario_id="tromso_isfjorden_august_2026_demo_v1",
        generation_id=0,
        input_revision=0,
        generated_at=datetime(2026, 8, 15, tzinfo=UTC),
        planning_contract="cd.route-plan.v2",
        max_snap_km=150.0,
        replan_after_hours=6,
        per_stage_timeout_seconds=timeout,
    )


def test_stage_record_append_and_report_persist(tmp_path) -> None:
    spec = _spec()
    records: list[dict[str, object]] = []
    started = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
    _append_stage_record(
        stage_records=records,
        spec=spec,
        stage="b_build",
        started_at=started,
        duration_seconds=12.345,
        status="completed",
    )
    assert records[0]["stage"] == "b_build"
    assert records[0]["duration_seconds"] == 12.345
    assert records[0]["status"] == "completed"

    report_path = tmp_path / "out" / "run-stage-report.json"
    _write_stage_report(report_path, spec, records, "failed", "stage_timeout", "boom")
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == "orchestrator.stage-report.v1"
    assert document["run_id"] == spec.run_id
    assert document["status"] == "failed"
    assert document["error_code"] == "stage_timeout"
    assert document["stages"][0]["stage"] == "b_build"
    assert not report_path.with_suffix(".json.tmp").exists()


def test_stage_timeout_raises_and_preserves_report(tmp_path) -> None:
    spec = _spec(timeout=5.0)
    with pytest.raises(OrchestrationError, match="stage_timeout"):
        _check_stage_timeout(spec, 6.0, "c_initial_planning")

    records: list[dict[str, object]] = []
    started = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
    _append_stage_record(
        stage_records=records,
        spec=spec,
        stage="c_initial_planning",
        started_at=started,
        duration_seconds=6.0,
        status="failed",
    )
    report_path = tmp_path / "out" / "run-stage-report.json"
    _write_stage_report(
        report_path,
        spec,
        records,
        "failed",
        "stage_timeout",
        "stage exceeded timeout",
    )
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert document["stages"][0]["stage"] == "c_initial_planning"
    assert document["stages"][0]["status"] == "failed"


def test_stage_report_does_not_overwrite_existing(tmp_path) -> None:
    report_path = tmp_path / "run-stage-report.json"
    report_path.write_text("already-published", encoding="utf-8")
    _write_stage_report(report_path, _spec(), [], "completed")
    assert report_path.read_text(encoding="utf-8") == "already-published"
