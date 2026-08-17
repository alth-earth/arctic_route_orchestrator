"""CLI run subcommand consumes the worker result payload correctly."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arctic_route_orchestrator import cli
from arctic_route_orchestrator.errors import OrchestrationError
from arctic_route_orchestrator.models import ExecutionSpec


def _run_args(spec_path: str) -> list[str]:
    return [
        "run",
        "--execution-spec",
        spec_path,
        "--bundle",
        "b.json",
        "--a-data-root",
        "a",
        "--b-config",
        "b.json",
        "--c-config-root",
        "c",
        "--risk-store-root",
        "r",
        "--output-dir",
        "o",
    ]


def _write_spec(tmp_path: Path) -> str:
    spec = ExecutionSpec(
        schema_version="orchestrator.execution-spec.v1",
        run_id="run-00000000-0000-4000-8000-000000000077",
        scenario_id="test_scenario_v1",
        generation_id=0,
        input_revision=0,
        generated_at=datetime(2026, 8, 17, tzinfo=UTC),
        planning_contract="cd.four-layer-route-plan-set.v3",
    )
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec.to_document()), encoding="utf-8")
    return str(path)


def test_run_cli_handles_dict_result_from_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    payload = {
        "ok": True,
        "output_dir": "/tmp/out",
        "run_id": "run-00000000-0000-4000-8000-000000000077",
        "planning_contract": "cd.four-layer-route-plan-set.v3",
        "digests": {"dataset_bundle_id": "a-bundle-x"},
    }

    def fake_run(spec, paths):
        return payload

    monkeypatch.setattr(cli, "run_with_timeout", fake_run)
    assert cli.main(_run_args(_write_spec(tmp_path))) == 0


def test_run_cli_failure_payload_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fake_run(spec, paths):
        raise OrchestrationError("boom", "boom")

    monkeypatch.setattr(cli, "run_with_timeout", fake_run)
    assert cli.main(_run_args(_write_spec(tmp_path))) == 2
