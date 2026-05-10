"""
DataStore — loads and serves the hometown hub seed data from CSV.

The CSV lives at app/data/hometown_hubs.csv. This module:
  - Parses all 12 hub rows at startup (cached in memory).
  - Exposes typed helpers used by the route handlers.
  - Builds ApiHubSummary, ApiHubDetail, and ApiMapPin objects from CSV fields.
  - Adds contextual map pins (Olympic & Paralympic signals, venue reference)
    from public data only — no private athlete information.
  - Dynamically enriches the 'narrative' column using Google's GenAI SDK
    understanding of the hubs, replacing the static CSV descriptions with 
    deep, human storytelling.

IMPORTANT: All language is conditional/descriptive. No performance predictions.
"""

from __future__ import annotations

import csv
import io
import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from app.models import (
    ApiCoordinates,
    ApiHubDetail,
    ApiHubSummary,
    ApiMapPin,
    ApiParitySnapshot,
    MapPinColor,
)

# ---------------------------------------------------------------------------
# Path resolution (works inside Docker and locally)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent.parent  # → app/
_CSV_PATH = _HERE / "data" / "hometown_hubs.csv"


# ---------------------------------------------------------------------------
# Internal raw row type (everything is str from CSV)
# ---------------------------------------------------------------------------

def _int_or_none(val: str) -> Optional[int]:
    v = val.strip()
    return int(v) if v.lstrip("-").isdigit() else None


def _float_safe(val: str, default: float = 0.0) -> float:
    try:
        return float(val.strip())
    except (ValueError, AttributeError):
        return default


def _tags(val: str) -> List[str]:
    """Split a comma-separated tags string into a clean list."""
    if not val or not val.strip():
        return []
    return [t.strip() for t in val.split(",") if t.strip()]


def _sports(val: str) -> List[str]:
    """Split a comma-separated sports string; strips outer quotes."""
    raw = val.strip().strip('"')
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


# ---------------------------------------------------------------------------
# Build ApiMapPin list for a hub
# ---------------------------------------------------------------------------

def _build_map_pins(row: dict) -> List[ApiMapPin]:
    """
    Generate contextual map pins from public aggregate data.
    Pins represent community/sport-access signals, not individual athletes.
    No private athlete information is included.
    """
    pins: List[ApiMapPin] = []
    lat = _float_safe(row.get("lat", "0"))
    lng = _float_safe(row.get("lng", "0"))
    city = row.get("city", "")
    state = row.get("state", "")
    o_count = _int_or_none(row.get("olympian_count", ""))
    p_count = _int_or_none(row.get("paralympian_count", ""))
    sports_list = _sports(row.get("sports", ""))

    if o_count is not None:
        sport_str = ", ".join(sports_list[:3]) if sports_list else "various sports"
        pins.append(
            ApiMapPin(
                label=f"Olympic Hometown Signal — {city}",
                lat=lat + 0.02,
                lng=lng + 0.02,
                color=MapPinColor.red,
                description=(
                    f"{city}, {state} has {o_count} official Olympic hometown roster "
                    f"entr{'y' if o_count == 1 else 'ies'} in the 2024 USOPC dataset "
                    f"across sports including {sport_str}. These are aggregate roster counts, "
                    f"not medal counts or performance predictions."
                ),
                external_link=row.get("source_url", "").split(";")[0].strip() or None,
            )
        )

    if p_count is not None:
        para_sports = [s for s in sports_list if any(
            kw in s.lower() for kw in ["wheelchair", "para", "sitting", "blind", "bocce"]
        )] or sports_list[:2]
        sport_str = ", ".join(para_sports[:3]) or "adaptive sports"
        pins.append(
            ApiMapPin(
                label=f"Paralympic Hometown Signal — {city}",
                lat=lat - 0.02,
                lng=lng - 0.02,
                color=MapPinColor.blue,
                description=(
                    f"{city}, {state} has {p_count} official Paralympic hometown roster "
                    f"entr{'y' if p_count == 1 else 'ies'} in the 2024 USOPC dataset "
                    f"across sports including {sport_str}. These are aggregate roster counts, "
                    f"not medal counts or performance predictions."
                ),
                external_link=row.get("source_url", "").split(";")[-1].strip() or None,
            )
        )

    hub_id = row.get("hub_id", "")
    if hub_id in {"la", "lb", "sd"}:
        pins.append(
            ApiMapPin(
                label="LA28 Host-Region Context",
                lat=lat + 0.01,
                lng=lng - 0.03,
                color=MapPinColor.white,
                description=(
                    f"{city} is part of the greater LA28 host region. "
                    f"Fan storytelling may connect current Team USA hometown signals "
                    f"to the road to the 2028 Olympics and Paralympics in Los Angeles."
                ),
                external_link="https://la28.org",
            )
        )

    return pins


