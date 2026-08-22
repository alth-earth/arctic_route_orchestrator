"""The intake-only CLI can bind a strict ExecutionSpec without running B/C."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from arctic_route_orchestrator import cli
from arctic_route_orchestrator.intake import ArtifactIntakeReport
from arctic_route_orchestrator.models import ExecutionSpec


def _write_spec(tmp_path: Path, *, generation_id: int = 0) -> Path:
    spec = ExecutionSpec(
        schema_version="orchestrator.execution-spec.v1",
        run_id="run-00000000-0000-4000-8000-000000000088",
        scenario_id="winter_scenario_v1",
        generation_id=generation_id,
        input_revision=0,
        generated_at=datetime(2026, 8, 22, 17, 9, tzinfo=UTC),
        planning_contract="cd.four-layer-route-plan-set.v3",
    )
    path = tmp_path / "execution-spec.json"
    path.write_text(json.dumps(spec.to_document()), encoding="utf-8")
    return path


def _args(spec_path: Path, *, generation_id: int = 0) -> list[str]:
    return [
        "intake",
        "--bundle",
        "bundle.json",
        "--run-context",
        "run-context.json",
        "--execution-spec",
        str(spec_path),
        "--a-data-root",
        "a-data",
        "--generation-id",
        str(generation_id),
    ]


def test_intake_cli_binds_execution_spec_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_validate(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            report=ArtifactIntakeReport(
                bundle_id="a-bundle-test",
                bundle_digest="a" * 64,
                run_id="run-00000000-0000-4000-8000-000000000088",
                corridor_id="corridor",
                horizon_hours=144,
                requested_data_types=("wind_field",),
                record_count=1,
                generation_id=0,
                knowledge_as_of="2026-08-22T12:00:00Z",
            )
        )

    monkeypatch.setattr(cli.ArtifactIntake, "validate", staticmethod(fake_validate))

    assert cli.main(_args(_write_spec(tmp_path))) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_spec_validated"] is True
    assert payload["planning_contract"] == "cd.four-layer-route-plan-set.v3"
    assert captured["scenario_id"] == "winter_scenario_v1"
    assert captured["run_id"] == "run-00000000-0000-4000-8000-000000000088"
    assert captured["created_at"] == datetime(2026, 8, 22, 17, 9, tzinfo=UTC)


def test_intake_cli_rejects_generation_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(_args(_write_spec(tmp_path, generation_id=1))) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error_code"] == "execution_spec_invalid"
