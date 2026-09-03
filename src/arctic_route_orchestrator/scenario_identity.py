"""Strong scenario-identity validation for viewer/replay package publication.

Background (2026-09-02): the "v13 holdout" incident shipped a viewer package whose
scenario identity silently differed from the intended frozen source (same basemap and
grid, but a different DatasetBundle / RiskWindow / selected route).  ``replay_viewer_export.py``
only cross-checked three fields (bundle / run-context / risk-window commit); it never
verified that the *requested* scenario matched every artifact's ``scenario_id``.

This module is the fail-closed gate used by ``publish_viewer_package.py``.  It compares a
``--scenario-id`` request against every artifact's declared identity and against the
canonical scenario contract (``arctic_route_contracts/configs/scenarios/*.toml``).
Any mismatch raises ``ValueError`` and no package is published.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SCENARIO_SCHEMA = "scenario.v2"
CORRIDOR_ID_FIELD = "corridor_id"
RUNCONTEXT_SCHEMA = "run-context.v2"
RISK_COMMIT_SCHEMA = "bc.risk-window-commit.v1"
PLAN_SET_SCHEMA = "cd.four-layer-route-plan-set.v3"
CANDIDATE_SCHEMA = "presentation.route-candidates.v1"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_scenario_contract(
    contracts_config_root: str | Path,
    scenario_id: str,
) -> dict[str, Any]:
    """Load the canonical scenario TOML and return its identity fields."""
    from arctic_route_contracts.config import load_scenario

    scenario = load_scenario(Path(contracts_config_root), scenario_id)
    return {
        "scenario_id": scenario.scenario_id,
        "corridor_id": scenario.corridor_id,
        "simulation_start": (
            scenario.simulation_start.isoformat().replace("+00:00", "Z")
            if scenario.simulation_start is not None
            else None
        ),
        "simulation_end": (
            scenario.simulation_end.isoformat().replace("+00:00", "Z")
            if scenario.simulation_end is not None
            else None
        ),
        "corridor_version": None,  # resolved below only if needed
    }


def verify_viewer_identity(
    *,
    scenario_id: str,
    contracts_config_root: str | Path,
    dataset_bundle: dict[str, Any],
    run_context: dict[str, Any],
    risk_index: dict[str, Any],
    risk_commit: dict[str, Any],
    plan_set: dict[str, Any],
    route_candidates: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed when any artifact identity crosses the requested scenario.

    The returned dict carries the canonical identity (bundle/risk-window/layer/candidate)
    that downstream publication must use verbatim.
    """
    contract = load_scenario_contract(contracts_config_root, scenario_id)

    _require(
        run_context.get("schema_version") == RUNCONTEXT_SCHEMA,
        "RunContext is not run-context.v2",
    )
    _require(
        run_context.get("scenario_id") == scenario_id,
        f"RunContext scenario {run_context.get('scenario_id')!r} != requested {scenario_id!r}",
    )
    _require(
        run_context.get("corridor_id") == contract["corridor_id"],
        "RunContext corridor "
        f"{run_context.get('corridor_id')!r} != scenario contract "
        f"{contract['corridor_id']!r}",
    )
    if contract["simulation_start"] and contract["simulation_end"]:
        _require(
            run_context.get("simulation_start") == contract["simulation_start"]
            and run_context.get("simulation_end") == contract["simulation_end"],
            "RunContext simulation window differs from scenario contract",
        )

    # --- DatasetBundle ---
    bundle_id = dataset_bundle.get("bundle_id")
    bundle_digest = dataset_bundle.get("bundle_digest")
    _require(bundle_id is not None, "DatasetBundle bundle_id missing")
    _require(
        run_context.get("dataset_bundle_id") == bundle_id
        and run_context.get("dataset_bundle_digest") == bundle_digest,
        "RunContext does not bind the supplied DatasetBundle",
    )

    # --- RiskWindow commit + frame index ---
    _require(
        risk_commit.get("schema_version") == RISK_COMMIT_SCHEMA,
        "risk window is not bc.risk-window-commit.v1",
    )
    _require(risk_commit.get("scenario_id") == scenario_id, "RiskWindow scenario mismatch")
    risk_window_id = risk_commit.get("commit_id")
    risk_window_digest = risk_commit.get("content_digest")
    _require(
        risk_index.get("status") == "FORMAL_VALIDATED",
        "RiskFrame index is not FORMAL_VALIDATED",
    )
    _require(risk_index.get("scenario_id") == scenario_id, "RiskFrame index scenario mismatch")
    _require(
        risk_index.get("commit_id") == risk_window_id,
        "RiskFrame index commit differs from RiskWindow",
    )
    _require(
        risk_index.get("content_digest") == risk_window_digest,
        "RiskFrame index content digest differs from RiskWindow",
    )
    _require(
        risk_index.get("dataset_bundle_id") == bundle_id
        and risk_index.get("dataset_bundle_digest") == bundle_digest,
        "RiskFrame index does not bind the supplied DatasetBundle",
    )
    commit_frame_ids = [
        f.get("risk_id")
        for f in risk_commit.get("frames", [])
        if isinstance(f, dict)
    ]
    _require(
        commit_frame_ids == risk_index.get("frame_ids"),
        "RiskFrame index order differs from RiskWindow commit",
    )

    # --- C plan set + route candidates ---
    _require(
        plan_set.get("schema_version") == PLAN_SET_SCHEMA,
        "plan set is not cd.four-layer-route-plan-set.v3",
    )
    _require(plan_set.get("scenario_id") == scenario_id, "plan set scenario mismatch")
    layer_set_id = plan_set.get("layer_set_id")
    _require(
        route_candidates.get("schema_version") == CANDIDATE_SCHEMA,
        "route candidates is not presentation.route-candidates.v1",
    )
    _require(
        route_candidates.get("layer_set_id") == layer_set_id,
        "route candidate layer_set differs from plan set",
    )
    candidates = route_candidates.get("candidates")
    _require(
        isinstance(candidates, list) and len(candidates) == 12,
        "route candidates must contain exactly 12 routes",
    )
    candidate_scenarios = {
        c.get("provenance", {}).get("scenario_id") for c in candidates if isinstance(c, dict)
    }
    _require(candidate_scenarios <= {scenario_id}, "route candidate provenance scenario mismatch")

    selected_id = route_candidates.get("selected_candidate_id")
    plan_ids = {
        p.get("plan_id")
        for layer in plan_set.get("layers", [])
        for p in (layer.get("plans") or {}).values()
        if isinstance(p, dict)
    }
    _require(selected_id in plan_ids, "route candidate selected_candidate_id is not in plan set")

    return {
        "scenario_id": scenario_id,
        "run_id": run_context.get("run_id"),
        "corridor_id": contract["corridor_id"],
        "dataset_bundle_id": bundle_id,
        "dataset_bundle_digest": bundle_digest,
        "risk_window_id": risk_window_id,
        "risk_window_digest": risk_window_digest,
        "layer_set_id": layer_set_id,
        "selected_candidate_id": selected_id,
    }