# ---------------------------------------------------------------------------
# Build ApiParitySnapshot for a hub
# ---------------------------------------------------------------------------

def _build_parity_snapshot(row: dict) -> ApiParitySnapshot:
    o_count = _int_or_none(row.get("olympian_count", ""))
    p_count = _int_or_none(row.get("paralympian_count", ""))
    city = row.get("city", "this hub")

    if o_count is not None:
        o_story = (
            f"{city} could support an estimated {o_count} unique Olympic hometown "
            f"story thread{'s' if o_count != 1 else ''} based on official 2024 USOPC roster data. "
            f"Counts are aggregate roster entries, not medal predictions."
        )
    else:
        o_story = f"No Olympic hometown roster entries found for {city} in this dataset."

    if p_count is not None:
        p_story = (
            f"{city} could support an estimated {p_count} unique Paralympic hometown "
            f"story thread{'s' if p_count != 1 else ''} based on official 2024 USOPC roster data. "
            f"Counts are aggregate roster entries, not medal predictions."
        )
    else:
        p_story = (
            f"No Paralympic hometown roster entries appeared for {city} in the parsed "
            f"2024 USOPC Paralympic spreadsheet. Blank means not represented in this seed, "
            f"not a performance judgment."
        )

    if o_count is not None and p_count is not None:
        if p_count >= o_count:
            parity = (
                f"{city} shows a strong Paralympic-weighted hometown signal in this dataset, "
                f"with {p_count} Paralympic {'entry' if p_count == 1 else 'entries'} "
                f"versus {o_count} Olympic {'entry' if o_count == 1 else 'entries'}. "
                f"Olympic and Paralympic stories should be displayed with equal prominence."
            )
        elif o_count > p_count * 2:
            parity = (
                f"{city} has a higher Olympic count ({o_count}) than Paralympic ({p_count}) "
                f"in this seed. The app should still surface Paralympic stories prominently "
                f"to reflect true parity."
            )
        else:
            parity = (
                f"{city} shows a balanced Olympic ({o_count}) and Paralympic ({p_count}) "
                f"hometown signal. Both pathways should be displayed with equal weight."
            )
    elif o_count is not None:
        parity = (
            f"Only Olympic hometown signals are present for {city} in this dataset. "
            f"Paralympic data may exist outside this seed; the app should indicate "
            f"data availability rather than absence of athletes."
        )
    else:
        parity = f"Limited hometown data available for {city} in the current seed."

    return ApiParitySnapshot(
        olympic_story_estimate=o_story,
        paralympic_story_estimate=p_story,
        parity_note=parity,
    )


# ---------------------------------------------------------------------------
# Narrative fallback builder
# ---------------------------------------------------------------------------

