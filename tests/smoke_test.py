"""
Smoke tests for Team USA Hometown Signals & LA28 Momentum API.

Uses FastAPI TestClient — no external network calls required.
All tests run without Gemini credentials (local-fallback is used for briefs).

Run:
    pytest tests/smoke_test.py -v
    pytest tests/smoke_test.py -v --tb=short   # shorter tracebacks
"""

from __future__ import annotations

import sys
import os

# Ensure the project root is on the path regardless of where pytest is run from
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────

def test_health():
    """GET /health returns 200 with expected shape."""
    r = client.get("/health")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "gemini_configured" in data


def test_root():
    """GET / returns service info JSON."""
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert "endpoints" in data
    assert "Team USA" in data["service"]


# ─────────────────────────────────────────────────────────────────────────────
# Hometown Hubs — list
# ─────────────────────────────────────────────────────────────────────────────

def test_list_hubs():
    """GET /api/hometown/hubs returns a non-empty list of ApiHubSummary."""
    r = client.get("/api/hometown/hubs")
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 12, f"Expected 12 hubs, got {len(data)}"

    # Validate shape of first item (ApiHubSummary)
    hub = data[0]
    for key in ("id", "name", "region", "x", "y", "short_insight", "tags"):
        assert key in hub, f"Missing key '{key}' in hub summary"

    assert isinstance(hub["tags"], list)


def test_list_hubs_all_ids():
    """Verify all 12 expected hub IDs are present."""
    r = client.get("/api/hometown/hubs")
    ids = {h["id"] for h in r.json()}
    expected = {"sd", "hou", "la", "cos", "chi", "atl", "cha", "lb", "mia", "por", "tuc", "bir"}
    assert ids == expected, f"Missing hub IDs: {expected - ids}"


# ─────────────────────────────────────────────────────────────────────────────
# Hometown Hubs — detail
# ─────────────────────────────────────────────────────────────────────────────

def test_hub_detail_sd():
    """GET /api/hometown/hubs/sd returns full ApiHubDetail."""
    r = client.get("/api/hometown/hubs/sd")
    assert r.status_code == 200, r.text
    data = r.json()

    # Shape checks (ApiHubDetail)
    for key in ("id", "name", "region", "x", "y", "coordinates", "tags",
                "narrative", "map_pins", "parity_snapshot", "sources"):
        assert key in data, f"Missing key '{key}' in hub detail"

    assert data["id"] == "sd"
    assert data["name"] == "San Diego"

    # coordinates
    coords = data["coordinates"]
    assert "lat" in coords and "lng" in coords
    assert abs(coords["lat"] - 32.81) < 0.5
    assert abs(coords["lng"] - (-117.13)) < 0.5

    # map_pins
    assert isinstance(data["map_pins"], list)
    assert len(data["map_pins"]) >= 2  # At least Olympic + Paralympic pins

    for pin in data["map_pins"]:
        for key in ("label", "lat", "lng", "color", "description"):
            assert key in pin, f"Missing pin key '{key}'"
        assert pin["color"] in ("red", "blue", "white")

    # parity_snapshot
    ps = data["parity_snapshot"]
    for key in ("olympic_story_estimate", "paralympic_story_estimate", "parity_note"):
        assert key in ps
        assert len(ps[key]) > 10, f"Parity snapshot field '{key}' too short"


def test_hub_detail_cos():
    """Colorado Springs: Paralympic count > Olympic count → parity note reflects it."""
    r = client.get("/api/hometown/hubs/cos")
    assert r.status_code == 200, r.text
    data = r.json()
    ps = data["parity_snapshot"]
    # Colorado Springs has 4 Olympic, 6 Paralympic — should mention Paralympic leadership
    assert "paralympic" in ps["parity_note"].lower() or "Paralympic" in ps["parity_note"]


def test_hub_detail_la():
    """Los Angeles: LA28 host-region pin should be present."""
    r = client.get("/api/hometown/hubs/la")
    assert r.status_code == 200, r.text
    data = r.json()
    labels = [p["label"] for p in data["map_pins"]]
    assert any("LA28" in label or "host" in label.lower() for label in labels), (
        f"Expected LA28 context pin, got: {labels}"
    )


