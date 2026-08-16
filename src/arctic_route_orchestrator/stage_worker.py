"""Worker process entrypoint for interruptible stage execution."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.service import RunPaths, execute_formal_run


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 3:
        print("usage: stage_worker <spec.json> <paths.json> <result.json>", file=sys.stderr)
        return 2
    spec_path, paths_json, result_path = args
    spec = ExecutionSpec.from_path(spec_path)
    paths = RunPaths(
        **{
            name: Path(value)
            for name, value in json.loads(paths_json).items()
        }
    )

    def heartbeat(event: dict[str, object]) -> None:
        print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)

    try:
        result = execute_formal_run(spec, paths, heartbeat=heartbeat)
        payload = {
            "ok": True,
            "output_dir": str(result.output_dir),
            "run_id": result.report["identity"]["run_id"],
            "planning_contract": result.report["planning_contract"],
            "digests": result.report["digests"],
        }
        Path(result_path).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return 0
    except OrchestrationError as exc:
        payload = {"ok": False, "error_code": exc.code, "message": exc.message}
        Path(result_path).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        payload = {
            "ok": False,
            "error_code": "worker_crash",
            "message": f"{type(exc).__name__}: {exc}",
        }
        Path(result_path).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
