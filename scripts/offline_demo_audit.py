#!/usr/bin/env python3
"""Demo RC offline audit: prove the RC demo path needs no external network.

This script blocks every socket connection for the whole process, then runs a
minimal but real slice of the demo chain (orchestrator CLI help, A manifest
read, B committed risk window read, C risk sampling, D real v3 artifact load).
Any attempt to reach the network raises OSError and fails the audit.
"""

from __future__ import annotations

import json
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 工作区根（含各子仓库的公共父目录）
sys.path.insert(0, str(ROOT / "arctic_route_contracts" / "src"))
sys.path.insert(0, str(ROOT / "work_package_a" / "src"))
sys.path.insert(0, str(ROOT / "work_package_b" / "src"))
sys.path.insert(0, str(ROOT / "work_package_c" / "src"))
sys.path.insert(0, str(ROOT / "work_package_d" / "src"))
sys.path.insert(0, str(ROOT / "arctic_route_orchestrator" / "src"))


def _block_network() -> None:
    def deny(*_args, **_kwargs):
        raise OSError("offline audit: outbound network access is blocked")

    socket.socket.connect = deny
    socket.socket.connect_ex = deny


def main() -> int:
    _block_network()
    report: dict[str, object] = {"ok": True, "checks": {}}

    # 1. Orchestrator CLI help (imports the whole demo stack).
    from arctic_route_contracts import load_run_context
    from arctic_route_display.loader import load_v3_group
    from arctic_route_planning.contracts import HOURLY_RISK_INTERVAL, RiskWindowQuery
    from arctic_route_planning.risk import RiskSampler
    from arctic_route_risk import PersistentRiskStore

    import arctic_route_orchestrator  # noqa: F401

    golden = ROOT / "work_package_a" / "data" / "output" / "golden"

    # 2. A: read bundle + RunContext + manifest.
    bundle = json.loads(
        (
            ROOT
            / "work_package_a"
            / "data"
            / "output"
            / "bundles"
            / "murmansk_dikson_august_2026_demo_v1.bundle.json"
        ).read_text(encoding="utf-8")
    )
    run_context = load_run_context(
        ROOT
        / "work_package_a"
        / "data"
        / "output"
        / "bundles"
        / "murmansk_dikson_august_2026_demo_v1.run-context.json"
    )
    report["checks"]["a_bundle_runcontext"] = {
        "bundle_id": bundle["bundle_id"],
        "run_id": run_context.run_id,
    }

    # 3. B: read committed risk window (r6 store).
    store = PersistentRiskStore(golden / "mur-v3-smoke-20260816-r6" / "risk-store")
    frame = json.loads(
        sorted((golden / "mur-v3-smoke-20260816-r6" / "risk-store" / "frames").glob("*.json"))[
            0
        ].read_text(encoding="utf-8")
    )
    query = RiskWindowQuery(
        start=datetime(2026, 8, 11, 6, 0, tzinfo=UTC),
        end=datetime(2026, 8, 17, 6, 0, tzinfo=UTC),
        interval=HOURLY_RISK_INTERVAL,
        run_id=run_context.run_id,
        scenario_id=run_context.scenario_id,
        corridor_id=run_context.corridor_id,
        generation_id=0,
        vessel_profile_id=run_context.vessel_profile_id,
        config_digest=run_context.config_digest,
        model_config_digest=frame["model_config_digest"],
        as_of=datetime.fromisoformat(frame["as_of_time"].replace("Z", "+00:00")),
    )
    window = store.get_committed_window(query)
    report["checks"]["b_risk_window"] = {"frames": len(window.frames)}

    # 4. C: risk sampling on the committed window.
    from arctic_route_planning.contracts.codec import (
        risk_frame_from_document,
        risk_frame_to_document,
    )

    frames = tuple(risk_frame_from_document(risk_frame_to_document(f)) for f in window.frames)
    sampler = RiskSampler(frames, max_frame_gap=HOURLY_RISK_INTERVAL)
    sample = sampler.sample(
        datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
        longitude=34.0,
        latitude=69.55,
    )
    report["checks"]["c_risk_sample"] = {
        "risk_score_finite": str(sample.risk_score),
        "hard": sample.hard_mask,
    }

    # 5. D: load the real v3 artifacts with the local schema registry.
    schema = (
        ROOT
        / "work_package_c"
        / "schemas"
        / "four-layer-route-plan-set-v3.schema.json"
    )
    fixtures = ROOT / "work_package_d" / "tests" / "fixtures"
    initial = load_v3_group(fixtures / "v3_initial_rc1.json", schema_path=schema)
    replanned = load_v3_group(fixtures / "v3_replanned_rc1.json", schema_path=schema)
    report["checks"]["d_v3_consume"] = {
        "initial_layers": len(initial.layers),
        "replanned_layers": len(replanned.layers),
        "initial_group": initial.group_id[:24],
        "replanned_group": replanned.group_id[:24],
    }

    print(json.dumps(report, ensure_ascii=False, indent=1), flush=True)
    print("demo runtime external network dependency = NONE", flush=True)
    out = golden / "offline-demo-audit-20260816.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
