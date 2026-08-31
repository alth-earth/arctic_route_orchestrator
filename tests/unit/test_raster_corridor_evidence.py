from __future__ import annotations

import json

from arctic_route_orchestrator.replay.raster_corridor_evidence import (
    METHOD,
    evaluate_raster_corridor_evidence,
)


def _metadata() -> dict:
    return {
        "coordinate_frame": "local_equirectangular_east_north_m",
        "origin_x_m": 0.0,
        "origin_y_m": 0.0,
        "cell_size_m": 10.0,
        "rows": 3,
        "cols": 3,
    }


def _hull() -> list[list[float]]:
    return [[12.0, 12.0], [18.0, 12.0], [18.0, 18.0], [12.0, 18.0]]


def test_raster_resolution_evidence_enumerates_intersecting_sea_cells() -> None:
    cells = {(row, column): {"status": "SEA", "coverage_complete": True}
             for row in range(3) for column in range(3)}

    evidence = evaluate_raster_corridor_evidence(
        _metadata(), cells, [_hull()], expansion_m=0.0
    )

    assert evidence["accepted"] is True
    assert evidence["method"] == METHOD == "RASTER_RESOLUTION_CONTAINMENT"
    assert evidence["continuous_containment_proved"] is False
    assert evidence["raster_resolution_containment_proved"] is True
    assert evidence["continuous_containment_scope"].startswith("supplied_raster_resolution")
    assert evidence["enumerated_cells"] == [[1, 1]]
    json.dumps(evidence)


def test_land_cell_rejects_containment() -> None:
    cells = {(row, column): "SEA" for row in range(3) for column in range(3)}
    cells[(1, 1)] = "LAND"

    evidence = evaluate_raster_corridor_evidence(
        _metadata(), cells, [_hull()], expansion_m=0.0
    )

    assert evidence["accepted"] is False
    assert evidence["land_cells"] == [[1, 1]]
    assert evidence["continuous_containment_proved"] is False


def test_unknown_cell_rejects_containment() -> None:
    cells = {(row, column): "SEA" for row in range(3) for column in range(3)}
    cells[(1, 1)] = "UNKNOWN"

    evidence = evaluate_raster_corridor_evidence(
        _metadata(), cells, [_hull()], expansion_m=0.0
    )

    assert evidence["accepted"] is False
    assert evidence["unknown_cells"] == [[1, 1]]


def test_missing_cell_is_missing_coverage_and_rejects_containment() -> None:
    cells = {(row, column): "SEA" for row in range(3) for column in range(3)}
    del cells[(1, 1)]

    evidence = evaluate_raster_corridor_evidence(
        _metadata(), cells, [_hull()], expansion_m=0.0
    )

    assert evidence["accepted"] is False
    assert evidence["missing_coverage_cells"] == [[1, 1]]
    assert evidence["coverage_complete"] is False
