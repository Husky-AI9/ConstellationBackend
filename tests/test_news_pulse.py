"""
Tests for the live News Pulse endpoint:

  GET /api/hometown/hubs/{hub_id}/news-pulse
  GET /hometown/hubs/{hub_id}/news-pulse   (alias)

These tests exercise:

  - The "unavailable" path that fires when no Gemini path produces validated
    cards (default in the test environment, which strips Gemini env vars).
  - The "happy" path with a mocked Gemini search response — verifies that
    cards are parsed, validated, capped at 3, and that unsafe / URL-less
    cards are dropped.
  - URL validation: every returned card MUST carry an http(s) source_url.
  - Safety scan: every returned card and the brief must avoid forbidden
    ranking / endorsement / outcome wording.
  - 404 on unknown hubs.
  - Service-level helper unit tests.
  - The retry path: a later angle succeeds when an earlier angle returned
    nothing parseable.
  - The metadata-only path: JSON parsing fails but grounding metadata /
    citation chunks carry usable source URLs that we use to build cards.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure no Gemini creds are set during tests so the default unavailable path
# fires unless an individual test explicitly patches the service helpers.
for _env in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "GEMINI_API_KEY"):
    os.environ.pop(_env, None)

from app.main import app  # noqa: E402
from app.services import news_pulse_service  # noqa: E402

client = TestClient(app)


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


def _assert_safe_wording(text: str) -> None:
    for rx in _FORBIDDEN_REGEXES:
        match = rx.search(text)
        assert match is None, (
            f"Forbidden phrase '{match.group(0)}' found in: {text[:200]}"
        )


def _assert_response_shape(data: dict, hub_id: str) -> None:
    expected_keys = {
        "hub_id",
        "hub_name",
        "generated_with_gemini",
        "source",
        "model",
        "query",
        "cards",
        "brief",
        "disclaimer",
        "generated_at",
    }
    missing = expected_keys - set(data.keys())
    assert not missing, f"Missing keys: {missing}"
    assert data["hub_id"] == hub_id
    assert isinstance(data["hub_name"], str) and data["hub_name"]
    assert isinstance(data["generated_with_gemini"], bool)
    assert data["source"] in ("gemini-search", "vertex-grounded", "unavailable")
    assert isinstance(data["query"], str) and data["query"]
    assert isinstance(data["cards"], list)
    assert isinstance(data["brief"], str) and data["brief"].strip()
    assert isinstance(data["disclaimer"], str) and len(data["disclaimer"]) > 30
    try:
        datetime.fromisoformat(data["generated_at"])
    except ValueError:
        pytest.fail(f"generated_at not ISO-8601: {data['generated_at']}")


def _assert_card_shape(card: dict) -> None:
    for key in ("title", "summary", "category", "source_label", "source_url"):
        assert key in card, f"Missing card key '{key}'"
        assert isinstance(card[key], str)
    assert card["title"].strip()
    assert len(card["summary"]) >= 20
    assert card["source_label"].strip()
    assert card["category"] in ("olympic", "paralympic", "team-usa", "la28")
    parsed = urlparse(card["source_url"])
    assert parsed.scheme in ("http", "https"), card
    assert parsed.netloc, card


# ─────────────────────────────────────────────────────────────────────────────
# Unavailable path (default test environment)
# ─────────────────────────────────────────────────────────────────────────────

def test_news_pulse_unavailable_when_no_gemini_configured():
    r = client.get("/api/hometown/hubs/sd/news-pulse")
    assert r.status_code == 200, r.text
    data = r.json()
    _assert_response_shape(data, "sd")
    assert data["hub_name"] == "San Diego"
    assert data["generated_with_gemini"] is False
    assert data["source"] == "unavailable"
    assert data["model"] is None
    assert data["cards"] == []
    # Brief must explain the unavailable state and never invent news.
    assert "unavailable" in data["brief"].lower()


def test_news_pulse_unavailable_when_gemini_returns_nothing(monkeypatch):
    """Even with Gemini configured, if both backends return None we go unavailable."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            return_value=(None, None, None),
        ),
        patch.object(
            news_pulse_service,
            "_try_genai_search",
            return_value=(None, None, None),
        ),
    ):
        r = client.get("/api/hometown/hubs/cos/news-pulse")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["generated_with_gemini"] is False
    assert data["source"] == "unavailable"
    assert data["cards"] == []


def test_news_pulse_unavailable_alias_route_matches():
    canonical = client.get("/api/hometown/hubs/sd/news-pulse").json()
    alias = client.get("/hometown/hubs/sd/news-pulse").json()
    for field in ("hub_id", "hub_name", "generated_with_gemini", "source", "cards"):
        assert canonical[field] == alias[field]


