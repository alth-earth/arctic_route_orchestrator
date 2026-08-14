from __future__ import annotations

import json

import pytest

from arctic_route_orchestrator.errors import ArtifactIntakeError
from arctic_route_orchestrator.intake import ArtifactIntake


def test_intake_rejects_legacy_bundle_before_archive_access(tmp_path) -> None:
    bundle_path = tmp_path / "bundle.json"
    context_path = tmp_path / "context.json"
    bundle_path.write_text(json.dumps({"schema_version": "a.dataset-bundle.v1"}), encoding="utf-8")
    context_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ArtifactIntakeError) as captured:
        ArtifactIntake.validate(
            bundle_path=bundle_path,
            run_context_path=context_path,
            a_data_root=tmp_path / "archive",
            generation_id=0,
        )

    assert captured.value.code == "a_artifact_legacy"
