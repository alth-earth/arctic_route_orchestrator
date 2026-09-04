from __future__ import annotations

from pathlib import Path

import pytest
from arctic_route_planning.ingress import PreparedRiskPlanning

from arctic_route_orchestrator.replay import parallel


def test_install_restores_c_private_planner_and_exposes_profile(tmp_path: Path) -> None:
    original = PreparedRiskPlanning._private_planner

    with parallel.install(
        workers=2,
        risk_store_root=tmp_path / "risk-store",
        c_config_root=tmp_path / "c-config",
        contracts_config_root=tmp_path / "contracts-config",
        pool_mode="percall",
    ):
        assert PreparedRiskPlanning._private_planner is not original
        telemetry = parallel.snapshot_telemetry()
        assert telemetry["enabled"] is True
        assert telemetry["requested_workers"] == 2
        assert telemetry["pool_mode"] == "percall"
        assert telemetry["parallel_active"] is False

    assert PreparedRiskPlanning._private_planner is original


def test_install_passes_named_profile_and_digest_to_worker_envelope(tmp_path: Path) -> None:
    with parallel.install(
        workers=1,
        risk_store_root=tmp_path / "risk-store",
        c_config_root=tmp_path / "c-config",
        contracts_config_root=tmp_path / "contracts-config",
        pool_mode="percall",
        planner_name="winter_motion_reserve_5pct",
        replanning_name="winter_viewer_dynamic",
        planner_config_digest="a" * 64,
    ):
        assert parallel._active_paths is not None
        assert parallel._active_paths["planner_name"] == "winter_motion_reserve_5pct"
        assert parallel._active_paths["replanning_name"] == "winter_viewer_dynamic"
        assert parallel._active_paths["planner_config_digest"] == "a" * 64


def test_worker_configuration_uses_named_profiles(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_load_configuration(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(parallel, "load_configuration", fake_load_configuration)
    result = parallel._load_worker_configuration(
        {
            "c_config_root": "/configs/c",
            "contracts_config_root": "/configs/contracts",
            "planner_name": "winter_motion_reserve_5pct",
            "replanning_name": "winter_viewer_dynamic",
        },
        "winter-scenario",
    )

    assert result is not None
    assert observed["args"] == ("/configs/c", "winter-scenario")
    assert observed["kwargs"] == {
        "shared_config_root": Path("/configs/contracts"),
        "planner_name": "winter_motion_reserve_5pct",
        "replanning_name": "winter_viewer_dynamic",
    }


def test_worker_configuration_preserves_legacy_default_when_selectors_absent(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_load_configuration(*args, **kwargs):
        observed["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(parallel, "load_configuration", fake_load_configuration)
    parallel._load_worker_configuration(
        {
            "c_config_root": "/configs/c",
            "contracts_config_root": "/configs/contracts",
        },
        "scenario",
    )

    assert observed["kwargs"] == {
        "shared_config_root": Path("/configs/contracts"),
        "planner_name": "default",
        "replanning_name": "default",
    }


@pytest.mark.parametrize("workers", (0, -1, True))
def test_install_rejects_invalid_worker_count(tmp_path: Path, workers: object) -> None:
    with pytest.raises(ValueError, match="positive integer"), parallel.install(
        workers=workers,  # type: ignore[arg-type]
        risk_store_root=tmp_path / "risk-store",
        c_config_root=tmp_path / "c-config",
        contracts_config_root=tmp_path / "contracts-config",
    ):
        pass


def test_install_rejects_unknown_pool_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="persistent or percall"), parallel.install(
        workers=1,
        risk_store_root=tmp_path / "risk-store",
        c_config_root=tmp_path / "c-config",
        contracts_config_root=tmp_path / "contracts-config",
        pool_mode="unknown",
    ):
        pass