def test_news_pulse_unknown_hub_returns_404():
    r = client.get("/api/hometown/hubs/not_a_real_hub/news-pulse")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Happy path — mocked Gemini search response
# ─────────────────────────────────────────────────────────────────────────────

_VALID_GEMINI_PAYLOAD = {
    "brief": (
        "Recent reporting suggests San Diego could continue to surface storylines "
        "across Team USA and Paralympic pathways heading into LA28."
    ),
    "cards": [
        {
            "title": "San Diego skater could feature in upcoming Team USA training block",
            "summary": (
                "Public reporting suggests a skateboarding athlete with San Diego ties "
                "may participate in an upcoming Team USA training block. The piece "
                "outlines the athlete's recent competition schedule and points toward "
                "potential LA28 visibility."
            ),
            "category": "olympic",
            "source_label": "Team USA",
            "source_url": "https://www.teamusa.com/news/example-skate-article",
            "published_date": "2025-11-12",
        },
        {
            "title": "Paralympic surf community in San Diego signals growth",
            "summary": (
                "A community feature suggests adaptive surfing programs in the San "
                "Diego region appear to be expanding. The article points toward how "
                "the local pipeline could connect Paralympic pathways to the LA28 "
                "host-region story."
            ),
            "category": "paralympic",
            "source_label": "Olympics.com",
            "source_url": "https://olympics.com/en/news/example-paralympic-feature",
            "published_date": "2025-10-04",
        },
        {
            "title": "LA28 venue planning could touch San Diego sport corridors",
            "summary": (
                "Coverage suggests early LA28 venue planning may reference Southern "
                "California training infrastructure. The piece signals possible "
                "regional connections to San Diego's sport culture."
            ),
            "category": "la28",
            "source_label": "LA Times",
            "source_url": "https://www.latimes.com/sports/example-la28-piece",
            "published_date": "2025-09-21",
        },
    ],
}


def _mocked_genai_response(prompt: str):
    return json.dumps(_VALID_GEMINI_PAYLOAD), None, "gemini-search"


def _mocked_vertex_response(prompt: str):
    return json.dumps(_VALID_GEMINI_PAYLOAD), None, "vertex-grounded"


def test_news_pulse_happy_path_via_gemini_search(monkeypatch):
    """When google-generativeai returns a valid payload we surface it."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            return_value=(None, None, None),
        ),
        patch.object(
            news_pulse_service,
            "_try_genai_search",
            side_effect=_mocked_genai_response,
        ),
    ):
        r = client.get("/api/hometown/hubs/sd/news-pulse")

    assert r.status_code == 200, r.text
    data = r.json()
    _assert_response_shape(data, "sd")
    assert data["generated_with_gemini"] is True
    assert data["source"] == "gemini-search"
    assert data["model"]
    assert 2 <= len(data["cards"]) <= 3
    for card in data["cards"]:
        _assert_card_shape(card)
    categories = {c["category"] for c in data["cards"]}
    assert "paralympic" in categories
    full_text = " ".join(
        [data["brief"]]
        + [c["title"] for c in data["cards"]]
        + [c["summary"] for c in data["cards"]]
    )
    _assert_safe_wording(full_text)


def test_news_pulse_happy_path_via_vertex_grounding(monkeypatch):
    """Vertex grounding wins over genai when both are available."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            side_effect=_mocked_vertex_response,
        ) as v,
        patch.object(
            news_pulse_service,
            "_try_genai_search",
            side_effect=_mocked_genai_response,
        ) as g,
    ):
        r = client.get("/api/hometown/hubs/la/news-pulse")

    data = r.json()
    assert r.status_code == 200, r.text
    assert data["generated_with_gemini"] is True
    assert data["source"] == "vertex-grounded"
    assert v.called
    assert not g.called


