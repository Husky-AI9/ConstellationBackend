"""
Gemini / Vertex AI service for generating fan-facing brief copy.

Priority order:
  1. Vertex AI (google-cloud-aiplatform) — preferred for Cloud Run / GCP native.
     Uses Application Default Credentials (ADC) automatically in Cloud Run.
  2. google-generativeai SDK with GEMINI_API_KEY env var — fallback for local dev.
  3. Local template fallback — no external calls needed.

All prompts:
  - Use conditional / descriptive language ("could", "may", "might").
  - Treat Olympic and Paralympic signals with equal prominence (parity mandate).
  - Explicitly exclude private athlete information.
  - Include a disclaimer that counts are roster entries, not medal predictions.
"""

from __future__ import annotations

import logging
import os
from textwrap import dedent
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model constants
# ---------------------------------------------------------------------------

DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
_MAX_OUTPUT_TOKENS = 512
_TEMPERATURE = 0.4                      # Slightly creative but controlled


def _model_name() -> str:
    """Return the configured Gemini model, defaulting to Gemini 3 Flash Preview."""
    return os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(
    hub_id: str,
    city: str,
    state: str,
    region: str,
    narrative: str,
    sports: str,
    o_count: Optional[int],
    p_count: Optional[int],
    tags: str,
    climate: str,
    interests: Optional[List[str]],
) -> str:
    """
    Build a safe, parity-aware Gemini prompt for hub brief generation.
    Uses conditional language throughout; no private athlete info.
    """
    interests_clause = ""
    if interests:
        clean = [i.strip() for i in interests if i.strip()][:5]
        if clean:
            interests_clause = (
                f"\nFan interest tags to weave in if relevant: {', '.join(clean)}."
            )

    o_str = str(o_count) if o_count is not None else "unknown"
    p_str = str(p_count) if p_count is not None else "not represented in this seed"

    return dedent(f"""
        You are a fan-facing storytelling assistant for Team USA at the 2024 Paris Olympics
        and Paralympics. Your job is to write a concise, engaging brief (150-200 words) for
        a hometown hub card in a fan app.

        HUB DATA (aggregate public data only — no private athlete information):
        - City: {city}, {state}
        - Region: {region}
        - Olympic hometown roster entries (2024 USOPC official data): {o_str}
        - Paralympic hometown roster entries (2024 USOPC official data): {p_str}
        - Sports represented: {sports}
        - Climate / landscape context: {climate}
        - Descriptive tags: {tags}
        - Existing narrative context: {narrative}
        {interests_clause}

        WRITING RULES — follow all of these exactly:
        1. Use ONLY conditional / descriptive language. Words like "could", "may", "might",
           "suggests", "signals", "points toward". NEVER say an athlete "will" win or "is
           likely to" achieve a result.
        2. Give Olympic AND Paralympic stories EQUAL prominence. Do not treat Paralympic
           data as secondary or an afterthought. If both counts exist, mention both.
        3. Do NOT include any individual athlete names, personal details, or private data.
           Write only about the city, region, sport culture, and aggregate patterns.
        4. Keep language fan-friendly, inspiring, and brief (150-200 words).
        5. End with a one-sentence disclaimer that counts are aggregate roster hometown
           entries from official USOPC spreadsheets, not medal counts or predictions.
        6. Return ONLY the brief text — no headers, no markdown, no JSON.
    """).strip()


def _extract_themes(brief_text: str, sports: str) -> List[str]:
    """
    Derive a short list of themes from the brief text and sport list.
    This is a lightweight heuristic — no LLM call needed.
    """
    themes: List[str] = []
    lowered = brief_text.lower()
    sport_list = [s.strip() for s in sports.split(",") if s.strip()]

    # Sport-based themes
    themes.extend(sport_list[:3])

    # Keyword-based themes
    keyword_map = {
        "parity": ["paralympic", "adaptive", "wheelchair", "para "],
        "coastal": ["coastal", "ocean", "water", "sailing", "water polo"],
        "altitude": ["altitude", "high-altitude", "mountain", "elevation"],
        "urban": ["urban", "city", "metro", "community"],
        "la28": ["la28", "2028", "host city", "host region"],
        "track & field": ["track", "field", "sprint", "distance"],
    }
    for theme, keywords in keyword_map.items():
        if any(kw in lowered for kw in keywords) and theme not in themes:
            themes.append(theme)

    return themes[:6]


# ---------------------------------------------------------------------------
# Vertex AI path (preferred on Cloud Run)
# ---------------------------------------------------------------------------