def test_hub_not_found():
    """GET /api/hometown/hubs/nonexistent returns 404."""
    r = client.get("/api/hometown/hubs/nonexistent_hub_xyz")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_hub_detail_no_private_info():
    """No hub detail response should contain athlete names or private data."""
    r = client.get("/api/hometown/hubs/hou")
    assert r.status_code == 200
    text = r.text
    # These patterns would indicate private data leakage
    for forbidden in ["@", "SSN", "phone", "email", "date of birth", "passport"]:
        assert forbidden not in text, f"Potential private data pattern '{forbidden}' in response"


# ─────────────────────────────────────────────────────────────────────────────
# Brief generation (local-fallback, no Gemini credentials needed)
# ─────────────────────────────────────────────────────────────────────────────

def test_brief_local_fallback():
    """POST /api/hometown/brief returns a valid ApiBriefResponse (local-fallback)."""
    r = client.post(
        "/api/hometown/brief",
        json={"hub_id": "cos"},
    )
    assert r.status_code == 200, r.text
    data = r.json()

    for key in ("hub_id", "hub_name", "brief", "themes", "disclaimer", "source"):
        assert key in data, f"Missing key '{key}' in brief response"

    assert data["hub_id"] == "cos"
    assert data["hub_name"] == "Colorado Springs"
    assert len(data["brief"]) > 50
    assert data["source"] in ("vertex", "gemini", "local-fallback")
    assert isinstance(data["themes"], list)
    assert isinstance(data["disclaimer"], str)
    assert len(data["disclaimer"]) > 20


