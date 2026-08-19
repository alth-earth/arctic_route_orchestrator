"""Thin replay -> presentation adapter (Strategy B viewer foundation).

The adapter translated the causal-replay artifacts into a stable
presentation contract.  It does NOT recompute the planner, reinterpret risk
business rules, invent vessel speed, or change adoption timing.  It only
projects backend business semantics into a small set of viewer-friendly
dataclasses.

Motion contract for arbitrary render time: the vessel position is always
derived from the accepted route waypoint ETAs via ``vessel_state_at``.  The
snapshot cadence (1h) therefore never equals the vessel render cadence; the
adapter can answer any ``simulation_time`` between snapshots.

Route payload schema (added to ``NavigationExecutionState`` on latest HEAD):

    {"distance_km": float | None,
     "waypoints": [{"longitude": float, "latitude": float, "eta": str}]}

ETAs in ``accepted_route`` are expressed in physical simulation-clock time,
so ``vessel_state_at(t, accepted_route)`` reproduces the runner's motion
exactly.  Replays produced before this schema contain no route payload; the
adapter then answers snapshot-time states only and refuses to invent an
arbitrary-time position.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from arctic_route_orchestrator.replay.vessel_motion import vessel_state_at

PHYSICAL_POSITION_SOURCE = "accepted_route_eta_linear_interpolation"
ROUTE_CHANGED_EVENTS = frozenset({"ROUTE_CHANGED", "REPLAN_ADOPTED"})
DECISION_EVENTS = frozenset({"REPLAN_TRIGGERED", "REPLAN_DECIDED"})


class PresentationDataError(ValueError):
    """Raised when a snapshot lacks data needed for a presentation query."""


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _document(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value)


@dataclass(frozen=True, slots=True)
class _Waypoint:
    longitude: float
    latitude: float
    eta: datetime


def _as_waypoints(waypoints: Iterable[dict[str, Any]]) -> tuple[_Waypoint, ...]:
    return tuple(
        _Waypoint(
            longitude=float(item["longitude"]),
            latitude=float(item["latitude"]),
            eta=_parse_utc(item["eta"]),
        )
        for item in waypoints
    )


@dataclass(frozen=True, slots=True)
class PresentationVessel:
    status: str
    longitude: float | None
    latitude: float | None
    speed_mps: float | None
    speed_knots: float | None
    current_edge_index: int | None
    edge_progress: float | None
    executed_distance_km: float | None
    cumulative_travelled_km: float | None
    remaining_distance_km: float | None
    physical_position_source: str = PHYSICAL_POSITION_SOURCE

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "lon": self.longitude,
            "lat": self.latitude,
            "speed_mps": self.speed_mps,
            "speed_knots": self.speed_knots,
            "current_edge_index": self.current_edge_index,
            "edge_progress": self.edge_progress,
            "executed_distance_km": self.executed_distance_km,
            "cumulative_travelled_km": self.cumulative_travelled_km,
            "remaining_distance_km": self.remaining_distance_km,
            "physical_position_source": self.physical_position_source,
        }


@dataclass(frozen=True, slots=True)
class PresentationSegment:
    index: int | None
    start: dict[str, float] | None
    end: dict[str, float] | None
    start_eta: str | None
    end_eta: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "start_eta": self.start_eta,
            "end_eta": self.end_eta,
        }


@dataclass(frozen=True, slots=True)
class PresentationPlan:
    accepted_plan_revision: int
    completed_track: list[dict[str, Any]]
    current_authoritative_segment: PresentationSegment
    accepted_future_route: list[dict[str, Any]]
    route_distance_km: float | None
    active_plan_revision: int | None = None
    pending_plan_revision: int | None = None
    pending_plan_status: str | None = None
    pending_candidate: dict[str, Any] | None = None
    pending_adoption: dict[str, Any] | None = None
    superseded_future_route: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_plan_revision": self.accepted_plan_revision,
            "active_plan_revision": (
                self.active_plan_revision
                if self.active_plan_revision is not None
                else self.accepted_plan_revision
            ),
            "pending_plan_revision": self.pending_plan_revision,
            "pending_plan_status": self.pending_plan_status,
            "completed_track": self.completed_track,
            "current_authoritative_segment": self.current_authoritative_segment.to_dict(),
            "accepted_future_route": self.accepted_future_route,
            "route_distance_km": self.route_distance_km,
            "pending_candidate": self.pending_candidate,
            "pending_adoption": self.pending_adoption,
            "superseded_future_route": self.superseded_future_route,
        }


@dataclass(frozen=True, slots=True)
class PresentationRisk:
    risk_content_revision: int
    risk_window_revision: int
    current_resource: str | None
    current_resource_digest: str | None
    available_valid_range: list[str] | None
    hard_reason_resource: str | None
    presentation_horizons: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_content_revision": self.risk_content_revision,
            "risk_window_revision": self.risk_window_revision,
            "current_resource": self.current_resource,
            "current_resource_digest": self.current_resource_digest,
            "available_valid_range": self.available_valid_range,
            "hard_reason_resource": self.hard_reason_resource,
            "presentation_horizons": self.presentation_horizons,
        }


@dataclass(frozen=True, slots=True)
class PresentationEvent:
    type: str
    simulation_time: str
    revision: str | None = None
    description: str = ""
    observed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "simulation_time": self.simulation_time,
            "revision": self.revision,
            "description": self.description,
            "observed": self.observed,
        }


@dataclass(frozen=True, slots=True)
class PresentationState:
    snapshot_index: int
    simulation_time: str
    knowledge_as_of: str
    scenario_mode: str
    vessel: PresentationVessel
    plan: PresentationPlan
    risk: PresentationRisk
    events: list[PresentationEvent]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_index": self.snapshot_index,
            "simulation_time": self.simulation_time,
            "knowledge_as_of": self.knowledge_as_of,
            "scenario_mode": self.scenario_mode,
            "vessel": self.vessel.to_dict(),
            "plan": self.plan.to_dict(),
            "risk": self.risk.to_dict(),
            "events": [event.to_dict() for event in self.events],
        }


class PresentationAdapter:
    """Read a replay manifest + snapshots into the presentation contract."""

    def __init__(
        self,
        manifest: Any,
        snapshots: Iterable[Any],
    ) -> None:
        self.manifest = _document(manifest)
        self.snapshots = [
            _document(snapshot) for snapshot in snapshots
        ]
        self.snapshots.sort(key=lambda item: _parse_utc(item["simulation_time"]))
        if not self.snapshots:
            raise PresentationDataError("no snapshots provided")
        self._snapshot_times = [
            _parse_utc(snapshot["simulation_time"]) for snapshot in self.snapshots
        ]
        self._events = self._collect_events()
        self._route_changes = self._build_route_changes()
        self._routes_by_revision = self._build_route_index()

    def _collect_events(self) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, str | None]] = set()
        events: list[dict[str, Any]] = []
        for event in self.manifest.get("events", ()):
            item = dict(event)
            key = (
                item.get("type", ""),
                item.get("simulation_time", ""),
                item.get("revision"),
            )
            if key not in seen:
                seen.add(key)
                events.append(item)
        events.sort(key=lambda item: (_parse_utc(item["simulation_time"]), item["type"]))
        return events

    def _build_route_changes(self) -> list[dict[str, Any]]:
        changes = [
            event
            for event in self._events
            if event["type"] == "ROUTE_CHANGED"
            and event.get("revision") not in (None, "")
        ]
        changes.sort(key=lambda item: (_parse_utc(item["simulation_time"]), item["type"]))
        return changes

    def _build_route_index(self) -> dict[int, dict[str, Any]]:
        index: dict[int, dict[str, Any]] = {}
        for snapshot in self.snapshots:
            ship = snapshot.get("ship_state") or {}
            revision = ship.get("accepted_plan_revision")
            payload = ship.get("accepted_route")
            if revision is None or not payload:
                continue
            revision = int(revision)
            if revision not in index:
                index[revision] = {"snapshot": snapshot, "route": payload}
        return index

    @property
    def replay_start(self) -> datetime:
        return _parse_utc(self.manifest.get("replay_start", self.snapshots[0]["simulation_time"]))

    @property
    def replay_end(self) -> datetime:
        return _parse_utc(self.manifest.get("replay_end", self.snapshots[-1]["simulation_time"]))

    def events(self, *, up_to: datetime | str | None = None) -> list[PresentationEvent]:
        cutoff = _parse_utc(up_to) if isinstance(up_to, str) else up_to
        result: list[PresentationEvent] = []
        for event in self._events:
            moment = _parse_utc(event["simulation_time"])
            if cutoff is not None and moment > cutoff:
                break
            result.append(
                PresentationEvent(
                    type=event.get("type", ""),
                    simulation_time=event.get("simulation_time", ""),
                    revision=event.get("revision"),
                    description=event.get("description", ""),
                    observed=bool(event.get("observed", False)),
                )
            )
        return result

    def _snapshot_at(self, tick: datetime) -> dict[str, Any]:
        if tick <= self._snapshot_times[0]:
            return self.snapshots[0]
        if tick >= self._snapshot_times[-1]:
            return self.snapshots[-1]
        position = bisect.bisect_right(self._snapshot_times, tick) - 1
        return self.snapshots[max(0, position)]

    def _active_revision_at(self, tick: datetime) -> int:
        revision = 1
        for change in self._route_changes:
            if _parse_utc(change["simulation_time"]) <= tick:
                revision = int(change["revision"])
            else:
                break
        return revision

    def _route_for_revision(self, revision: int) -> dict[str, Any] | None:
        entry = self._routes_by_revision.get(int(revision))
        return entry["route"] if entry else None

    def vessel_at(self, tick: datetime | str) -> dict[str, Any]:
        """Physical vessel state at any simulation time (route-ETA driven)."""

        moment = _parse_utc(tick) if isinstance(tick, str) else tick
        if moment.tzinfo is None or moment.utcoffset() is None:
            moment = moment.replace(tzinfo=UTC)
        revision = self._active_revision_at(moment)
        route = self._route_for_revision(revision)
        if route is None:
            raise PresentationDataError(
                f"accepted_route unavailable for plan revision {revision}; "
                "arbitrary-time vessel motion requires the latest HEAD replay schema"
            )
        waypoints = _as_waypoints(route.get("waypoints", ()))
        if not waypoints:
            raise PresentationDataError(
                f"accepted_route for revision {revision} has no waypoints"
            )
        total = route.get("distance_km")
        state = vessel_state_at(
            moment,
            waypoints,
            total_distance_km=float(total) if total is not None else None,
        )
        position = state.position
        return {
            "status": state.status,
            "longitude": position["longitude"],
            "latitude": position["latitude"],
            "speed_mps": state.speed_mps,
            "speed_knots": state.speed_knots,
            "current_edge_index": state.edge_index,
            "edge_progress": state.edge_progress,
            "segment_start_eta": state.segment_start_eta,
            "segment_end_eta": state.segment_end_eta,
            "executed_distance_km": state.executed_distance_km,
            "remaining_distance_km": state.remaining_distance_km,
            "course_degrees": state.course_degrees,
            "physical_position_source": PHYSICAL_POSITION_SOURCE,
        }

    def state_at(self, tick: datetime | str) -> PresentationState:
        """Presentation state anchored at the nearest snapshot <= ``tick``."""

        moment = _parse_utc(tick) if isinstance(tick, str) else tick
        if moment.tzinfo is None or moment.utcoffset() is None:
            moment = moment.replace(tzinfo=UTC)
        snapshot = self._snapshot_at(moment)
        snapshot_index = int(snapshot["snapshot_index"])
        simulation_time = snapshot["simulation_time"]
        ship = snapshot.get("ship_state") or {}
        risk = snapshot.get("risk") or {}

        live_vessel: dict[str, Any] | None = None
        try:
            live_vessel = self.vessel_at(moment)
        except PresentationDataError:
            live_vessel = None
            snapshot_moment = _parse_utc(simulation_time)
            if moment != snapshot_moment:
                raise PresentationDataError(
                    "arbitrary-time vessel motion requires the latest HEAD "
                    "replay schema (accepted_route); refusing to invent speed"
                ) from None

        if live_vessel is not None:
            snapshot_vessel = self.vessel_at(_parse_utc(simulation_time))
            cumulative = ship.get("cumulative_travelled_km")
            if (
                cumulative is not None
                and snapshot_vessel.get("executed_distance_km") is not None
            ):
                cumulative_live = float(cumulative) + max(
                    0.0,
                    float(live_vessel["executed_distance_km"])
                    - float(snapshot_vessel["executed_distance_km"]),
                )
            else:
                cumulative_live = (
                    float(cumulative) if cumulative is not None else None
                )
            vessel = PresentationVessel(
                status=live_vessel["status"],
                longitude=live_vessel["longitude"],
                latitude=live_vessel["latitude"],
                speed_mps=live_vessel["speed_mps"],
                speed_knots=live_vessel["speed_knots"],
                current_edge_index=live_vessel["current_edge_index"],
                edge_progress=live_vessel["edge_progress"],
                executed_distance_km=live_vessel["executed_distance_km"],
                cumulative_travelled_km=cumulative_live,
                remaining_distance_km=live_vessel["remaining_distance_km"],
                physical_position_source=live_vessel["physical_position_source"],
            )
        else:
            position = ship.get("current_position") or {}
            vessel = PresentationVessel(
                status=ship.get("status", "DEFERRED"),
                longitude=position.get("longitude"),
                latitude=position.get("latitude"),
                speed_mps=ship.get("speed_mps"),
                speed_knots=ship.get("effective_speed_knots"),
                current_edge_index=ship.get("current_edge_index"),
                edge_progress=ship.get("edge_progress"),
                executed_distance_km=ship.get("executed_distance_km"),
                cumulative_travelled_km=ship.get("cumulative_travelled_km"),
                remaining_distance_km=ship.get("remaining_distance_km"),
                physical_position_source=(
                    ship.get("speed_source")
                    or "snapshot_ship_state"
                ),
            )

        plan = self._presentation_plan(snapshot, ship, moment)
        risk_view = PresentationRisk(
            risk_content_revision=int(risk.get("risk_content_revision", 0)),
            risk_window_revision=int(risk.get("risk_window_revision", 0)),
            current_resource=risk.get("resource_identity"),
            current_resource_digest=risk.get("resource_digest"),
            available_valid_range=(
                [risk["risk_valid_start"], risk["risk_valid_end"]]
                if risk.get("risk_valid_start") and risk.get("risk_valid_end")
                else None
            ),
            hard_reason_resource=self._hard_reason_resource(snapshot),
            presentation_horizons=dict(risk.get("presentation_horizons", {})),
        )
        events = self.events(up_to=moment)
        return PresentationState(
            snapshot_index=snapshot_index,
            simulation_time=_iso(moment),
            knowledge_as_of=snapshot.get("knowledge_as_of", simulation_time),
            scenario_mode=snapshot.get("scenario_mode", "causal_replay"),
            vessel=vessel,
            plan=plan,
            risk=risk_view,
            events=events,
        )

    def _presentation_plan(
        self,
        snapshot: dict[str, Any],
        ship: dict[str, Any],
        moment: datetime,
    ) -> PresentationPlan:
        accepted_revision = int(ship.get("accepted_plan_revision", 0))
        route = ship.get("accepted_route") or {}
        waypoints = route.get("waypoints", ())
        vessel_index = ship.get("current_edge_index")
        has_segment = (
            vessel_index is not None
            and 0 <= int(vessel_index) < len(waypoints) - 1
        )
        segment = PresentationSegment(
            index=(
                int(vessel_index)
                if vessel_index is not None
                else None
            ),
            start=(
                {
                    "longitude": waypoints[int(vessel_index)].get("longitude"),
                    "latitude": waypoints[int(vessel_index)].get("latitude"),
                }
                if has_segment
                else None
            ),
            end=(
                {
                    "longitude": waypoints[int(vessel_index) + 1].get("longitude"),
                    "latitude": waypoints[int(vessel_index) + 1].get("latitude"),
                }
                if has_segment
                else None
            ),
            start_eta=ship.get("current_segment_start_eta"),
            end_eta=ship.get("current_segment_end_eta"),
        )
        future = [
            {
                "longitude": item.get("longitude"),
                "latitude": item.get("latitude"),
                "eta": item.get("eta"),
            }
            for item in waypoints
            if item.get("eta") and _parse_utc(item["eta"]) >= moment
        ]
        completed_track = [
            dict(item) for item in ship.get("completed_track", ())
        ]
        pending_route = ship.get("pending_route")
        pending_candidate = None
        if pending_route:
            pending_candidate = {
                "plan_revision": ship.get("candidate_plan_revision"),
                "decision_time": ship.get("replan_decision_time"),
                "effective_adoption_time": ship.get("effective_adoption_time"),
                "adoption_mode": (
                    "NEXT_WAYPOINT_DEFERRED"
                    if ship.get("adoption_status") == "PENDING"
                    else "IMMEDIATE"
                ),
                "route": pending_route,
            }
        pending_adoption = None
        if ship.get("replan_decision_time") or ship.get("effective_adoption_time"):
            pending_adoption = {
                "mode": (
                    "NEXT_WAYPOINT_DEFERRED"
                    if ship.get("adoption_status") in ("PENDING", "DEFERRED")
                    else "IMMEDIATE"
                ),
                "decision_time": ship.get("replan_decision_time"),
                "effective_adoption_time": ship.get("effective_adoption_time"),
            }
        superseded = ship.get("superseded_route")
        superseded_future = None
        if superseded and superseded.get("route"):
            superseded_future = [
                dict(item) for item in (superseded["route"].get("waypoints") or ())
            ]
        return PresentationPlan(
            accepted_plan_revision=accepted_revision,
            active_plan_revision=accepted_revision,
            completed_track=completed_track,
            current_authoritative_segment=segment,
            accepted_future_route=future,
            route_distance_km=route.get("distance_km"),
            pending_plan_revision=(
                ship.get("candidate_plan_revision") if pending_route else None
            ),
            pending_plan_status=(
                ship.get("adoption_status")
                if ship.get("adoption_status") == "PENDING"
                else None
            ),
            pending_candidate=pending_candidate,
            pending_adoption=pending_adoption,
            superseded_future_route=superseded_future,
        )

    @staticmethod
    def _hard_reason_resource(snapshot: dict[str, Any]) -> str | None:
        hard_reason = snapshot.get("hard_reason") or {}
        resource = hard_reason.get("resource")
        if resource:
            return str(resource)
        identity = hard_reason.get("identity")
        return str(identity) if identity else None

    def adoption_audit(self) -> dict[str, Any]:
        """Machine-readable audit of every accepted replan / route change."""

        entries: list[dict[str, Any]] = []
        snap_adjustments: list[float] = []
        revisions = [1]
        for change in self._route_changes:
            after = int(change["revision"])
            before = revisions[-1] if revisions else 1
            revisions.append(after)
            entry = self._audit_route_change(change, before=before, after=after)
            adjustment = entry.get("snap_adjustment_km")
            if adjustment is not None:
                snap_adjustments.append(float(adjustment))
            entries.append(entry)
        summary: dict[str, Any] = {
            "accepted_replan_count": len(entries),
            "IMMEDIATE": sum(
                1 for entry in entries if entry["adoption_mode"] == "IMMEDIATE"
            ),
            "NEXT_WAYPOINT_DEFERRED": sum(
                1
                for entry in entries
                if entry["adoption_mode"] == "NEXT_WAYPOINT_DEFERRED"
            ),
        }
        if snap_adjustments:
            summary["snap_adjustment_km"] = {
                "min": min(snap_adjustments),
                "median": statistics.median(snap_adjustments),
                "p95": _percentile(snap_adjustments, 0.95),
                "max": max(snap_adjustments),
            }
        else:
            summary["snap_adjustment_km"] = None
        return {"entries": entries, "summary": summary}

    def _audit_route_change(
        self,
        change: dict[str, Any],
        *,
        before: int,
        after: int,
    ) -> dict[str, Any]:
        change_time = _parse_utc(change["simulation_time"])
        revision = str(after)
        decision_event = next(
            (
                event
                for event in self._events
                if event["type"] in DECISION_EVENTS
                and str(event.get("revision")) == revision
            ),
            None,
        )
        mode = "IMMEDIATE"
        if decision_event is not None and decision_event["type"] == "REPLAN_DECIDED":
            mode = "NEXT_WAYPOINT_DEFERRED"
        decision_time = (
            _parse_utc(decision_event["simulation_time"])
            if decision_event is not None
            else change_time
        )
        adoption_time = change_time
        adoption_event = next(
            (
                event
                for event in self._events
                if event["type"] == "REPLAN_ADOPTED"
                and str(event.get("revision")) == revision
            ),
            None,
        )
        if adoption_event is not None:
            adoption_time = _parse_utc(adoption_event["simulation_time"])

        decision_snapshot = self._snapshot_at(decision_time)
        ship = decision_snapshot.get("ship_state") or {}
        physical = ship.get("current_position") or ship.get("replan_physical_position") or {}
        edge_progress = ship.get("edge_progress")
        origin = ship.get("planner_origin_position") or {}
        origin_node = ship.get("planner_origin_node") or ship.get("current_node")
        before_snapshot = self._latest_snapshot_for_revision(
            before, before_time=decision_time
        )
        after_snapshot = self._earliest_snapshot_for_revision(
            after, at_or_after=adoption_time
        )
        return {
            "decision_time": _iso(decision_time),
            "physical_lon": physical.get("longitude"),
            "physical_lat": physical.get("latitude"),
            "physical_current_edge_index": ship.get("current_edge_index"),
            "physical_edge_progress": edge_progress,
            "physical_at_waypoint": (
                edge_progress is not None and abs(float(edge_progress)) < 1e-9
            ),
            "planner_origin_node": list(origin_node) if origin_node else None,
            "planner_origin_lon": origin.get("longitude"),
            "planner_origin_lat": origin.get("latitude"),
            "snap_adjustment_km": (
                ship.get("snap_adjustment_km")
                or ship.get("planner_origin_adjustment_km")
            ),
            "adoption_mode": mode,
            "scheduled_adoption_time": ship.get("effective_adoption_time"),
            "effective_adoption_time": (
                _iso(adoption_time) if mode == "NEXT_WAYPOINT_DEFERRED" else _iso(change_time)
            ),
            "route_changed_time": _iso(change_time),
            "plan_revision_before": before,
            "plan_revision_after": after,
            "completed_track_length_before": (
                len(before_snapshot.get("ship_state", {}).get("completed_track", ()))
                if before_snapshot is not None
                else None
            ),
            "completed_track_length_after": (
                len(after_snapshot.get("ship_state", {}).get("completed_track", ()))
                if after_snapshot is not None
                else None
            ),
        }

    def _latest_snapshot_for_revision(
        self,
        revision: int,
        *,
        before_time: datetime,
    ) -> dict[str, Any] | None:
        """Most recent snapshot at/before ``before_time`` carrying ``revision``."""

        found: dict[str, Any] | None = None
        for snapshot in self.snapshots:
            if _parse_utc(snapshot["simulation_time"]) > before_time:
                break
            ship = snapshot.get("ship_state") or {}
            if int(ship.get("accepted_plan_revision", 0)) == int(revision):
                found = snapshot
        return found

    def _earliest_snapshot_for_revision(
        self,
        revision: int,
        *,
        at_or_after: datetime,
    ) -> dict[str, Any] | None:
        """First snapshot at/after ``at_or_after`` carrying ``revision``."""

        for snapshot in self.snapshots:
            if _parse_utc(snapshot["simulation_time"]) < at_or_after:
                continue
            ship = snapshot.get("ship_state") or {}
            if int(ship.get("accepted_plan_revision", 0)) == int(revision):
                return snapshot
        return None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight
