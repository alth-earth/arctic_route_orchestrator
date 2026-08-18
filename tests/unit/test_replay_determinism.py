"""Determinism: semantic digests exclude wall-clock and derived identities."""

from __future__ import annotations

from arctic_route_orchestrator.replay.digests import replay_semantic_digest


def _document(commit_id: str, generated_at: str, revision: str) -> dict:
    return {
        "simulation_time": "2026-08-15T10:00:00Z",
        "knowledge_as_of": "2026-08-15T10:00:00Z",
        "risk": {
            "risk_revision": 1,
            "prediction_as_of": "2026-08-15T10:00:00Z",
            "resource_identity": commit_id,
            "resource_digest": commit_id + "-digest",
        },
        "events": [
            {
                "type": "B_UPDATED",
                "simulation_time": "2026-08-15T10:00:00Z",
                "revision": revision,
            }
        ],
        "generated_at": generated_at,
        "provenance": {"published_at": generated_at},
    }


def test_semantic_digest_ignores_wall_clock_and_derived_identities() -> None:
    first = _document(
        commit_id="risk-window-sha256-aaaa",
        generated_at="2026-08-18T00:00:00Z",
        revision="risk-window-sha256-aaaa",
    )
    second = _document(
        commit_id="risk-window-sha256-bbbb",
        generated_at="2026-08-18T01:00:00Z",
        revision="risk-window-sha256-bbbb",
    )
    assert replay_semantic_digest(first) == replay_semantic_digest(second)


def test_semantic_digest_still_sensitive_to_business_content() -> None:
    base = _document("c", "2026-08-18T00:00:00Z", "c")
    changed = _document("c", "2026-08-18T00:00:00Z", "c")
    changed["simulation_time"] = "2026-08-15T11:00:00Z"
    assert replay_semantic_digest(base) != replay_semantic_digest(changed)
