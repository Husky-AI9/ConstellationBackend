"""
Team USA Hometown Signals & LA28 Momentum API
FastAPI application — Cloud Run deployable.

Endpoints (matching Hub.tsx exactly):
  GET  /health                          — Health check
  GET  /api/hometown/hubs               — List all hub summaries
  GET  /api/hometown/hubs/{hub_id}      — Hub detail with map pins, parity snapshot
  POST /api/hometown/brief              — Generate Gemini/Vertex AI fan brief
  GET  /api/hometown/hubs/{hub_id}/gemini-brief — Structured analyst brief (sectioned)
  POST /api/hometown/hubs/{hub_id}/gemini-brief — Same, with optional interests body
  GET  /api/hometown/hubs/{hub_id}/athlete-spotlights — Public athlete spotlights
  GET  /api/hometown/hubs/{hub_id}/news-pulse — Live Gemini-grounded news pulse
  GET  /api/la28/momentum               — LA28 momentum summaries for all hubs
  GET  /api/la28/momentum/{hub_id}      — LA28 sport momentum detail for a hub

Fallback aliases (handle slight URL variations):
  GET  /hometown/hubs                   → same as /api/hometown/hubs
  GET  /hometown/hubs/{hub_id}          → same as /api/hometown/hubs/{hub_id}
  POST /hometown/brief                  → same as /api/hometown/brief
  GET  /la28/momentum                   → same as /api/la28/momentum
  GET  /la28/momentum/{hub_id}          → same as /api/la28/momentum/{hub_id}

CORS: allows all Lovable origins, localhost dev, and wildcard for hackathon use.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.models import (
    AnalystBriefRequest,
    AnalystBriefResponse,
    ApiBriefResponse,
    ApiHubDetail,
    ApiHubSummary,
    AthleteSpotlightsResponse,
    BriefRequest,
    BriefSource,
    HealthResponse,
    LA28MomentumResponse,
    LA28MomentumSummary,
    NewsPulseResponse,
)
from app.services.data_store import DataStore, get_data_store
from app.services import (
    analyst_brief_service,
    athlete_spotlights_service,
    gemini_service,
    momentum_service,
    news_pulse_service,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CORS allowed origins
# Lovable previews use *.lovable.app, *.lovableproject.com, and *.vercel.app.
# For a hackathon we also allow * as a final fallback (configurable via env).
# ---------------------------------------------------------------------------

_CORS_ORIGINS_ENV = os.getenv(
    "CORS_ALLOWED_ORIGINS",
    (
        "https://*.lovable.app,"
        "https://*.lovableproject.com,"
        "https://*.vercel.app,"
        "https://*.netlify.app,"
        "http://localhost:3000,"
        "http://localhost:5173,"
        "http://localhost:8080,"
        "http://127.0.0.1:3000,"
        "http://127.0.0.1:5173"
    ),
)
_CORS_ORIGINS: List[str] = [o.strip() for o in _CORS_ORIGINS_ENV.split(",") if o.strip()]

# If CORS_ALLOW_ALL_ORIGINS=true the API accepts requests from anywhere.
# Recommended only for hackathon/demo environments.
_CORS_ALLOW_ALL = os.getenv("CORS_ALLOW_ALL_ORIGINS", "false").lower() == "true"

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Team USA Hometown Signals & LA28 Momentum API",
    description=(
        "Fan-facing storytelling API for the Team USA Hackathon Hub. "
        "Serves hometown hub data, Gemini-generated fan briefs, and LA28 "
        "momentum indicators. All data is aggregate public information; "
        "no private athlete data is exposed. "
        "Olympic and Paralympic signals are treated with equal prominence."
    ),
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _CORS_ALLOW_ALL else _CORS_ORIGINS,
    allow_origin_regex=r"https://.*\.(lovable\.app|lovableproject\.com|vercel\.app|netlify\.app)$",
    allow_credentials=False,  # no cookies/auth
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Authorization"],
    max_age=600,
)

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    # Eagerly initialise the data store to catch CSV errors at boot time
    store = get_data_store()
    hub_count = len(store.all_hub_ids())
    logger.info("DataStore loaded: %d hubs", hub_count)
    gemini_ok = gemini_service.gemini_is_configured()
    logger.info("Gemini configured: %s", gemini_ok)


# ---------------------------------------------------------------------------
# Helper: hub-name lookup map (avoids repeated list traversal)
# ---------------------------------------------------------------------------

def _hub_name_map(store: DataStore) -> dict:
    return {h.id: h.name for h in store.list_summaries()}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Health check",
)
async def health(store: DataStore = Depends(get_data_store)) -> HealthResponse:
    """
    Liveness/readiness probe endpoint.
    Returns 200 when the service is healthy.
    Used by Cloud Run health checks.
    """
    return HealthResponse(
        status="ok",
        version=__version__,
        gemini_configured=gemini_service.gemini_is_configured(),
    )


# ---------------------------------------------------------------------------
# Hometown Signals endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/api/hometown/hubs",
    response_model=List[ApiHubSummary],
    tags=["hometown"],
    summary="List all hometown hub summaries",
)
async def list_hubs(
    store: DataStore = Depends(get_data_store),
) -> List[ApiHubSummary]:
    """
    Return a summary list of all 12 hometown hubs.
    Matches TypeScript type: ApiHubSummary[]

    Used by useHometownHubs() hook in Hub.tsx.
    """
    return store.list_summaries()


@app.get(
    "/api/hometown/hubs/{hub_id}",
    response_model=ApiHubDetail,
    tags=["hometown"],
    summary="Get full hometown hub detail",
)
async def get_hub(
    hub_id: str = Path(..., description="Hub identifier, e.g. 'sd', 'cos', 'la'"),
    store: DataStore = Depends(get_data_store),
) -> ApiHubDetail:
    """
    Return full detail for a single hometown hub including:
    - Coordinates (real lat/lng from Census Gazetteer)
    - Map pins (Olympic & Paralympic signals, LA28 context)
    - Parity snapshot (Olympic vs Paralympic story estimate)
    - Source attribution

    Matches TypeScript type: ApiHubDetail.
    Used by useHubDetail(hubId) hook in Hub.tsx.
    """
    detail = store.get_detail(hub_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Hub '{hub_id}' not found. Available hubs: {store.all_hub_ids()}",
        )
    return detail


@app.post(
    "/api/hometown/brief",
    response_model=ApiBriefResponse,
    tags=["hometown"],
    summary="Generate a Gemini fan brief for a hub",
)
async def generate_brief(
    body: BriefRequest,
    store: DataStore = Depends(get_data_store),
) -> ApiBriefResponse:
    """
    Generate a fan-facing storytelling brief for a hometown hub.

    Uses Vertex AI (preferred on Cloud Run) or google-generativeai SDK
    (GEMINI_API_KEY fallback) to generate ~150-200 words of conditional,
    parity-aware copy. Falls back to a local template if no AI service
    is configured.

    Matches TypeScript type: ApiBriefResponse.
    Used by useGenerateBrief() hook in Hub.tsx.
    """
    if not store.hub_exists(body.hub_id):
        raise HTTPException(
            status_code=404,
            detail=f"Hub '{body.hub_id}' not found.",
        )

    row = store.get_raw_row(body.hub_id)
    if row is None:
        raise HTTPException(status_code=500, detail="Data store error.")

    city = row.get("city", body.hub_id)
    state = row.get("state", "")
    region = row.get("region", "")
    narrative = row.get("narrative", "")
    sports = row.get("sports", "").strip('"')
    tags = row.get("tags", "")
    climate = row.get("climate_type", "")

    o_count_raw = row.get("olympian_count", "")
    p_count_raw = row.get("paralympian_count", "")
    o_count = int(o_count_raw) if o_count_raw.strip().lstrip("-").isdigit() else None
    p_count = int(p_count_raw) if p_count_raw.strip().lstrip("-").isdigit() else None

    brief_text, source_label = gemini_service.generate_brief(
        hub_id=body.hub_id,
        city=city,
        state=state,
        region=region,
        narrative=narrative,
        sports=sports,
        o_count=o_count,
        p_count=p_count,
        tags=tags,
        climate=climate,
        interests=body.interests,
    )

    themes = gemini_service.get_themes_for_brief(brief_text, sports)

    return ApiBriefResponse(
        hub_id=body.hub_id,
        hub_name=city,
        brief=brief_text,
        themes=themes,
        disclaimer=(
            "All counts are aggregate hometown roster entries from official USOPC "
            "2024 spreadsheets. They are not medal counts, performance predictions, "
            "or individual athlete data. Language is intentionally conditional and "
            "descriptive. Olympic and Paralympic signals are treated with equal prominence."
        ),
        source=BriefSource(source_label),
    )


# ---------------------------------------------------------------------------
# Analyst Brief (sectioned Gemini brief for hub detail pane)
# ---------------------------------------------------------------------------

def _analyst_brief_for_hub(
    hub_id: str,
    interests: Optional[List[str]],
    store: DataStore,
) -> AnalystBriefResponse:
    """Shared implementation for the GET and POST analyst-brief routes."""
    detail = store.get_detail(hub_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Hub '{hub_id}' not found. Available hubs: {store.all_hub_ids()}",
        )

    name_map = _hub_name_map(store)
    hub_name = name_map.get(hub_id, detail.name)
    momentum = momentum_service.get_momentum_for_hub(hub_id, hub_name)

    return analyst_brief_service.generate_analyst_brief(
        detail=detail,
        momentum=momentum,
        interests=interests,
    )


@app.get(
    "/api/hometown/hubs/{hub_id}/gemini-brief",
    response_model=AnalystBriefResponse,
    tags=["hometown"],
    summary="Structured analyst brief for a hometown hub",
)
async def get_analyst_brief(
    hub_id: str = Path(..., description="Hub identifier, e.g. 'sd', 'cos', 'la'"),
    store: DataStore = Depends(get_data_store),
) -> AnalystBriefResponse:
    """
    Return a structured analyst brief for a hub, suitable for the frontend
    Analyst Brief pane. Combines real hub detail (parity snapshot, map pins,
    tags) and LA28 momentum data.

    Tries Vertex AI / Gemini if configured; otherwise returns a deterministic,
    safe fallback with `generated_with_gemini: false`. All copy uses
    conditional language and avoids any medal predictions or individual
    performance guarantees. Olympic and Paralympic signals are treated with
    equal prominence.
    """
    return _analyst_brief_for_hub(hub_id, interests=None, store=store)


@app.post(
    "/api/hometown/hubs/{hub_id}/gemini-brief",
    response_model=AnalystBriefResponse,
    tags=["hometown"],
    summary="Structured analyst brief for a hub (with optional interests)",
)
async def post_analyst_brief(
    hub_id: str = Path(..., description="Hub identifier, e.g. 'sd', 'cos', 'la'"),
    body: Optional[AnalystBriefRequest] = None,
    store: DataStore = Depends(get_data_store),
) -> AnalystBriefResponse:
    """
    POST variant of the analyst brief endpoint. Accepts an optional body with
    `interests` so the brief can lightly reflect fan interest tags.
    """
    interests = body.interests if body else None
    return _analyst_brief_for_hub(hub_id, interests=interests, store=store)


# ---------------------------------------------------------------------------
# Public Athlete Spotlights
# ---------------------------------------------------------------------------

@app.get(
    "/api/hometown/hubs/{hub_id}/athlete-spotlights",
    response_model=AthleteSpotlightsResponse,
    tags=["hometown"],
    summary="Public athlete spotlights for a hometown hub",
)
async def get_athlete_spotlights(
    hub_id: str = Path(..., description="Hub identifier, e.g. 'sd', 'cos', 'la'"),
    store: DataStore = Depends(get_data_store),
) -> AthleteSpotlightsResponse:
    """
    Return up to two Olympic and up to two Paralympic public-facing athlete
    spotlights for a hub, plus a short Gemini-generated brief (with
    deterministic local fallback).

    All spotlights include a public source URL. Where no reliable public
    Paralympic spotlight was available for a hub, that slot is intentionally
    left empty — no Paralympic data is invented. Olympic and Paralympic
    arrays are returned separately so the frontend can render them with
    equal prominence.

    Copy is intentionally conditional and avoids ranking, endorsement, or
    performance-guarantee language.
    """
    if not athlete_spotlights_service.hub_in_seed(hub_id):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Hub '{hub_id}' not found in athlete-spotlights seed. "
                f"Available hubs: {store.all_hub_ids()}"
            ),
        )

    name_map = _hub_name_map(store)
    hub_name_hint = name_map.get(hub_id, hub_id)
    response = athlete_spotlights_service.get_spotlights_response(
        hub_id=hub_id, hub_name_hint=hub_name_hint
    )
    if response is None:
        raise HTTPException(
            status_code=404,
            detail=f"Hub '{hub_id}' not found in athlete-spotlights seed.",
        )
    return response


# ---------------------------------------------------------------------------
# Live News Pulse (Gemini-grounded; never persisted)
# ---------------------------------------------------------------------------

@app.get(
    "/api/hometown/hubs/{hub_id}/news-pulse",
    response_model=NewsPulseResponse,
    tags=["hometown"],
    summary="Live Gemini-grounded news pulse for a hometown hub",
)
async def get_news_pulse(
    hub_id: str = Path(..., description="Hub identifier, e.g. 'sd', 'cos', 'la'"),
    store: DataStore = Depends(get_data_store),
) -> NewsPulseResponse:
    """
    Return 2-3 live, Gemini-grounded news cards for a hometown hub.

    Each request asks Gemini (Vertex AI grounding or google-generativeai with
    the google_search tool) to fetch fresh public news related to hometown
    Olympians/Paralympians, Team USA, the U.S. Olympic/Paralympic team, or
    LA28. Nothing is stored in a database or seed file.

    If Gemini / search is unavailable (no credentials, SDK missing, request
    failed, or the model produced unsafe / unparseable output), the response
    returns `generated_with_gemini: false`, `source: 'unavailable'`, an empty
    `cards` list, and a `brief` explaining live news could not be fetched.
    No invented or fallback news is ever returned.

    Every card carries a public http(s) `source_url`. Copy is conditional and
    descriptive — no rankings, endorsements, or performance predictions.
    Olympic and Paralympic stories are treated with equal prominence.
    """
    detail = store.get_detail(hub_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Hub '{hub_id}' not found. Available hubs: {store.all_hub_ids()}",
        )

    raw_row = store.get_raw_row(hub_id) or {}
    sports_raw = raw_row.get("sports", "").strip().strip('"')
    sports = [s.strip() for s in sports_raw.split(",") if s.strip()] if sports_raw else []

    return news_pulse_service.generate_news_pulse(detail=detail, sports=sports)


# ---------------------------------------------------------------------------
# LA28 Momentum endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/api/la28/momentum",
    response_model=List[LA28MomentumSummary],
    tags=["la28"],
    summary="LA28 momentum summaries for all hubs",
)
async def list_momentum(
    store: DataStore = Depends(get_data_store),
) -> List[LA28MomentumSummary]:
    """
    Return lightweight momentum summaries for every hub that has LA28 data.
    Includes top sport, top score, and Olympic/Paralympic sport counts.

    Scores are descriptive composite indicators — not performance predictions.
    """
    name_map = _hub_name_map(store)
    return momentum_service.get_momentum_summaries(name_map)


@app.get(
    "/api/la28/momentum/{hub_id}",
    response_model=LA28MomentumResponse,
    tags=["la28"],
    summary="Full LA28 momentum detail for a hub",
)
async def get_hub_momentum(
    hub_id: str = Path(..., description="Hub identifier, e.g. 'la', 'cos', 'atl'"),
    store: DataStore = Depends(get_data_store),
) -> LA28MomentumResponse:
    """
    Return full LA28 sport momentum data for a single hub, including:
    - All sports with signal breakdowns (hometown, world_champ, news, la28)
    - Composite momentum score per sport (descriptive, not predictive)
    - Conditional reason text for each sport entry
    - Full disclaimer

    Sorted by momentum score descending. Olympic and Paralympic sports
    are listed with equal analytical prominence.
    """
    if not store.hub_exists(hub_id) and hub_id not in momentum_service.get_available_hub_ids():
        raise HTTPException(
            status_code=404,
            detail=f"Hub '{hub_id}' not found.",
        )

    name_map = _hub_name_map(store)
    hub_name = name_map.get(hub_id, hub_id)

    result = momentum_service.get_momentum_for_hub(hub_id, hub_name)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No LA28 momentum data available for hub '{hub_id}'.",
        )
    return result


# ---------------------------------------------------------------------------
# Fallback alias routes (handle URL prefix variations from frontend)
# ---------------------------------------------------------------------------

@app.get("/hometown/hubs", include_in_schema=False)
async def list_hubs_alias(store: DataStore = Depends(get_data_store)):
    return await list_hubs(store)


@app.get("/hometown/hubs/{hub_id}", include_in_schema=False)
async def get_hub_alias(hub_id: str, store: DataStore = Depends(get_data_store)):
    return await get_hub(hub_id, store)


@app.post("/hometown/brief", include_in_schema=False)
async def generate_brief_alias(body: BriefRequest, store: DataStore = Depends(get_data_store)):
    return await generate_brief(body, store)


@app.get("/hometown/hubs/{hub_id}/gemini-brief", include_in_schema=False)
async def get_analyst_brief_alias(hub_id: str, store: DataStore = Depends(get_data_store)):
    return await get_analyst_brief(hub_id, store)


@app.post("/hometown/hubs/{hub_id}/gemini-brief", include_in_schema=False)
async def post_analyst_brief_alias(
    hub_id: str,
    body: Optional[AnalystBriefRequest] = None,
    store: DataStore = Depends(get_data_store),
):
    return await post_analyst_brief(hub_id, body, store)


@app.get("/hometown/hubs/{hub_id}/athlete-spotlights", include_in_schema=False)
async def get_athlete_spotlights_alias(
    hub_id: str, store: DataStore = Depends(get_data_store)
):
    return await get_athlete_spotlights(hub_id, store)


@app.get("/hometown/hubs/{hub_id}/news-pulse", include_in_schema=False)
async def get_news_pulse_alias(
    hub_id: str, store: DataStore = Depends(get_data_store)
):
    return await get_news_pulse(hub_id, store)


@app.get("/la28/momentum", include_in_schema=False)
async def list_momentum_alias(store: DataStore = Depends(get_data_store)):
    return await list_momentum(store)


@app.get("/la28/momentum/{hub_id}", include_in_schema=False)
async def get_hub_momentum_alias(hub_id: str, store: DataStore = Depends(get_data_store)):
    return await get_hub_momentum(hub_id, store)


# ---------------------------------------------------------------------------
# Root redirect to docs
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root():
    return JSONResponse(
        {
            "service": "Team USA Hometown Signals & LA28 Momentum API",
            "version": __version__,
            "docs": "/docs",
            "health": "/health",
            "endpoints": [
                "GET  /api/hometown/hubs",
                "GET  /api/hometown/hubs/{hub_id}",
                "POST /api/hometown/brief",
                "GET  /api/hometown/hubs/{hub_id}/gemini-brief",
                "POST /api/hometown/hubs/{hub_id}/gemini-brief",
                "GET  /api/hometown/hubs/{hub_id}/athlete-spotlights",
                "GET  /api/hometown/hubs/{hub_id}/news-pulse",
                "GET  /api/la28/momentum",
                "GET  /api/la28/momentum/{hub_id}",
            ],
        }
    )
