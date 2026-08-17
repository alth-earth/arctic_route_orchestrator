from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from arctic_route_contracts import (
    create_run_context,
    load_corridor,
    load_scenario,
    load_vessel_profile,
    run_context_to_dict,
    verify_dataset_bundle,
)
from arctic_route_data import (
    AcquisitionPublisher,
    PartitionedABCache,
    SimulationClock,
    WorkPackageA,
)
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.sources import LocalArchiveSource
from jsonschema import Draft202012Validator, FormatChecker

from arctic_route_orchestrator import ExecutionSpec, RunPaths, execute_formal_run

ORCHESTRATOR_ROOT = Path(__file__).parents[2]
WORKSPACE = ORCHESTRATOR_ROOT.parent
CONTRACTS_CONFIG_ROOT = WORKSPACE / "arctic_route_contracts" / "configs"
C_CONFIG_ROOT = WORKSPACE / "work_package_c" / "configs"
B_CONFIG = (
    WORKSPACE
    / "work_package_b"
    / "configs"
    / "models"
    / "demo_unvalidated_smoke_grid_v4.json"
)
SCENARIO_ID = "murmansk_dikson_july_2026_retrospective_v1"
CORRIDOR_ID = "offshore_murmansk_to_offshore_dikson"
AS_OF = datetime(2026, 8, 14, tzinfo=UTC)

VARIABLES = {
    "land_sea_mask": ("land_sea_mask",),
    "ocean_current": ("ocean_current_u", "ocean_current_v"),
    "sea_ice_concentration": ("ice_concentration",),
    "sea_ice_drift": ("ice_drift_u", "ice_drift_v"),
    "sea_ice_edge": ("ice_edge",),
    "sea_ice_thickness": ("ice_thickness",),
    "sea_ice_type": ("ice_type",),
    "temperature": ("air_temperature_2m",),
    "visibility": ("visibility",),
    "water_level": ("sea_surface_height",),
    "wave": (
        "significant_wave_height",
        "mean_wave_direction",
        "peak_wave_period",
    ),
    "wind_field": ("wind_u10", "wind_v10"),
}
CADENCE = {
    "land_sea_mask": None,
    "ocean_current": 1.0,
    "sea_ice_concentration": 1.0,
    "sea_ice_drift": 1.0,
    "sea_ice_edge": 1.0,
    "sea_ice_thickness": 1.0,
    "sea_ice_type": 1.0,
    "temperature": 3.0,
    "visibility": 3.0,
    "water_level": 1.0,
    "wave": 3.0,
    "wind_field": 3.0,
}
UNITS = {
    "land_sea_mask": "1",
    "ocean_current_u": "m s-1",
    "ocean_current_v": "m s-1",
    "ice_concentration": "1",
    "ice_drift_u": "m s-1",
    "ice_drift_v": "m s-1",
    "ice_edge": "1",
    "ice_thickness": "m",
    "ice_type": "1",
    "air_temperature_2m": "K",
    "visibility": "m",
    "sea_surface_height": "m",
    "significant_wave_height": "m",
    "mean_wave_direction": "degree",
    "peak_wave_period": "s",
    "wind_u10": "m s-1",
    "wind_v10": "m s-1",
}
STANDARD_NAMES = {
    "ocean_current_u": "eastward_sea_water_velocity",
    "ocean_current_v": "northward_sea_water_velocity",
    "ice_concentration": "sea_ice_area_fraction",
    "ice_drift_u": "eastward_sea_ice_velocity",
    "ice_drift_v": "northward_sea_ice_velocity",
    "mean_wave_direction": "sea_surface_wave_from_direction",
    "wind_u10": "eastward_wind",
    "wind_v10": "northward_wind",
}
BASE = {
    "land_sea_mask": 1.0,
    "ocean_current_u": 0.2,
    "ocean_current_v": 0.1,
    "ice_concentration": 0.15,
    "ice_drift_u": 0.05,
    "ice_drift_v": 0.02,
    "ice_edge": 0.0,
    "ice_thickness": 0.4,
    "ice_type": 1.0,
    "air_temperature_2m": 273.0,
    "visibility": 15_000.0,
    "sea_surface_height": 0.1,
    "significant_wave_height": 0.8,
    "mean_wave_direction": 90.0,
    "peak_wave_period": 8.0,
    "wind_u10": 3.0,
    "wind_v10": 1.0,
}


