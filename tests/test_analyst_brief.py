"""
Tests for the analyst brief endpoint:

  GET  /api/hometown/hubs/{hub_id}/gemini-brief
  POST /api/hometown/hubs/{hub_id}/gemini-brief

These tests run without Gemini credentials, so the deterministic local
fallback path is exercised. They verify:

  - Response shape matches AnalystBriefResponse
  - generated_with_gemini is False when no creds are configured
  - All five expected sections are present and non-trivial
  - key_takeaway and disclaimer are present and substantive
  - Copy uses conditional language only (no medal predictions)
  - Olympic and Paralympic signals are both represented
  - Alias route (no /api prefix) returns the same shape
  - 404 on unknown hubs
  - The service-level safety scan rejects unsafe model output
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure no Gemini creds are set during tests so we exercise the fallback path.
for _env in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "GEMINI_API_KEY"):
    os.environ.pop(_env, None)

from app.main import app  # noqa: E402
from app.services import analyst_brief_service  # noqa: E402

client = TestClient(app)


# Forbidden phrases that would indicate medal predictions or guarantees.
_FORBIDDEN_REGEXES = [
    re.compile(r"\bwill\s+(?:win|medal|finish|place|take|secure|earn|capture)\b", re.I),
    re.compile(r"\bguarantee[ds]?\b", re.I),
    re.compile(r"\bcertain\s+to\b", re.I),
    re.compile(r"\bsure\s+thing\b", re.I),
    re.compile(r"\bgold\s+medal\s+(?:lock|favorite)\b", re.I),
]

_EXPECTED_SECTION_TITLES = {
    "Hometown Snapshot",
    "Olympic Signal",
    "Paralympic Signal",
    "LA28 Momentum",
    "What Fans Could Watch For",
}


def _assert_safe_wording(text: str) -> None:
    for rx in _FORBIDDEN_REGEXES:
        match = rx.search(text)
        assert match is None, f"Forbidden phrase '{match.group(0)}' found in: {text[:200]}"


def _assert_brief_shape(data: dict, hub_id: str) -> None:
    for key in (
        "hub_id", "hub_name", "generated_with_gemini", "model", "source",
        "sections", "key_takeaway", "disclaimer", "generated_at",
    ):
        assert key in data, f"missing key '{key}' in analyst brief response"

    assert data["hub_id"] == hub_id
    assert isinstance(data["hub_name"], str) and data["hub_name"]
    assert isinstance(data["generated_with_gemini"], bool)
    assert data["source"] in ("vertex", "gemini", "local-fallback")
    assert isinstance(data["sections"], list) and len(data["sections"]) >= 4
    assert isinstance(data["key_takeaway"], str) and len(data["key_takeaway"]) > 10
    assert isinstance(data["disclaimer"], str) and len(data["disclaimer"]) > 30

    # Validate ISO timestamp
    ts = data["generated_at"]
    try:
        datetime.fromisoformat(ts)
    except ValueError:
        pytest.fail(f"generated_at not ISO-8601: {ts}")

    titles = {s["title"] for s in data["sections"]}
    assert _EXPECTED_SECTION_TITLES.issubset(titles), (
        f"Missing expected sections: {_EXPECTED_SECTION_TITLES - titles}"
    )

    for section in data["sections"]:
        assert isinstance(section["title"], str) and section["title"].strip()
        assert isinstance(section["body"], str)
        assert len(section["body"]) >= 30, f"section '{section['title']}' body too short"


# ─────────────────────────────────────────────────────────────────────────────
# Happy paths
# ─────────────────────────────────────────────────────────────────────────────

def test_get_analyst_brief_basic():
    r = client.get("/api/hometown/hubs/sd/gemini-brief")
    assert r.status_code == 200, r.text
    data = r.json()
    _assert_brief_shape(data, "sd")
    assert data["hub_name"] == "San Diego"

    # No Gemini credentials configured in tests → fallback path.
    assert data["generated_with_gemini"] is False
    assert data["source"] == "local-fallback"
    assert data["model"] is None


def test_post_analyst_brief_with_interests():
    r = client.post(
        "/api/hometown/hubs/cos/gemini-brief",
        json={"interests": ["wheelchair_basketball", "altitude_training"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    _assert_brief_shape(data, "cos")
    assert data["hub_name"] == "Colorado Springs"
    # Interest tags should appear in the "What Fans Could Watch For" section
    watch = next(s for s in data["sections"] if s["title"] == "What Fans Could Watch For")
    assert "wheelchair_basketball" in watch["body"] or "altitude_training" in watch["body"]


def test_post_analyst_brief_empty_body():
    """POST with no body still works."""
    r = client.post("/api/hometown/hubs/la/gemini-brief")
    assert r.status_code == 200, r.text
    _assert_brief_shape(r.json(), "la")


# ─────────────────────────────────────────────────────────────────────────────
# Safety / parity / no-private-data
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hub_id", ["sd", "cos", "la", "atl", "hou", "mia"])
def test_brief_uses_safe_conditional_wording(hub_id):
    r = client.get(f"/api/hometown/hubs/{hub_id}/gemini-brief")
    assert r.status_code == 200
    data = r.json()
    full_text = " ".join(s["body"] for s in data["sections"]) + " " + data["key_takeaway"]
    _assert_safe_wording(full_text)

    # At least one conditional marker should appear somewhere in the brief.
    conditionals = ["could", "may", "might", "suggest", "signal", "potential", "appear"]
    assert any(c in full_text.lower() for c in conditionals), (
        f"No conditional markers found in brief for {hub_id}"
    )


def test_brief_treats_olympic_and_paralympic_with_equal_prominence():
    r = client.get("/api/hometown/hubs/cos/gemini-brief")
    data = r.json()
    titles = [s["title"] for s in data["sections"]]
    assert "Olympic Signal" in titles
    assert "Paralympic Signal" in titles
    olympic = next(s for s in data["sections"] if s["title"] == "Olympic Signal")
    paralympic = next(s for s in data["sections"] if s["title"] == "Paralympic Signal")
    # Bodies should be of similar order of magnitude in length (parity).
    assert len(paralympic["body"]) >= max(40, len(olympic["body"]) // 3)


def test_brief_disclaimer_mentions_aggregate_and_no_predictions():
    r = client.get("/api/hometown/hubs/atl/gemini-brief")
    disclaimer = r.json()["disclaimer"].lower()
    assert "aggregate" in disclaimer
    assert "not" in disclaimer  # "not medal counts / not predictions"
    assert "predict" in disclaimer or "performance" in disclaimer


def test_brief_no_private_athlete_patterns():
    r = client.get("/api/hometown/hubs/hou/gemini-brief")
    text = r.text
    for forbidden in ["@", "SSN", "phone", "email", "date of birth", "passport"]:
        assert forbidden not in text, f"Possible private pattern '{forbidden}' in response"


# ─────────────────────────────────────────────────────────────────────────────
# Aliases / errors
# ─────────────────────────────────────────────────────────────────────────────

def test_alias_route_matches_canonical_shape():
    canonical = client.get("/api/hometown/hubs/sd/gemini-brief").json()
    alias = client.get("/hometown/hubs/sd/gemini-brief").json()
    # generated_at differs across calls → compare everything else.
    for field in ("hub_id", "hub_name", "generated_with_gemini", "source",
                  "key_takeaway", "disclaimer"):
        assert canonical[field] == alias[field], f"Mismatch on field '{field}'"
    assert [s["title"] for s in canonical["sections"]] == [s["title"] for s in alias["sections"]]


def test_brief_unknown_hub_returns_404():
    r = client.get("/api/hometown/hubs/not_a_real_hub/gemini-brief")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()

    r2 = client.post("/api/hometown/hubs/not_a_real_hub/gemini-brief", json={})
    assert r2.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Service-level unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_safety_scan_rejects_outcome_phrases():
    assert analyst_brief_service._is_safe(
        "San Diego could offer rich storylines and may suggest momentum."
    ) is True
    assert analyst_brief_service._is_safe(
        "San Diego will win gold in surfing and is guaranteed to medal."
    ) is False
    assert analyst_brief_service._is_safe(
        "This city is a sure thing for the podium."
    ) is False


def test_parse_model_json_handles_code_fences():
    raw = '```json\n{"sections": [{"title": "T", "body": "B"}], "key_takeaway": "K"}\n```'
    parsed = analyst_brief_service._parse_model_json(raw)
    assert parsed is not None
    assert parsed["sections"][0]["title"] == "T"
    assert parsed["key_takeaway"] == "K"


def test_parse_model_json_returns_none_for_garbage():
    assert analyst_brief_service._parse_model_json("not json at all") is None
    assert analyst_brief_service._parse_model_json("") is None
