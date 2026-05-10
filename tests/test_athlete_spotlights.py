"""
Tests for the public Athlete Spotlights endpoint:

  GET /api/hometown/hubs/{hub_id}/athlete-spotlights
  GET /hometown/hubs/{hub_id}/athlete-spotlights   (alias)

These tests run without Gemini credentials, so the deterministic local
fallback path is exercised. They verify:

  - Response shape matches AthleteSpotlightsResponse
  - generated_with_gemini is False when no creds are configured
  - Olympic and Paralympic arrays are returned separately
  - Every spotlight carries a source URL (no private data)
  - Copy uses conditional language and avoids forbidden ranking /
    guarantee / endorsement words ("best", "top", "major", "will win",
    "guaranteed", etc.)
  - Known hubs (sd, cos, chi, atl) return expected data
  - Hubs with no Paralympic spotlights have an empty list and a fallback
    brief that acknowledges that gap
  - Both seed-form (`san_diego`) and short-form (`sd`) hub_ids resolve
  - Alias route returns the same shape
  - Unknown hubs return 404
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure no Gemini creds are set during tests so we exercise the fallback path.
for _env in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "GEMINI_API_KEY"):
    os.environ.pop(_env, None)

from app.main import app  # noqa: E402
from app.services import athlete_spotlights_service  # noqa: E402

client = TestClient(app)


# ---------------------------------------------------------------------------
# Forbidden / ranking phrases that must never appear in fallback brief copy.
# ---------------------------------------------------------------------------

_FORBIDDEN_REGEXES = [
    re.compile(r"\bwill\s+(?:win|medal|finish|place|take|secure|earn|capture)\b", re.I),
    re.compile(r"\bguarantee[ds]?\b", re.I),
    re.compile(r"\bcertain\s+to\b", re.I),
    re.compile(r"\bsure\s+thing\b", re.I),
    re.compile(r"\bbest\b", re.I),
    re.compile(r"\btop\b", re.I),
    re.compile(r"\bmajor\b", re.I),
    re.compile(r"\b(?:greatest|elite|premier|leading)\b", re.I),
    re.compile(r"\bendorse[ds]?\b", re.I),
]


def _safe_text_for_scan(data: dict) -> str:
    """Return only the brief copy + summary fields for the safety scan.

    The factual `recent_achievement` strings come from public sources and may
    legitimately contain words like "first" or sport titles; the safety
    contract applies to the generated brief copy, not the verbatim public
    achievements.
    """
    brief = data["gemini_brief"]
    return " ".join([
        brief["title"],
        brief["summary"],
        *brief["bullets"],
    ])


def _assert_safe_wording(text: str) -> None:
    for rx in _FORBIDDEN_REGEXES:
        match = rx.search(text)
        assert match is None, f"Forbidden phrase '{match.group(0)}' found in: {text[:200]}"


def _assert_response_shape(data: dict, expected_hub_id: str, expected_name: str) -> None:
    expected_keys = {
        "hub_id",
        "hub_name",
        "generated_with_gemini",
        "source",
        "model",
        "olympic_spotlights",
        "paralympic_spotlights",
        "gemini_brief",
        "disclaimer",
        "generated_at",
    }
    assert expected_keys.issubset(data.keys()), (
        f"Missing keys: {expected_keys - set(data.keys())}"
    )

    assert data["hub_id"] == expected_hub_id
    assert data["hub_name"] == expected_name
    assert isinstance(data["generated_with_gemini"], bool)
    assert data["source"] in ("vertex", "gemini", "local-fallback")
    assert isinstance(data["olympic_spotlights"], list)
    assert isinstance(data["paralympic_spotlights"], list)
    assert isinstance(data["disclaimer"], str) and len(data["disclaimer"]) > 30

    # Validate ISO timestamp
    try:
        datetime.fromisoformat(data["generated_at"])
    except ValueError:
        pytest.fail(f"generated_at not ISO-8601: {data['generated_at']}")

    brief = data["gemini_brief"]
    for key in ("title", "summary", "bullets"):
        assert key in brief, f"Missing brief key '{key}'"
    assert isinstance(brief["title"], str) and brief["title"].strip()
    assert isinstance(brief["summary"], str) and len(brief["summary"]) >= 20
    assert isinstance(brief["bullets"], list) and brief["bullets"]


def _assert_spotlight_shape(spotlight: dict, expected_category: str) -> None:
    for key in (
        "category",
        "name",
        "sport",
        "hometown_or_region",
        "recent_achievement",
        "source_url",
        "source_label",
    ):
        assert key in spotlight, f"Missing spotlight key '{key}'"
    assert spotlight["category"] == expected_category
    # Required, non-empty fields
    assert spotlight["name"].strip(), "name must be non-empty"
    assert spotlight["sport"].strip(), "sport must be non-empty"
    assert spotlight["recent_achievement"].strip()
    assert spotlight["source_label"].strip()

    # Source URL must be a real http(s) URL
    parsed = urlparse(spotlight["source_url"])
    assert parsed.scheme in ("http", "https"), (
        f"source_url not http(s): {spotlight['source_url']}"
    )
    assert parsed.netloc, f"source_url has no host: {spotlight['source_url']}"


# ─────────────────────────────────────────────────────────────────────────────
# Happy paths — known hubs
# ─────────────────────────────────────────────────────────────────────────────

def test_spotlights_san_diego_short_id():
    r = client.get("/api/hometown/hubs/sd/athlete-spotlights")
    assert r.status_code == 200, r.text
    data = r.json()
    _assert_response_shape(data, "sd", "San Diego")

    # No Gemini credentials → fallback path
    assert data["generated_with_gemini"] is False
    assert data["source"] == "local-fallback"
    assert data["model"] is None

    # San Diego seed: 2 Olympic, 1 Paralympic (1 blank slot filtered out)
    assert len(data["olympic_spotlights"]) == 2
    assert len(data["paralympic_spotlights"]) == 1
    for s in data["olympic_spotlights"]:
        _assert_spotlight_shape(s, "olympic")
    for s in data["paralympic_spotlights"]:
        _assert_spotlight_shape(s, "paralympic")

    olympic_names = {s["name"] for s in data["olympic_spotlights"]}
    assert "Tate Carew" in olympic_names
    paralympic_names = {s["name"] for s in data["paralympic_spotlights"]}
    assert "Kate Delson" in paralympic_names


def test_spotlights_seed_form_hub_id_also_resolves():
    """The seed long-form ID (san_diego) should resolve as well as 'sd'."""
    r = client.get("/api/hometown/hubs/san_diego/athlete-spotlights")
    assert r.status_code == 200, r.text
    data = r.json()
    # The canonical id in the response is the short form
    assert data["hub_id"] == "sd"
    assert data["hub_name"] == "San Diego"


def test_spotlights_chicago_has_two_olympic_and_two_paralympic():
    r = client.get("/api/hometown/hubs/chi/athlete-spotlights")
    assert r.status_code == 200, r.text
    data = r.json()
    _assert_response_shape(data, "chi", "Chicago")
    assert len(data["olympic_spotlights"]) == 2
    assert len(data["paralympic_spotlights"]) == 2
    paralympic_names = {s["name"] for s in data["paralympic_spotlights"]}
    assert "Sarah Adam" in paralympic_names
    assert "Brody Roybal" in paralympic_names


def test_spotlights_colorado_springs_includes_paralympic():
    r = client.get("/api/hometown/hubs/cos/athlete-spotlights")
    assert r.status_code == 200, r.text
    data = r.json()
    _assert_response_shape(data, "cos", "Colorado Springs")
    paralympic_names = {s["name"] for s in data["paralympic_spotlights"]}
    assert "Noah Elliott" in paralympic_names


def test_spotlights_atlanta_paralympic_only_olympic_empty():
    """
    Atlanta seed has no Olympic spotlights and two Paralympic spotlights.
    Olympic array must be an empty list (not invented), and the brief must
    acknowledge the gap.
    """
    r = client.get("/api/hometown/hubs/atl/athlete-spotlights")
    assert r.status_code == 200, r.text
    data = r.json()
    _assert_response_shape(data, "atl", "Atlanta")
    assert data["olympic_spotlights"] == []
    assert len(data["paralympic_spotlights"]) == 2
    bullets_text = " ".join(data["gemini_brief"]["bullets"]).lower()
    assert "olympic" in bullets_text and "not loaded" in bullets_text


# ─────────────────────────────────────────────────────────────────────────────
# Paralympic parity / fallback acknowledgement
# ─────────────────────────────────────────────────────────────────────────────

def test_houston_paralympic_empty_brief_acknowledges_gap():
    """Houston seed has no Paralympic spotlights — brief must say so."""
    r = client.get("/api/hometown/hubs/hou/athlete-spotlights")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["paralympic_spotlights"] == []
    bullets = " ".join(data["gemini_brief"]["bullets"]).lower()
    summary = data["gemini_brief"]["summary"].lower()
    combined = bullets + " " + summary
    assert "paralympic" in combined
    # Some phrasing acknowledging that paralympic data is not loaded.
    assert "not loaded" in combined or "not available" in combined


# ─────────────────────────────────────────────────────────────────────────────
# Source URLs / no private data
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hub_id", ["sd", "cos", "chi", "atl", "mia", "cha"])
def test_every_loaded_spotlight_has_source_url(hub_id):
    r = client.get(f"/api/hometown/hubs/{hub_id}/athlete-spotlights")
    assert r.status_code == 200, r.text
    data = r.json()
    all_spotlights = data["olympic_spotlights"] + data["paralympic_spotlights"]
    # Every loaded spotlight must carry a real http(s) source URL
    assert all_spotlights, f"Expected at least one spotlight for {hub_id}"
    for s in all_spotlights:
        parsed = urlparse(s["source_url"])
        assert parsed.scheme in ("http", "https"), s
        assert parsed.netloc, s
        assert s["source_label"].strip(), s


def test_no_private_data_patterns():
    r = client.get("/api/hometown/hubs/sd/athlete-spotlights")
    assert r.status_code == 200
    text = r.text.lower()
    for forbidden in [
        "ssn",
        "phone number",
        "date of birth",
        "passport",
        "home address",
    ]:
        assert forbidden not in text, f"Possible private pattern '{forbidden}' in response"


# ─────────────────────────────────────────────────────────────────────────────
# Safe / non-ranking language — applies to brief copy, not factual fields
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hub_id", ["sd", "cos", "chi", "atl", "hou", "mia"])
def test_brief_uses_safe_non_ranking_wording(hub_id):
    r = client.get(f"/api/hometown/hubs/{hub_id}/athlete-spotlights")
    assert r.status_code == 200
    text = _safe_text_for_scan(r.json())
    _assert_safe_wording(text)


def test_brief_uses_conditional_language():
    r = client.get("/api/hometown/hubs/sd/athlete-spotlights")
    text = _safe_text_for_scan(r.json()).lower()
    conditionals = ["could", "may", "might", "suggest", "signal", "appears"]
    assert any(c in text for c in conditionals), (
        f"No conditional markers in brief: {text[:200]}"
    )


def test_disclaimer_mentions_public_sources_and_no_predictions():
    r = client.get("/api/hometown/hubs/sd/athlete-spotlights")
    disclaimer = r.json()["disclaimer"].lower()
    assert "public" in disclaimer
    assert "source" in disclaimer
    assert "predict" in disclaimer or "guarantee" in disclaimer or "endors" in disclaimer


# ─────────────────────────────────────────────────────────────────────────────
# Alias / errors
# ─────────────────────────────────────────────────────────────────────────────

def test_alias_route_matches_canonical_shape():
    canonical = client.get("/api/hometown/hubs/sd/athlete-spotlights").json()
    alias = client.get("/hometown/hubs/sd/athlete-spotlights").json()
    # generated_at differs across calls — compare everything else.
    for field in (
        "hub_id",
        "hub_name",
        "generated_with_gemini",
        "source",
        "olympic_spotlights",
        "paralympic_spotlights",
        "disclaimer",
    ):
        assert canonical[field] == alias[field], f"Mismatch on field '{field}'"
    assert canonical["gemini_brief"]["title"] == alias["gemini_brief"]["title"]


def test_unknown_hub_returns_404():
    r = client.get("/api/hometown/hubs/not_a_real_hub/athlete-spotlights")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Service-level unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_safety_scan_rejects_ranking_and_outcome_phrases():
    assert (
        athlete_spotlights_service._is_safe(
            "San Diego could highlight publicly documented Team USA pathways."
        )
        is True
    )
    assert (
        athlete_spotlights_service._is_safe("This city has the best athletes around.")
        is False
    )
    assert (
        athlete_spotlights_service._is_safe("She will win gold for sure.") is False
    )
    assert (
        athlete_spotlights_service._is_safe("Top athletes are guaranteed to medal.")
        is False
    )
    assert (
        athlete_spotlights_service._is_safe("This is a major endorsement of the city.")
        is False
    )


def test_seed_hub_id_indexing_covers_both_forms():
    assert athlete_spotlights_service.hub_in_seed("sd") is True
    assert athlete_spotlights_service.hub_in_seed("san_diego") is True
    assert athlete_spotlights_service.hub_in_seed("not_a_hub") is False
