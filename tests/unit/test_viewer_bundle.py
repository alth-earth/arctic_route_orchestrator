"""Viewer bundle contract tests (backend-driven presentation)."""

from __future__ import annotations

import json
import math
from pathlib import Path

VIEWER_DIR = Path(__file__).resolve().parents[2] / "viewer"


def _haversine_km(a: dict, b: dict) -> float:
    lat1 = math.radians(a["lat"])
    lat2 = math.radians(b["lat"])
    delta_lat = lat2 - lat1
    delta_lon = math.radians(b["lon"] - a["lon"])
    h = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def _bundle() -> dict:
    return json.loads((VIEWER_DIR / "bundle.json").read_text(encoding="utf-8"))


def test_bundle_gates_and_basemap() -> None:
    bundle = _bundle()
    assert bundle["gates"]["status"] == "PASS"
    assert bundle["gates"]["l2_status"] == "PASS"
    assert bundle["basemap"]["projection"] == "EPSG:4326"
    assert bundle["replay"]["manifest_semantic_digest"].startswith("1bdcbce5")


def test_bundle_intermediate_ship_positions_change() -> None:
    bundle = _bundle()
    a = bundle["acceptance_positions"]["10:00"]
    b = bundle["acceptance_positions"]["10:30"]
    c = bundle["acceptance_positions"]["11:00"]
    assert a["status"] == "UNDERWAY" == b["status"] == c["status"]
    assert a["latitude"] < b["latitude"] < c["latitude"]
    assert 0.1 < _haversine_km(
        {"lon": a["longitude"], "lat": a["latitude"]},
        {"lon": b["longitude"], "lat": b["latitude"]},
    ) < 12


def test_timeline_moves_and_track_never_rewinds() -> None:
    bundle = _bundle()
    timeline = bundle["timeline"]
    assert len(timeline) == 721
    assert timeline[0]["t"] == bundle["replay"]["start"]
    assert timeline[-1]["t"] == bundle["replay"]["end"]
    previous_length = 0
    previous_pos = None
    max_delta = 0.0
    for entry in timeline:
        assert entry["ctl"] >= previous_length
        previous_length = entry["ctl"]
        pos = {"lon": entry["v"]["lon"], "lat": entry["v"]["lat"]}
        if previous_pos:
            max_delta = max(max_delta, _haversine_km(previous_pos, pos))
        previous_pos = pos
    assert max_delta < 2.0


def test_deferred_revision_visible_in_timeline() -> None:
    bundle = _bundle()
    timeline = bundle["timeline"]
    decided_at_1300 = next(
        entry for entry in timeline if entry["t"] >= "2026-08-15T13:00:00Z"
    )
    pending_at_1330 = next(entry for entry in timeline if entry["t"] >= "2026-08-15T13:30:00Z")
    adopted_at_1500 = next(entry for entry in timeline if entry["t"] >= "2026-08-15T15:00:00Z")
    assert pending_at_1330["arv"] == 1
    assert pending_at_1330["prv"] == 2
    assert pending_at_1330["prs"] == "PENDING"
    assert decided_at_1300["pending"]["revision"] == 2
    assert adopted_at_1500["arv"] == 2


def test_replan_skipped_does_not_change_active_revision() -> None:
    bundle = _bundle()
    at_1100 = next(entry for entry in bundle["timeline"] if entry["t"] >= "2026-08-15T11:00:00Z")
    assert at_1100["arv"] == 1
    skipped = [e for e in bundle["events"] if e["type"] == "REPLAN_SKIPPED"]
    assert len(skipped) == 1
