"""Explicit research-only motion reader for a C route-smoothing sidecar.

The production replay motion contract remains :mod:`vessel_motion`.  This
module accepts only a validated ``c.research-route-smoothing-sidecar.v1``
document and returns a small diagnostic state.  Callers must use the existing
timeline/``vessel_state_at`` path when the sidecar is invalid or unavailable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from arctic_route_planning.domain.models import GeoPoint
from arctic_route_planning.grid import initial_bearing_degrees

SIDECAR_SCHEMA_VERSION = "c.research-route-smoothing-sidecar.v1"
INTERPOLATION = "linear_lon_lat_between_sidecar_eta_samples_research_only"


@dataclass(frozen=True, slots=True)
class ResearchRouteMotionValidation:
    """Validation result used to decide whether research motion is usable."""

    valid: bool
    reason: str | None
    sample_count: int = 0


@dataclass(frozen=True, slots=True)
class ResearchRouteMotionState:
    """A research motion point; it is not a production vessel state."""

    valid: bool
    status: str
    position: dict[str, float] | None
    sample_index: int | None
    course_degrees: float | None
    fallback_reason: str | None
    interpolation: str = INTERPOLATION


@dataclass(frozen=True, slots=True)
class _Sample:
    longitude: float
    latitude: float
    eta: datetime


def _parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _sample(value: Any) -> _Sample | None:
    if not isinstance(value, dict):
        return None
    try:
        longitude = float(value.get("lon", value.get("longitude")))
        latitude = float(value.get("lat", value.get("latitude")))
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or not -180.0 <= longitude <= 180.0
        or not -90.0 <= latitude <= 90.0
    ):
        return None
    eta = _parse_utc(value.get("eta"))
    if eta is None:
        return None
    return _Sample(longitude, latitude, eta)


def _invalid(reason: str) -> ResearchRouteMotionValidation:
    return ResearchRouteMotionValidation(valid=False, reason=reason)


def validate_research_route_sidecar(
    sidecar: Any,
    *,
    expected_route_digest: str | None = None,
) -> ResearchRouteMotionValidation:
    """Validate identity, status, coordinates and monotonic sample ETA."""

    if not isinstance(sidecar, dict):
        return _invalid("sidecar_not_object")
    if sidecar.get("schema_version") != SIDECAR_SCHEMA_VERSION:
        return _invalid("unsupported_sidecar_schema")
    if sidecar.get("research_only") is not True:
        return _invalid("sidecar_not_research_only")
    if sidecar.get("status") != "ACCEPTED" or sidecar.get("applied") is not True:
        return _invalid(str(sidecar.get("fallback_reason") or "sidecar_not_accepted"))
    raw_digest = sidecar.get("raw_route_digest")
    if not isinstance(raw_digest, str) or not raw_digest:
        return _invalid("missing_raw_route_digest")
    if expected_route_digest is not None and raw_digest != expected_route_digest:
        return _invalid("authoritative_route_digest_mismatch")
    authoritative = sidecar.get("authoritative_route")
    if not isinstance(authoritative, dict):
        return _invalid("missing_authoritative_route")
    if authoritative.get("route_digest") != raw_digest:
        return _invalid("sidecar_route_digest_mismatch")
    values = sidecar.get("motion_samples")
    if not isinstance(values, list) or len(values) < 2:
        return _invalid("missing_motion_samples")
    samples = tuple(_sample(value) for value in values)
    if any(value is None for value in samples):
        return _invalid("invalid_motion_sample")
    typed_samples = tuple(value for value in samples if value is not None)
    if any(
        current.eta <= previous.eta
        for previous, current in pairwise(typed_samples)
    ):
        return _invalid("non_monotonic_motion_sample_eta")
    return ResearchRouteMotionValidation(valid=True, reason=None, sample_count=len(typed_samples))


def _validated_samples(
    sidecar: dict[str, Any],
    *,
    expected_route_digest: str | None,
) -> tuple[ResearchRouteMotionValidation, tuple[_Sample, ...]]:
    validation = validate_research_route_sidecar(
        sidecar,
        expected_route_digest=expected_route_digest,
    )
    if not validation.valid:
        return validation, ()
    samples = tuple(_sample(value) for value in sidecar["motion_samples"])
    return validation, tuple(value for value in samples if value is not None)


def _course(start: _Sample, end: _Sample) -> float | None:
    if start.longitude == end.longitude and start.latitude == end.latitude:
        return None
    return initial_bearing_degrees(
        GeoPoint(longitude=start.longitude, latitude=start.latitude),
        GeoPoint(longitude=end.longitude, latitude=end.latitude),
    )


def research_motion_at(
    tick: datetime,
    sidecar: Any,
    *,
    expected_route_digest: str | None = None,
) -> ResearchRouteMotionState:
    """Interpolate a validated sidecar point, or return explicit fallback."""

    if tick.tzinfo is None or tick.utcoffset() is None:
        raise ValueError("research motion tick must be timezone-aware UTC")
    tick = tick.astimezone(UTC)
    if not isinstance(sidecar, dict):
        return ResearchRouteMotionState(
            valid=False,
            status="FALLBACK",
            position=None,
            sample_index=None,
            course_degrees=None,
            fallback_reason="sidecar_not_object",
        )
    validation, samples = _validated_samples(
        sidecar,
        expected_route_digest=expected_route_digest,
    )
    if not validation.valid:
        return ResearchRouteMotionState(
            valid=False,
            status="FALLBACK",
            position=None,
            sample_index=None,
            course_degrees=None,
            fallback_reason=validation.reason,
        )
    if tick < samples[0].eta:
        return ResearchRouteMotionState(
            valid=True,
            status="NOT_STARTED",
            position={"longitude": samples[0].longitude, "latitude": samples[0].latitude},
            sample_index=0,
            course_degrees=None,
            fallback_reason=None,
        )
    if tick >= samples[-1].eta:
        return ResearchRouteMotionState(
            valid=True,
            status="ARRIVED",
            position={"longitude": samples[-1].longitude, "latitude": samples[-1].latitude},
            sample_index=len(samples) - 1,
            course_degrees=None,
            fallback_reason=None,
        )
    index = 0
    for candidate in range(len(samples) - 1):
        if samples[candidate].eta <= tick < samples[candidate + 1].eta:
            index = candidate
            break
    start = samples[index]
    end = samples[index + 1]
    duration = (end.eta - start.eta).total_seconds()
    fraction = (tick - start.eta).total_seconds() / duration
    return ResearchRouteMotionState(
        valid=True,
        status="UNDERWAY",
        position={
            "longitude": start.longitude + (end.longitude - start.longitude) * fraction,
            "latitude": start.latitude + (end.latitude - start.latitude) * fraction,
        },
        sample_index=index,
        course_degrees=_course(start, end),
        fallback_reason=None,
    )


__all__ = [
    "INTERPOLATION",
    "ResearchRouteMotionState",
    "ResearchRouteMotionValidation",
    "research_motion_at",
    "validate_research_route_sidecar",
]