def test_news_pulse_drops_card_without_url(monkeypatch):
    """Cards without an http(s) source_url are dropped, not invented."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    bad_payload = {
        "brief": "Recent reporting could shape how fans track San Diego pathways.",
        "cards": [
            {
                "title": "Card with valid URL",
                "summary": (
                    "Public reporting suggests this is a valid article that could be "
                    "useful for fans following the hub story."
                ),
                "category": "olympic",
                "source_label": "Team USA",
                "source_url": "https://www.teamusa.com/news/valid",
                "published_date": "2025-08-01",
            },
            {
                "title": "Card missing URL",
                "summary": (
                    "This card has no usable source_url and must be dropped by the "
                    "validator rather than surfaced to fans."
                ),
                "category": "team-usa",
                "source_label": "Unknown outlet",
                "source_url": "not-a-real-url",
                "published_date": None,
            },
        ],
    }

    def _mock(prompt: str):
        return json.dumps(bad_payload), None, "gemini-search"

    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            return_value=(None, None, None),
        ),
        patch.object(news_pulse_service, "_try_genai_search", side_effect=_mock),
    ):
        r = client.get("/api/hometown/hubs/sd/news-pulse")

    data = r.json()
    assert r.status_code == 200
    assert data["generated_with_gemini"] is True
    assert len(data["cards"]) == 1
    assert data["cards"][0]["title"] == "Card with valid URL"


def test_news_pulse_drops_unsafe_card(monkeypatch):
    """A card whose copy uses ranking / outcome language is dropped."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    payload = {
        "brief": "San Diego could surface useful stories for fans tracking the hub.",
        "cards": [
            {
                "title": "Best San Diego athlete will win gold for sure",
                "summary": (
                    "This headline-style summary is intentionally unsafe — it makes "
                    "performance promises and uses ranking language that the safety "
                    "scan must reject before fans ever see the card."
                ),
                "category": "olympic",
                "source_label": "Outlet",
                "source_url": "https://example.com/unsafe",
                "published_date": "2025-12-01",
            },
            {
                "title": "Adaptive surf clinic in San Diego signals growth",
                "summary": (
                    "Public reporting suggests adaptive surf clinics may be expanding. "
                    "The piece points toward potential pipeline storylines without "
                    "predicting any individual outcome."
                ),
                "category": "paralympic",
                "source_label": "Olympics.com",
                "source_url": "https://olympics.com/en/news/safe-card",
                "published_date": "2025-11-20",
            },
        ],
    }

    def _mock(prompt: str):
        return json.dumps(payload), None, "gemini-search"

    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            return_value=(None, None, None),
        ),
        patch.object(news_pulse_service, "_try_genai_search", side_effect=_mock),
    ):
        r = client.get("/api/hometown/hubs/sd/news-pulse")

    data = r.json()
    assert r.status_code == 200
    assert data["generated_with_gemini"] is True
    assert len(data["cards"]) == 1
    assert data["cards"][0]["category"] == "paralympic"
    full_text = " ".join(c["title"] + " " + c["summary"] for c in data["cards"])
    _assert_safe_wording(full_text)


