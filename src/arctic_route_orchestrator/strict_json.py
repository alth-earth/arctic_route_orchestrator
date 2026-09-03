"""Strict JSON loading shared by release-facing artifact tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def loads_strict_json(value: str | bytes, *, label: str) -> Any:
    try:
        return json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not strict JSON: {exc}") from exc


def read_strict_json(path: Path, *, label: str) -> Any:
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {label}: {path}: {exc}") from exc
    return loads_strict_json(value, label=f"{label}: {path}")


def read_strict_json_object(path: Path, *, label: str) -> dict[str, Any]:
    value = read_strict_json(path, label=label)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


__all__ = ["loads_strict_json", "read_strict_json", "read_strict_json_object"]
