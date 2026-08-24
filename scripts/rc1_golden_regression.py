"""RC1 golden regression check against frozen r6/r7 v3 artifacts.

Usage:
    python -m arctic_route_orchestrator.scripts.rc1_golden_regression \
        ${ARCTIC_ROUTE_ROOT}/work_package_a/data/output/golden

Checks that the frozen r6/r7 layer-set digests still match the documented RC1
identifiers and that r6/r7 business payloads remain semantically identical.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

EXPECTED = {
    "initial": "layer-set-sha256-51824e965e7914427cba3b1ad191c0f4498823beadf0451db37209dbdf7bc11f",
    "replanned": (
        "layer-set-sha256-ec74a1454bee0f4511fc6d9e53a889d810c8f4b292c1c936e6ed9dbc11831c2f"
    ),
}


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


def _check_checksums(output_dir: Path) -> bool:
    manifest = json.loads((output_dir / "checksums.json").read_text(encoding="utf-8"))
    ok = True
    for relative, expected in manifest["files"].items():
        payload = (output_dir / relative).read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected:
            print(f"  checksum mismatch: {relative}")
            ok = False
    return ok


def main(golden_root: str) -> int:
    root = Path(golden_root)
    failures: list[str] = []
    payloads: dict[str, list[dict[str, object]]] = {}
    for run in ("r6", "r7"):
        output_dir = root / f"mur-v3-smoke-20260816-{run}" / "output"
        for phase, expected_id in EXPECTED.items():
            route_file = output_dir / "routes" / "v3" / f"{phase}.json"
            if not route_file.is_file():
                failures.append(f"{run}/{phase}: missing {route_file}")
                continue
            document = json.loads(route_file.read_text(encoding="utf-8"))
            embedded = document.get("layer_set_id")
            actual = _document_semantic_digest(route_file)
            if embedded != expected_id:
                failures.append(
                    f"{run}/{phase}: embedded layer_set_id {embedded} != {expected_id}"
                )
            if f"layer-set-sha256-{actual}" != expected_id:
                failures.append(
                    f"{run}/{phase}: semantic digest {actual} != {expected_id}"
                )
            payloads.setdefault(phase, []).append(
                {
                    "run": run,
                    "layer_set_id": embedded,
                    "semantic_digest": actual,
                    "route_count": len(document.get("layers", [])) * 3,
                }
            )
        if not _check_checksums(output_dir):
            failures.append(f"{run}: checksums.json mismatch")
    if payloads.get("initial") and payloads.get("replanned"):
        for phase, items in payloads.items():
            first = items[0]["semantic_digest"]
            if any(item["semantic_digest"] != first for item in items[1:]):
                failures.append(f"{phase}: r6/r7 semantic digests differ")
    if failures:
        print("RC1 GOLDEN REGRESSION: FAIL")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("RC1 GOLDEN REGRESSION: PASS")
    for phase, items in payloads.items():
        print(
            f"  {phase}: "
            f"r6={items[0]['semantic_digest'][:16]}… "
            f"r7={items[1]['semantic_digest'][:16]}… "
            f"routes={items[0]['route_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
