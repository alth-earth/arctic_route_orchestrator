"""Interruptible per-stage timeout via an isolated worker process.

``per_stage_timeout_seconds`` must be able to stop a CPU-bound stage instead of
only being checked after the stage returns.  The orchestrator therefore runs
the formal chain inside a worker subprocess and enforces the timeout from the
parent using stage heartbeats.  On timeout the worker is terminated (SIGTERM,
then SIGKILL after a grace period), a TIMEOUT stage report is written, and no
partial formal v3 artifacts are published.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.service import RunPaths


class StageTimeoutError(OrchestrationError):
    def __init__(
        self,
        *,
        stage: str,
        elapsed: float,
        timeout: float,
        last_progress: str | None,
    ) -> None:
        self.stage = stage
        self.elapsed = elapsed
        self.timeout = timeout
        self.last_progress = last_progress
        message = (
            f"stage '{stage}' exceeded {timeout:.0f}s timeout "
            f"(elapsed {elapsed:.1f}s)"
        )
        if last_progress:
            message += f" | last progress: {last_progress}"
        super().__init__("stage_timeout", message)


def _run_paths_to_dict(paths: RunPaths) -> dict[str, str]:
    return {
        name: str(getattr(paths, name))
        for name in (
            "bundle_path",
            "a_data_root",
            "b_config_path",
            "c_config_root",
            "risk_store_root",
            "output_dir",
            "run_context_path",
            "contracts_config_root",
        )
    }


def _default_worker_command(
    spec_path: Path,
    paths_json: str,
    result_path: Path,
) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "--orchestrator-stage-worker",
            str(spec_path),
            paths_json,
            str(result_path),
        ]
    return [
        sys.executable,
        "-m",
        "arctic_route_orchestrator.stage_worker",
        str(spec_path),
        paths_json,
        str(result_path),
    ]


def run_with_timeout(
    spec: ExecutionSpec,
    paths: RunPaths,
    *,
    worker_cmd_factory: Callable[[Path], list[str]] | None = None,
    kill_grace_seconds: float = 10.0,
) -> dict[str, Any]:
    """Run the formal chain in a worker and enforce per-stage wall-clock timeout."""

    result_dir = paths.output_dir.parent
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / f".worker-{spec.run_id}.result.json"
    if result_path.exists():
        result_path.unlink()

    if worker_cmd_factory is None:
        paths_json = json.dumps(_run_paths_to_dict(paths), sort_keys=True)
        spec_path = result_dir / f".worker-{spec.run_id}.spec.json"
        spec_path.write_text(
            json.dumps(spec.to_document(), sort_keys=True), encoding="utf-8"
        )
        def worker_cmd_factory(_result: Path) -> list[str]:
            return _default_worker_command(spec_path, paths_json, _result)
    worker_cmd = worker_cmd_factory(result_path)

    heartbeat_events: list[dict[str, Any]] = []
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def read_stream(stream: Any, sink: list[str]) -> None:
        for line in iter(stream.readline, ""):
            line = line.rstrip("\n")
            if not line:
                continue
            sink.append(line)
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and "event" in parsed:
                    heartbeat_events.append(parsed)
            except (ValueError, TypeError):
                pass

    proc = subprocess.Popen(
        worker_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_thread = threading.Thread(
        target=read_stream, args=(proc.stdout, stdout_lines), daemon=True
    )
    stderr_thread = threading.Thread(
        target=read_stream, args=(proc.stderr, stderr_lines), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    current_stage: str | None = None
    stage_started: float | None = None
    completed_stages: list[dict[str, Any]] = []
    started = time.monotonic()
    debug = os.environ.get("ORCH_DEBUG_TIMEOUT") == "1"

    while proc.poll() is None:
        # consume heartbeat events
        while heartbeat_events:
            event = heartbeat_events.pop(0)
            if debug:
                print(
                    f"[orch-debug] event={event.get('event')} stage={event.get('stage')} "
                    f"backlog={len(heartbeat_events)}",
                    file=sys.stderr,
                    flush=True,
                )
            if event.get("event") == "stage_start":
                current_stage = event.get("stage")
                stage_started = time.monotonic()
            elif event.get("event") == "stage_done":
                if current_stage == event.get("stage") and stage_started is not None:
                    completed_stages.append(
                        {
                            "stage": current_stage,
                            "status": "completed",
                            "duration_seconds": float(
                                event.get("duration_seconds", time.monotonic() - stage_started)
                            ),
                        }
                    )
                current_stage = None
                stage_started = None
        if (
            current_stage is not None
            and stage_started is not None
            and time.monotonic() - stage_started > spec.per_stage_timeout_seconds
        ):
            if debug:
                print(
                    f"[orch-debug] FIRING timeout current={current_stage} "
                    f"elapsed={time.monotonic() - stage_started:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
            _terminate(proc, worker_cmd)
            _write_timeout_report(
                spec=spec,
                paths=paths,
                stage=current_stage,
                elapsed=time.monotonic() - stage_started,
                completed=completed_stages,
                last_progress=_last_astar_line(stderr_lines),
            )
            raise StageTimeoutError(
                stage=current_stage,
                elapsed=time.monotonic() - stage_started,
                timeout=spec.per_stage_timeout_seconds,
                last_progress=_last_astar_line(stderr_lines),
            )
        if debug and current_stage is not None and stage_started is not None:
            print(
                f"[orch-debug] stage={current_stage} "
                f"elapsed={time.monotonic() - stage_started:.1f}s "
                f"timeout={spec.per_stage_timeout_seconds:.0f}s "
                f"backlog={len(heartbeat_events)}",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(0.25)

    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    if result_path.exists():
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if payload.get("ok"):
            return payload
        _write_timeout_report(
            spec=spec,
            paths=paths,
            stage="worker",
            elapsed=time.monotonic() - started,
            completed=completed_stages,
            last_progress=_last_astar_line(stderr_lines),
            failed_code=payload.get("error_code", "worker_failed"),
            failed_message=payload.get("message", "worker failed"),
        )
        raise OrchestrationError(
            payload.get("error_code", "worker_failed"),
            payload.get("message", "worker failed"),
        )
    raise OrchestrationError("worker_crash", "worker exited without a result file")


def _terminate(proc: subprocess.Popen, cmd: list[str], grace: float = 10.0) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _last_astar_line(lines: list[str]) -> str | None:
    for line in reversed(lines):
        if "[astar]" in line:
            return line.strip()
    return None


def _write_timeout_report(
    *,
    spec: ExecutionSpec,
    paths: RunPaths,
    stage: str,
    elapsed: float,
    completed: list[dict[str, Any]],
    last_progress: str | None,
    failed_code: str | None = None,
    failed_message: str | None = None,
) -> None:
    report_path = paths.output_dir / "run-stage-report.json"
    if report_path.exists():
        return
    status = "TIMEOUT" if failed_code is None else "failed"
    stages = [
        {
            "schema_version": "orchestrator.stage-record.v1",
            "run_id": spec.run_id,
            "stage": item["stage"],
            "started_at": datetime.now(UTC).isoformat(),
            "duration_seconds": round(float(item["duration_seconds"]), 3),
            "status": item["status"],
        }
        for item in completed
    ]
    if failed_code is None:
        stages.append(
            {
                "schema_version": "orchestrator.stage-record.v1",
                "run_id": spec.run_id,
                "stage": stage,
                "started_at": datetime.now(UTC).isoformat(),
                "duration_seconds": round(float(elapsed), 3),
                "status": "TIMEOUT",
            }
        )
    document = {
        "schema_version": "orchestrator.stage-report.v1",
        "run_id": spec.run_id,
        "status": status,
        "per_stage_timeout_seconds": spec.per_stage_timeout_seconds,
        "error_code": failed_code or "stage_timeout",
        "error_message": failed_message or f"stage '{stage}' timed out after {elapsed:.1f}s",
        "last_progress": last_progress,
        "stages": stages,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    staging = report_path.with_suffix(report_path.suffix + ".tmp")
    staging.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(staging, report_path)
