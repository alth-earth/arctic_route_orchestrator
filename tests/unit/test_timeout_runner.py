"""Interruptible per-stage timeout: worker watchdog semantics."""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arctic_route_orchestrator import timeout_runner
from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.service import RunPaths
from arctic_route_orchestrator.timeout_runner import StageTimeoutError, run_with_timeout


def test_default_worker_command_uses_frozen_executable_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(timeout_runner.sys, "frozen", True, raising=False)
    command = timeout_runner._default_worker_command(
        tmp_path / "spec.json", "{}", tmp_path / "result.json"
    )
    assert command[:2] == [timeout_runner.sys.executable, "--orchestrator-stage-worker"]


def _spec(per_stage_timeout: float) -> ExecutionSpec:
    return ExecutionSpec(
        schema_version="orchestrator.execution-spec.v1",
        run_id="run-00000000-0000-4000-8000-000000000001",
        scenario_id="test_scenario_v1",
        generation_id=0,
        input_revision=0,
        generated_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
        planning_contract="cd.four-layer-route-plan-set.v3",
        per_stage_timeout_seconds=per_stage_timeout,
    )


def _paths(tmp: Path) -> RunPaths:
    return RunPaths(
        bundle_path=tmp / "bundle.json",
        a_data_root=tmp / "a-data",
        b_config_path=tmp / "b.json",
        c_config_root=tmp / "c-configs",
        risk_store_root=tmp / "risk-store",
        output_dir=tmp / "output",
        run_context_path=tmp / "run-context.json",
        contracts_config_root=tmp / "contracts",
    )


def _normal_worker(result_path: Path) -> list[str]:
    code = (
        "import json,sys,time;"
        "print(json.dumps({'event':'stage_start','stage':'c_initial_planning'}), flush=True);"
        "time.sleep(0.2);"
        "print(json.dumps({'event':'stage_done','stage':'c_initial_planning',"
        "'duration_seconds':0.2}), flush=True);"
        f"open({str(result_path)!r},'w').write(json.dumps({{'ok':True,'run_id':'x','planning_contract':'p','digests':{{}}}}));"
    )
    return [sys.executable, "-u", "-c", code]


def _slow_worker(result_path: Path, pid_file: Path) -> list[str]:
    code = (
        "import json,os,sys,time;"
        f"open({str(pid_file)!r},'w').write(str(os.getpid()));"
        "print(json.dumps({'event':'stage_start','stage':'c_initial_planning'}), flush=True);"
        "print('[astar] obj=fastest expanded=123 rate=300/s', file=sys.stderr, flush=True);"
        "time.sleep(60)"
    )
    return [sys.executable, "-u", "-c", code]


def test_normal_stage_completes_before_timeout(tmp_path: Path) -> None:
    spec = _spec(5.0)
    paths = _paths(tmp_path)
    payload = run_with_timeout(
        spec, paths, worker_cmd_factory=_normal_worker
    )
    assert payload["ok"] is True
    assert not (paths.output_dir / "run-stage-report.json").exists()


def test_slow_stage_times_out_with_report_and_no_orphan(tmp_path: Path) -> None:
    spec = _spec(1.0)
    paths = _paths(tmp_path)
    pid_file = tmp_path / "worker.pid"

    def factory(result_path: Path) -> list[str]:
        return _slow_worker(result_path, pid_file)

    with pytest.raises(StageTimeoutError) as excinfo:
        run_with_timeout(spec, paths, worker_cmd_factory=factory, kill_grace_seconds=2.0)
    assert excinfo.value.stage == "c_initial_planning"
    assert excinfo.value.timeout == 1.0
    assert "last progress" in str(excinfo.value.message)

    report_path = paths.output_dir / "run-stage-report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "TIMEOUT"
    assert report["error_code"] == "stage_timeout"
    assert report["per_stage_timeout_seconds"] == 1.0
    assert report["last_progress"] and "[astar]" in report["last_progress"]
    assert any(stage["status"] == "TIMEOUT" for stage in report["stages"])

    # no orphan worker process
    pid = int(pid_file.read_text().strip())
    deadline = time.time() + 10
    while time.time() < deadline:
        if not Path(f"/proc/{pid}").exists():
            break
        time.sleep(0.2)
    assert not Path(f"/proc/{pid}").exists(), "worker process still alive"

    # no partial formal artifact (worker never wrote output)
    assert not (paths.output_dir / "routes").exists()


def test_failed_worker_writes_failed_report(tmp_path: Path) -> None:
    spec = _spec(5.0)
    paths = _paths(tmp_path)

    def failing(result_path: Path) -> list[str]:
        code = (
            "import json,sys;"
            f"open({str(result_path)!r},'w').write(json.dumps({{'ok':False,'error_code':'boom','message':'boom'}}));"
        )
        return [sys.executable, "-c", code]

    with pytest.raises(OrchestrationError) as excinfo:
        run_with_timeout(spec, paths, worker_cmd_factory=failing)
    assert excinfo.value.code == "boom"
    report = json.loads(
        (paths.output_dir / "run-stage-report.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "failed"
    assert report["error_code"] == "boom"
