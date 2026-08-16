"""Fail-closed intake of an externally produced formal A bundle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from arctic_route_contracts import (
    create_run_context,
    load_corridor,
    load_run_context,
    load_scenario,
    load_vessel_profile,
    verify_dataset_bundle,
)
from arctic_route_data import DatasetBundle, PartitionedABCache, SimulationClock, WorkPackageA
from arctic_route_data.sources import LocalArchiveSource

from arctic_route_orchestrator.errors import ArtifactIntakeError

FORMAL_REQUIRED_TYPES = frozenset(
    {
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
)


@dataclass(frozen=True, slots=True)
class ArtifactIntakeReport:
    bundle_id: str
    bundle_digest: str
    run_id: str
    corridor_id: str
    horizon_hours: int
    requested_data_types: tuple[str, ...]
    record_count: int
    generation_id: int
    knowledge_as_of: str


@dataclass(frozen=True, slots=True)
class SourceRecordEvidence:
    data_id: str
    data_type: str
    issue_time: str
    valid_time: str
    source: str
    version: str
    quality_flag: str
    checksum: str
    source_snapshot_id: str


@dataclass(frozen=True, slots=True)
class ArtifactIntake:
    report: ArtifactIntakeReport
    prepared_window: object
    run_context: object
    clock: SimulationClock
    source_records: tuple[SourceRecordEvidence, ...]

    @classmethod
    def validate(
        cls,
        *,
        bundle_path: str | Path,
        run_context_path: str | Path | None,
        a_data_root: str | Path,
        generation_id: int,
        scenario_id: str | None = None,
        run_id: str | None = None,
        created_at: datetime | None = None,
        contracts_config_root: str | Path | None = None,
    ) -> ArtifactIntake:
        if (
            isinstance(generation_id, bool)
            or not isinstance(generation_id, int)
            or generation_id < 0
        ):
            raise ArtifactIntakeError(
                "a_artifact_generation_unavailable",
                "generation_id must be a non-negative integer",
            )
        try:
            document = json.loads(
                Path(bundle_path).read_text(encoding="utf-8"),
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_non_finite_constant,
            )
        except Exception as exc:
            raise ArtifactIntakeError(
                "a_artifact_invalid", "bundle JSON failed strict parsing"
            ) from exc
        if not isinstance(document, dict):
            raise ArtifactIntakeError("a_artifact_invalid", "bundle must be a JSON object")
        if document.get("schema_version") != "a.dataset-bundle.v2":
            raise ArtifactIntakeError(
                "a_artifact_legacy", "formal intake requires a.dataset-bundle.v2"
            )
        try:
            bundle = DatasetBundle.from_dict(document)
            verified = verify_dataset_bundle(document)
            if run_context_path is not None:
                run_context = load_run_context(run_context_path)
            else:
                if scenario_id is None or run_id is None or created_at is None:
                    raise ValueError(
                        "scenario_id, run_id and created_at are required without RunContext"
                    )
                scenario = load_scenario(contracts_config_root, scenario_id)
                corridor = load_corridor(contracts_config_root, scenario.corridor_id)
                vessel = load_vessel_profile(
                    contracts_config_root, scenario.default_vessel_profile_id
                )
                run_context = create_run_context(
                    scenario=scenario,
                    corridor=corridor,
                    vessel=vessel,
                    dataset_bundle=verified,
                    run_id=run_id,
                    created_at=created_at,
                )
        except Exception as exc:
            raise ArtifactIntakeError(
                "a_artifact_invalid", "bundle or RunContext failed semantic parsing"
            ) from exc
        if bundle.corridor_id != run_context.corridor_id:
            raise ArtifactIntakeError(
                "a_artifact_corridor_mismatch",
                f"bundle corridor {bundle.corridor_id} differs from "
                f"RunContext {run_context.corridor_id}",
            )
        if set(bundle.requested_data_types) != FORMAL_REQUIRED_TYPES:
            missing = sorted(FORMAL_REQUIRED_TYPES - set(bundle.requested_data_types))
            extra = sorted(set(bundle.requested_data_types) - FORMAL_REQUIRED_TYPES)
            raise ArtifactIntakeError(
                "a_artifact_data_profile_mismatch", f"missing={missing}, extra={extra}"
            )
        if not verified.coverage_complete or not verified.formal_run_eligible:
            raise ArtifactIntakeError(
                "a_artifact_coverage_incomplete", "coverage/provenance is not formal eligible"
            )
        missing_snapshots = sorted(
            record.data_id
            for record in bundle.records
            if not isinstance(record.source_snapshot_id, str)
            or not record.source_snapshot_id
        )
        if missing_snapshots:
            raise ArtifactIntakeError(
                "a_artifact_provenance_incomplete",
                f"records without source_snapshot_id: {missing_snapshots[:5]}",
            )
        latest_issue_time = max(record.issue_time for record in bundle.records)
        if bundle.as_of_time != latest_issue_time:
            raise ArtifactIntakeError(
                "a_artifact_knowledge_cutoff_mismatch",
                "bundle as_of_time must equal the maximum selected record issue_time",
            )
        horizon_hours = int(
            (bundle.requested_end - bundle.requested_start).total_seconds() // 3600
        )
        if bundle.minimum_required_end != bundle.requested_end:
            raise ArtifactIntakeError(
                "a_artifact_window_mismatch", "bundle must be a complete requested window"
            )
        if scenario_id is not None and run_context.scenario_id != scenario_id:
            raise ArtifactIntakeError(
                "a_artifact_context_mismatch", "RunContext scenario_id differs from execution spec"
            )
        if run_id is not None and run_context.run_id != run_id:
            raise ArtifactIntakeError(
                "a_artifact_context_mismatch", "RunContext run_id differs from execution spec"
            )
        if created_at is not None and run_context.created_at != created_at:
            raise ArtifactIntakeError(
                "a_artifact_context_mismatch",
                "RunContext created_at differs from execution spec generated_at",
            )
        expected_context = {
            "corridor_id": bundle.corridor_id,
            "dataset_bundle_id": bundle.bundle_id,
            "dataset_bundle_digest": bundle.bundle_digest,
            "simulation_start": bundle.requested_start,
            "simulation_end": bundle.requested_end,
        }
        mismatched = [
            name
            for name, expected in expected_context.items()
            if getattr(run_context, name) != expected
        ]
        if mismatched:
            raise ArtifactIntakeError(
                "a_artifact_context_mismatch", ", ".join(mismatched)
            )
        clock = SimulationClock(bundle.requested_start)
        if generation_id != 0:
            raise ArtifactIntakeError(
                "a_artifact_generation_unavailable",
                "fresh external artifact intake must start at generation 0",
            )
        service = WorkPackageA(
            source=LocalArchiveSource(a_data_root),
            clock=clock,
            cache=PartitionedABCache(max_memory_mb=512),
        )
        try:
            prepared = service.resolve_dataset_bundle_for_b(
                document,
                generation_id=generation_id,
                knowledge_as_of=bundle.as_of_time,
            )
        except Exception as exc:
            raise ArtifactIntakeError(
                "a_artifact_exact_resolver_failed", "archive does not reproduce exact bundle"
            ) from exc
        finally:
            service.close()
        return cls(
            report=ArtifactIntakeReport(
                bundle_id=bundle.bundle_id,
                bundle_digest=bundle.bundle_digest,
                run_id=run_context.run_id,
                corridor_id=bundle.corridor_id,
                horizon_hours=horizon_hours,
                requested_data_types=tuple(bundle.requested_data_types),
                record_count=len(bundle.records),
                generation_id=generation_id,
                knowledge_as_of=bundle.as_of_time.isoformat().replace("+00:00", "Z"),
            ),
            prepared_window=prepared,
            run_context=run_context,
            clock=clock,
            source_records=tuple(
                SourceRecordEvidence(
                    data_id=record.data_id,
                    data_type=record.data_type,
                    issue_time=record.issue_time.isoformat().replace("+00:00", "Z"),
                    valid_time=record.valid_time.isoformat().replace("+00:00", "Z"),
                    source=record.source,
                    version=record.version,
                    quality_flag=record.quality_flag,
                    checksum=record.checksum,
                    source_snapshot_id=_required_snapshot_id(record.source_snapshot_id),
                )
                for record in bundle.records
            ),
        )


def _required_snapshot_id(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactIntakeError(
            "a_artifact_provenance_incomplete",
            "formal source record lacks source_snapshot_id",
        )
    return value


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")
