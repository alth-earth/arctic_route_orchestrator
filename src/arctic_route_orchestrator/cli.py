"""Command-line entrypoint for formal artifact intake and A-B-C execution."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from arctic_route_orchestrator import __version__
from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.intake import ArtifactIntake
from arctic_route_orchestrator.models import ExecutionSpec
from arctic_route_orchestrator.service import RunPaths
from arctic_route_orchestrator.timeout_runner import run_with_timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arctic-route-orchestrator",
        description="只通过公共合同运行 A→B→C；当前系统仅用于科研演示。",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    intake = subparsers.add_parser("intake", help="验证外部正式 A bundle/RunContext")
    intake.add_argument("--bundle", required=True)
    intake.add_argument("--run-context", required=True)
    intake.add_argument(
        "--execution-spec",
        type=Path,
        help="optionally bind strict ExecutionSpec identity during intake-only validation",
    )
    intake.add_argument("--a-data-root", required=True)
    intake.add_argument("--generation-id", required=True, type=int)
    run = subparsers.add_parser("run", help="执行正式 A→B→C 与 6 h 同代次重规划")
    run.add_argument("--execution-spec", required=True, type=Path)
    run.add_argument("--bundle", required=True, type=Path)
    run.add_argument("--run-context", type=Path)
    run.add_argument("--a-data-root", required=True, type=Path)
    run.add_argument("--b-config", required=True, type=Path)
    run.add_argument("--c-config-root", required=True, type=Path)
    run.add_argument("--contracts-config-root", type=Path)
    run.add_argument("--risk-store-root", required=True, type=Path)
    run.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "intake":
            spec = (
                ExecutionSpec.from_path(args.execution_spec)
                if args.execution_spec is not None
                else None
            )
            if spec is not None and spec.generation_id != args.generation_id:
                raise OrchestrationError(
                    "execution_spec_invalid",
                    "ExecutionSpec generation_id differs from intake generation_id",
                )
            result = ArtifactIntake.validate(
                bundle_path=args.bundle,
                run_context_path=args.run_context,
                a_data_root=args.a_data_root,
                generation_id=args.generation_id,
                scenario_id=spec.scenario_id if spec is not None else None,
                run_id=spec.run_id if spec is not None else None,
                created_at=spec.generated_at if spec is not None else None,
            )
            response = {"ok": True, **asdict(result.report)}
            if spec is not None:
                response.update(
                    {
                        "execution_spec_validated": True,
                        "planning_contract": spec.planning_contract,
                    }
                )
        else:
            spec = ExecutionSpec.from_path(args.execution_spec)
            result = run_with_timeout(
                spec,
                RunPaths(
                    bundle_path=args.bundle,
                    run_context_path=args.run_context,
                    a_data_root=args.a_data_root,
                    b_config_path=args.b_config,
                    c_config_root=args.c_config_root,
                    contracts_config_root=args.contracts_config_root,
                    risk_store_root=args.risk_store_root,
                    output_dir=args.output_dir,
                ),
            )
            response = {
                "ok": True,
                "output_dir": str(result["output_dir"]),
                "run_id": result["run_id"],
                "planning_contract": result["planning_contract"],
                "digests": result["digests"],
            }
    except OrchestrationError as exc:
        print(
            json.dumps(
                {"ok": False, "error_code": exc.code, "message": exc.message},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(response, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