def _build_narrative(row: dict) -> str:
    """
    Build a compliance-safe, conditional narrative for a hub from structured fields.
    Used as a fallback when GenAI is disabled or fails.
    """
    city = row.get("city", "This city")
    state = row.get("state", "")
    region = row.get("region", "this region")
    climate = row.get("climate_type", "")
    o_count = _int_or_none(row.get("olympian_count", ""))
    p_count = _int_or_none(row.get("paralympian_count", ""))
    sports_list = _sports(row.get("sports", ""))
    sport_str = ", ".join(sports_list[:4]) if sports_list else "various sports"
    short_insight = row.get("short_insight", "").strip()

    o_part = ""
    p_part = ""
    if o_count is not None:
        o_part = (
            f"{city} has {o_count} official Olympic hometown roster "
            f"{'entry' if o_count == 1 else 'entries'} in the 2024 USOPC dataset."
        )
    if p_count is not None:
        p_part = (
            f" The city also has {p_count} official Paralympic hometown roster "
            f"{'entry' if p_count == 1 else 'entries'}, making both pathways "
            f"visible in the fan-facing storytelling layer."
        )
    elif p_count is None:
        p_part = (
            f" No Paralympic hometown entry appeared for {city} in the parsed "
            f"2024 USOPC Paralympic spreadsheet; this reflects data availability, "
            f"not a performance judgment."
        )

    climate_part = ""
    if climate:
        climate_part = (
            f" {city}'s {climate.split('/')[0].strip().lower()} climate context "
            f"and {region} setting may suggest how local environment could connect "
            f"fans to Team USA sporting storylines."
        )

    insight_part = f" {short_insight}" if short_insight and len(short_insight) > 15 else ""

    return (
        f"{o_part}{p_part} Sports represented in this dataset include "
        f"{sport_str}.{climate_part}{insight_part} "
        f"All counts are aggregate hometown roster entries from official USOPC "
        f"spreadsheets — not medal counts or performance predictions."
    ).strip()


# ---------------------------------------------------------------------------
# Main DataStore class
# ---------------------------------------------------------------------------

