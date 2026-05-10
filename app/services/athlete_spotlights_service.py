"""
Athlete Spotlights service.

Loads the public, source-linked seed dataset from
`app/data/athlete_spotlights.json` and serves it via
`GET /api/hometown/hubs/{hub_id}/athlete-spotlights`.

The seed file groups up to 2 Olympic and up to 2 Paralympic spotlights per
hub, with full source URLs. Where no reliable Paralympic spotlight was found
the slot is left blank in the seed and is filtered out here so we never
invent missing Paralympic data.

A short companion brief (`gemini_brief`) is built either with Gemini/Vertex
(when configured) or via a deterministic local-fallback template that
mirrors the analyst-brief safety rules:

  - Conditional / descriptive language only.
  - No medal predictions, performance guarantees, or ranking language
    (no "best", "top", "major", etc.).
  - Olympic and Paralympic stories at equal prominence.
  - When public Paralympic spotlights are not loaded for a hub, the
    fallback brief explicitly acknowledges that.

The seed-file `hub_id`s use long-form snake-case (`san_diego`, `colorado_springs`),
while the rest of the API uses short codes (`sd`, `cos`). A bidirectional
mapping is built once at import time so callers may use either form.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional, Tuple

from app.models import (
    AthleteSpotlight,
    AthleteSpotlightBrief,
    AthleteSpotlightsResponse,
    BriefSource,
    SportKind,
)
from app.services import gemini_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent.parent  # → app/
_SEED_PATH = _HERE / "data" / "athlete_spotlights.json"


# Map between the long-form seed hub_ids and the short CSV hub_ids used
# elsewhere in the API. Either form can be used in URLs.
_SEED_TO_SHORT: Dict[str, str] = {
    "san_diego": "sd",
    "houston": "hou",
    "los_angeles": "la",
    "colorado_springs": "cos",
    "chicago": "chi",
    "atlanta": "atl",
    "charlotte": "cha",
    "long_beach": "lb",
    "miami": "mia",
    "portland": "por",
    "tucson": "tuc",
    "birmingham": "bir",
}
_SHORT_TO_SEED: Dict[str, str] = {v: k for k, v in _SEED_TO_SHORT.items()}


def _load_seed() -> Dict[str, Dict[str, Any]]:
    """Load and index the seed JSON by both seed and short hub_id."""
    raw = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    indexed: Dict[str, Dict[str, Any]] = {}
    for hub in raw.get("hubs", []):
        seed_id = hub.get("hub_id", "").strip()
        if not seed_id:
            continue
        indexed[seed_id] = hub
        short_id = _SEED_TO_SHORT.get(seed_id)
        if short_id:
            indexed[short_id] = hub
    return indexed


_INDEXED_HUBS: Dict[str, Dict[str, Any]] = _load_seed()


# ---------------------------------------------------------------------------
# Safety / wording enforcement (mirrors analyst_brief_service)
# ---------------------------------------------------------------------------

_FORBIDDEN_PATTERNS = [
    # Outcome guarantees
    r"\bwill\s+(?:win|medal|finish|place|take|secure|earn|capture|defeat|beat)\b",
    r"\bguarantee[ds]?\b",
    r"\bcertain\s+to\b",
    r"\bdefinitely\s+(?:win|medal|finish)\b",
    r"\bpredict(?:s|ed|ion|ions)?\b\s+(?:gold|silver|bronze|medal)",
    r"\bgold\s+medal\s+(?:lock|favorite)\b",
    r"\b(?:lock|shoo-?in)\s+for\s+(?:gold|silver|bronze|medal)\b",
    r"\bsure\s+thing\b",
    # Ranking / endorsement language
    r"\bbest\b",
    r"\btop\b",
    r"\bmajor\b",
    r"\b(?:greatest|elite|premier|leading)\b",
    r"\bendorse[ds]?\b",
    r"\brecommend(?:s|ed|ation)?\b",
]
_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in _FORBIDDEN_PATTERNS]


def _is_safe(text: str) -> bool:
    """Return True if text avoids forbidden ranking / outcome / endorsement language."""
    for rx in _FORBIDDEN_RE:
        if rx.search(text):
            return False
    return True


# ---------------------------------------------------------------------------
# Disclaimer (re-used for live and fallback responses)
# ---------------------------------------------------------------------------

DISCLAIMER = (
    "Athlete spotlights are drawn from public sources (official Team USA / "
    "federation pages, Olympics.com, university athletics, and reputable "
    "outlet reporting) and include source URLs for every entry. They are "
    "factual public-record summaries — not endorsements, performance "
    "predictions, or guarantees. Where no reliable public Paralympic "
    "spotlight was available for a hub, that slot is intentionally left "
    "empty rather than invented. Olympic and Paralympic stories are treated "
    "with equal prominence."
)


# ---------------------------------------------------------------------------
# Spotlight extraction
# ---------------------------------------------------------------------------

def _is_filled(slot: Dict[str, Any]) -> bool:
    """Return True if the seed slot has at least a name and a source URL."""
    return bool(str(slot.get("name", "")).strip()) and bool(
        str(slot.get("source_url", "")).strip()
    )


def _to_spotlight(slot: Dict[str, Any]) -> AthleteSpotlight:
    return AthleteSpotlight(
        category=SportKind(slot["category"]),
        name=str(slot.get("name", "")).strip(),
        sport=str(slot.get("sport", "")).strip(),
        hometown_or_region=str(slot.get("hometown_or_region", "")).strip(),
        recent_achievement=str(slot.get("recent_achievement", "")).strip(),
        source_url=str(slot.get("source_url", "")).strip(),
        source_label=str(slot.get("source_label", "")).strip(),
    )


def _split_spotlights(
    hub: Dict[str, Any],
) -> Tuple[List[AthleteSpotlight], List[AthleteSpotlight]]:
    olympic: List[AthleteSpotlight] = []
    paralympic: List[AthleteSpotlight] = []
    for slot in hub.get("spotlights", []):
        if not _is_filled(slot):
            continue
        category = str(slot.get("category", "")).strip().lower()
        if category == "olympic":
            olympic.append(_to_spotlight(slot))
        elif category == "paralympic":
            paralympic.append(_to_spotlight(slot))
    return olympic, paralympic


# ---------------------------------------------------------------------------
# Brief prompt + parsing for Gemini / Vertex
# ---------------------------------------------------------------------------

def _athletes_block(spotlights: List[AthleteSpotlight]) -> str:
    if not spotlights:
        return "  (none loaded for this hub)"
    return "\n".join(
        f"  - {s.name} — {s.sport} ({s.hometown_or_region}). "
        f"{s.recent_achievement} Source: {s.source_label}."
        for s in spotlights
    )


def _build_prompt(
    hub_name: str,
    regional_summary: str,
    olympic: List[AthleteSpotlight],
    paralympic: List[AthleteSpotlight],
) -> str:
    para_note = (
        ""
        if paralympic
        else (
            "\nNote: no public Paralympic athlete spotlights are loaded for "
            "this hub in the current seed. Acknowledge this directly in the "
            "brief — do not invent any Paralympic athletes or achievements."
        )
    )
    return dedent(
        f"""
        You are a fan-facing assistant writing a SHORT public spotlight brief
        for the {hub_name} hometown hub. Produce a JSON document only — no
        prose outside JSON, no markdown fences.

        REGIONAL CONTEXT:
        {regional_summary}

        OLYMPIC SPOTLIGHTS (public sources, factual):
        {_athletes_block(olympic)}

        PARALYMPIC SPOTLIGHTS (public sources, factual):
        {_athletes_block(paralympic)}
        {para_note}

        WRITING RULES — follow ALL of these exactly:
        1. Use ONLY conditional / descriptive language: "could", "may",
           "might", "appears", "suggests", "signals". Never claim an
           athlete "will" win, medal, or guarantee any outcome.
        2. Do NOT use ranking or endorsement language. Avoid the words
           "best", "top", "major", "greatest", "elite", "premier",
           "leading", "endorse", "recommend".
        3. Treat Olympic and Paralympic stories with EQUAL prominence. Do
           not present Paralympic data as secondary or an afterthought.
           If no Paralympic spotlights are loaded for this hub, state that
           directly — do not invent any.
        4. Do NOT add private athlete details (contact info, addresses,
           dates of birth, etc.). Stick to the public-record fields above.
        5. Output JSON in this exact shape:
             {{
               "title": "short title (<= 70 chars)",
               "summary": "2-3 sentence paragraph (<= 90 words)",
               "bullets": [
                 "one descriptive bullet per spotlight, source-faithful",
                 "..."
               ]
             }}
           Bullets should describe the loaded spotlights in conditional,
           non-ranking language, and should reference the same factual
           achievements provided above.
        6. Return ONLY valid JSON — no markdown fences, no commentary.
        """
    ).strip()


def _try_vertex_json(prompt: str) -> Optional[str]:
    project_id = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        return None
    location = os.getenv("VERTEX_LOCATION", "global")
    try:
        import vertexai  # type: ignore
        from vertexai.generative_models import (  # type: ignore
            GenerationConfig,
            GenerativeModel,
        )

        vertexai.init(project=project_id, location=location)
        model = GenerativeModel(gemini_service._model_name())
        config = GenerationConfig(
            temperature=0.3,
            max_output_tokens=768,
            response_mime_type="application/json",
        )
        response = model.generate_content(prompt, generation_config=config)
        return response.text.strip()
    except ImportError:
        logger.debug("SPOTLIGHTS: vertexai not installed.")
        return None
    except Exception as exc:
        logger.warning("SPOTLIGHTS: Vertex generation failed: %s", exc)
        return None


def _try_genai_json(prompt: str) -> Optional[str]:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        import google.generativeai as genai  # type: ignore

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(gemini_service._model_name())
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.3,
                "max_output_tokens": 768,
                "response_mime_type": "application/json",
            },
        )
        return response.text.strip()
    except ImportError:
        logger.debug("SPOTLIGHTS: google-generativeai not installed.")
        return None
    except Exception as exc:
        logger.warning("SPOTLIGHTS: GenAI generation failed: %s", exc)
        return None


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


def _parse_brief_json(raw: str) -> Optional[Dict[str, Any]]:
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


def _validate_brief(payload: Dict[str, Any]) -> Optional[AthleteSpotlightBrief]:
    title = payload.get("title")
    summary = payload.get("summary")
    bullets = payload.get("bullets")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(summary, str) or len(summary.strip()) < 20:
        return None
    if not isinstance(bullets, list) or not bullets:
        return None
    clean_bullets: List[str] = []
    for b in bullets:
        if not isinstance(b, str) or not b.strip():
            return None
        clean_bullets.append(b.strip())

    full = " ".join([title, summary, *clean_bullets])
    if not _is_safe(full):
        logger.warning("SPOTLIGHTS: model brief failed safety scan; falling back.")
        return None
    return AthleteSpotlightBrief(
        title=title.strip(), summary=summary.strip(), bullets=clean_bullets
    )


# ---------------------------------------------------------------------------
# Deterministic fallback brief
# ---------------------------------------------------------------------------

def _fallback_bullet(s: AthleteSpotlight) -> str:
    """Build a single descriptive, non-ranking bullet from a spotlight."""
    kind = "Olympic" if s.category == SportKind.olympic else "Paralympic"
    return (
        f"{s.name} ({s.sport}, {s.hometown_or_region}) — public-record "
        f"{kind} spotlight: {s.recent_achievement} (source: {s.source_label})"
    )


def _fallback_brief(
    hub_name: str,
    olympic: List[AthleteSpotlight],
    paralympic: List[AthleteSpotlight],
) -> AthleteSpotlightBrief:
    title = f"Public athlete spotlights from the {hub_name} hub"

    if olympic and paralympic:
        summary = (
            f"This snapshot draws on public sources to highlight Team USA "
            f"connections from {hub_name}. Olympic and Paralympic stories "
            f"appear with equal weight, and every entry could help fans "
            f"explore a publicly documented pathway rather than any "
            f"performance prediction."
        )
    elif olympic and not paralympic:
        summary = (
            f"This snapshot highlights publicly documented Olympic spotlights "
            f"with {hub_name} ties. Public Paralympic spotlights are not "
            f"loaded for this hub in the current seed; that gap reflects "
            f"data availability and could be filled as additional public "
            f"profiles are confirmed."
        )
    elif paralympic and not olympic:
        summary = (
            f"This snapshot highlights publicly documented Paralympic "
            f"spotlights with {hub_name} ties. Public Olympic spotlights are "
            f"not loaded for this hub in the current seed; that gap reflects "
            f"data availability rather than absence of athletes."
        )
    else:
        summary = (
            f"No public athlete spotlights are loaded for {hub_name} in the "
            f"current seed. Both Olympic and Paralympic slots could be filled "
            f"as additional source-linked public profiles are confirmed."
        )

    bullets: List[str] = []
    for s in olympic:
        bullets.append(_fallback_bullet(s))
    for s in paralympic:
        bullets.append(_fallback_bullet(s))
    if not paralympic:
        bullets.append(
            f"Public Paralympic spotlights are not loaded for {hub_name} in "
            f"this seed — that gap is acknowledged here rather than filled "
            f"with invented profiles."
        )
    if not olympic:
        bullets.append(
            f"Public Olympic spotlights are not loaded for {hub_name} in "
            f"this seed — that gap is acknowledged here rather than filled "
            f"with invented profiles."
        )

    return AthleteSpotlightBrief(title=title, summary=summary, bullets=bullets)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def hub_in_seed(hub_id: str) -> bool:
    """Return True if the seed dataset has an entry for the given hub id."""
    return hub_id in _INDEXED_HUBS


def get_spotlights_response(hub_id: str, hub_name_hint: str) -> Optional[
    AthleteSpotlightsResponse
]:
    """
    Build the public spotlights response for a hub. Returns None if the hub
    is not in the seed dataset.

    `hub_name_hint` is used only when the seed entry is missing a hub_name
    (it is taken from the canonical CSV-backed DataStore).
    """
    hub = _INDEXED_HUBS.get(hub_id)
    if hub is None:
        return None

    hub_name = (
        str(hub.get("hub_name", "")).strip() or hub_name_hint or hub_id
    )
    regional_summary = str(hub.get("regional_summary", "")).strip()
    olympic, paralympic = _split_spotlights(hub)

    # Always use the short-form hub_id for the canonical response so it
    # matches the rest of the API's identifiers.
    seed_id = hub.get("hub_id", "").strip()
    canonical_id = _SEED_TO_SHORT.get(seed_id, seed_id)

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    brief: Optional[AthleteSpotlightBrief] = None
    source: BriefSource = BriefSource.local_fallback
    model_name: Optional[str] = None
    generated_with_gemini = False

    if gemini_service.gemini_is_configured():
        prompt = _build_prompt(hub_name, regional_summary, olympic, paralympic)
        for label, fn in (("vertex", _try_vertex_json), ("gemini", _try_genai_json)):
            raw = fn(prompt)
            if not raw:
                continue
            payload = _parse_brief_json(raw)
            if not payload:
                logger.warning("SPOTLIGHTS: failed to parse %s JSON output.", label)
                continue
            validated = _validate_brief(payload)
            if validated is None:
                continue
            brief = validated
            source = BriefSource.vertex if label == "vertex" else BriefSource.gemini
            model_name = gemini_service._model_name()
            generated_with_gemini = True
            logger.info("SPOTLIGHTS: brief generated via %s.", label)
            break

    if brief is None:
        brief = _fallback_brief(hub_name, olympic, paralympic)
        source = BriefSource.local_fallback
        model_name = None
        generated_with_gemini = False

    return AthleteSpotlightsResponse(
        hub_id=canonical_id,
        hub_name=hub_name,
        generated_with_gemini=generated_with_gemini,
        source=source,
        model=model_name,
        olympic_spotlights=olympic,
        paralympic_spotlights=paralympic,
        gemini_brief=brief,
        disclaimer=DISCLAIMER,
        generated_at=generated_at,
    )