@pytest.fixture(scope="module")
def formal_a_artifacts(tmp_path_factory):
    root = tmp_path_factory.mktemp("orchestrator-formal-a")
    data_root = root / "archive"
    snapshot = (
        data_root
        / "source_snapshots"
        / "orchestrator-fixture"
        / "snapshot-168h"
        / "source.bin"
    )
    snapshot.parent.mkdir(parents=True)
    snapshot_bytes = b"formal-shape fixture only; this is not downloaded source data"
    snapshot.write_bytes(snapshot_bytes)
    snapshot_metadata = {
        "source_snapshot_id": "snapshot-168h",
        "source_file": snapshot.name,
        "source_file_checksum": hashlib.sha256(snapshot_bytes).hexdigest(),
        "source_snapshot_relative_path": snapshot.relative_to(data_root).as_posix(),
    }
    scenario = load_scenario(CONTRACTS_CONFIG_ROOT, SCENARIO_ID)
    corridor = load_corridor(CONTRACTS_CONFIG_ROOT, scenario.corridor_id)
    vessel = load_vessel_profile(
        CONTRACTS_CONFIG_ROOT,
        scenario.default_vessel_profile_id,
    )
    assert scenario.simulation_start is not None
    assert scenario.simulation_end is not None
    publisher = AcquisitionPublisher(data_root)
    evidence = IssueTimeEvidence(
        issue_time=AS_OF,
        method=IssueTimeMethod.EXPLICIT_CATALOG,
        authority="orchestrator integration fixture",
        reference="formal-shape 168-hour archive fixture",
        observed_at=AS_OF,
        raw_value=AS_OF.isoformat(),
    )
    for data_type in sorted(VARIABLES):
        publisher.publish_dataset(
            _dataset(
                data_type,
                start=scenario.simulation_start,
                horizon_hours=scenario.horizon_hours,
                bbox=(
                    corridor.data_bbox.west,
                    corridor.data_bbox.south,
                    corridor.data_bbox.east,
                    corridor.data_bbox.north,
                ),
            ),
            data_type=data_type,
            route_id=CORRIDOR_ID,
            source="orchestrator-formal-shape-fixture",
            version="1.0.0",
            issue_evidence=evidence,
            metadata={
                **snapshot_metadata,
                "nominal_interval_hours": CADENCE[data_type],
            },
        )

    service = WorkPackageA(
        source=LocalArchiveSource(data_root),
        clock=SimulationClock(scenario.simulation_start),
        cache=PartitionedABCache(max_memory_mb=64),
    )
    try:
        prepared = service.prepare_window_for_b(
            route_id=CORRIDOR_ID,
            data_types=scenario.required_data_types,
            start_time=scenario.simulation_start,
            target_horizon_hours=scenario.horizon_hours,
            minimum_complete_horizon_hours=scenario.horizon_hours,
            expected_interval_hours=CADENCE,
            knowledge_as_of=AS_OF,
        )
    finally:
        service.close()
    verified = verify_dataset_bundle(prepared.dataset_bundle.to_dict())
    run_context = create_run_context(
        scenario=scenario,
        corridor=corridor,
        vessel=vessel,
        dataset_bundle=verified,
        run_id="run-00000000-0000-4000-8000-000000000168",
        created_at=AS_OF,
    )
    bundle_path = root / "dataset-bundle-v2.json"
    context_path = root / "run-context-v2.json"
    bundle_path.write_text(
        json.dumps(
            prepared.dataset_bundle.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    context_path.write_text(
        json.dumps(
            run_context_to_dict(run_context),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return root, data_root, bundle_path, context_path


@pytest.mark.integration
@pytest.mark.parametrize(
    ("planning_contract", "run_id", "route_count", "route_directory"),
    (
        (
            "cd.route-plan.v2",
            "run-00000000-0000-4000-8000-000000000202",
            3,
            "v2",
        ),
        (
            "cd.four-layer-route-plan-set.v3",
            "run-00000000-0000-4000-8000-000000000303",
            12,
            "v3",
        ),
    ),
)
def test_formal_archive_to_b_to_c_and_six_hour_replan(
    formal_a_artifacts,
    planning_contract: str,
    run_id: str,
    route_count: int,
    route_directory: str,
) -> None:
    root, data_root, bundle_path, context_path = formal_a_artifacts
    context_document = json.loads(context_path.read_text(encoding="utf-8"))
    context_document["run_id"] = run_id
    # RunContext digest intentionally excludes run_id/created_at; this creates
    # two independent executions over the same immutable A evidence.
    run_context_path = root / f"run-context-{route_directory}.json"
    run_context_path.write_text(
        json.dumps(context_document, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    output_dir = root / f"output-{route_directory}"
    result = execute_formal_run(
        ExecutionSpec(
            schema_version="orchestrator.execution-spec.v1",
            run_id=run_id,
            scenario_id=SCENARIO_ID,
            generation_id=0,
            input_revision=0,
            generated_at=AS_OF,
            planning_contract=planning_contract,
            max_snap_km=150.0,
            replan_after_hours=6,
            # Integration fixture is intentionally full-scale; allow 60 min per
            # stage so the test exercises success paths, not the production 900s
            # timeout that already failed fast in the failure-report test.
            per_stage_timeout_seconds=3600.0,
        ),
        RunPaths(
            bundle_path=bundle_path,
            run_context_path=run_context_path,
            a_data_root=data_root,
            b_config_path=B_CONFIG,
            c_config_root=C_CONFIG_ROOT,
            contracts_config_root=CONTRACTS_CONFIG_ROOT,
            risk_store_root=root / f"risk-store-{route_directory}",
            output_dir=output_dir,
        ),
    )

    report_schema = json.loads(
        (ORCHESTRATOR_ROOT / "schemas" / "run-report-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(
        report_schema,
        format_checker=FormatChecker(),
    ).validate(result.report)
    assert result.report["b_output"]["full_frame_count"] == 169
    assert result.report["b_output"]["suffix_frame_count"] == 163
    assert len(result.report["routes"]["initial"]) == route_count
    assert len(result.report["routes"]["replanned"]) == route_count
    assert all(
        route["layer_goal_reached"]
        and route["metrics"]["hard_constraint_violations"] == 0
        for phase in ("initial", "replanned")
        for route in result.report["routes"][phase]
    )
    assert result.report["replanning"]["triggered"]
    assert result.report["replanning"]["published"]
    assert (output_dir / "checksums.json").is_file()
    stage_report = json.loads(
        (output_dir / "run-stage-report.json").read_text(encoding="utf-8")
    )
    assert stage_report["schema_version"] == "orchestrator.stage-report.v1"
    assert stage_report["status"] == "completed"
    assert {record["stage"] for record in stage_report["stages"]} == {
        "initialization",
        "b_build",
        "coverage_preflight",
        "endpoint_mapping",
        "c_initial_planning",
        "b_suffix_commit",
        "c_replanning",
        "output_publication",
    }
    assert (output_dir / "routes" / route_directory).is_dir()
    other = "v3" if route_directory == "v2" else "v2"
    assert not (output_dir / "routes" / other).exists()
    _assert_checksums(output_dir)


def _dataset(
    data_type: str,
    *,
    start: datetime,
    horizon_hours: int,
    bbox: tuple[float, float, float, float],
) -> xr.Dataset:
    cadence = CADENCE[data_type]
    hours = (0,) if cadence is None else range(0, horizon_hours + 1, int(cadence))
    times = [
        np.datetime64((start + timedelta(hours=hour)).replace(tzinfo=None))
        for hour in hours
    ]
    latitude = np.array([bbox[1], bbox[3]], dtype=np.float64)
    longitude = np.array([bbox[0], bbox[2]], dtype=np.float64)
    variables = {}
    for variable in VARIABLES[data_type]:
        values = []
        for index in range(len(times)):
            increment = 0.0
            if variable not in {"land_sea_mask", "ice_edge", "ice_type"}:
                increment = index * 1e-5
            values.append(
                np.full(
                    (latitude.size, longitude.size),
                    BASE[variable] + increment,
                    dtype=np.float64,
                )
            )
        variables[variable] = (
            ("time", "latitude", "longitude"),
            np.stack(values),
        )
    dataset = xr.Dataset(
        variables,
        coords={"time": times, "latitude": latitude, "longitude": longitude},
    )
    for variable in VARIABLES[data_type]:
        dataset[variable].attrs["units"] = UNITS[variable]
        if variable in STANDARD_NAMES:
            dataset[variable].attrs["standard_name"] = STANDARD_NAMES[variable]
    return dataset


def _assert_checksums(output_dir: Path) -> None:
    manifest = json.loads((output_dir / "checksums.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) == set(manifest["sizes_bytes"])
    for relative, expected in manifest["files"].items():
        payload = (output_dir / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected
        assert len(payload) == manifest["sizes_bytes"][relative]
