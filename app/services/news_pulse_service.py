"""
News Pulse service — Internal Knowledge generation for a hometown hub.

Used by GET /api/hometown/hubs/{hub_id}/news-pulse.

This service does NOT use live web search. Each request asks Gemini 
(using its internal training data via Vertex AI or the google-genai SDK) 
to recall and synthesize 2-3 recent or relevant public news narratives 
related to:

  - Hometown Olympians and Paralympians from the hub
  - Team USA / Olympic / Paralympic team headlines that touch the hub
  - LA28-related news with a connection to the hub or its sports
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from textwrap import dedent
from typing import Any, Dict, List, Optional, Tuple

from app.models import (
    ApiHubDetail,
    NewsCard,
    NewsPulseResponse,
    NewsPulseSource,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants & Disclaimer
# ---------------------------------------------------------------------------

# We force the most up-to-date Pro model to ensure the deepest internal knowledge base is used
KNOWLEDGE_MODEL = "gemini-3.1-pro"

DISCLAIMER = (
    "News cards are synthesized via Gemini from its internal knowledge base "
    "and historical data. Olympic and Paralympic stories are treated with "
    "equal prominence. Because these are generated from training data rather "
    "than live web scraping, URLs may point to general publisher homepages "
    "rather than specific articles."
)


# ---------------------------------------------------------------------------
# Query + prompt builders
# ---------------------------------------------------------------------------

def _build_query(detail: ApiHubDetail) -> str:
    sport_hint = ", ".join(detail.tags[:3]) if detail.tags else ""
    sport_str = f" ({sport_hint})" if sport_hint else ""
    return (
        f"Team USA, Olympic, Paralympic, and LA28 narratives connected to "
        f"{detail.name}, {detail.region}{sport_str}"
    )


def _retry_queries(detail: ApiHubDetail) -> List[Tuple[str, str]]:
    name = detail.name
    return [
        ("general", f"{name} Team USA Olympic Paralympic LA28 news"),
        ("olympian", f"{name} Team USA Olympian news"),
        ("paralympic", f"{name} Paralympic athlete Team USA news"),
        ("la28", f"{name} LA28 Olympic news"),
    ]


def _build_prompt(detail: ApiHubDetail, sports: List[str], focus: str) -> str:
    o_est = getattr(detail.parity_snapshot, "olympic_story_estimate", "")
    p_est = getattr(detail.parity_snapshot, "paralympic_story_estimate", "")
    parity = getattr(detail.parity_snapshot, "parity_note", "")
    tags = ", ".join(detail.tags) if detail.tags else "(none)"
    sports_str = ", ".join(sports[:6]) if sports else "(none recorded)"

    return dedent(
        f"""
        You are a fan-facing news-pulse assistant for the Team USA Constellation
        Hub. Use your extensive internal knowledge base to recall and synthesize 
        2-3 recent, historical, or highly relevant news narratives connected to the
        hometown hub below.

        FOCUS FOR THIS QUERY: {focus}

        HUB CONTEXT:
        - City: {detail.name}
        - Region: {detail.region}
        - Tags: {tags}
        - Sports represented: {sports_str}
        - Olympic story estimate: {o_est}
        - Paralympic story estimate: {p_est}
        - Parity note: {parity}

        TOPIC SCOPE — pull narratives connected to the hub from any of these:
          1. Hometown Olympians or Paralympians from {detail.name} or its region.
          2. Team USA / U.S. Olympic / U.S. Paralympic team news that ties to
             the hub (federation training site, athlete pipeline, host events).
          3. LA28 (2028 Los Angeles Olympic / Paralympic Games) news connected
             to the hub or its sports.

        WRITING RULES — follow ALL of these exactly:
        1. Treat Olympic and Paralympic stories with EQUAL prominence.
        2. Stick to reported facts from your training data. Do NOT include private athlete info.
        3. Provide a realistic `source_url` (e.g., the homepage of a relevant local newspaper, 
           or https://www.teamusa.org).
        4. Output JSON in this exact shape (no markdown fences):
             {{
               "brief": "1-2 sentence overview of the storylines for this hub.",
               "cards": [
                 {{
                   "title": "short headline (<= 110 chars)",
                   "summary": "2-3 sentence summary of the storyline or event",
                   "category": "olympic | paralympic | team-usa | la28",
                   "source_label": "publication or organisation name",
                   "source_url": "https://...",
                   "published_date": "YYYY-MM-DD or null"
                 }}
               ]
             }}
            Return between 2 and 3 cards.
        5. Return ONLY valid JSON — no markdown fences, no commentary.
        """
    ).strip()


# ---------------------------------------------------------------------------
# Vertex AI (Pure Generation - No Tools)
# ---------------------------------------------------------------------------

def _try_vertex_generation(prompt: str) -> Tuple[Optional[str], Optional[str]]:
    project_id = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        return None, None
    # Adjust location to 'us-central1' if 'global' does not support gemini-3.1-pro yet
    location = os.getenv("VERTEX_LOCATION", "us-central1") 
    try:
        import vertexai  # type: ignore
        from vertexai.generative_models import GenerativeModel, GenerationConfig  # type: ignore

        vertexai.init(project=project_id, location=location)
        model = GenerativeModel(KNOWLEDGE_MODEL)
        config = GenerationConfig(
            temperature=0.4, 
            max_output_tokens=1024,
        )

        response = model.generate_content(prompt, generation_config=config)
        text = (response.text or "").strip()
        return text or None, "vertex-generation"
    except ImportError:
        logger.debug("NEWS_PULSE: vertexai not installed.")
        return None, None
    except Exception as exc:
        logger.warning("NEWS_PULSE: Vertex generation failed: %s", exc)
        return None, None


# ---------------------------------------------------------------------------
# LATEST SDK: google-genai (Pure Generation - No Tools)
# ---------------------------------------------------------------------------

def _try_genai_generation(prompt: str) -> Tuple[Optional[str], Optional[str]]:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None, None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        
        config = types.GenerateContentConfig(
            temperature=0.4,
            max_output_tokens=1024,
        )

        response = client.models.generate_content(
            model=KNOWLEDGE_MODEL,
            contents=prompt,
            config=config,
        )
        
        text = (response.text or "").strip()
        return text or None, "gemini-generation"
        
    except ImportError:
        pass 
    except Exception as exc:
        logger.warning("NEWS_PULSE: GenAI generation failed: %s", exc)
        return None, None

    # Fallback to legacy SDK
    try:
        import google.generativeai as legacy_genai
        legacy_genai.configure(api_key=api_key)
        
        model = legacy_genai.GenerativeModel(KNOWLEDGE_MODEL)
        
        response = model.generate_content(
            prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": 1024}
        )
        
        text = (response.text or "").strip()
        return text or None, "gemini-generation"
        
    except ImportError:
        logger.debug("NEWS_PULSE: google-generativeai not installed.")
        return None, None
    except Exception as exc:
        logger.warning("NEWS_PULSE: Legacy GenAI generation failed: %s", exc)
        return None, None


# ---------------------------------------------------------------------------
# JSON parsing + card validation
# ---------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _parse_payload(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    candidate = _strip_code_fences(raw)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


_ALLOWED_CATEGORIES = {"olympic", "paralympic", "team-usa", "la28"}

def _normalise_category(raw: str) -> str:
    if not isinstance(raw, str):
        return "team-usa"
    c = raw.strip().lower().replace("_", "-").replace(" ", "-")
    if c in _ALLOWED_CATEGORIES:
        return c
    if "paralympic" in c:
        return "paralympic"
    if "olympic" in c:
        return "olympic"
    if "la28" in c or "2028" in c:
        return "la28"
    return "team-usa"


def _validate_cards(payload: Dict[str, Any]) -> Optional[Tuple[List[NewsCard], str]]:
    cards_raw = payload.get("cards")
    brief = str(payload.get("brief", "")).strip()
    
    if not isinstance(cards_raw, list) or not cards_raw:
        return None
    if len(brief) < 10:
        return None

    cards: List[NewsCard] = []
    for item in cards_raw:
        if not isinstance(item, dict):
            continue
            
        title = str(item.get("title", "")).strip()
        summary = str(item.get("summary", "")).strip()
        
        # Soft fallback for URLs since the AI is generating from memory
        source_url = str(item.get("source_url", "[https://www.teamusa.org](https://www.teamusa.org)")).strip()
        source_label = str(item.get("source_label", "Team USA Updates")).strip()
        
        if not title or len(summary) < 20:
            continue
            
        category = _normalise_category(item.get("category", "team-usa"))
        published_raw = item.get("published_date")
        published_date: Optional[str] = None
        if isinstance(published_raw, str) and published_raw.strip():
            published_date = published_raw.strip()
            
        cards.append(
            NewsCard(
                title=title,
                summary=summary,
                category=category,
                source_label=source_label,
                source_url=source_url,
                published_date=published_date,
            )
        )

    if not cards:
        return None
    return cards[:3], brief


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def gemini_is_configured() -> bool:
    has_project = bool(os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT"))
    has_key = bool(os.getenv("GEMINI_API_KEY"))
    return has_project or has_key


def _unavailable_brief(hub_name: str) -> str:
    return (
        f"Knowledge synthesis for {hub_name} is currently unavailable. "
        f"Please retry shortly."
    )


def _label_to_source(label: str) -> NewsPulseSource:
    if label == "vertex-generation":
        return NewsPulseSource.vertex_grounded 
    if label == "gemini-generation":
        return NewsPulseSource.gemini_search
    return NewsPulseSource.unavailable


def _attempt(
    fn,
    prompt: str,
) -> Optional[Tuple[List[NewsCard], str, str]]:
    raw, label = fn(prompt)
    if not label or not raw:
        return None

    payload = _parse_payload(raw)
    if payload is not None:
        validated = _validate_cards(payload)
        if validated is not None:
            cards, brief = validated
            return cards, brief, label

    return None


def generate_news_pulse(
    detail: ApiHubDetail,
    sports: Optional[List[str]] = None,
) -> NewsPulseResponse:
    sports = sports or []
    query = _build_query(detail)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cards: List[NewsCard] = []
    brief: Optional[str] = None
    source: NewsPulseSource = NewsPulseSource.unavailable
    model_name: Optional[str] = None
    generated_with_gemini = False

    if gemini_is_configured():
        for angle_label, angle_query in _retry_queries(detail):
            prompt = _build_prompt(detail, sports, focus=angle_query)
            
            for fn in (_try_vertex_generation, _try_genai_generation):
                result = _attempt(fn, prompt)
                if result is None:
                    continue
                    
                cards, brief, label = result
                source = _label_to_source(label)
                model_name = KNOWLEDGE_MODEL
                generated_with_gemini = True
                logger.info(
                    "NEWS_PULSE: %d synthesized cards via %s on angle '%s'.",
                    len(cards),
                    label,
                    angle_label,
                )
                break
                
            if generated_with_gemini:
                break

    if not generated_with_gemini:
        cards = []
        brief = _unavailable_brief(detail.name)
        source = NewsPulseSource.unavailable
        model_name = None

    return NewsPulseResponse(
        hub_id=detail.id,
        hub_name=detail.name,
        generated_with_gemini=generated_with_gemini,
        source=source,
        model=model_name,
        query=query,
        cards=cards,
        brief=brief or _unavailable_brief(detail.name),
        disclaimer=DISCLAIMER,
        generated_at=generated_at,
    )