class DataStore:
    """
    In-memory store for the 12-hub seed dataset.
    Thread-safe for read operations (all writes happen at startup).
    """

    def __init__(self, csv_path: Path = _CSV_PATH) -> None:
        self._rows: Dict[str, dict] = {}
        self._load(csv_path)
        self._enrich_narrative_with_gemini()

    def _load(self, csv_path: Path) -> None:
        """Parse the CSV, skipping comment lines."""
        raw_lines = csv_path.read_text(encoding="utf-8").splitlines()
        data_lines = [ln for ln in raw_lines if not ln.startswith("#")]
        reader = csv.DictReader(io.StringIO("\n".join(data_lines)))
        for row in reader:
            hub_id = row.get("hub_id", "")
            if isinstance(hub_id, list):
                hub_id = hub_id[0] if hub_id else ""
            hub_id = hub_id.strip()
            if hub_id:
                clean = {}
                for k, v in row.items():
                    if k is None:
                        continue
                    if isinstance(v, list):
                        clean[k] = str(v[0]).strip() if v else ""
                    elif v is None:
                        clean[k] = ""
                    else:
                        clean[k] = str(v).strip()
                self._rows[hub_id] = clean

    def _enrich_narrative_with_gemini(self) -> None:
        """
        Dynamically calls the Google GenAI API to replace the static CSV 'narrative'
        with an engaging, story-driven explanation of the hub's culture.
        Requires the 'google-genai' package and GEMINI_API_KEY.
        """
        if os.getenv("ENABLE_GEMINI_AI", "").lower() not in ("1", "true", "yes"):
            print("Notice: ENABLE_GEMINI_AI not set. Using default CSV narratives.")
            return

        try:
            from google import genai
        except ImportError:
            print("Notice: 'google-genai' package not installed. Using default CSV narratives.")
            return

        print("Enriching Team USA hub narratives dynamically via Google GenAI...")
        
        try:
            # Client automatically detects the GEMINI_API_KEY from environment variables
            client = genai.Client()

            for hub_id, row in self._rows.items():
                city = row.get("city", "this city")
                state = row.get("state", "")
                sports = row.get("sports", "")
                climate = row.get("climate_type", "local weather")
                landscape = row.get("landscape_tags", "geography")
                o_count = row.get("olympian_count", "0")
                p_count = row.get("paralympian_count", "0")

                # PROMPT: Tuned specifically for engaging, long-form narrative storytelling
                # that avoids typical LLM structure and language crutches.
                prompt = (
                    f"You are an expert local sports storyteller writing for a fan app. "
                    f"Write a 2 to 3 sentence engaging narrative about {city}, {state}'s contribution to Team USA. "
                    f"This area produced {o_count} Olympic and {p_count} Paralympic roster entries in sports like {sports}. "
                    f"The local climate is {climate} with features like {landscape}. "
                    f"Explain exactly how the daily environment, community culture, and geography naturally shape the athletes who train here. "
                    f"Make it sound like a deeply knowledgeable human journalist wrote it. "
                    f"CRITICAL CONSTRAINTS: "
                    f"1. Do NOT use cliché AI filler words like 'boasting', 'nestled', 'tapestry', 'testament', 'showcases', 'beacon', or 'hub'. "
                    f"2. Use normal, active verbs. "
                    f"3. Do not mention medal predictions or outcomes, focus only on the vibe, training culture, and community footprint."
                )

                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt
                )
                
                if response.text:
                    # Target the narrative column specifically instead of short_insight
                    # Ensure newlines are preserved for proper paragraph breaks in the UI
                    self._rows[hub_id]["narrative"] = response.text.strip()
        except Exception as e:
            print(f"GenAI initialization or enrichment failed: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_summaries(self) -> List[ApiHubSummary]:
        results = []
        for row in self._rows.values():
            results.append(
                ApiHubSummary(
                    id=row["hub_id"],
                    name=row["city"],
                    region=row.get("region", ""),
                    x=_float_safe(row.get("x_svg", "50")),
                    y=_float_safe(row.get("y_svg", "50")),
                    short_insight=row.get("short_insight", ""),
                    tags=_tags(row.get("tags", "")),
                    coordinates=ApiCoordinates(
                        lat=_float_safe(row.get("lat", "0")),
                        lng=_float_safe(row.get("lng", "0")),
                    ),
                )
            )
        return results

    def get_detail(self, hub_id: str) -> Optional[ApiHubDetail]:
        row = self._rows.get(hub_id)
        if row is None:
            return None

        sources_raw = row.get("source_url", "")
        sources = [s.strip() for s in sources_raw.split(";") if s.strip()]

        # Grab the dynamically generated narrative, falling back to the builder
        # if the column is blank for some reason.
        raw_narrative = row.get("narrative", "").strip()
        narrative = _build_narrative(row)

        return ApiHubDetail(
            id=row["hub_id"],
            name=row["city"],
            region=row.get("region", ""),
            x=_float_safe(row.get("x_svg", "50")),
            y=_float_safe(row.get("y_svg", "50")),
            coordinates=ApiCoordinates(
                lat=_float_safe(row.get("lat", "0")),
                lng=_float_safe(row.get("lng", "0")),
            ),
            tags=_tags(row.get("tags", "")),
            narrative=narrative,
            map_pins=_build_map_pins(row),
            parity_snapshot=_build_parity_snapshot(row),
            sources=sources,
        )

    def get_raw_row(self, hub_id: str) -> Optional[dict]:
        return self._rows.get(hub_id)

    def all_hub_ids(self) -> List[str]:
        return list(self._rows.keys())

    def hub_exists(self, hub_id: str) -> bool:
        return hub_id in self._rows


# ---------------------------------------------------------------------------
# Singleton accessor (used in FastAPI dependency injection)
# ---------------------------------------------------------------------------

_store: Optional[DataStore] = None

def get_data_store() -> DataStore:
    global _store
    if _store is None:
        _store = DataStore()
    return _store
