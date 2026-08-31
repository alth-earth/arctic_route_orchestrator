"""Caller-owned, raster-resolution corridor evidence for research paths.

This module deliberately has no knowledge of A/B data structures and never
opens a file.  A caller supplies the raster geometry, the available cell
values, and the already-built convex hull for every curve span.  The result
is therefore evidence about the supplied raster resolution only; it is not a
continuous geospatial proof between raster cells.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

METHOD = "RASTER_RESOLUTION_CONTAINMENT"
_SEA_VALUES = frozenset({"SEA", "WATER", "OCEAN", "VALID_SEA", "VALID_WATER"})
_LAND_VALUES = frozenset({"LAND", "COAST", "OBSTACLE"})
_UNKNOWN_VALUES = frozenset(
    {"UNKNOWN", "MISSING", "DATA_UNAVAILABLE", "UNAVAILABLE", "INVALID"}
)
_COVERED_VALUES = frozenset({"AVAILABLE", "COVERED", "COMPLETE", "VALID", "PRESENT"})


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bounds(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, Mapping):
        nested = value.get("bounds", value.get("bbox"))
        if nested is not None and nested is not value:
            return _bounds(nested)
        aliases = (
            ("min_x", "min_y", "max_x", "max_y"),
            ("xmin", "ymin", "xmax", "ymax"),
            ("west", "south", "east", "north"),
        )
        for keys in aliases:
            if all(key in value for key in keys):
                parsed = tuple(_number(value[key]) for key in keys)
                if all(item is not None for item in parsed):
                    lower_x, lower_y, upper_x, upper_y = parsed
                    if lower_x <= upper_x and lower_y <= upper_y:
                        return lower_x, lower_y, upper_x, upper_y
                return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 4:
            return None
        parsed = tuple(_number(item) for item in value)
        if all(item is not None for item in parsed):
            lower_x, lower_y, upper_x, upper_y = parsed
            if lower_x <= upper_x and lower_y <= upper_y:
                return lower_x, lower_y, upper_x, upper_y
    return None


def _cell_key(value: Any) -> tuple[str, Any]:
    if isinstance(value, (tuple, list)):
        return "index", tuple(value)
    return "id", str(value)


def _public_cell_id(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return list(value)
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return str(value)


def _cell_identifier(record: Any, fallback: Any = None) -> Any:
    if isinstance(record, Mapping):
        if "cell_id" in record:
            return record["cell_id"]
        if "id" in record:
            return record["id"]
        if "row" in record and "col" in record:
            return (record["row"], record["col"])
    return fallback


def _regular_grid_bounds(metadata: Mapping[str, Any]) -> dict[tuple[str, Any], dict[str, Any]]:
    origin = metadata.get("origin")
    if isinstance(origin, Mapping):
        origin_x = _number(origin.get("x", origin.get("lon")))
        origin_y = _number(origin.get("y", origin.get("lat")))
    else:
        origin_x = _number(metadata.get("origin_x_m", metadata.get("origin_x")))
        origin_y = _number(metadata.get("origin_y_m", metadata.get("origin_y")))

    cell_size = metadata.get("cell_size_m", metadata.get("cell_size"))
    if isinstance(cell_size, Mapping):
        width = _number(cell_size.get("x", cell_size.get("width")))
        height = _number(cell_size.get("y", cell_size.get("height")))
    else:
        width = _number(metadata.get("cell_width_m", cell_size))
        height = _number(metadata.get("cell_height_m", cell_size))

    shape = metadata.get("shape")
    if isinstance(shape, Mapping):
        rows = shape.get("rows", shape.get("height"))
        columns = shape.get("cols", shape.get("columns", shape.get("width")))
    else:
        rows = metadata.get("rows", metadata.get("height"))
        columns = metadata.get("cols", metadata.get("columns", metadata.get("width")))
    try:
        row_count = int(rows)
        column_count = int(columns)
    except (TypeError, ValueError):
        return {}
    if (
        origin_x is None
        or origin_y is None
        or width is None
        or height is None
        or width <= 0.0
        or height <= 0.0
        or row_count <= 0
        or column_count <= 0
    ):
        return {}
    result: dict[tuple[str, Any], dict[str, Any]] = {}
    for row in range(row_count):
        for column in range(column_count):
            identifier = (row, column)
            result[_cell_key(identifier)] = {
                "cell_id": identifier,
                "bounds": (
                    origin_x + column * width,
                    origin_y + row * height,
                    origin_x + (column + 1) * width,
                    origin_y + (row + 1) * height,
                ),
            }
    return result


def _metadata_cell_bounds(metadata: Mapping[str, Any]) -> dict[tuple[str, Any], dict[str, Any]]:
    supplied = metadata.get("cell_bounds", metadata.get("cells"))
    result: dict[tuple[str, Any], dict[str, Any]] = {}
    if isinstance(supplied, Mapping):
        records = supplied.items()
    elif isinstance(supplied, Sequence) and not isinstance(supplied, (str, bytes)):
        records = ((index, value) for index, value in enumerate(supplied))
    else:
        return _regular_grid_bounds(metadata)
    for fallback, value in records:
        identifier = _cell_identifier(value, fallback)
        cell_bounds = _bounds(value)
        if identifier is None or cell_bounds is None:
            return {}
        key = _cell_key(identifier)
        result[key] = {"cell_id": identifier, "bounds": cell_bounds}
    return result


def _hull_bbox(hull: Any) -> tuple[float, float, float, float] | None:
    if isinstance(hull, Mapping):
        direct = _bounds(hull)
        if direct is not None:
            return direct
        hull = hull.get("points", hull.get("vertices"))
    if isinstance(hull, Sequence) and not isinstance(hull, (str, bytes)):
        if len(hull) == 4 and all(_number(item) is not None for item in hull):
            return _bounds(hull)
        points: list[tuple[float, float]] = []
        for point in hull:
            if not isinstance(point, Sequence) or isinstance(point, (str, bytes)):
                return None
            if len(point) != 2:
                return None
            x = _number(point[0])
            y = _number(point[1])
            if x is None or y is None:
                return None
            points.append((x, y))
        if len(points) >= 2:
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            return min(xs), min(ys), max(xs), max(ys)
    return None


def _cell_status(value: Any) -> tuple[str, bool]:
    """Return (status, coverage_complete) for one caller-supplied cell."""

    coverage: Any = True
    status: Any = value
    if isinstance(value, Mapping):
        for name in ("covered", "coverage_complete", "available"):
            if name in value:
                coverage = value[name]
                break
        if "coverage" in value:
            coverage = value["coverage"]
        status = value.get("status", value.get("state", value.get("classification")))
        if status is None and "land" in value:
            status = "LAND" if value["land"] is True else "SEA"
        if status is None and "is_sea" in value:
            status = "SEA" if value["is_sea"] is True else "LAND"
    if coverage is not True:
        if isinstance(coverage, str):
            coverage_complete = coverage.upper() in _COVERED_VALUES
        else:
            coverage_complete = False
    else:
        coverage_complete = True
    if isinstance(status, bool):
        normalized = "SEA" if status else "LAND"
    elif status is None:
        normalized = "UNKNOWN"
    else:
        normalized = str(status).upper()
    if normalized in _LAND_VALUES:
        return "LAND", coverage_complete
    if normalized in _SEA_VALUES:
        return "SEA", coverage_complete
    if normalized in _UNKNOWN_VALUES:
        return "UNKNOWN", coverage_complete
    return "UNKNOWN", coverage_complete


def _supplied_cells(
    cells: Mapping[Any, Any] | Sequence[Any],
) -> dict[tuple[str, Any], Any]:
    if isinstance(cells, Mapping):
        return {_cell_key(identifier): value for identifier, value in cells.items()}
    if isinstance(cells, Sequence) and not isinstance(cells, (str, bytes)):
        result: dict[tuple[str, Any], Any] = {}
        for index, value in enumerate(cells):
            identifier = _cell_identifier(value, index)
            if identifier is not None:
                result[_cell_key(identifier)] = value
        return result
    return {}


def evaluate_raster_corridor_evidence(
    raster_metadata: Mapping[str, Any],
    raster_cells: Mapping[Any, Any] | Sequence[Any],
    span_convex_hulls: Sequence[Any],
    *,
    expansion_m: float = 500.0,
) -> dict[str, Any]:
    """Evaluate supplied raster cells against expanded span-hull bboxes.

    ``raster_metadata`` must describe either ``cell_bounds`` records or a
    regular grid using origin, cell size, and row/column counts.  Coordinates
    are caller-owned local metre coordinates; this function intentionally does
    not perform a CRS or degree-to-metre conversion.
    """

    base: dict[str, Any] = {
        "accepted": False,
        "complete": False,
        "method": METHOD,
        "continuous_containment_proved": False,
        "raster_resolution_containment_proved": False,
        "continuous_containment_scope": (
            "supplied_raster_resolution_and_axis_aligned_expanded_hull_bbox_only"
        ),
        "expansion_m": expansion_m,
        "span_count": len(span_convex_hulls) if isinstance(span_convex_hulls, Sequence) else 0,
        "enumerated_cells": [],
        "land_cells": [],
        "unknown_cells": [],
        "missing_coverage_cells": [],
    }
    if not isinstance(raster_metadata, Mapping):
        base["reason"] = "invalid_raster_metadata"
        return base
    try:
        expansion_value = float(expansion_m)
    except (TypeError, ValueError):
        expansion_value = math.nan
    if isinstance(expansion_m, bool) or not math.isfinite(expansion_value) or expansion_value < 0.0:
        base["reason"] = "invalid_expansion"
        return base
    hulls = list(span_convex_hulls) if isinstance(span_convex_hulls, Sequence) else []
    expanded: list[tuple[float, float, float, float]] = []
    for hull in hulls:
        bbox = _hull_bbox(hull)
        if bbox is None:
            base["reason"] = "invalid_span_convex_hull"
            return base
        expanded.append(
            (
                bbox[0] - expansion_value,
                bbox[1] - expansion_value,
                bbox[2] + expansion_value,
                bbox[3] + expansion_value,
            )
        )
    if not expanded:
        base["reason"] = "missing_span_convex_hulls"
        return base
    expected = _metadata_cell_bounds(raster_metadata)
    supplied = _supplied_cells(raster_cells)
    if not expected:
        base["reason"] = "invalid_raster_cell_bounds"
        return base
    raster_coverage_complete = raster_metadata.get("coverage_complete", True) is True
    if not raster_coverage_complete:
        base["reason"] = "raster_coverage_incomplete"

    enumerated: dict[tuple[str, Any], dict[str, Any]] = {}
    for key, cell in expected.items():
        lower_x, lower_y, upper_x, upper_y = cell["bounds"]
        spans = [
            index
            for index, bbox in enumerate(expanded)
            if not (
                upper_x < bbox[0]
                or lower_x > bbox[2]
                or upper_y < bbox[1]
                or lower_y > bbox[3]
            )
        ]
        if spans:
            enumerated[key] = {**cell, "spans": spans}
    ordered = sorted(enumerated.items(), key=lambda item: repr(item[1]["cell_id"]))
    base["enumerated_cells"] = [
        _public_cell_id(cell["cell_id"]) for _, cell in ordered
    ]
    base["cells"] = []
    for key, cell in ordered:
        cell_id = cell["cell_id"]
        if not raster_coverage_complete or key not in supplied:
            status = "MISSING_COVERAGE"
            coverage_complete = False
            base["missing_coverage_cells"].append(_public_cell_id(cell_id))
        else:
            status, coverage_complete = _cell_status(supplied[key])
            if not coverage_complete:
                base["missing_coverage_cells"].append(_public_cell_id(cell_id))
            elif status == "LAND":
                base["land_cells"].append(_public_cell_id(cell_id))
            elif status != "SEA":
                base["unknown_cells"].append(_public_cell_id(cell_id))
        base["cells"].append(
            {
                "cell_id": _public_cell_id(cell_id),
                "bounds": list(cell["bounds"]),
                "spans": list(cell["spans"]),
                "status": status,
                "coverage_complete": coverage_complete,
            }
        )
    if not ordered and "reason" not in base:
        base["reason"] = "no_intersecting_raster_cells"
    elif not base["missing_coverage_cells"] and (
        base["land_cells"] or base["unknown_cells"]
    ):
        base["reason"] = "raster_cell_not_sea"
    elif base["missing_coverage_cells"]:
        base["reason"] = base.get("reason", "missing_raster_coverage")
    elif "reason" not in base:
        base["reason"] = None
    accepted = bool(ordered) and not any(
        base[name] for name in ("land_cells", "unknown_cells", "missing_coverage_cells")
    ) and base.get("reason") is None
    base.update(
        {
            "accepted": accepted,
            "complete": accepted,
            "coverage_complete": (
                raster_coverage_complete and not base["missing_coverage_cells"]
            ),
            "hard_mask_envelope_complete": accepted,
            # Raster-cell enumeration is not a continuous-ocean proof.
            "continuous_containment_proved": False,
            "raster_resolution_containment_proved": accepted,
        }
    )
    return base


build_raster_corridor_evidence = evaluate_raster_corridor_evidence
raster_corridor_evidence = evaluate_raster_corridor_evidence


__all__ = [
    "METHOD",
    "build_raster_corridor_evidence",
    "evaluate_raster_corridor_evidence",
    "raster_corridor_evidence",
]
