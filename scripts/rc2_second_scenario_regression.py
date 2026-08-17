"""RC2 Scenario B (Tromso -> Isfjorden) regression check.

Usage:
    python scripts/rc2_second_scenario_regression.py \
        /root/my_project/work_package_a/data/output/rc2-smoke/output-tromso-144h \
        [golden.json]

Validates the Tromso 144 h v3 qualification artifact: coverage preflight gate,
risk window shape, route invariants, checksums, D consumption, and (when a
golden JSON is supplied) layer-set semantic digests.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from arctic_route_display.loader import load_coverage_preflight, load_v3_group


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_checksums(output_dir: Path) -> list[str]:
    failures: list[str] = []
    manifest = json.loads((output_dir / "checksums.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["files"].items():
        actual = _sha256_file(output_dir / relative)
        if actual != expected:
            failures.append(f"checksum mismatch: {relative}")
    return failures


def _document_semantic_digest(path: Path) -> str:
    document = json.loads(path.read_text(encoding="utf-8"))
    for key in ("layer_set_id", "planning_request_id", "generated_at"):
        document.pop(key, None)
    for layer in document.get("layers", []):
        for plan in layer.get("plans", {}).values():
            for key in ("layer_set_id", "planning_request_id", "generated_at"):
                plan.pop(key, None)
            metrics = plan.get("metrics")
            if isinstance(metrics, dict):
                metrics.pop("compute_ms", None)
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main(output_dir: str, golden_path: str | None = None) -> int:
    out = Path(output_dir)
    failures: list[str] = []

    report = json.loads((out / "run-report.json").read_text(encoding="utf-8"))
    if report.get("status") != "success":
        failures.append("run-report status != success")
    if report.get("planning_contract") != "cd.four-layer-route-plan-set.v3":
        failures.append("planning contract is not v3")
    for phase in ("initial", "replanned"):
        routes = report["routes"][phase]
        if len(routes) != 12:
            failures.append(f"{phase} route count != 12")
        for route in routes:
            if not route.get("layer_goal_reached"):
                failures.append(f"{phase} route missing layer_goal_reached")
            if route["metrics"]["hard_constraint_violations"] != 0:
                failures.append(f"{phase} route has hard violations")
    if not report["replanning"]["triggered"] or not report["replanning"]["published"]:
        failures.append("replanning not triggered/published")

    preflight = json.loads(
        (out / "planning-coverage-preflight.json").read_text(encoding="utf-8")
    )
    if not preflight["gate_passed"]:
        failures.append("coverage preflight gate failed")
    if preflight["frames_checked"] != 145:
        failures.append("coverage preflight frames != 145")
    if any(frame["unknown_navigable_nodes"] != 0 for frame in preflight["frames"]):
        failures.append("unknown-navigable nodes found in coverage preflight")
    if not any(
        frame.get("ice_free_neutralized_nodes", 0) > 0 for frame in preflight["frames"]
    ):
        failures.append("ice-free neutralization provenance missing in preflight")

    full_commit = json.loads(
        (out / "risk" / "full-window-commit.json").read_text(encoding="utf-8")
    )
    if full_commit["count"] != 145:
        failures.append("full risk window count != 145")

    for phase in ("initial", "replanned"):
        route_file = out / "routes" / "v3" / f"{phase}.json"
        try:
            view = load_v3_group(route_file)
            if not view.is_complete:
                failures.append(f"D load incomplete for {phase}")
        except Exception as exc:
            failures.append(f"D load failed for {phase}: {exc}")
    try:
        load_coverage_preflight(out / "planning-coverage-preflight.json")
    except Exception as exc:
        failures.append(f"D coverage load failed: {exc}")

    failures.extend(_check_checksums(out))

    digests: dict[str, str] = {}
    for phase in ("initial", "replanned"):
        route_file = out / "routes" / "v3" / f"{phase}.json"
        document = json.loads(route_file.read_text(encoding="utf-8"))
        digests[phase] = document.get("layer_set_id", "")
        embedded = document.get("layer_set_id", "")
        actual = f"layer-set-sha256-{_document_semantic_digest(route_file)}"
        if embedded != actual:
            failures.append(f"{phase} embedded digest != computed semantic digest")

    if golden_path is not None:
        golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
        for phase in ("initial", "replanned"):
            expected = golden.get("layer_set_ids", {}).get(phase)
            if expected and digests.get(phase) != expected:
                failures.append(
                    f"{phase} digest {digests.get(phase)} != golden {expected}"
                )

    if failures:
        print("RC2 SECOND SCENARIO REGRESSION: FAIL")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("RC2 SECOND SCENARIO REGRESSION: PASS")
    print(f"  initial={digests['initial']}")
    print(f"  replanned={digests['replanned']}")
    print("  coverage gate=true, frames=145, ice-free provenance present")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    raise SystemExit(main(args[0], args[1] if len(args) > 1 else None))