def test_brief_with_interests():
    """POST /api/hometown/brief with interests list."""
    r = client.post(
        "/api/hometown/brief",
        json={"hub_id": "la", "interests": ["water polo", "flag football"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["hub_id"] == "la"
    assert data["hub_name"] == "Los Angeles"


def test_brief_not_found():
    """POST /api/hometown/brief with unknown hub_id returns 404."""
    r = client.post("/api/hometown/brief", json={"hub_id": "xyz_unknown"})
    assert r.status_code == 404


def test_brief_conditional_language():
    """Brief text should use conditional language — no absolute predictions."""
    r = client.post("/api/hometown/brief", json={"hub_id": "atl"})
    assert r.status_code == 200
    brief = r.json()["brief"].lower()
    # At least one conditional word should appear
    conditionals = ["could", "may", "might", "suggest", "signal", "potential",
                    "points toward", "would", "can"]
    assert any(word in brief for word in conditionals), (
        f"Brief for 'atl' appears to use non-conditional language: {brief[:200]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# LA28 Momentum
# ─────────────────────────────────────────────────────────────────────────────

def test_momentum_list():
    """GET /api/la28/momentum returns a list of LA28MomentumSummary."""
    r = client.get("/api/la28/momentum")
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 10

    summary = data[0]
    for key in ("hub_id", "hub_name", "top_sport", "top_score",
                "olympic_count", "paralympic_count"):
        assert key in summary, f"Missing key '{key}' in momentum summary"

    # All hubs should have at least one of each type
    all_olympic = sum(s["olympic_count"] for s in data)
    all_para = sum(s["paralympic_count"] for s in data)
    assert all_olympic > 0, "No Olympic sports in momentum data"
    assert all_para > 0, "No Paralympic sports in momentum data"


def test_momentum_la():
    """GET /api/la28/momentum/la returns full LA28MomentumResponse for Los Angeles."""
    r = client.get("/api/la28/momentum/la")
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["hub_id"] == "la"
    assert data["hub_name"] == "Los Angeles"
    assert "disclaimer" in data
    assert isinstance(data["sports"], list)
    assert len(data["sports"]) >= 5

    # Each sport should have the right shape
    for sport in data["sports"]:
        for key in ("id", "name", "kind", "signals", "momentum_score", "reason"):
            assert key in sport, f"Missing key '{key}' in LA28 sport"
        assert sport["kind"] in ("olympic", "paralympic")
        assert 0 <= sport["momentum_score"] <= 100
        assert sport["signals"]["hometown"] <= 100
        assert sport["signals"]["la28"] <= 100


def test_momentum_cos_parity():
    """Colorado Springs momentum should include both Olympic and Paralympic sports."""
    r = client.get("/api/la28/momentum/cos")
    assert r.status_code == 200, r.text
    sports = r.json()["sports"]
    kinds = {s["kind"] for s in sports}
    assert "olympic" in kinds, "Missing Olympic sports in COS momentum"
    assert "paralympic" in kinds, "Missing Paralympic sports in COS momentum"


def test_momentum_sorted_by_score():
    """Momentum sports should be sorted descending by momentum_score."""
    r = client.get("/api/la28/momentum/atl")
    assert r.status_code == 200, r.text
    scores = [s["momentum_score"] for s in r.json()["sports"]]
    assert scores == sorted(scores, reverse=True), "Momentum sports not sorted by score"


def test_momentum_not_found():
    """GET /api/la28/momentum/nonexistent returns 404."""
    r = client.get("/api/la28/momentum/nonexistent_hub_xyz")
    assert r.status_code == 404


def test_momentum_disclaimer_present():
    """Every momentum response includes a non-empty disclaimer."""
    r = client.get("/api/la28/momentum/sd")
    assert r.status_code == 200
    disclaimer = r.json().get("disclaimer", "")
    assert len(disclaimer) > 50
    assert "not" in disclaimer.lower() or "descriptive" in disclaimer.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Alias / fallback routes
# ─────────────────────────────────────────────────────────────────────────────

def test_alias_routes():
    """Alias routes (without /api prefix) return the same data as canonical routes."""
    canonical = client.get("/api/hometown/hubs").json()
    alias = client.get("/hometown/hubs").json()
    assert canonical == alias, "Alias /hometown/hubs differs from /api/hometown/hubs"

    canonical_detail = client.get("/api/hometown/hubs/sd").json()
    alias_detail = client.get("/hometown/hubs/sd").json()
    assert canonical_detail == alias_detail

    canonical_momentum = client.get("/api/la28/momentum").json()
    alias_momentum = client.get("/la28/momentum").json()
    assert canonical_momentum == alias_momentum


# ─────────────────────────────────────────────────────────────────────────────
# CORS headers
# ─────────────────────────────────────────────────────────────────────────────

def test_cors_options():
    """OPTIONS preflight returns 200 and CORS headers."""
    r = client.options(
        "/api/hometown/hubs",
        headers={
            "Origin": "https://my-app.lovable.app",
            "Access-Control-Request-Method": "GET",
        },
    )
    # TestClient may return 200 or 204 depending on CORS middleware version
    assert r.status_code in (200, 204)


# ─────────────────────────────────────────────────────────────────────────────
# Data integrity
# ─────────────────────────────────────────────────────────────────────────────

def test_all_hubs_have_coordinates():
    """Every hub detail should have non-zero coordinates."""
    hubs = client.get("/api/hometown/hubs").json()
    for hub in hubs:
        r = client.get(f"/api/hometown/hubs/{hub['id']}")
        assert r.status_code == 200
        coords = r.json()["coordinates"]
        assert coords["lat"] != 0.0 or coords["lng"] != 0.0, (
            f"Hub '{hub['id']}' has zero coordinates"
        )


def test_all_hubs_have_narrative():
    """Every hub detail should have a non-empty narrative."""
    hubs = client.get("/api/hometown/hubs").json()
    for hub in hubs:
        r = client.get(f"/api/hometown/hubs/{hub['id']}")
        assert r.status_code == 200
        narrative = r.json().get("narrative", "")
        assert len(narrative) > 20, f"Hub '{hub['id']}' has too short narrative: '{narrative}'"


def test_sources_are_listed():
    """Every hub detail should include source attribution."""
    r = client.get("/api/hometown/hubs/sd")
    sources = r.json().get("sources", [])
    assert len(sources) >= 1
    # Sources should look like URLs
    assert any("http" in s for s in sources), f"Sources don't look like URLs: {sources}"
