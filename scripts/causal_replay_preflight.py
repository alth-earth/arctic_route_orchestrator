"""Operational causal replay preflight for the Scenario B short-window MVP.

Answers, from real archived manifest records visible at the replay start:

* available future valid-time coverage per required B data type;
* the resulting causal risk horizon (min over types, capped at replay end);
* C layer support candidates (executable/rolling/main corridor/full voyage);
* visible-set and B-relevant input digests at the start tick.

Read-only: no B/C computation, no writes outside the output path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arctic_route_data.causal_replay import (
    CATEGORICAL_TYPES,
    REQUIRED_FORMAL_DATA_TYPES,
    STATIC_TYPES,
    load_manifest_records,
)


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _corridor_bbox(corridor_toml: str) -> tuple[float, float, float, float]:
    with open(corridor_toml, "rb") as handle:
        document = tomllib.load(handle)
    box = document["data_bbox"]
    return (float(box["west"]), float(box["south"]), float(box["east"]), float(box["north"]))


def run_preflight(
    *,
    manifest_path: Path,
    route_id: str,
    replay_start: datetime,
    replay_end: datetime,
    target_bbox: tuple[float, float, float, float],
) -> dict[str, object]:
    records = load_manifest_records(str(manifest_path), route_id)
    visible = tuple(
        record for record in records if record.issue_time <= replay_start
    )
    by_type: dict[str, list[object]] = {}
    for record in visible:
        by_type.setdefault(record.data_type, []).append(record)
    per_type: dict[str, dict[str, object]] = {}
    usable_ends: list[datetime] = []
    for data_type in sorted(REQUIRED_FORMAL_DATA_TYPES):
        type_records = by_type.get(data_type, [])
        if not type_records:
            per_type[data_type] = {
                "visible_record_count": 0,
                "coverage_start": None,
                "coverage_end": None,
                "future_hours": None,
                "usable_start": None,
                "usable_end": None,
                "status": "NO_VISIBLE_RECORDS",
            }
            continue
        valid_times = sorted(record.valid_time for record in type_records)
        coverage_start = valid_times[0]
        coverage_end = valid_times[-1]
        if data_type in STATIC_TYPES:
            usable_start = replay_start
            usable_end = replay_end
            status = "SUPPORTED_STATIC_PRIOR"
        else:
            usable_start, usable_end, blocker = _usable_range(
                type_records,
                start=replay_start,
                end=coverage_end,
                bbox=target_bbox,
                categorical=data_type in CATEGORICAL_TYPES,
            )
            status = "INSUFFICIENT" if blocker else "SUPPORTED"
            if usable_end is not None:
                usable_ends.append(usable_end)
        per_type[data_type] = {
            "visible_record_count": len(type_records),
            "coverage_start": _iso(coverage_start),
            "coverage_end": _iso(coverage_end),
            "future_hours": round(
                max(0.0, (coverage_end - replay_start).total_seconds() / 3600.0),
                3,
            ),
            "usable_start": _iso(usable_start) if usable_start else None,
            "usable_end": _iso(usable_end) if usable_end else None,
            "status": status,
        }
    common_end = min(usable_ends) if usable_ends else replay_end
    risk_horizon_hours = max(
        0.0, (common_end - replay_start).total_seconds() / 3600.0
    )

    def layer_support(required_hours: float, label: str) -> dict[str, object]:
        if risk_horizon_hours >= required_hours:
            return {
                "status": "SUPPORTED",
                "reason": (
                    f"horizon {risk_horizon_hours:.1f}h >= "
                    f"{required_hours:.0f}h"
                ),
            }
        return {
            "status": "NOT_SUPPORTED",
            "reason": f"horizon {risk_horizon_hours:.1f}h < {required_hours:.0f}h",
        }

    layers = {
        "executable_0_6h": layer_support(6.0, "executable"),
        "rolling_0_24h": layer_support(24.0, "rolling"),
        "main_corridor_24_72h": layer_support(72.0, "main corridor"),
        "full_voyage": {
            "status": "CANDIDATE_NOT_SUPPORTED",
            "reason": (
                f"horizon {risk_horizon_hours:.1f}h < frozen replanned ETA "
                "~47.9h; final verdict requires real C planning attempt"
            ),
        },
    }
    visible_ids = sorted(record.data_id for record in visible)
    visible_digest = hashlib.sha256(
        "\n".join(visible_ids).encode("utf-8")
    ).hexdigest()
    # B-relevant identity: selected best record per (type, valid_time) among
    # visible records, using A's revision ordering (quality, issue, ingest
    # rank is approximated by data_id order after sorting by type/valid).
    selected: list[str] = []
    for data_type in sorted(REQUIRED_FORMAL_DATA_TYPES):
        type_records = sorted(
            by_type.get(data_type, []),
            key=lambda record: (record.valid_time, record.data_id),
        )
        best: dict[object, object] = {}
        for record in type_records:
            best.setdefault(record.valid_time, record)
        selected.extend(
            f"{data_type}:{record.valid_time.isoformat()}:{record.data_id}"
            for record in sorted(best.values(), key=lambda item: item.valid_time)
        )
    relevant_digest = hashlib.sha256(
        "\n".join(selected).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "orchestrator.causal-replay-preflight.v1",
        "route_id": route_id,
        "replay_start": _iso(replay_start),
        "replay_end": _iso(replay_end),
        "knowledge_as_of": _iso(replay_start),
        "max_source_issue_time": (
            _iso(max(record.issue_time for record in visible)) if visible else None
        ),
        "visible_record_count": len(visible),
        "visible_record_set_digest": visible_digest,
        "b_relevant_input_digest": relevant_digest,
        "per_type_future_coverage": per_type,
        "risk_horizon_hours": round(risk_horizon_hours, 3),
        "replay_window": {
            "start": _iso(replay_start),
            "end": _iso(replay_end),
        },
        "risk_forecast_window": {
            "start": _iso(replay_start),
            "end": _iso(common_end),
        },
        "planning_window_candidate": {
            "start": _iso(replay_start),
            "end": _iso(common_end),
        },
        "common_causal_valid_end": _iso(common_end),
        "data_horizon_support": {
            "executable_0_6h": layer_support(6.0, "executable"),
            "rolling_0_24h": layer_support(24.0, "rolling"),
            "main_corridor_24_72h": layer_support(72.0, "main corridor"),
            "full_voyage": layer_support(48.0, "full voyage"),
        },
        "planner_contract_support": {
            layer: {
                "status": (
                    "BLOCKED_BY_FULL_VOYAGE_ANCHOR"
                    if layer in ("executable_0_6h", "rolling_0_24h")
                    else "CANDIDATE"
                ),
                "reason": (
                    "existing layered API requires a full_voyage anchor"
                    if layer in ("executable_0_6h", "rolling_0_24h")
                    else "depends on real C planning result"
                ),
            }
            for layer in (
                "executable_0_6h",
                "rolling_0_24h",
                "main_corridor_24_72h",
                "full_voyage",
            )
        },
        "c_layer_support": layers,
        "note": (
            "Layer verdicts are horizon candidates; full_voyage and "
            "main_corridor require real C planning to confirm (or fail "
            "fail-closed)."
        ),
    }


def _bbox_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> bool:
    west, south, east, north = outer
    target_west, target_south, target_east, target_north = inner
    tolerance = 1e-9
    return (
        west <= target_west + tolerance
        and south <= target_south + tolerance
        and east >= target_east - tolerance
        and north >= target_north - tolerance
    )


def _usable_range(
    records: list,
    *,
    start: datetime,
    end: datetime,
    bbox: tuple[float, float, float, float],
    categorical: bool,
) -> tuple[datetime | None, datetime | None, str | None]:
    """Largest [start, usable_end] with per-hour exact/bracket support."""

    covering = sorted(
        (
            record
            for record in records
            if _bbox_contains(record.bbox, bbox)
        ),
        key=lambda record: record.valid_time,
    )
    if not covering:
        return None, None, "no bbox-covering records"
    usable_end: datetime | None = None
    target = start
    while target <= end:
        exact = [r for r in covering if r.valid_time == target]
        if exact:
            support = True
        else:
            lower = [r for r in covering if r.valid_time < target]
            upper = [r for r in covering if r.valid_time > target]
            support = (
                bool(lower) or bool(upper)
                if categorical
                else bool(lower) and bool(upper)
            )
        if not support:
            break
        usable_end = target
        target += timedelta(hours=1)
    if usable_end is None:
        return None, None, "no hourly target supported at replay start"
    return start, usable_end, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="causal-replay-preflight")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=_workspace_root() / "work_package_a" / "data" / "manifest" / "manifest.sqlite3",
    )
    parser.add_argument(
        "--corridor-toml",
        type=Path,
        default=(
            _workspace_root() / "arctic_route_contracts" / "configs" / "corridors"
            / "tromso_to_isfjorden_outer.toml"
        ),
    )
    parser.add_argument("--replay-start", default="2026-08-15T10:00:00Z")
    parser.add_argument("--replay-end", default="2026-08-17T06:00:00Z")
    parser.add_argument("--route-id", default="tromso_to_isfjorden_outer")
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            _workspace_root() / "work_package_a" / "data" / "output" / "rc2-smoke"
            / "causal-replay-mvp" / "preflight.json"
        ),
    )
    args = parser.parse_args(argv)
    bbox = _corridor_bbox(str(args.corridor_toml))
    report = run_preflight(
        manifest_path=args.manifest,
        route_id=args.route_id,
        replay_start=_parse_utc(args.replay_start),
        replay_end=_parse_utc(args.replay_end),
        target_bbox=bbox,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
