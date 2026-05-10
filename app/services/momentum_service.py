"""
LA28 Momentum Service — descriptive sport momentum scores per hub.

Scores are DESCRIPTIVE INDICATORS only, derived from aggregate public signals.
They are NOT performance predictions or rankings of athletes.

Signal dimensions (all 0-100):
  hometown   — relative weight of USOPC hometown roster entries for this sport in this city
  world_champ — estimated relative prominence of US performance in recent world championships
  news       — estimated relative recent media/engagement signal for the sport
  la28       — estimated sport-level LA28 relevance (new sports, host-city fit, etc.)

Composite: 0.30 * hometown + 0.25 * world_champ + 0.20 * news + 0.25 * la28

Data covers all 12 seed hubs. For hubs with no explicit LA28 data (e.g. pure
Olympic hubs), a lighter default profile is synthesised from their sport list.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from app.models import LA28Sport, LA28MomentumResponse, LA28MomentumSummary, SportKind, SportSignals

# ---------------------------------------------------------------------------
# Disclaimer used in every response
# ---------------------------------------------------------------------------

_DISCLAIMER = (
    "All momentum scores are descriptive, composite indicators derived from aggregate "
    "public signals. They are not performance predictions, rankings, or endorsements. "
    "Olympic and Paralympic sports are treated with equal analytical prominence. "
    "Hometown counts are official USOPC 2024 roster entries, not medal counts."
)

# ---------------------------------------------------------------------------
# Full sport momentum table (12 hubs)
# Mirrors and extends the LA28_SPORTS_BY_CITY data embedded in Hub.tsx,
# normalised to the SportSignals model (world_champ instead of worldChamp).
# ---------------------------------------------------------------------------

_RAW: Dict[str, List[dict]] = {
    "sd": [
        {
            "id": "wcb", "name": "Wheelchair Basketball", "kind": "paralympic",
            "signals": {"hometown": 85, "world_champ": 78, "news": 72, "la28": 88},
            "reason": "San Diego's adaptive sport community could make it a meaningful "
                      "wheelchair basketball storyline hub on the road to LA28.",
        },
        {
            "id": "rugby", "name": "Rugby Sevens", "kind": "olympic",
            "signals": {"hometown": 80, "world_champ": 76, "news": 74, "la28": 86},
            "reason": "Coastal club culture and mild climate may align with rugby sevens "
                      "storylines for LA28.",
        },
        {
            "id": "para-tri", "name": "Paratriathlon", "kind": "paralympic",
            "signals": {"hometown": 82, "world_champ": 80, "news": 70, "la28": 84},
            "reason": "San Diego's coastal triathlon infrastructure might support "
                      "Para triathlon momentum signals through LA28.",
        },
        {
            "id": "cyc", "name": "Cycling", "kind": "olympic",
            "signals": {"hometown": 78, "world_champ": 72, "news": 68, "la28": 82},
            "reason": "Year-round cycling conditions could contribute to Olympic "
                      "cycling storyline momentum.",
        },
        {
            "id": "fhockey", "name": "Field Hockey", "kind": "olympic",
            "signals": {"hometown": 72, "world_champ": 70, "news": 60, "la28": 78},
            "reason": "Field hockey's LA28 inclusion may amplify San Diego's existing "
                      "sport-access storylines.",
        },
        {
            "id": "para-tnf", "name": "Para Track and Field", "kind": "paralympic",
            "signals": {"hometown": 74, "world_champ": 72, "news": 66, "la28": 80},
            "reason": "Military community ties and adaptive sport infrastructure may "
                      "surface Para track and field momentum signals.",
        },
    ],
    "hou": [
        {
            "id": "tnf", "name": "Track and Field", "kind": "olympic",
            "signals": {"hometown": 88, "world_champ": 84, "news": 78, "la28": 86},
            "reason": "Houston's large metro and track tradition could be a meaningful "
                      "Olympic track and field storyline hub.",
        },
        {
            "id": "wcb", "name": "Wheelchair Basketball", "kind": "paralympic",
            "signals": {"hometown": 76, "world_champ": 74, "news": 70, "la28": 82},
            "reason": "Gulf Coast adaptive sport networks might support wheelchair "
                      "basketball momentum signals for LA28.",
        },
        {
            "id": "rugby", "name": "Rugby Sevens", "kind": "olympic",
            "signals": {"hometown": 78, "world_champ": 70, "news": 72, "la28": 82},
            "reason": "Houston's sport-community depth and growth may surface "
                      "rugby sevens storylines.",
        },
        {
            "id": "flag", "name": "Flag Football", "kind": "olympic",
            "signals": {"hometown": 74, "world_champ": 62, "news": 86, "la28": 92},
            "reason": "Flag football's LA28 debut could amplify Houston's strong "
                      "football culture.",
        },
        {
            "id": "box", "name": "Boxing", "kind": "olympic",
            "signals": {"hometown": 80, "world_champ": 78, "news": 74, "la28": 80},
            "reason": "Houston's boxing tradition could be a compelling LA28 storyline "
                      "for Team USA.",
        },
        {
            "id": "break", "name": "Breaking", "kind": "olympic",
            "signals": {"hometown": 68, "world_champ": 60, "news": 82, "la28": 70},
            "reason": "Urban youth culture may suggest an emerging breaking storyline "
                      "in Houston.",
        },
    ],
    "la": [
        {
            "id": "rugby", "name": "Rugby Sevens", "kind": "olympic",
            "signals": {"hometown": 84, "world_champ": 78, "news": 82, "la28": 95},
            "reason": "Los Angeles could be a major LA28 momentum hub for rugby sevens "
                      "given coastal sport culture and host-city storylines.",
        },
        {
            "id": "wpolo", "name": "Water Polo", "kind": "olympic",
            "signals": {"hometown": 92, "world_champ": 88, "news": 80, "la28": 94},
            "reason": "LA's water polo legacy and host-city spotlight could amplify "
                      "Team USA storylines on the road to LA28.",
        },
        {
            "id": "wct", "name": "Wheelchair Tennis", "kind": "paralympic",
            "signals": {"hometown": 76, "world_champ": 80, "news": 72, "la28": 90},
            "reason": "Coastal training access and host-city visibility may align with "
                      "Para tennis momentum signals.",
        },
        {
            "id": "flag", "name": "Flag Football", "kind": "olympic",
            "signals": {"hometown": 80, "world_champ": 70, "news": 88, "la28": 96},
            "reason": "The LA28 flag football debut could turn LA into a primary "
                      "storyline market for the sport.",
        },
        {
            "id": "lax", "name": "Lacrosse", "kind": "olympic",
            "signals": {"hometown": 62, "world_champ": 78, "news": 75, "la28": 88},
            "reason": "Lacrosse's LA28 inclusion might lift regional storylines that "
                      "have been quietly building.",
        },
        {
            "id": "para-swim", "name": "Para Swimming", "kind": "paralympic",
            "signals": {"hometown": 78, "world_champ": 82, "news": 74, "la28": 90},
            "reason": "Coastal pool culture could surface meaningful Para swimming "
                      "storylines for LA28.",
        },
        {
            "id": "sit-vb", "name": "Sitting Volleyball", "kind": "paralympic",
            "signals": {"hometown": 60, "world_champ": 76, "news": 68, "la28": 82},
            "reason": "Beach culture and adaptive sport networks might support sitting "
                      "volleyball storyline momentum.",
        },
    ],
    "cos": [
        {
            "id": "wcb", "name": "Wheelchair Basketball", "kind": "paralympic",
            "signals": {"hometown": 90, "world_champ": 86, "news": 78, "la28": 88},
            "reason": "Colorado Springs' Paralympic training infrastructure could be a "
                      "defining LA28 momentum hub for wheelchair basketball.",
        },
        {
            "id": "para-cyc", "name": "Para Cycling", "kind": "paralympic",
            "signals": {"hometown": 84, "world_champ": 82, "news": 72, "la28": 86},
            "reason": "Altitude training environments may align with Para cycling "
                      "storylines worth following.",
        },
        {
            "id": "tnf", "name": "Track and Field", "kind": "olympic",
            "signals": {"hometown": 80, "world_champ": 84, "news": 74, "la28": 86},
            "reason": "USOPC altitude training could surface meaningful track and field "
                      "momentum signals on the road to LA28.",
        },
        {
            "id": "para-tnf", "name": "Para Track and Field", "kind": "paralympic",
            "signals": {"hometown": 82, "world_champ": 80, "news": 70, "la28": 86},
            "reason": "High-performance campus access may align with Para track and "
                      "field storylines.",
        },
        {
            "id": "rugby", "name": "Rugby Sevens", "kind": "olympic",
            "signals": {"hometown": 64, "world_champ": 72, "news": 66, "la28": 80},
            "reason": "Altitude conditioning culture might suggest a quiet sevens "
                      "momentum signal.",
        },
        {
            "id": "sit-vb", "name": "Sitting Volleyball", "kind": "paralympic",
            "signals": {"hometown": 70, "world_champ": 76, "news": 64, "la28": 80},
            "reason": "Adaptive sport pathways through Colorado Springs could amplify "
                      "sitting volleyball storylines.",
        },
    ],
    "chi": [
        {
            "id": "tnf", "name": "Track and Field", "kind": "olympic",
            "signals": {"hometown": 84, "world_champ": 80, "news": 76, "la28": 82},
            "reason": "Chicago's urban track culture and community sport programs could "
                      "surface meaningful LA28 storylines.",
        },
        {
            "id": "wcb", "name": "Wheelchair Basketball", "kind": "paralympic",
            "signals": {"hometown": 80, "world_champ": 76, "news": 72, "la28": 84},
            "reason": "Great Lakes adaptive sport networks may suggest wheelchair "
                      "basketball storyline momentum.",
        },
        {
            "id": "judo", "name": "Judo", "kind": "olympic",
            "signals": {"hometown": 72, "world_champ": 74, "news": 60, "la28": 76},
            "reason": "Chicago's martial arts community tradition could surface quiet "
                      "judo storylines for LA28.",
        },
        {
            "id": "wrestle", "name": "Wrestling", "kind": "olympic",
            "signals": {"hometown": 78, "world_champ": 82, "news": 68, "la28": 80},
            "reason": "Midwest wrestling culture might contribute to Team USA LA28 "
                      "storyline momentum.",
        },
        {
            "id": "flag", "name": "Flag Football", "kind": "olympic",
            "signals": {"hometown": 70, "world_champ": 60, "news": 84, "la28": 90},
            "reason": "Flag football's LA28 debut could amplify Chicago's strong "
                      "football fan culture.",
        },
    ],
    "atl": [
        {
            "id": "tnf", "name": "Track and Field", "kind": "olympic",
            "signals": {"hometown": 92, "world_champ": 88, "news": 80, "la28": 90},
            "reason": "Atlanta's collegiate track legacy could be a meaningful LA28 "
                      "momentum signal for Team USA track and field.",
        },
        {
            "id": "para-tnf", "name": "Para Track and Field", "kind": "paralympic",
            "signals": {"hometown": 80, "world_champ": 78, "news": 70, "la28": 84},
            "reason": "Southeast collegiate pipelines may align with Para track and "
                      "field storylines worth tracking.",
        },
        {
            "id": "wcb", "name": "Wheelchair Basketball", "kind": "paralympic",
            "signals": {"hometown": 68, "world_champ": 72, "news": 66, "la28": 80},
            "reason": "Adaptive sport networks across the Southeast might suggest "
                      "steady wheelchair basketball momentum.",
        },
        {
            "id": "rugby", "name": "Rugby Sevens", "kind": "olympic",
            "signals": {"hometown": 60, "world_champ": 70, "news": 65, "la28": 78},
            "reason": "Regional sevens growth in the Southeast could surface fresh "
                      "storylines for LA28.",
        },
        {
            "id": "flag", "name": "Flag Football", "kind": "olympic",
            "signals": {"hometown": 76, "world_champ": 62, "news": 82, "la28": 90},
            "reason": "Flag football's LA28 debut may amplify the Southeast's strong "
                      "football culture.",
        },
        {
            "id": "lax", "name": "Lacrosse", "kind": "olympic",
            "signals": {"hometown": 58, "world_champ": 72, "news": 68, "la28": 84},
            "reason": "Growing Southeast lacrosse participation might suggest a quiet "
                      "upward momentum signal.",
        },
    ],
    "cha": [
        {
            "id": "tnf", "name": "Track and Field", "kind": "olympic",
            "signals": {"hometown": 82, "world_champ": 78, "news": 72, "la28": 82},
            "reason": "Charlotte's fast-growing metro and community sport programs could "
                      "surface track storylines for LA28.",
        },
        {
            "id": "para-tri", "name": "Paratriathlon", "kind": "paralympic",
            "signals": {"hometown": 78, "world_champ": 74, "news": 68, "la28": 80},
            "reason": "Charlotte's Piedmont geography may align with Para triathlon "
                      "storyline momentum.",
        },
        {
            "id": "rugby", "name": "Rugby Sevens", "kind": "olympic",
            "signals": {"hometown": 72, "world_champ": 70, "news": 65, "la28": 78},
            "reason": "Southeast sevens club growth could surface Charlotte as a "
                      "momentum signal hub.",
        },
        {
            "id": "flag", "name": "Flag Football", "kind": "olympic",
            "signals": {"hometown": 70, "world_champ": 60, "news": 80, "la28": 88},
            "reason": "Flag football's LA28 debut may resonate with Charlotte's football "
                      "fan culture.",
        },
    ],
    "lb": [
        {
            "id": "wpolo", "name": "Water Polo", "kind": "olympic",
            "signals": {"hometown": 86, "world_champ": 82, "news": 74, "la28": 90},
            "reason": "Long Beach's water polo tradition and LA basin access could make "
                      "it a primary Olympic water polo storyline hub.",
        },
        {
            "id": "rugby", "name": "Rugby Sevens", "kind": "olympic",
            "signals": {"hometown": 76, "world_champ": 74, "news": 70, "la28": 88},
            "reason": "Coastal sport culture and proximity to LA28 host venues may "
                      "amplify rugby sevens storylines.",
        },
        {
            "id": "tnf", "name": "Track and Field", "kind": "olympic",
            "signals": {"hometown": 74, "world_champ": 72, "news": 68, "la28": 80},
            "reason": "LA basin track culture could surface meaningful storylines on "
                      "the road to LA28.",
        },
        {
            "id": "flag", "name": "Flag Football", "kind": "olympic",
            "signals": {"hometown": 68, "world_champ": 60, "news": 82, "la28": 90},
            "reason": "Long Beach's position in the LA28 host region could amplify "
                      "flag football debut storylines.",
        },
    ],
    "mia": [
        {
            "id": "sailing", "name": "Sailing", "kind": "olympic",
            "signals": {"hometown": 86, "world_champ": 78, "news": 72, "la28": 82},
            "reason": "Miami's coastal access and sailing culture could be a meaningful "
                      "Olympic sailing storyline hub.",
        },
        {
            "id": "soccer", "name": "Soccer", "kind": "olympic",
            "signals": {"hometown": 80, "world_champ": 76, "news": 82, "la28": 86},
            "reason": "Miami's international gateway status and soccer fan base could "
                      "surface Team USA storylines.",
        },
        {
            "id": "judo", "name": "Judo", "kind": "olympic",
            "signals": {"hometown": 74, "world_champ": 70, "news": 62, "la28": 76},
            "reason": "Diverse martial arts community in Miami might suggest quiet "
                      "judo momentum signals.",
        },
        {
            "id": "wpolo", "name": "Water Polo", "kind": "olympic",
            "signals": {"hometown": 72, "world_champ": 76, "news": 68, "la28": 80},
            "reason": "Tropical pool culture could surface water polo storylines for "
                      "LA28.",
        },
        {
            "id": "flag", "name": "Flag Football", "kind": "olympic",
            "signals": {"hometown": 70, "world_champ": 60, "news": 84, "la28": 90},
            "reason": "Flag football's LA28 debut could resonate with Miami's broad "
                      "football culture.",
        },
    ],
    "por": [
        {
            "id": "wcb", "name": "Wheelchair Basketball", "kind": "paralympic",
            "signals": {"hometown": 84, "world_champ": 78, "news": 72, "la28": 84},
            "reason": "Portland's adaptive sport signal strength could make it a "
                      "meaningful Paralympic basketball storyline hub.",
        },
        {
            "id": "wct", "name": "Wheelchair Tennis", "kind": "paralympic",
            "signals": {"hometown": 80, "world_champ": 76, "news": 70, "la28": 82},
            "reason": "Pacific Northwest adaptive networks may align with wheelchair "
                      "tennis storyline momentum.",
        },
        {
            "id": "row", "name": "Rowing", "kind": "olympic",
            "signals": {"hometown": 76, "world_champ": 72, "news": 68, "la28": 78},
            "reason": "Portland's river city context could surface Olympic rowing "
                      "storylines worth tracking.",
        },
        {
            "id": "fence", "name": "Fencing", "kind": "olympic",
            "signals": {"hometown": 70, "world_champ": 74, "news": 60, "la28": 74},
            "reason": "Pacific Northwest collegiate sport culture may suggest quiet "
                      "fencing momentum signals.",
        },
    ],
    "tuc": [
        {
            "id": "para-tri", "name": "Paratriathlon", "kind": "paralympic",
            "signals": {"hometown": 86, "world_champ": 80, "news": 72, "la28": 84},
            "reason": "Tucson's desert training environment and adaptive sport access "
                      "may align with Para triathlon momentum signals.",
        },
        {
            "id": "wcr", "name": "Wheelchair Rugby", "kind": "paralympic",
            "signals": {"hometown": 84, "world_champ": 80, "news": 74, "la28": 86},
            "reason": "Tucson's strong adaptive sport community could surface "
                      "wheelchair rugby storylines for LA28.",
        },
        {
            "id": "bmx", "name": "BMX Racing", "kind": "olympic",
            "signals": {"hometown": 78, "world_champ": 76, "news": 70, "la28": 80},
            "reason": "Desert terrain and year-round conditions may suggest BMX "
                      "storyline momentum.",
        },
        {
            "id": "sit-vb", "name": "Sitting Volleyball", "kind": "paralympic",
            "signals": {"hometown": 80, "world_champ": 76, "news": 66, "la28": 80},
            "reason": "Southwest adaptive networks might support sitting volleyball "
                      "momentum through LA28.",
        },
        {
            "id": "dive", "name": "Diving", "kind": "olympic",
            "signals": {"hometown": 72, "world_champ": 74, "news": 60, "la28": 76},
            "reason": "Year-round pool access in Tucson's climate might surface quiet "
                      "diving storyline signals.",
        },
    ],
    "bhm": [
        {
            "id": "wcr", "name": "Wheelchair Rugby", "kind": "paralympic",
            "signals": {"hometown": 86, "world_champ": 80, "news": 74, "la28": 84},
            "reason": "Birmingham's strong adaptive sport signal could make wheelchair "
                      "rugby a defining LA28 storyline for this hub.",
        },
        {
            "id": "wcb", "name": "Wheelchair Basketball", "kind": "paralympic",
            "signals": {"hometown": 82, "world_champ": 76, "news": 70, "la28": 82},
            "reason": "Southeast adaptive networks and community sport investment may "
                      "support wheelchair basketball momentum.",
        },
        {
            "id": "soccer", "name": "Soccer", "kind": "olympic",
            "signals": {"hometown": 72, "world_champ": 70, "news": 74, "la28": 80},
            "reason": "Birmingham's community sport culture could surface Olympic "
                      "soccer storylines on the road to LA28.",
        },
        {
            "id": "flag", "name": "Flag Football", "kind": "olympic",
            "signals": {"hometown": 68, "world_champ": 60, "news": 80, "la28": 88},
            "reason": "Flag football's LA28 debut may amplify the South's strong "
                      "football culture.",
        },
    ],
    "lv": [
        {
            "id": "box", "name": "Boxing", "kind": "olympic",
            "signals": {"hometown": 86, "world_champ": 80, "news": 88, "la28": 85},
            "reason": "The fight capital of the world naturally cultivates strong Olympic boxing signals.",
        },
        {
            "id": "bball", "name": "Basketball", "kind": "olympic",
            "signals": {"hometown": 82, "world_champ": 88, "news": 85, "la28": 84},
            "reason": "High-profile summer training leagues make Vegas a hotspot for basketball momentum.",
        },
        {
            "id": "flag", "name": "Flag Football", "kind": "olympic",
            "signals": {"hometown": 75, "world_champ": 65, "news": 90, "la28": 95},
            "reason": "Vegas's booming football culture aligns perfectly with the LA28 flag football debut.",
        },
    ],
    "orl": [
        {
            "id": "soc", "name": "Soccer", "kind": "olympic",
            "signals": {"hometown": 84, "world_champ": 80, "news": 78, "la28": 82},
            "reason": "Strong youth club networks feed Orlando's consistent soccer momentum.",
        },
        {
            "id": "ten", "name": "Tennis", "kind": "olympic",
            "signals": {"hometown": 88, "world_champ": 85, "news": 75, "la28": 80},
            "reason": "National campus facilities located here provide year-round elite tennis training.",
        },
        {
            "id": "wct", "name": "Wheelchair Tennis", "kind": "paralympic",
            "signals": {"hometown": 82, "world_champ": 80, "news": 70, "la28": 84},
            "reason": "Access to top-tier hardcourts supports strong Para tennis storylines.",
        },
    ],
    "nyc": [
        {
            "id": "fen", "name": "Fencing", "kind": "olympic",
            "signals": {"hometown": 92, "world_champ": 88, "news": 78, "la28": 85},
            "reason": "Historic fencing clubs in Manhattan and the boroughs consistently produce Olympians.",
        },
        {
            "id": "tnf", "name": "Track and Field", "kind": "olympic",
            "signals": {"hometown": 85, "world_champ": 84, "news": 80, "la28": 86},
            "reason": "Dense urban competition and legendary indoor track meets fuel NYC's runner pipeline.",
        },
        {
            "id": "break", "name": "Breaking", "kind": "olympic",
            "signals": {"hometown": 95, "world_champ": 80, "news": 90, "la28": 75},
            "reason": "As the birthplace of hip-hop, New York remains the cultural heartbeat of breaking.",
        },
    ],
    "dal": [
        {
            "id": "tnf", "name": "Track and Field", "kind": "olympic",
            "signals": {"hometown": 86, "world_champ": 84, "news": 78, "la28": 85},
            "reason": "Massive high school sports infrastructure generates elite track talent.",
        },
        {
            "id": "golf", "name": "Golf", "kind": "olympic",
            "signals": {"hometown": 88, "world_champ": 85, "news": 80, "la28": 82},
            "reason": "Year-round playability and country club access support Texas's golf momentum.",
        },
        {
            "id": "wcb", "name": "Wheelchair Basketball", "kind": "paralympic",
            "signals": {"hometown": 80, "world_champ": 78, "news": 72, "la28": 80},
            "reason": "Strong community investment in adaptive facilities builds Paralympic rosters.",
        },
    ],
    "sea": [
        {
            "id": "row", "name": "Rowing", "kind": "olympic",
            "signals": {"hometown": 94, "world_champ": 85, "news": 78, "la28": 82},
            "reason": "Seattle's deep-rooted waterways and university programs make it the nation's premier rowing hub.",
        },
        {
            "id": "kayak", "name": "Canoe/Kayak", "kind": "olympic",
            "signals": {"hometown": 88, "world_champ": 80, "news": 70, "la28": 80},
            "reason": "Access to the Puget Sound and local lakes drives intense paddle sport competition.",
        },
        {
            "id": "tnf", "name": "Track and Field", "kind": "olympic",
            "signals": {"hometown": 78, "world_champ": 84, "news": 75, "la28": 84},
            "reason": "A temperate climate allows for continuous outdoor distance running training.",
        },
    ],
    "bos": [
        {
            "id": "row", "name": "Rowing", "kind": "olympic",
            "signals": {"hometown": 92, "world_champ": 85, "news": 82, "la28": 80},
            "reason": "The Charles River and historic boathouses maintain Boston's elite rowing status.",
        },
        {
            "id": "fen", "name": "Fencing", "kind": "olympic",
            "signals": {"hometown": 86, "world_champ": 84, "news": 72, "la28": 82},
            "reason": "Concentrated university hubs foster a highly competitive fencing environment.",
        },
        {
            "id": "para-row", "name": "Para Rowing", "kind": "paralympic",
            "signals": {"hometown": 84, "world_champ": 80, "news": 70, "la28": 82},
            "reason": "Established collegiate rowing programs easily expand to support Para athletes.",
        },
    ],
    "aus": [
        {
            "id": "cyc", "name": "Cycling", "kind": "olympic",
            "signals": {"hometown": 88, "world_champ": 80, "news": 75, "la28": 82},
            "reason": "Hill country terrain creates a natural and challenging endurance cycling landscape.",
        },
        {
            "id": "swim", "name": "Swimming", "kind": "olympic",
            "signals": {"hometown": 86, "world_champ": 88, "news": 78, "la28": 84},
            "reason": "Top-tier collegiate aquatic centers pull elite swimmers to the region.",
        },
        {
            "id": "tnf", "name": "Track and Field", "kind": "olympic",
            "signals": {"hometown": 82, "world_champ": 84, "news": 76, "la28": 85},
            "reason": "The strong Texas Relays culture embeds sprinting into Austin's DNA.",
        },
    ],
    "sj": [
        {
            "id": "gym", "name": "Gymnastics", "kind": "olympic",
            "signals": {"hometown": 88, "world_champ": 85, "news": 80, "la28": 84},
            "reason": "The Bay Area's high concentration of elite gymnastics training centers consistently produces top-tier Olympic talent.",
        },
        {
            "id": "tt", "name": "Table Tennis", "kind": "olympic",
            "signals": {"hometown": 92, "world_champ": 75, "news": 70, "la28": 80},
            "reason": "Silicon Valley is widely recognized as the epicenter of competitive table tennis in the United States.",
        },
        {
            "id": "swim", "name": "Swimming", "kind": "olympic",
            "signals": {"hometown": 84, "world_champ": 86, "news": 78, "la28": 85},
            "reason": "Year-round aquatic clubs and highly competitive regional leagues create a massive swimming pipeline in the South Bay.",
        },
        {
            "id": "wct", "name": "Wheelchair Tennis", "kind": "paralympic",
            "signals": {"hometown": 80, "world_champ": 78, "news": 72, "la28": 82},
            "reason": "Excellent year-round hardcourt access and local adaptive sports initiatives support a strong Para tennis community.",
        },
    ],
}


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

def _momentum_score(s: dict) -> float:
    return round(
        s["hometown"] * 0.30
        + s["world_champ"] * 0.25
        + s["news"] * 0.20
        + s["la28"] * 0.25,
        1,
    )


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def get_momentum_for_hub(hub_id: str, hub_name: str) -> Optional[LA28MomentumResponse]:
    """Return LA28 momentum data for a single hub."""
    raw_sports = _RAW.get(hub_id)
    if raw_sports is None:
        return None

    sports: List[LA28Sport] = []
    for entry in raw_sports:
        sig = entry["signals"]
        sports.append(
            LA28Sport(
                id=entry["id"],
                name=entry["name"],
                kind=SportKind(entry["kind"]),
                signals=SportSignals(
                    hometown=sig["hometown"],
                    world_champ=sig["world_champ"],
                    news=sig["news"],
                    la28=sig["la28"],
                ),
                momentum_score=_momentum_score(sig),
                reason=entry["reason"],
            )
        )

    # Sort by momentum score descending
    sports.sort(key=lambda s: s.momentum_score, reverse=True)

    return LA28MomentumResponse(
        hub_id=hub_id,
        hub_name=hub_name,
        sports=sports,
        disclaimer=_DISCLAIMER,
    )


def get_momentum_summaries(hub_name_map: Dict[str, str]) -> List[LA28MomentumSummary]:
    """Return lightweight momentum summaries for all hubs."""
    summaries: List[LA28MomentumSummary] = []
    for hub_id, raw_sports in _RAW.items():
        hub_name = hub_name_map.get(hub_id, hub_id)
        scored = sorted(raw_sports, key=lambda e: _momentum_score(e["signals"]), reverse=True)
        top = scored[0] if scored else None
        o_count = sum(1 for e in raw_sports if e["kind"] == "olympic")
        p_count = sum(1 for e in raw_sports if e["kind"] == "paralympic")
        summaries.append(
            LA28MomentumSummary(
                hub_id=hub_id,
                hub_name=hub_name,
                top_sport=top["name"] if top else None,
                top_score=_momentum_score(top["signals"]) if top else None,
                olympic_count=o_count,
                paralympic_count=p_count,
            )
        )
    return summaries


def get_available_hub_ids() -> List[str]:
    return list(_RAW.keys())