def test_news_pulse_unavailable_when_payload_unparseable_and_no_metadata(monkeypatch):
    """Garbage model output AND no grounding metadata → unavailable."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def _mock(prompt: str):
        return "this is not json at all and has no cards", None, "gemini-search"

    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            return_value=(None, None, None),
        ),
        patch.object(news_pulse_service, "_try_genai_search", side_effect=_mock),
    ):
        r = client.get("/api/hometown/hubs/sd/news-pulse")

    data = r.json()
    assert r.status_code == 200
    assert data["generated_with_gemini"] is False
    assert data["source"] == "unavailable"
    assert data["cards"] == []


def test_news_pulse_unavailable_when_brief_unsafe(monkeypatch):
    """If the model brief itself uses banned wording, return unavailable."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    payload = {
        "brief": "San Diego is the best hub and will win every medal at LA28.",
        "cards": [
            {
                "title": "Card with valid URL",
                "summary": (
                    "Public reporting could help fans track adaptive surfing pathways "
                    "in the region without predicting any individual outcome."
                ),
                "category": "paralympic",
                "source_label": "Outlet",
                "source_url": "https://example.com/valid",
                "published_date": "2025-12-01",
            }
        ],
    }

    def _mock(prompt: str):
        return json.dumps(payload), None, "gemini-search"

    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            return_value=(None, None, None),
        ),
        patch.object(news_pulse_service, "_try_genai_search", side_effect=_mock),
    ):
        r = client.get("/api/hometown/hubs/sd/news-pulse")

    data = r.json()
    # Brief unsafe blows away the JSON path; with no grounding metadata we go
    # unavailable on this attempt — and the retry angles also have no metadata,
    # so the final answer is unavailable.
    assert data["generated_with_gemini"] is False
    assert data["source"] == "unavailable"
    assert data["cards"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Retry path — first angle returns nothing, a later angle succeeds
# ─────────────────────────────────────────────────────────────────────────────

def test_news_pulse_retry_path_uses_later_angle(monkeypatch):
    """
    First call returns no usable output; a later call returns a valid JSON
    payload. The endpoint should surface the second-attempt result rather
    than giving up after the first miss.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    call_state = {"n": 0}

    def _mock(prompt: str):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return None, None, "gemini-search"
        return json.dumps(_VALID_GEMINI_PAYLOAD), None, "gemini-search"

    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            return_value=(None, None, None),
        ),
        patch.object(news_pulse_service, "_try_genai_search", side_effect=_mock),
    ):
        r = client.get("/api/hometown/hubs/sd/news-pulse")

    data = r.json()
    assert r.status_code == 200
    assert data["generated_with_gemini"] is True
    assert data["source"] == "gemini-search"
    assert len(data["cards"]) >= 2
    assert call_state["n"] >= 2  # Proves we actually retried.


# ─────────────────────────────────────────────────────────────────────────────
# Metadata-only path — JSON unparseable but grounding has source URLs
# ─────────────────────────────────────────────────────────────────────────────

def _fake_response_with_metadata(uris):
    """
    Build a Vertex-shaped response object that exposes
    candidates[0].grounding_metadata.grounding_chunks[*].web.{uri,title}.
    """
    chunks = [
        SimpleNamespace(
            web=SimpleNamespace(
                uri=uri,
                title=f"Public coverage about Team USA Olympian San Diego ({uri})",
            )
        )
        for uri in uris
    ]
    candidate = SimpleNamespace(
        grounding_metadata=SimpleNamespace(grounding_chunks=chunks),
        citation_metadata=None,
    )
    return SimpleNamespace(candidates=[candidate])


def test_news_pulse_uses_grounding_metadata_when_json_fails(monkeypatch):
    """
    The model returns prose (no parseable JSON) but the response carries
    grounding metadata with two valid URLs. The service should reconstruct
    source-backed cards from the metadata rather than giving up.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fake = _fake_response_with_metadata(
        [
            "https://www.teamusa.com/news/sd-olympian-feature",
            "https://www.paralympic.org/feature/sd-paralympian",
        ]
    )

    def _mock(prompt: str):
        return (
            "Here is some prose summary that is not JSON; ignore me.",
            fake,
            "gemini-search",
        )

    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            return_value=(None, None, None),
        ),
        patch.object(news_pulse_service, "_try_genai_search", side_effect=_mock),
    ):
        r = client.get("/api/hometown/hubs/sd/news-pulse")

    data = r.json()
    assert r.status_code == 200, r.text
    assert data["generated_with_gemini"] is True
    assert data["source"] == "gemini-search"
    assert 1 <= len(data["cards"]) <= 3
    seen_urls = {c["source_url"] for c in data["cards"]}
    assert "https://www.teamusa.com/news/sd-olympian-feature" in seen_urls
    assert "https://www.paralympic.org/feature/sd-paralympian" in seen_urls
    for card in data["cards"]:
        _assert_card_shape(card)
        # No invented URLs.
        assert card["source_url"] in {
            "https://www.teamusa.com/news/sd-olympian-feature",
            "https://www.paralympic.org/feature/sd-paralympian",
        }
    full_text = " ".join(
        [data["brief"]]
        + [c["title"] for c in data["cards"]]
        + [c["summary"] for c in data["cards"]]
    )
    _assert_safe_wording(full_text)


