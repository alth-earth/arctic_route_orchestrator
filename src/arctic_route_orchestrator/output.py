"""Crash-safe publication and semantic identities for orchestration outputs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from arctic_route_contracts import canonical_json_bytes
from arctic_route_planning.publishing import atomic_write_json

from arctic_route_orchestrator.errors import OrchestrationError


def semantic_route_plan_digest(document: Mapping[str, Any]) -> str:
    """Hash route meaning while excluding volatile execution bookkeeping."""

    normalized = dict(document)
    for key in ("plan_id", "planning_request_id", "generated_at"):
        normalized.pop(key, None)
    metrics = normalized.get("metrics")
    if isinstance(metrics, Mapping):
        normalized["metrics"] = dict(metrics)
        normalized["metrics"].pop("compute_ms", None)
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def publish_output_directory(
    output_dir: str | Path,
    documents: Mapping[str, Mapping[str, Any]],
) -> tuple[Path, dict[str, str]]:
    """Publish a complete immutable result directory with the manifest last."""

    if not documents:
        raise OrchestrationError("output_empty", "at least one output document is required")
    normalized_paths = {
        relative_name: _safe_relative_path(relative_name) for relative_name in documents
    }
    if len(set(normalized_paths.values())) != len(normalized_paths):
        raise OrchestrationError("output_path_invalid", "output paths are not unique")
    target = Path(output_dir).resolve()
    if target.exists():
        raise OrchestrationError(
            "output_conflict", f"immutable output directory already exists: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        for relative_name, document in sorted(documents.items()):
            if not isinstance(document, Mapping):
                raise OrchestrationError(
                    "output_document_invalid", f"{relative_name} must contain a JSON object"
                )
            relative = normalized_paths[relative_name]
            atomic_write_json(staging / relative, document)
        checksums = {
            relative_name: _sha256_file(staging / normalized_paths[relative_name])
            for relative_name in sorted(documents)
        }
        sizes = {
            relative_name: (staging / normalized_paths[relative_name]).stat().st_size
            for relative_name in sorted(documents)
        }
        atomic_write_json(
            staging / "checksums.json",
            {
                "schema_version": "orchestrator.checksums.v1",
                "algorithm": "sha256",
                "files": checksums,
                "sizes_bytes": sizes,
                "total_bytes": sum(sizes.values()),
            },
        )
        _fsync_directory(staging)
        os.replace(staging, target)
        _fsync_directory(target.parent)
        return target, checksums
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _safe_relative_path(value: str) -> Path:
    if not isinstance(value, str):
        raise OrchestrationError("output_path_invalid", "output path must be a string")
    pure = PurePosixPath(value)
    if (
        not value
        or pure.is_absolute()
        or pure.as_posix() != value
        or ".." in pure.parts
        or any(part in {"", "."} for part in pure.parts)
        or pure.name == "checksums.json"
    ):
        raise OrchestrationError("output_path_invalid", f"unsafe output path: {value!r}")
    return Path(*pure.parts)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def canonical_pretty_json(value: Mapping[str, Any]) -> bytes:
    """Test helper matching the on-disk human-readable JSON policy."""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


__all__ = [
    "canonical_pretty_json",
    "publish_output_directory",
    "semantic_route_plan_digest",
]
