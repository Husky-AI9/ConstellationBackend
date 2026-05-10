"""
Pydantic models for the Team USA Hometown Signals & LA28 Momentum API.

All shapes mirror the TypeScript types in Hub.tsx exactly:
  ApiHubSummary, ApiMapPin, ApiParitySnapshot, ApiHubDetail, ApiBriefResponse

Additional models:
  LA28Sport, SportSignals — for the /api/la28/momentum endpoints.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------

class SportKind(str, Enum):
    olympic = "olympic"
    paralympic = "paralympic"


class MapPinColor(str, Enum):
    red = "red"
    blue = "blue"
    white = "white"


class BriefSource(str, Enum):
    local_fallback = "local-fallback"
    gemini = "gemini"
    vertex = "vertex"


# ---------------------------------------------------------------------------
# Hometown Signals response models (match Hub.tsx TypeScript types exactly)
# ---------------------------------------------------------------------------

class ApiCoordinates(BaseModel):
    lat: float
    lng: float


class ApiHubSummary(BaseModel):
    """
    Returned by GET /api/hometown/hubs
    Matches TypeScript: ApiHubSummary
    """
    id: str
    name: str
    region: str
    x: float
    y: float
    short_insight: str
    tags: List[str]
    coordinates: Optional["ApiCoordinates"] = None


class ApiMapPin(BaseModel):
    """
    Embedded in ApiHubDetail.map_pins
    Matches TypeScript: ApiMapPin
    """
    label: str
    lat: float
    lng: float
    color: MapPinColor
    description: str
    external_link: Optional[str] = None


class ApiParitySnapshot(BaseModel):
    """
    Embedded in ApiHubDetail.parity_snapshot
    Matches TypeScript: ApiParitySnapshot
    """
    olympic_story_estimate: str
    paralympic_story_estimate: str
    parity_note: str


class ApiHubDetail(BaseModel):
    """
    Returned by GET /api/hometown/hubs/{hub_id}
    Matches TypeScript: ApiHubDetail
    """
    id: str
    name: str
    region: str
    x: float
    y: float
    coordinates: ApiCoordinates
    tags: List[str]
    narrative: str
    map_pins: List[ApiMapPin]
    parity_snapshot: ApiParitySnapshot
    sources: List[str]


# ---------------------------------------------------------------------------
# Brief generation (Gemini / Vertex AI)
# ---------------------------------------------------------------------------

class BriefRequest(BaseModel):
    """
    Request body for POST /api/hometown/brief
    """
    hub_id: str = Field(..., description="Hub identifier, e.g. 'sd' or 'cos'")
    interests: Optional[List[str]] = Field(
        None,
        description="Optional fan interest tags, e.g. ['track', 'wheelchair_basketball']",
    )


class ApiBriefResponse(BaseModel):
    """
    Returned by POST /api/hometown/brief
    Matches TypeScript: ApiBriefResponse
    """
    hub_id: str
    hub_name: str
    brief: str
    themes: List[str]
    disclaimer: str
    source: BriefSource


# ---------------------------------------------------------------------------
# Analyst Brief (sectioned long-form Gemini brief for hub detail pane)
# ---------------------------------------------------------------------------

class AnalystBriefSection(BaseModel):
    """A titled section of an analyst brief."""
    title: str
    body: str


class AnalystBriefRequest(BaseModel):
    """Optional request body for POST /api/hometown/hubs/{hub_id}/gemini-brief."""
    interests: Optional[List[str]] = Field(
        None,
        description="Optional fan interest tags, e.g. ['track', 'wheelchair_basketball']",
    )


class AnalystBriefResponse(BaseModel):
    """
    Structured analyst brief returned for a hub.
    Returned by GET/POST /api/hometown/hubs/{hub_id}/gemini-brief.
    """
    hub_id: str
    hub_name: str
    generated_with_gemini: bool
    model: Optional[str] = None
    source: BriefSource
    sections: List[AnalystBriefSection]
    key_takeaway: str
    disclaimer: str
    generated_at: str


# ---------------------------------------------------------------------------
# LA28 Momentum models
# ---------------------------------------------------------------------------

class SportSignals(BaseModel):
    """
    Normalised 0-100 signal dimensions for a sport in a city.
    These are descriptive indicators, never performance predictions.
    """
    hometown: float = Field(..., ge=0, le=100)
    world_champ: float = Field(..., ge=0, le=100)
    news: float = Field(..., ge=0, le=100)
    la28: float = Field(..., ge=0, le=100)


class LA28Sport(BaseModel):
    """
    A single sport's LA28 momentum entry for a city.
    Returned in /api/la28/momentum/{hub_id}
    """
    id: str
    name: str
    kind: SportKind
    signals: SportSignals
    momentum_score: float = Field(
        ...,
        ge=0,
        le=100,
        description="Weighted composite of signal dimensions (descriptive, not predictive).",
    )
    reason: str


class LA28MomentumResponse(BaseModel):
    """
    Full momentum response for a hub.
    Returned by GET /api/la28/momentum/{hub_id}
    """
    hub_id: str
    hub_name: str
    sports: List[LA28Sport]
    disclaimer: str


class LA28MomentumSummary(BaseModel):
    """
    Lightweight summary for the hub list.
    Returned by GET /api/la28/momentum
    """
    hub_id: str
    hub_name: str
    top_sport: Optional[str]
    top_score: Optional[float]
    olympic_count: int
    paralympic_count: int


# ---------------------------------------------------------------------------
# Public Athlete Spotlights
# ---------------------------------------------------------------------------

class AthleteSpotlight(BaseModel):
    """
    A single public-facing athlete spotlight, sourced from official Team USA,
    federation, university, or reputable public profile pages. All fields are
    aggregate public information; no private athlete data is included.
    """
    category: SportKind
    name: str
    sport: str
    hometown_or_region: str
    recent_achievement: str
    source_url: str
    source_label: str


class AthleteSpotlightBrief(BaseModel):
    """A short Gemini- or fallback-generated brief about a hub's spotlights."""
    title: str
    summary: str
    bullets: List[str]