def test_news_pulse_metadata_drops_invalid_urls(monkeypatch):
    """
    Grounding URLs that aren't http(s) must be dropped. If only invalid URLs
    are present and JSON didn't parse, the attempt fails — and with all
    retry angles also empty, we end up unavailable.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    bad_response = _fake_response_with_metadata(
        ["not-a-url", "ftp://example.com/no-good"]
    )

    def _mock(prompt: str):
        return ("prose only", bad_response, "gemini-search")

    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            return_value=(None, None, None),
        ),
        patch.object(news_pulse_service, "_try_genai_search", side_effect=_mock),
    ):
        r = client.get("/api/hometown/hubs/sd/news-pulse")

    data = r.json()
    assert data["generated_with_gemini"] is False
    assert data["source"] == "unavailable"
    assert data["cards"] == []


def test_news_pulse_metadata_via_citation_sources_shape(monkeypatch):
    """
    Older SDK shape: candidate.citation_metadata.citation_sources[*].uri.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    citations = [
        SimpleNamespace(
            uri="https://olympics.com/en/news/sd-olympian-piece",
            title="Public Olympic feature about San Diego",
            snippet=(
                "Public reporting could outline how San Diego ties into Team USA "
                "Olympic pathways heading into the 2028 Games window."
            ),
        )
    ]
    candidate = SimpleNamespace(
        grounding_metadata=None,
        citation_metadata=SimpleNamespace(citation_sources=citations),
    )
    fake = SimpleNamespace(candidates=[candidate])

    def _mock(prompt: str):
        return ("prose only", fake, "gemini-search")

    with (
        patch.object(
            news_pulse_service,
            "_try_vertex_grounded",
            return_value=(None, None, None),
        ),
        patch.object(news_pulse_service, "_try_genai_search", side_effect=_mock),
    ):
        r = client.get("/api/hometown/hubs/sd/news-pulse")

    data = r.json()
    assert r.status_code == 200
    assert data["generated_with_gemini"] is True
    assert len(data["cards"]) == 1
    assert (
        data["cards"][0]["source_url"]
        == "https://olympics.com/en/news/sd-olympian-piece"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Service-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_is_safe_rejects_ranking_and_outcome_phrases():
    assert news_pulse_service._is_safe(
        "San Diego could highlight publicly documented Team USA pathways."
    ) is True
    assert news_pulse_service._is_safe("She will win gold for sure.") is False
    assert news_pulse_service._is_safe("Top athletes are guaranteed to medal.") is False
    assert news_pulse_service._is_safe("This is a major endorsement of the city.") is False
    assert news_pulse_service._is_safe("The best hub for fans.") is False


def test_normalise_category_maps_known_values():
    assert news_pulse_service._normalise_category("olympic") == "olympic"
    assert news_pulse_service._normalise_category("Paralympic") == "paralympic"
    assert news_pulse_service._normalise_category("team_usa") == "team-usa"
    assert news_pulse_service._normalise_category("LA28") == "la28"
    assert news_pulse_service._normalise_category("random") == "team-usa"
    assert news_pulse_service._normalise_category("Olympic Games") == "olympic"
    assert news_pulse_service._normalise_category("paralympic-team") == "paralympic"
    assert news_pulse_service._normalise_category("LA 2028 venue") == "la28"


def test_is_valid_url_accepts_http_https_only():
    assert news_pulse_service._is_valid_url("https://example.com/path") is True
    assert news_pulse_service._is_valid_url("http://example.com") is True
    assert news_pulse_service._is_valid_url("ftp://example.com") is False
    assert news_pulse_service._is_valid_url("not-a-url") is False
    assert news_pulse_service._is_valid_url("") is False


def test_parse_payload_handles_code_fences():
    raw = '```json\n{"brief": "x", "cards": []}\n```'
    parsed = news_pulse_service._parse_payload(raw)
    assert parsed is not None
    assert parsed["brief"] == "x"


def test_parse_payload_returns_none_for_garbage():
    assert news_pulse_service._parse_payload("") is None
    assert news_pulse_service._parse_payload("not json at all") is None


def test_extract_grounding_sources_handles_dicts_and_objects():
    """Both dict-style and attribute-style metadata should yield URLs."""
    dict_response = {
        "candidates": [
            {
                "grounding_metadata": {
                    "grounding_chunks": [
                        {
                            "web": {
                                "uri": "https://example.com/a",
                                "title": "Article A",
                            }
                        }
                    ]
                }
            }
        ]
    }
    sources = news_pulse_service._extract_grounding_sources(dict_response)
    assert any(s["url"] == "https://example.com/a" for s in sources)

    obj_response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    grounding_chunks=[
                        SimpleNamespace(
                            web=SimpleNamespace(
                                uri="https://example.com/b", title="Article B"
                            )
                        )
                    ]
                )
            )
        ]
    )
    sources = news_pulse_service._extract_grounding_sources(obj_response)
    assert any(s["url"] == "https://example.com/b" for s in sources)


def test_extract_grounding_sources_handles_missing_metadata():
    assert news_pulse_service._extract_grounding_sources(None) == []
    assert news_pulse_service._extract_grounding_sources(SimpleNamespace(candidates=[])) == []


def test_retry_queries_includes_required_angles():
    from app.models import (
        ApiCoordinates,
        ApiHubDetail,
        ApiParitySnapshot,
    )

    detail = ApiHubDetail(
        id="sd",
        name="San Diego",
        region="Pacific Coast",
        x=0.0,
        y=0.0,
        coordinates=ApiCoordinates(lat=0.0, lng=0.0),
        tags=["coastal"],
        narrative="",
        map_pins=[],
        parity_snapshot=ApiParitySnapshot(
            olympic_story_estimate="multiple",
            paralympic_story_estimate="multiple",
            parity_note="ok",
        ),
        sources=[],
    )
    queries = [q for _, q in news_pulse_service._retry_queries(detail)]
    assert any("Team USA" in q and "Olympic" in q for q in queries)
    assert any("Olympian" in q for q in queries)
    assert any("Paralympic" in q for q in queries)
    assert any("LA28" in q for q in queries)
