"""Precompute a backend-driven viewer bundle from an authoritative replay.

The bundle is produced entirely by the Presentation Adapter
(``PresentationAdapter.state_at`` / route index / events).  The browser never
reasons about replay internals: it only renders backend-provided routes,
track milestones, pending state, and vessel positions, interpolating at 60 FPS
between backend segment waypoints for smooth display.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arctic_route_orchestrator.replay.presentation import PresentationAdapter


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _route_meta(adapter: PresentationAdapter, revision: int) -> dict:
    entry = adapter._routes_by_revision[revision]
    route = entry["route"]
    decision_time = None
    adopt_time = None
    mode = "INITIAL"
    for event in adapter._events:
        if str(event.get("revision")) == str(revision):
            if event["type"] == "REPLAN_DECIDED":
                decision_time = event["simulation_time"]
                mode = "NEXT_WAYPOINT_DEFERRED"
            elif event["type"] == "REPLAN_TRIGGERED":
                decision_time = event["simulation_time"]
                mode = "IMMEDIATE"
            elif event["type"] == "ROUTE_CHANGED":
                adopt_time = event["simulation_time"]
    if revision == 1:
        adopt_time = adapter.replay_start.isoformat().replace("+00:00", "Z")
    return {
        "revision": revision,
        "layer": "full_voyage",
        "objective": "recommended",
        "decision_time": decision_time,
        "effective_adoption_time": adopt_time,
        "adoption_mode": mode,
        "distance_km": route.get("distance_km"),
        "waypoints": [
            {
                "lon": item["longitude"],
                "lat": item["latitude"],
                "eta": item["eta"],
            }
            for item in route["waypoints"]
        ],
    }


def _timeline(adapter: PresentationAdapter, cadence_seconds: int) -> list[dict]:
    results: list[dict] = []
    previous_track_key: tuple | None = None
    previous_pending_key: tuple | None = None
    moment = adapter.replay_start
    while moment <= adapter.replay_end:
        state = adapter.state_at(moment)
        plan = state.plan
        vessel = state.vessel
        plan_dict = plan.to_dict()
        segment = plan_dict["current_authoritative_segment"]
        track = [dict(item) for item in plan.completed_track]
        track_key = tuple((p["longitude"], p["latitude"], p["eta"]) for p in track)
        pending = plan.pending_candidate
        pending_key = (
            (
                pending["plan_revision"],
                pending["decision_time"],
                pending["effective_adoption_time"],
                len(pending["route"].get("waypoints", ())),
            )
            if pending
            else None
        )
        entry: dict = {
            "t": _iso(moment),
            "v": {
                "lon": vessel.longitude,
                "lat": vessel.latitude,
                "kn": vessel.speed_knots,
                "status": vessel.status,
                "ep": vessel.edge_progress,
                "eidx": vessel.current_edge_index,
            },
            "arv": plan_dict["active_plan_revision"],
            "prv": plan_dict["pending_plan_revision"],
            "prs": plan_dict["pending_plan_status"],
            "dt": plan_dict["pending_adoption"]["decision_time"]
            if plan_dict["pending_adoption"]
            else None,
            "eat": plan_dict["pending_adoption"]["effective_adoption_time"]
            if plan_dict["pending_adoption"]
            else None,
            "ctl": len(track),
            "seg": {
                "index": segment["index"],
                "start_eta": segment["start_eta"],
                "end_eta": segment["end_eta"],
            },
        }
        if track_key != previous_track_key:
            entry["track"] = track
            previous_track_key = track_key
        if pending_key != previous_pending_key:
            if pending:
                entry["pending"] = {
                    "revision": pending["plan_revision"],
                    "decision_time": pending["decision_time"],
                    "effective_adoption_time": pending["effective_adoption_time"],
                    "route": [
                        {
                            "lon": item["longitude"],
                            "lat": item["latitude"],
                            "eta": item["eta"],
                        }
                        for item in pending["route"]["waypoints"]
                    ],
                }
            else:
                entry["pending"] = None
            previous_pending_key = pending_key
        results.append(entry)
        moment += timedelta(seconds=cadence_seconds)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="viewer-build-bundle")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--snapshots-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--cadence-seconds", type=int, default=60)
    parser.add_argument("--preflight", type=Path, default=None)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    snapshots_dir = args.snapshots_dir or args.manifest.parent / "snapshots"
    snapshots = [
        json.loads(
            (snapshots_dir / f"{entry['index']:04d}.json").read_text(encoding="utf-8")
        )
        for entry in manifest["snapshots"]
    ]
    adapter = PresentationAdapter(manifest, snapshots)
    gates = {"status": "NOT_RUN", "l2_status": None}
    if args.preflight is not None and args.preflight.exists():
        preflight = json.loads(args.preflight.read_text(encoding="utf-8"))
        gates = {
            "status": preflight.get("overall"),
            "l2_status": preflight.get("l2", {}).get("overall"),
        }
    baseline_meta = (args.output_dir / "basemap_metadata.json")
    basemap = (
        json.loads(baseline_meta.read_text(encoding="utf-8"))
        if baseline_meta.exists()
        else None
    )
    positions = {
        time_label: adapter.vessel_at(moment)
        for time_label, moment in (
            ("10:00", adapter.replay_start),
            ("10:30", adapter.replay_start + timedelta(minutes=30)),
            ("11:00", adapter.replay_start + timedelta(minutes=60)),
        )
    }
    bundle = {
        "schema_version": "replay.viewer-bundle.v1",
        "replay": {
            "replay_id": manifest.get("replay_id"),
            "scenario_id": manifest.get("scenario_id"),
            "scenario_mode": manifest.get("scenario_mode"),
            "start": manifest.get("replay_start"),
            "end": manifest.get("replay_end"),
            "manifest_semantic_digest": manifest.get("semantic_digest"),
        },
        "basemap": basemap,
        "gates": gates,
        "routes": [
            _route_meta(adapter, revision)
            for revision in sorted(adapter._routes_by_revision)
        ],
        "events": [
            {
                "t": event["simulation_time"],
                "type": event["type"],
                "rev": event.get("revision"),
            }
            for event in adapter._events
        ],
        "timeline": _timeline(adapter, args.cadence_seconds),
        "acceptance_positions": positions,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "bundle.json"
    output.write_text(
        json.dumps(bundle, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        "wrote",
        output,
        "size",
        output.stat().st_size,
        "timeline",
        len(bundle["timeline"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