def _try_vertex(
    prompt: str,
    project_id: Optional[str],
    location: str,
) -> Optional[str]:
    """Attempt generation via Vertex AI SDK. Returns text or None on failure."""
    if not project_id:
        logger.debug("VERTEX: GCP_PROJECT not set, skipping Vertex AI path.")
        return None
    try:
        import vertexai  # type: ignore
        from vertexai.generative_models import GenerativeModel, GenerationConfig  # type: ignore

        # Note: If 'global' still fails after this fix, change VERTEX_LOCATION 
        # in your .env file to 'us-central1'
        vertexai.init(project=project_id, location=location)
        
        # FIX: Vertex AI will crash if the model name starts with "models/".
        # We must strip it out if it was set in the environment variables.
        model_string = _model_name()
        if model_string.startswith("models/"):
            model_string = model_string[7:]
            
        model = GenerativeModel(model_string)
        
        config = GenerationConfig(
            temperature=_TEMPERATURE,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        )
        response = model.generate_content(prompt, generation_config=config)
        text = response.text.strip()
        logger.info("VERTEX: Brief generated successfully via Vertex AI.")
        return text
    except ImportError:
        logger.debug("VERTEX: vertexai package not installed.")
        return None
    except Exception as exc:
        logger.warning("VERTEX: Generation failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# google-generativeai SDK path (GEMINI_API_KEY fallback)
# ---------------------------------------------------------------------------

def _try_genai(prompt: str, api_key: str) -> Optional[str]:
    """Attempt generation via google-generativeai SDK. Returns text or None."""
    try:
        import google.generativeai as genai  # type: ignore

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(_model_name())
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": _TEMPERATURE,
                "max_output_tokens": _MAX_OUTPUT_TOKENS,
            },
        )
        text = response.text.strip()
        logger.info("GENAI: Brief generated successfully via google-generativeai.")
        return text
    except ImportError:
        logger.debug("GENAI: google-generativeai package not installed.")
        return None
    except Exception as exc:
        logger.warning("GENAI: Generation failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Local fallback template
# ---------------------------------------------------------------------------

def _local_fallback(
    city: str,
    region: str,
    narrative: str,
    o_count: Optional[int],
    p_count: Optional[int],
    sports: str,
) -> str:
    """
    Return a pre-built template brief when no AI service is available.
    Uses conditional language and parity-aware phrasing.
    """
    o_str = f"{o_count} Olympic" if o_count is not None else "Olympic"
    p_str = f"{p_count} Paralympic" if p_count is not None else "Paralympic"
    sport_sample = ", ".join(s.strip() for s in sports.split(",") if s.strip())[:3] or "various sports"

    return (
        f"{city} could offer fans a compelling window into Team USA's {region} story. "
        f"With {o_str} and {p_str} hometown roster entries recorded in the official 2024 "
        f"USOPC dataset, this hub may suggest rich storytelling across {sport_sample} and more. "
        f"{narrative} "
        f"Olympic and Paralympic pathways from {city} deserve equal fan attention — "
        f"both sets of athletes reflect the full breadth of Team USA representation. "
        f"Disclaimer: all counts are aggregate hometown roster entries from official USOPC "
        f"spreadsheets; they are not medal counts or performance predictions."
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_brief(
    hub_id: str,
    city: str,
    state: str,
    region: str,
    narrative: str,
    sports: str,
    o_count: Optional[int],
    p_count: Optional[int],
    tags: str,
    climate: str,
    interests: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """
    Generate a fan-facing brief for a hub.

    Returns:
        (brief_text, source_label)
        where source_label is one of: "vertex", "gemini", "local-fallback"
    """
    prompt = _build_prompt(
        hub_id=hub_id,
        city=city,
        state=state,
        region=region,
        narrative=narrative,
        sports=sports,
        o_count=o_count,
        p_count=p_count,
        tags=tags,
        climate=climate,
        interests=interests,
    )

    # 1. Try Vertex AI (Cloud Run / GCP native)
    project_id = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    # Gemini 3 Flash Preview currently uses Vertex AI's global endpoint.
    location = os.getenv("VERTEX_LOCATION", "global")
    text = _try_vertex(prompt, project_id, location)
    if text:
        return text, "vertex"

    # 2. Try google-generativeai SDK (GEMINI_API_KEY)
    api_key = os.getenv("GEMINI_API_KEY", "")
    if api_key:
        text = _try_genai(prompt, api_key)
        if text:
            return text, "gemini"

    # 3. Local fallback (no external calls)
    logger.info("Using local-fallback brief for hub '%s'.", hub_id)
    text = _local_fallback(city, region, narrative, o_count, p_count, sports)
    return text, "local-fallback"


def gemini_is_configured() -> bool:
    """Return True if any Gemini path is likely to work."""
    has_project = bool(os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT"))
    has_key = bool(os.getenv("GEMINI_API_KEY"))
    return has_project or has_key


def get_themes_for_brief(brief_text: str, sports: str) -> List[str]:
    """Expose the theme extractor for use in routes."""
    return _extract_themes(brief_text, sports)
