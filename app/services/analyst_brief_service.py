"""
Analyst Brief service — generates a structured, sectioned brief for a hometown hub.

Used by GET/POST /api/hometown/hubs/{hub_id}/gemini-brief.

The output is shaped for the frontend Analyst Brief pane:

    {
      "hub_id": ...,
      "hub_name": ...,
      "generated_with_gemini": bool,
      "model": "...",
      "source": "vertex" | "gemini" | "local-fallback",
      "sections": [{"title": ..., "body": ...}, ...],
      "key_takeaway": "...",
      "disclaimer": "...",
      "generated_at": "ISO-8601 UTC"
    }

Priority order for live generation mirrors gemini_service:
  1. Vertex AI (preferred on Cloud Run, ADC).
  2. google-generativeai SDK (GEMINI_API_KEY).
  3. Deterministic local fallback (no external calls, never claims Gemini).

ALL responses (live or fallback) enforce safe wording:
  - Conditional verbs only ("could", "may", "might", "suggests", "signals").
  - No medal predictions or individual performance guarantees.
  - Olympic and Paralympic signals at equal prominence.
  - No private athlete information.

If Gemini is configured but its output fails the safety scan, we fall back to
the deterministic template and set generated_with_gemini=False so the frontend
never claims an unsafe response was Gemini-generated.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from textwrap import dedent
from typing import Any, Dict, List, Optional, Tuple

from app.models import (
    AnalystBriefResponse,
    AnalystBriefSection,
    ApiHubDetail,
    BriefSource,
    LA28MomentumResponse,
)
from app.services import gemini_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Safety / wording enforcement
# ---------------------------------------------------------------------------

# Phrases that promise outcomes — disqualify a model response.
_FORBIDDEN_PATTERNS = [
    r"\bwill\s+(?:win|medal|finish|place|take|secure|earn|capture|defeat|beat)\b",
    r"\bguarantee[ds]?\b",
    r"\bcertain\s+to\b",
    r"\bdefinitely\s+(?:win|medal|finish)\b",
    r"\bpredict(?:s|ed|ion|ions)?\b\s+(?:gold|silver|bronze|medal)",
    r"\bgold\s+medal\s+(?:lock|favorite)\b",
    r"\b(?:lock|shoo-?in)\s+for\s+(?:gold|silver|bronze|medal)\b",
    r"\bsure\s+thing\b",
]

_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in _FORBIDDEN_PATTERNS]

# Conditional / descriptive markers we expect to see in safe copy.
_CONDITIONAL_MARKERS = [
    "could", "may", "might", "suggests", "signals", "points toward",
    "appears", "potential", "potentially", "possible", "possibly",
]


def _is_safe(text: str) -> bool:
    """Return True if the text avoids forbidden outcome-language."""
    for rx in _FORBIDDEN_RE:
        if rx.search(text):
            return False
    return True


# ---------------------------------------------------------------------------
# Disclaimer text (re-used for both live and fallback responses)
# ---------------------------------------------------------------------------

DISCLAIMER = (
    "All counts are aggregate hometown roster entries from the official 2024 "
    "USOPC Olympic and Paralympic spreadsheets — they are not medal counts, "
    "performance predictions, or individual athlete data. Language is "
    "intentionally conditional and descriptive. Olympic and Paralympic signals "
    "are treated with equal prominence."
)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SECTION_TITLES = [
    "Hometown Snapshot",
    "Olympic Signal",
    "Paralympic Signal",
    "LA28 Momentum",
    "What Fans Could Watch For",
]


def _build_prompt(
    detail: ApiHubDetail,
    momentum: Optional[LA28MomentumResponse],
    interests: Optional[List[str]],
) -> str:
    o_est = detail.parity_snapshot.olympic_story_estimate
    p_est = detail.parity_snapshot.paralympic_story_estimate
    parity = detail.parity_snapshot.parity_note
    tags = ", ".join(detail.tags) if detail.tags else "(none)"

    pin_lines: List[str] = []
    for pin in detail.map_pins[:6]:
        pin_lines.append(f"  - [{pin.color}] {pin.label}: {pin.description}")
    pins_block = "\n".join(pin_lines) if pin_lines else "  (no map pins)"

    momentum_block = "(no LA28 momentum data available for this hub)"
    if momentum is not None and momentum.sports:
        rows: List[str] = []
        for sport in momentum.sports[:6]:
            rows.append(
                f"  - {sport.name} ({sport.kind.value}, score {sport.momentum_score:.1f}): {sport.reason}"
            )
        momentum_block = "\n".join(rows)

    interests_clause = ""
    if interests:
        clean = [i.strip() for i in interests if i.strip()][:5]
        if clean:
            interests_clause = (
                f"\nFan interest tags to weave in if relevant: {', '.join(clean)}."
            )

    sections_list = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(_SECTION_TITLES))

    return dedent(f"""
        You are a fan-facing analyst writing a structured hometown brief for the
        Team USA Constellation Hub. Produce a JSON document only — no prose
        outside JSON, no markdown fences.

        HUB CONTEXT (aggregate public data — no private athlete information):
        - Hub id: {detail.id}
        - City: {detail.name}
        - Region: {detail.region}
        - Tags: {tags}
        - Coordinates: {detail.coordinates.lat}, {detail.coordinates.lng}
        - Existing narrative: {detail.narrative}
        - Olympic story estimate: {o_est}
        - Paralympic story estimate: {p_est}
        - Parity note: {parity}
        - Map pins:
        {pins_block}
        - LA28 momentum (top sports, descriptive scores only):
        {momentum_block}
        {interests_clause}

        WRITING RULES — follow ALL of these exactly:
        1. Use ONLY conditional / descriptive language: "could", "may", "might",
           "suggests", "signals", "points toward", "appears". NEVER say an athlete
           or city "will" win, medal, or guarantee a result.
        2. Do NOT predict medals, finishes, rankings, or individual performance.
           Do NOT write "lock for gold", "sure thing", "guaranteed", etc.
        3. Treat Olympic and Paralympic stories with EQUAL prominence. Do not
           treat Paralympic data as secondary or an afterthought.
        4. Do NOT include any individual athlete names, personal details, or
           private data. Only aggregate city/region/sport patterns.
        5. Each section body should be 2-4 sentences (~50-90 words).
        6. Output JSON with this exact shape and section titles, in this order:
           {{
             "sections": [
        {sections_list}
             ],
             "key_takeaway": "single sentence (<= 30 words), conditional language."
           }}
           Each item in `sections` must be an object: {{"title": "...", "body": "..."}}.
        7. Return ONLY valid JSON — no markdown fences, no commentary.
    """).strip()


# ---------------------------------------------------------------------------
# Vertex / GenAI calls (reuse env detection from gemini_service)
# ---------------------------------------------------------------------------

def _try_vertex_json(prompt: str) -> Optional[str]:
    project_id = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        return None
    location = os.getenv("VERTEX_LOCATION", "global")
    try:
        import vertexai  # type: ignore
        from vertexai.generative_models import GenerationConfig, GenerativeModel  # type: ignore

        vertexai.init(project=project_id, location=location)
        model = GenerativeModel(gemini_service._model_name())
        config = GenerationConfig(
            temperature=0.4,
            max_output_tokens=1024,
            response_mime_type="application/json",
        )
        response = model.generate_content(prompt, generation_config=config)
        return response.text.strip()
    except ImportError:
        logger.debug("ANALYST: vertexai not installed.")
        return None
    except Exception as exc:
        logger.warning("ANALYST: Vertex generation failed: %s", exc)
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
                "temperature": 0.4,
                "max_output_tokens": 1024,
                "response_mime_type": "application/json",
            },
        )
        return response.text.strip()
    except ImportError:
        logger.debug("ANALYST: google-generativeai not installed.")
        return None
    except Exception as exc:
        logger.warning("ANALYST: GenAI generation failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# JSON parsing + validation
# ---------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # Drop the first fence line and the closing fence
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _parse_model_json(raw: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON parse; returns None on failure."""
    if not raw:
        return None
    candidate = _strip_code_fences(raw)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Try to locate the first {...} block
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _validate_sections(payload: Dict[str, Any]) -> Optional[Tuple[List[AnalystBriefSection], str]]:
    """Validate the parsed model JSON. Returns (sections, key_takeaway) or None."""
    sections_raw = payload.get("sections")
    takeaway = payload.get("key_takeaway")
    if not isinstance(sections_raw, list) or not sections_raw:
        return None
    if not isinstance(takeaway, str) or not takeaway.strip():
        return None

    sections: List[AnalystBriefSection] = []
    full_text_parts: List[str] = [takeaway]
    for item in sections_raw:
        if not isinstance(item, dict):
            return None
        title = item.get("title")
        body = item.get("body")
        if not isinstance(title, str) or not isinstance(body, str):
            return None
        title_s = title.strip()
        body_s = body.strip()
        if not title_s or len(body_s) < 20:
            return None
        sections.append(AnalystBriefSection(title=title_s, body=body_s))
        full_text_parts.append(body_s)

    full_text = " ".join(full_text_parts)
    if not _is_safe(full_text):
        logger.warning("ANALYST: model output failed safety scan; falling back.")
        return None

    return sections, takeaway.strip()


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------

