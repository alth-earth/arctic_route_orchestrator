#!/usr/bin/env python3
"""Create a non-destructive inventory of reusable A DatasetBundle samples.

This is a research inventory only.  It never reads A's private manifest store,
downloads data, or changes any existing bundle.  The output is an experiment
artifact used to decide whether a new severe-winter acquisition is justified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_TYPES = {
    "land_sea_mask",
    "ocean_current",
    "sea_ice_concentration",
    "sea_ice_drift",
    "sea_ice_edge",
    "sea_ice_thickness",
    "sea_ice_type",
    "temperature",
    "visibility",
    "water_level",
    "wave",
    "wind_field",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_status(path: Path, bundle: dict[str, Any], complete: bool) -> str:
    start = str(bundle.get("requested_start", ""))
    if start.startswith("2026-02") and complete:
        if "min144" in path.name:
            return "ACTIVE_WINTER_REUSABLE"
        return "HISTORICAL_SUPERSEDED_WINTER"
    if start.startswith("2026-08") and complete:
        return "SUMMER_CONTROL_ONLY"
    return "REQUIRES_A_GATES"


def inventory(bundle_paths: list[Path]) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for path in sorted(bundle_paths):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        types = {str(item) for item in bundle.get("requested_data_types", [])}
        coverage = bundle.get("coverage", [])
        complete = bool(
            bundle.get("record_count")
            and types == REQUIRED_TYPES
            and len(coverage) == len(REQUIRED_TYPES)
            and all(
                item.get("complete")
                and item.get("covers_requested_window")
                and item.get("provenance_complete")
                for item in coverage
            )
        )
        samples.append(
            {
                "path": str(path),
                "file_sha256": _sha256(path),
                "bundle_id": bundle.get("bundle_id"),
                "bundle_digest": bundle.get("bundle_digest"),
                "corridor_id": bundle.get("corridor_id"),
                "requested_start": bundle.get("requested_start"),
                "requested_end": bundle.get("requested_end"),
                "minimum_required_end": bundle.get("minimum_required_end"),
                "record_count": bundle.get("record_count"),
                "requested_data_type_count": len(types),
                "coverage_complete": complete,
                "status": _candidate_status(path, bundle, complete),
            }
        )
    active = [item for item in samples if item["status"] == "ACTIVE_WINTER_REUSABLE"]
    return {
        "schema_version": "research.winter-sample-inventory.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "non_destructive": True,
        "download_performed": False,
        "required_data_type_count": len(REQUIRED_TYPES),
        "sample_count": len(samples),
        "active_winter_reusable_count": len(active),
        "decision": (
            "reuse_existing_active_winter_and_defer_download"
            if active
            else "no_reusable_winter_found_review_acquisition"
        ),
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a-data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    candidates = [
        args.a_data_root / "tromso_to_isfjorden_outer_winter_20260215T000000Z_bundle.json",
        args.a_data_root / "tromso_to_isfjorden_outer_winter_20260215T000000Z_min144_bundle.json",
        args.a_data_root / "output/bundles/murmansk_dikson_august_2026_demo_v1.bundle.json",
        args.a_data_root / "output/bundles/tromso_isfjorden_august_2026_demo_v1.bundle.json",
    ]
    existing = [path for path in candidates if path.is_file()]
    result = inventory(existing)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "sample-inventory.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "output": str(args.output_dir / "sample-inventory.json"),
        **{
            key: result[key]
            for key in ("sample_count", "active_winter_reusable_count", "decision")
        },
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