class AthleteSpotlightsResponse(BaseModel):
    """
    Returned by GET /api/hometown/hubs/{hub_id}/athlete-spotlights.
    Contains separate Olympic and Paralympic arrays plus a short fan brief.
    """
    hub_id: str
    hub_name: str
    generated_with_gemini: bool
    source: BriefSource
    model: Optional[str] = None
    olympic_spotlights: List[AthleteSpotlight]
    paralympic_spotlights: List[AthleteSpotlight]
    gemini_brief: AthleteSpotlightBrief
    disclaimer: str
    generated_at: str


# ---------------------------------------------------------------------------
# News Pulse (live Gemini-grounded news cards)
# ---------------------------------------------------------------------------

class NewsPulseSource(str, Enum):
    gemini_search = "gemini-search"
    vertex_grounded = "vertex-grounded"
    unavailable = "unavailable"


class NewsCard(BaseModel):
    """
    A single live news card surfaced from a Gemini grounded/search response.
    Each card MUST carry a public http(s) source_url. Copy is conditional and
    descriptive — no rankings, endorsements, or performance predictions.
    """
    title: str
    summary: str
    category: str
    source_label: str
    source_url: str
    published_date: Optional[str] = None


class NewsPulseResponse(BaseModel):
    """
    Returned by GET /api/hometown/hubs/{hub_id}/news-pulse.
    Live, Gemini-grounded news for a hometown hub. Never persisted to disk
    or DB. When Gemini/search is unavailable, `cards` is empty and `brief`
    explains the unavailable state.
    """
    hub_id: str
    hub_name: str
    generated_with_gemini: bool
    source: NewsPulseSource
    model: Optional[str] = None
    query: str
    cards: List[NewsCard]
    brief: str
    disclaimer: str
    generated_at: str


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    version: str
    gemini_configured: bool