def _fallback(
    detail: ApiHubDetail,
    momentum: Optional[LA28MomentumResponse],
    interests: Optional[List[str]],
) -> Tuple[List[AnalystBriefSection], str]:
    """Build a safe, sectioned brief without any external calls."""
    city = detail.name
    region = detail.region or "this region"
    o_est = detail.parity_snapshot.olympic_story_estimate
    p_est = detail.parity_snapshot.paralympic_story_estimate
    parity_note = detail.parity_snapshot.parity_note
    tags = ", ".join(detail.tags[:4]) if detail.tags else "no specific tags recorded"

    snapshot_body = (
        f"{city} sits in the {region} and could offer fans a layered window into "
        f"Team USA storytelling. Descriptive tags for this hub include {tags}, which "
        f"may suggest the kinds of community and sport-culture threads worth tracking. "
        f"All signals here are aggregate, public-data indicators — they could spark fan "
        f"curiosity but do not predict any individual performance."
    )

    olympic_body = (
        f"{o_est} Combined with {city}'s broader sport-culture context, these aggregate "
        f"counts may point toward storylines fans could explore — without implying any "
        f"specific medal outcome."
    )

    paralympic_body = (
        f"{p_est} Paralympic narratives appear here with equal weight to Olympic ones, "
        f"reflecting the project's parity mandate. Counts are descriptive, not predictive."
    )

    momentum_body: str
    if momentum is not None and momentum.sports:
        top = momentum.sports[:3]
        chunks = []
        for sport in top:
            chunks.append(
                f"{sport.name} ({sport.kind.value}, descriptive score "
                f"{sport.momentum_score:.0f}/100)"
            )
        momentum_body = (
            f"LA28 momentum signals for {city} suggest the strongest current attention "
            f"could land on {', '.join(chunks)}. Scores are composite indicators of "
            f"public signals — hometown roster weight, recent media attention, and LA28 "
            f"relevance — and may shift as new data lands."
        )
    else:
        momentum_body = (
            f"No dedicated LA28 momentum profile is available for {city} in the current "
            f"seed dataset. Fans could still watch how the city's sport culture might "
            f"connect to the road to LA28 as new data lands."
        )

    interest_clause = ""
    if interests:
        clean = [i.strip() for i in interests if i.strip()][:3]
        if clean:
            interest_clause = (
                f" Given fan interest in {', '.join(clean)}, those threads could be "
                f"worth tracking specifically as the season unfolds."
            )

    watch_body = (
        f"Fans following {city} may find it useful to watch how Olympic and Paralympic "
        f"storylines develop side-by-side, since {parity_note.lower() if parity_note else 'both pathways deserve attention'} "
        f"All signals here are descriptive and conditional — they could inform fan "
        f"curiosity but should never be read as performance promises.{interest_clause}"
    )

    sections = [
        AnalystBriefSection(title="Hometown Snapshot", body=snapshot_body),
        AnalystBriefSection(title="Olympic Signal", body=olympic_body),
        AnalystBriefSection(title="Paralympic Signal", body=paralympic_body),
        AnalystBriefSection(title="LA28 Momentum", body=momentum_body),
        AnalystBriefSection(title="What Fans Could Watch For", body=watch_body),
    ]

    key_takeaway = (
        f"{city}'s hometown signals could help fans explore Team USA's Olympic and "
        f"Paralympic stories with equal weight — descriptive context, never a prediction."
    )

    return sections, key_takeaway


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_analyst_brief(
    detail: ApiHubDetail,
    momentum: Optional[LA28MomentumResponse],
    interests: Optional[List[str]] = None,
) -> AnalystBriefResponse:
    """
    Build a structured AnalystBriefResponse for a hub.

    - Tries Vertex AI, then google-generativeai, then deterministic fallback.
    - Validates model JSON shape and runs a safety scan; on any failure, falls
      back to the deterministic template and reports generated_with_gemini=False.
    """
    prompt = _build_prompt(detail, momentum, interests)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    sections: Optional[List[AnalystBriefSection]] = None
    key_takeaway: Optional[str] = None
    source: BriefSource = BriefSource.local_fallback
    model_name: Optional[str] = None
    generated_with_gemini = False

    # Attempt live generation only if something is actually configured.
    if gemini_service.gemini_is_configured():
        for attempt_label, fn in (("vertex", _try_vertex_json), ("gemini", _try_genai_json)):
            raw = fn(prompt)
            if not raw:
                continue
            payload = _parse_model_json(raw)
            if not payload:
                logger.warning("ANALYST: failed to parse %s JSON output.", attempt_label)
                continue
            validated = _validate_sections(payload)
            if validated is None:
                continue
            sections, key_takeaway = validated
            source = BriefSource.vertex if attempt_label == "vertex" else BriefSource.gemini
            model_name = gemini_service._model_name()
            generated_with_gemini = True
            logger.info("ANALYST: brief generated via %s.", attempt_label)
            break

    if sections is None or key_takeaway is None:
        sections, key_takeaway = _fallback(detail, momentum, interests)
        source = BriefSource.local_fallback
        model_name = None
        generated_with_gemini = False

    return AnalystBriefResponse(
        hub_id=detail.id,
        hub_name=detail.name,
        generated_with_gemini=generated_with_gemini,
        model=model_name,
        source=source,
        sections=sections,
        key_takeaway=key_takeaway,
        disclaimer=DISCLAIMER,
        generated_at=generated_at,
    )
