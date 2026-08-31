"""Explicit research-only motion reader for a C route-smoothing sidecar.

The production replay motion contract remains :mod:`vessel_motion`.  This
module accepts only validated v1/v2 research documents and returns a small
diagnostic state.  Callers must use the existing timeline/``vessel_state_at``
path when the sidecar is invalid or unavailable.  The v2 document adds
kinematics and qualification evidence, but does not alter the formal route.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from arctic_route_planning.domain.models import GeoPoint
from arctic_route_planning.grid import initial_bearing_degrees

SIDECAR_SCHEMA_VERSION = "c.research-route-smoothing-sidecar.v1"
SIDECAR_SCHEMA_VERSION_V1 = SIDECAR_SCHEMA_VERSION
SIDECAR_SCHEMA_VERSION_V2 = "c.research-route-smoothing-sidecar.v2"
SUPPORTED_SIDECAR_SCHEMA_VERSIONS = frozenset(
    {SIDECAR_SCHEMA_VERSION_V1, SIDECAR_SCHEMA_VERSION_V2}
)
INTERPOLATION = "linear_lon_lat_between_sidecar_eta_samples_research_only"


@dataclass(frozen=True, slots=True)
class ResearchRouteMotionValidation:
    """Validation result used to decide whether research motion is usable."""

    valid: bool
    reason: str | None
    sample_count: int = 0


@dataclass(frozen=True, slots=True)
class ResearchRouteMotionView:
    """Normalized motion-view payload shared by validated v1 and v2 inputs."""

    schema_version: str
    route_digest: str
    sidecar_digest: str
    motion_samples: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ResearchRouteMotionState:
    """A research motion point; it is not a production vessel state."""

    valid: bool
    status: str
    position: dict[str, float] | None
    sample_index: int | None
    course_degrees: float | None
    fallback_reason: str | None
    speed_knots: float = 0.0
    segment_progress: float | None = None
    interpolation: str = INTERPOLATION


@dataclass(frozen=True, slots=True)
class _Sample:
    longitude: float
    latitude: float
    eta: datetime
    course_degrees: float | None = None
    speed_knots: float | None = None


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


def _finite_float(value: Any) -> float | None:
    """Parse a JSON number without accepting booleans or non-finite values."""

    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _v2_sample(value: Any) -> _Sample | None:
    """Parse the strict v2 motion sample shape."""

    if not isinstance(value, dict):
        return None
    required = ("lon", "lat", "eta", "course_degrees", "speed_knots")
    if any(name not in value for name in required):
        return None
    longitude = _finite_float(value["lon"])
    latitude = _finite_float(value["lat"])
    course = _finite_float(value["course_degrees"])
    speed = _finite_float(value["speed_knots"])
    if (
        longitude is None
        or latitude is None
        or course is None
        or speed is None
        or not -180.0 <= longitude <= 180.0
        or not -90.0 <= latitude <= 90.0
        or not 0.0 <= course < 360.0
        or speed < 0.0
    ):
        return None
    eta = _parse_utc(value["eta"])
    if eta is None:
        return None
    return _Sample(longitude, latitude, eta, course, speed)


def _invalid(reason: str) -> ResearchRouteMotionValidation:
    return ResearchRouteMotionValidation(valid=False, reason=reason)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _v2_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _v2_qualification_value(
    sidecar: dict[str, Any],
    validation: dict[str, Any],
    name: str,
) -> tuple[bool, Any]:
    """Read a v2 qualification field while rejecting conflicting copies."""

    values: list[Any] = []
    for container in (
        sidecar,
        validation,
        sidecar.get("qualification"),
    ):
        if isinstance(container, dict) and name in container:
            values.append(container[name])
    if not values:
        return False, None
    first = values[0]
    if any(value != first for value in values[1:]):
        return False, None
    return True, first


def _v2_gate_is_true(validation: dict[str, Any], *names: str) -> bool:
    """Require one semantic gate and reject a conflicting false alias."""

    sources: list[dict[str, Any]] = [validation]
    nested = validation.get("gates")
    if isinstance(nested, dict):
        sources.append(nested)
    values = [source[name] for source in sources for name in names if name in source]
    return bool(values) and all(value is True for value in values)


def _validate_v2_sidecar(
    sidecar: dict[str, Any],
    *,
    expected_route_digest: str | None,
) -> ResearchRouteMotionValidation:
    """Validate the R1 sidecar without changing the formal route."""

    if sidecar.get("status") != "ACCEPTED":
        return _invalid(str(sidecar.get("fallback_reason") or "sidecar_not_accepted"))
    if any(sidecar.get(name) is not True for name in ("applied", "research_only")):
        return _invalid("sidecar_not_research_only")
    if sidecar.get("research_eligible") is not True:
        return _invalid("research_gate_not_passed")

    validation = sidecar.get("validation")
    if not isinstance(validation, dict):
        return _invalid("missing_validation_gates")
    required_gates = (
        ("research_gate_passed",),
        ("risk_rechecked",),
        ("hard_mask_rechecked",),
        ("coverage_complete",),
        ("eta_recomputed",),
        ("speed_checked",),
        ("curvature_checked", "curve_checked"),
        ("corridor_checked", "corridor_containment_checked"),
        ("kinematics_checked", "manoeuvring_checked"),
    )
    if any(not _v2_gate_is_true(validation, *names) for names in required_gates):
        return _invalid("research_gate_incomplete")

    production_present, production_qualified = _v2_qualification_value(
        sidecar, validation, "production_qualified"
    )
    if not production_present:
        return _invalid("missing_production_qualification")
    if production_qualified is not False:
        return _invalid("production_qualification_not_false")
    calibration_present, calibration_status = _v2_qualification_value(
        sidecar, validation, "calibration_status"
    )
    if not calibration_present:
        return _invalid("missing_calibration_status")
    if calibration_status not in {"NOT_CALIBRATED", "SYNTHETIC_UNCALIBRATED"}:
        return _invalid("unsupported_calibration_status")
    manoeuvring_present, manoeuvring_qualification = _v2_qualification_value(
        sidecar, validation, "manoeuvring_qualification"
    )
    if not manoeuvring_present:
        return _invalid("missing_manoeuvring_qualification")
    if manoeuvring_qualification not in {
        "SYNTHETIC_ONLY",
        "SYNTHETIC_ASSUMPTION_ONLY",
    }:
        return _invalid("unsupported_manoeuvring_qualification")

    raw_digest = sidecar.get("raw_route_digest")
    if not _v2_text(raw_digest):
        return _invalid("missing_raw_route_digest")
    if expected_route_digest is not None and raw_digest != expected_route_digest:
        return _invalid("authoritative_route_digest_mismatch")

    route_identity = sidecar.get("route_identity")
    if not isinstance(route_identity, dict):
        return _invalid("missing_route_identity")
    identity_digest = route_identity.get("route_digest")
    identity_route_id = route_identity.get("route_id")
    if not _v2_text(identity_route_id) or not _v2_text(identity_digest):
        return _invalid("incomplete_route_identity")
    if identity_digest != raw_digest:
        return _invalid("route_identity_digest_mismatch")
    if sidecar.get("route_id") is not None and sidecar.get("route_id") != identity_route_id:
        return _invalid("route_identity_id_mismatch")

    authoritative = sidecar.get("authoritative_route")
    if not isinstance(authoritative, dict):
        return _invalid("missing_authoritative_route")
    if authoritative.get("route_id") != identity_route_id:
        return _invalid("authoritative_route_identity_mismatch")
    if authoritative.get("route_digest") != raw_digest:
        return _invalid("sidecar_route_digest_mismatch")

    values = sidecar.get("motion_samples")
    if not isinstance(values, list) or len(values) < 2:
        return _invalid("missing_motion_samples")
    samples = tuple(_v2_sample(value) for value in values)
    if any(value is None for value in samples):
        return _invalid("invalid_motion_kinematics")
    typed_samples = tuple(value for value in samples if value is not None)
    if any(current.eta <= previous.eta for previous, current in pairwise(typed_samples)):
        return _invalid("non_monotonic_motion_sample_eta")

    curve_digest = sidecar.get("curve_digest")
    geometry_motion_digest = sidecar.get("same_geometry_motion_digest")
    if not _v2_text(curve_digest) or not _v2_text(geometry_motion_digest):
        return _invalid("missing_same_geometry_motion_digest")
    expected_geometry_motion_digest = _canonical_digest(
        {"curve_digest": curve_digest, "motion_samples": values}
    )
    if geometry_motion_digest != expected_geometry_motion_digest:
        return _invalid("same_geometry_motion_digest_invalid")
    geometry_motion_evidence = sidecar.get("same_geometry_motion_evidence")
    if (
        not isinstance(geometry_motion_evidence, dict)
        or geometry_motion_evidence.get("same_geometry_motion_digest")
        != geometry_motion_digest
    ):
        return _invalid("same_geometry_motion_digest_inconsistent")

    declared_digest = sidecar.get("sidecar_digest")
    if not _v2_text(declared_digest):
        return _invalid("missing_sidecar_digest")
    digest_payload = dict(sidecar)
    digest_payload.pop("sidecar_digest", None)
    if _canonical_digest(digest_payload) != declared_digest:
        return _invalid("sidecar_digest_invalid")
    return ResearchRouteMotionValidation(valid=True, reason=None, sample_count=len(typed_samples))


def validate_research_route_sidecar(
    sidecar: Any,
    *,
    expected_route_digest: str | None = None,
) -> ResearchRouteMotionValidation:
    """Validate identity, status, coordinates and monotonic sample ETA."""

    if not isinstance(sidecar, dict):
        return _invalid("sidecar_not_object")
    if sidecar.get("schema_version") == SIDECAR_SCHEMA_VERSION_V2:
        return _validate_v2_sidecar(
            sidecar,
            expected_route_digest=expected_route_digest,
        )
    if sidecar.get("schema_version") != SIDECAR_SCHEMA_VERSION:
        return _invalid("unsupported_sidecar_schema")
    if sidecar.get("research_only") is not True:
        return _invalid("sidecar_not_research_only")
    if sidecar.get("status") != "ACCEPTED" or sidecar.get("applied") is not True:
        return _invalid(str(sidecar.get("fallback_reason") or "sidecar_not_accepted"))
    if sidecar.get("research_eligible") is not True:
        return _invalid("research_gate_not_passed")
    gate = sidecar.get("validation")
    if not isinstance(gate, dict) or gate.get("research_gate_passed") is not True:
        return _invalid("research_gate_not_passed")
    if any(
        gate.get(name) is not True
        for name in (
            "risk_rechecked",
            "hard_mask_rechecked",
            "coverage_complete",
            "eta_recomputed",
            "speed_checked",
        )
    ):
        return _invalid("research_gate_incomplete")
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
    declared_digest = sidecar.get("sidecar_digest")
    if not isinstance(declared_digest, str):
        return _invalid("missing_sidecar_digest")
    digest_payload = dict(sidecar)
    digest_payload.pop("sidecar_digest", None)
    if _canonical_digest(digest_payload) != declared_digest:
        return _invalid("sidecar_digest_invalid")
    return ResearchRouteMotionValidation(valid=True, reason=None, sample_count=len(typed_samples))


def normalize_research_route_sidecar(
    sidecar: Any,
    *,
    expected_route_digest: str | None = None,
) -> dict[str, Any] | None:
    """Normalize a validated v1/v2 sidecar to the existing motion-view shape.

    The returned mapping is a new value and never mutates the sidecar or the
    authoritative route.  v1 samples retain their historical three fields;
    v2 samples retain the additional validated course and speed fields.
    """

    validation = validate_research_route_sidecar(
        sidecar,
        expected_route_digest=expected_route_digest,
    )
    if not validation.valid or not isinstance(sidecar, dict):
        return None
    is_v2 = sidecar.get("schema_version") == SIDECAR_SCHEMA_VERSION_V2
    source_samples = sidecar["motion_samples"]
    normalized_samples: list[dict[str, Any]] = []
    for source in source_samples:
        sample = _v2_sample(source) if is_v2 else _sample(source)
        if sample is None:
            return None
        normalized: dict[str, Any] = {
            "lon": sample.longitude,
            "lat": sample.latitude,
            "eta": source["eta"],
        }
        if is_v2:
            normalized.update(
                {
                    "course_degrees": sample.course_degrees,
                    "speed_knots": sample.speed_knots,
                }
            )
        normalized_samples.append(normalized)
    normalized_view = {
        "schema_version": sidecar["schema_version"],
        "route_digest": sidecar["raw_route_digest"],
        "sidecar_digest": sidecar["sidecar_digest"],
        "interpolation": INTERPOLATION,
        "motion_samples": normalized_samples,
    }
    if is_v2:
        normalized_view["same_geometry_motion_digest"] = sidecar[
            "same_geometry_motion_digest"
        ]
    return normalized_view


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
    parser = _v2_sample if sidecar.get("schema_version") == SIDECAR_SCHEMA_VERSION_V2 else _sample
    samples = tuple(parser(value) for value in sidecar["motion_samples"])
    return validation, tuple(value for value in samples if value is not None)


def _course(start: _Sample, end: _Sample) -> float | None:
    if start.longitude == end.longitude and start.latitude == end.latitude:
        return None
    return initial_bearing_degrees(
        GeoPoint(longitude=start.longitude, latitude=start.latitude),
        GeoPoint(longitude=end.longitude, latitude=end.latitude),
    )


def _interpolate_course(start: float, end: float, fraction: float) -> float:
    """Interpolate v2 headings across the shortest circular arc."""

    delta = (end - start + 180.0) % 360.0 - 180.0
    return (start + delta * fraction) % 360.0


def _haversine_km(start: GeoPoint, end: GeoPoint) -> float:
    from math import asin, cos, radians, sin, sqrt

    lat1 = radians(start.latitude)
    lat2 = radians(end.latitude)
    delta_lat = lat2 - lat1
    delta_lon = radians(end.longitude - start.longitude)
    haversine = (
        sin(delta_lat / 2.0) ** 2
        + cos(lat1) * cos(lat2) * sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * 6_371.0088 * asin(min(1.0, sqrt(haversine)))


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
            speed_knots=0.0,
            segment_progress=0.0,
        )
    if tick >= samples[-1].eta:
        return ResearchRouteMotionState(
            valid=True,
            status="ARRIVED",
            position={"longitude": samples[-1].longitude, "latitude": samples[-1].latitude},
            sample_index=len(samples) - 1,
            course_degrees=None,
            fallback_reason=None,
            speed_knots=0.0,
            segment_progress=1.0,
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
    if (
        sidecar.get("schema_version") == SIDECAR_SCHEMA_VERSION_V2
        and start.course_degrees is not None
        and end.course_degrees is not None
        and start.speed_knots is not None
        and end.speed_knots is not None
    ):
        course_degrees = _interpolate_course(
            start.course_degrees,
            end.course_degrees,
            fraction,
        )
        speed_knots = start.speed_knots + (end.speed_knots - start.speed_knots) * fraction
    else:
        distance_km = _haversine_km(
            GeoPoint(longitude=start.longitude, latitude=start.latitude),
            GeoPoint(longitude=end.longitude, latitude=end.latitude),
        )
        speed_knots = distance_km / (duration / 3600.0) / 1.852
    return ResearchRouteMotionState(
        valid=True,
        status="UNDERWAY",
        position={
            "longitude": start.longitude + (end.longitude - start.longitude) * fraction,
            "latitude": start.latitude + (end.latitude - start.latitude) * fraction,
        },
        sample_index=index,
        course_degrees=(
            course_degrees
            if sidecar.get("schema_version") == SIDECAR_SCHEMA_VERSION_V2
            else _course(start, end)
        ),
        fallback_reason=None,
        speed_knots=speed_knots,
        segment_progress=fraction,
    )


__all__ = [
    "INTERPOLATION",
    "SIDECAR_SCHEMA_VERSION_V1",
    "SIDECAR_SCHEMA_VERSION_V2",
    "SUPPORTED_SIDECAR_SCHEMA_VERSIONS",
    "ResearchRouteMotionState",
    "ResearchRouteMotionValidation",
    "ResearchRouteMotionView",
    "normalize_research_route_sidecar",
    "research_motion_at",
    "validate_research_route_sidecar",
]
