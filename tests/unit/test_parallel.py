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
