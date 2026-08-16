"""Immutable execution inputs owned by the root orchestrator."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_orchestrator.errors import OrchestrationError

_RUN_ID = re.compile(
    r"^run-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "scenario_id",
        "generation_id",
        "input_revision",
        "generated_at",
        "planning_contract",
        "max_snap_km",
        "replan_after_hours",
        "per_stage_timeout_seconds",
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    """Deterministic runtime values excluded from A/B/C configuration contracts."""

    schema_version: str
    run_id: str
    scenario_id: str
    generation_id: int
    input_revision: int
    generated_at: datetime
    planning_contract: str
    max_snap_km: float = 150.0
    replan_after_hours: int = 6
    per_stage_timeout_seconds: float = 900.0

    def __post_init__(self) -> None:
        for name in ("schema_version", "run_id", "scenario_id", "planning_contract"):
            if not isinstance(getattr(self, name), str):
                raise OrchestrationError(
                    "execution_spec_invalid", f"{name} must be a string"
                )
        if self.schema_version != "orchestrator.execution-spec.v1":
            raise OrchestrationError("execution_spec_invalid", "unsupported schema_version")
        if _RUN_ID.fullmatch(self.run_id) is None:
            raise OrchestrationError("execution_spec_invalid", "run_id must be run-UUID")
        if not self.scenario_id.strip():
            raise OrchestrationError("execution_spec_invalid", "scenario_id must be non-empty")
        if self.planning_contract not in {
            "cd.route-plan.v2",
            "cd.four-layer-route-plan-set.v3",
        }:
            raise OrchestrationError(
                "execution_spec_invalid", "planning_contract must select formal v2 or v3"
            )
        for name in ("generation_id", "input_revision"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise OrchestrationError(
                    "execution_spec_invalid", f"{name} must be a non-negative integer"
                )
        value = self.generated_at
        if not isinstance(value, datetime):
            raise OrchestrationError(
                "execution_spec_invalid", "generated_at must be a datetime"
            )
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise OrchestrationError("execution_spec_invalid", "generated_at must be UTC")
        object.__setattr__(self, "generated_at", value.astimezone(UTC))
        if (
            isinstance(self.max_snap_km, bool)
            or not isinstance(self.max_snap_km, int | float)
            or not math.isfinite(self.max_snap_km)
            or self.max_snap_km < 0
        ):
            raise OrchestrationError(
                "execution_spec_invalid", "max_snap_km must be finite and non-negative"
            )
        object.__setattr__(self, "max_snap_km", float(self.max_snap_km))
        if (
            isinstance(self.replan_after_hours, bool)
            or not isinstance(self.replan_after_hours, int)
            or self.replan_after_hours <= 0
        ):
            raise OrchestrationError(
                "execution_spec_invalid", "replan_after_hours must be a positive integer"
            )
        if (
            isinstance(self.per_stage_timeout_seconds, bool)
            or not isinstance(self.per_stage_timeout_seconds, int | float)
            or not math.isfinite(self.per_stage_timeout_seconds)
            or self.per_stage_timeout_seconds <= 0
        ):
            raise OrchestrationError(
                "execution_spec_invalid",
                "per_stage_timeout_seconds must be finite and positive",
            )
        object.__setattr__(
            self,
            "per_stage_timeout_seconds",
            float(self.per_stage_timeout_seconds),
        )

    @classmethod
    def from_path(cls, path: str | Path) -> ExecutionSpec:
        location = Path(path)
        try:
            value = json.loads(
                location.read_text(encoding="utf-8"),
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_non_finite_constant,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise OrchestrationError(
                "execution_spec_invalid", f"cannot read {location}"
            ) from exc
        if not isinstance(value, dict):
            raise OrchestrationError("execution_spec_invalid", "document must be an object")
        if set(value) != _FIELDS:
            raise OrchestrationError(
                "execution_spec_invalid",
                f"fields differ: missing={sorted(_FIELDS - set(value))}, "
                f"extra={sorted(set(value) - _FIELDS)}",
            )
        try:
            raw_generated_at = value["generated_at"]
            if not isinstance(raw_generated_at, str) or _UTC_TIMESTAMP.fullmatch(
                raw_generated_at
            ) is None:
                raise ValueError("generated_at must be canonical UTC text")
            generated_at = datetime.fromisoformat(raw_generated_at.replace("Z", "+00:00"))
            return cls(
                schema_version=value["schema_version"],
                run_id=value["run_id"],
                scenario_id=value["scenario_id"],
                generation_id=value["generation_id"],
                input_revision=value["input_revision"],
                generated_at=generated_at,
                planning_contract=value["planning_contract"],
                max_snap_km=value["max_snap_km"],
                replan_after_hours=value["replan_after_hours"],
                per_stage_timeout_seconds=value["per_stage_timeout_seconds"],
            )
        except (TypeError, ValueError) as exc:
            raise OrchestrationError(
                "execution_spec_invalid", "field values are malformed"
            ) from exc

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "generation_id": self.generation_id,
            "input_revision": self.input_revision,
            "generated_at": self.generated_at.isoformat().replace("+00:00", "Z"),
            "planning_contract": self.planning_contract,
            "max_snap_km": self.max_snap_km,
            "replan_after_hours": self.replan_after_hours,
            "per_stage_timeout_seconds": self.per_stage_timeout_seconds,
        }


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")
