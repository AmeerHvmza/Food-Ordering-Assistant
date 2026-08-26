"""Map a browser lat/lng to an area name this snapshot can search.

search_restaurants matches location as text against restaurant address/name
(LIKE '%Garden%'), not as coordinates. A paid reverse-geocoder is unnecessary
at this scale: we snap to the nearest known Karachi area centroid (the same
areas used for well-known discovery). Outside ~25 km of every centroid we
return None so a Lahore/Islamabad pin does not pretend to be Saddar.
"""

from __future__ import annotations

import math

# Names are chosen to match how addresses are stored (so LIKE still hits).
KARACHI_AREAS: tuple[tuple[str, float, float], ...] = (
    ("Saddar", 24.8607, 67.0011),
    ("Garden", 24.8820, 67.0270),
    ("Clifton", 24.8138, 67.0234),
    ("DHA Phase 5", 24.8030, 67.0550),
    ("Tariq Road", 24.8736, 67.0554),
    ("SMCHS", 24.8730, 67.0460),
    ("Gulshan-e-Iqbal", 24.9180, 67.0910),
    ("Nazimabad", 24.9070, 67.0300),
    ("North Nazimabad", 24.9375, 67.0420),
    ("Gulistan-e-Jauhar", 24.9170, 67.1340),
    ("Bahadurabad", 24.8825, 67.0680),
    ("North Karachi", 24.9730, 67.0650),
    ("Shahrah-e-Faisal", 24.8610, 67.0750),
    ("Burns Road", 24.8590, 67.0155),
    ("Kharadar", 24.8530, 66.9970),
)

MAX_AREA_KM = 25.0

# Addresses are typed by restaurant owners, so one area arrives spelled several
# ways: "Gulistan e jauhar block 13" and "gulistan e Johar block 12" are the
# same place. Searching for the user's spelling alone silently loses the rest,
# which is how a Jauhar chai search came back with two restaurants.
# Values are lowercase substrings matched against address / name / area text.
# Spellings are taken from the scraped addresses themselves, not guessed:
# see scripts/area_spellings.py (johar 18, jauhar 5, jouhar 2).
AREA_ALIASES: dict[str, tuple[str, ...]] = {
    "Gulistan-e-Jauhar": ("jauhar", "johar", "jouhar"),
    "Gulshan-e-Iqbal": ("gulshan", "gulshan e iqbal"),
    "North Nazimabad": ("north nazimabad",),
    "Shahrah-e-Faisal": ("shahrah e faisal", "shahra e faisal", "sharea faisal"),
    "DHA Phase 5": ("dha phase 5", "dha"),
    "Tariq Road": ("tariq road",),
    "SMCHS": ("smchs",),
}


def normalize_area_text(text: str) -> str:
    """Lowercase and turn punctuation into single spaces."""
    lowered = "".join(
        ch if ch.isalnum() else " " for ch in (text or "").lower()
    )
    return " ".join(lowered.split())


def area_search_terms(location: str) -> list[str]:
    """Substrings that identify `location` in free-typed address text.

    "Jauhar", "Gulistan-e-Jauhar" and "johar" all return the same variants, so
    the caller matches every spelling present in the snapshot.
    """
    normalized = normalize_area_text(location)
    if not normalized:
        return []
    for canonical, aliases in AREA_ALIASES.items():
        canonical_norm = normalize_area_text(canonical)
        candidates = (canonical_norm, *aliases)
        if any(
            term in normalized or normalized in term
            for term in candidates
        ):
            terms = {canonical_norm, *aliases}
            return sorted(terms, key=len)
    return [normalized]


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def nearest_area(lat: float, lng: float) -> tuple[str, float] | None:
    """Return (area_name, distance_km) or None if outside snapshot coverage."""
    best_name = ""
    best_km = float("inf")
    for name, alat, alng in KARACHI_AREAS:
        km = haversine_km(lat, lng, alat, alng)
        if km < best_km:
            best_km = km
            best_name = name
    if best_km > MAX_AREA_KM:
        return None
    return best_name, best_km
