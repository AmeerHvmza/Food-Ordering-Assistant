"""The Karachi pins every crawl is allowed to use.

Single source of truth so the one-shot discovery scripts and the daily refresh
cannot drift apart. That matters more than it looks: disco returns vendors that
deliver *to the queried point*, so a vendor discovered from a Gulshan pin is
generally not in a Saddar-centred feed at all. Refreshing from a different pin
set than discovery used would silently make most of the dataset look missing.

`canonical_area` maps a pin label to the area name stored in
`restaurants.delivery_areas`, which the agent's area search reads.
"""

from __future__ import annotations

Pin = tuple[str, float, float]

# Well-known expansion pins (NOTES §7). Korangi / Malir / Landhi were dropped
# for empty or near-empty listings.
WELLKNOWN_PINS: list[Pin] = [
    ("Saddar", 24.8607, 67.0011),
    ("Clifton", 24.8138, 67.0234),
    ("DHA Phase 5", 24.8030, 67.0550),
    ("PECHS / Tariq Road", 24.8736, 67.0554),
    ("Gulshan-e-Iqbal", 24.9180, 67.0910),
    ("Nazimabad", 24.9070, 67.0300),
    ("North Nazimabad", 24.9375, 67.0420),
    ("Gulistan-e-Jauhar", 24.9170, 67.1340),
    ("Bahadurabad", 24.8825, 67.0680),
    ("North Karachi", 24.9730, 67.0650),
    ("Shahrah-e-Faisal", 24.8610, 67.0750),
]

# Gulshan + Jauhar full-coverage pins (NOTES §9).
GULSHAN_JAUHAR_PINS: list[Pin] = [
    ("Gulshan-e-Iqbal NIPA / Block 5", 24.9180, 67.0910),
    ("Gulshan-e-Iqbal Hasan Square", 24.9075, 67.0770),
    ("Gulistan-e-Jauhar Chowk", 24.9170, 67.1340),
    # Suspect coordinate: returned 1 vendor in the 2026-08-21 midday coverage
    # crawl, while the three other Jauhar pins returned 247-348 at the same
    # hour. Almost certainly outside delivery coverage rather than a
    # time-of-day effect. Kept because the original 145-row Jauhar discovery
    # ran with it; verify or replace before enabling the daily schedule.
    ("Gulistan-e-Jauhar east / Block 18-19", 24.9060, 67.1450),
]

# Extra Jauhar pins. Al-Asif Square on Superhighway is Sohrab Goth, not used.
EXTRA_JAUHAR_PINS: list[Pin] = [
    ("Gulistan-e-Jauhar Johar Mor / Block 19", 24.9047, 67.1144),
    ("Gulistan-e-Jauhar Rabia City / Block 18", 24.9072, 67.1331),
    ("Gulistan-e-Jauhar north / Blocks 1-4", 24.9280, 67.1400),
]

# Label prefix -> the canonical area recorded in restaurants.delivery_areas.
# Longest prefix wins, so the specific Gulshan/Jauhar pins fold into the two
# area names the agent's search already knows.
_CANONICAL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Gulistan-e-Jauhar", "Gulistan-e-Jauhar"),
    ("Gulshan-e-Iqbal", "Gulshan-e-Iqbal"),
)


def refresh_pins() -> list[Pin]:
    """Every pin the daily refresh crawls, deduped by coordinate.

    Union of both discovery batches: the daily job has to be able to see any
    vendor either of them could have inserted.
    """
    seen: set[tuple[float, float]] = set()
    pins: list[Pin] = []
    for label, lat, lng in (
        WELLKNOWN_PINS + GULSHAN_JAUHAR_PINS + EXTRA_JAUHAR_PINS
    ):
        key = (round(lat, 6), round(lng, 6))
        if key in seen:
            continue
        seen.add(key)
        pins.append((label, lat, lng))
    return pins


def canonical_area(label: str) -> str:
    """Area name to store for a pin label."""
    for prefix, area in _CANONICAL_PREFIXES:
        if label.startswith(prefix):
            return area
    return label


def canonical_areas(labels: object) -> list[str]:
    """Sorted, deduped canonical areas for an iterable of pin labels."""
    if not labels:
        return []
    out = {canonical_area(str(label)) for label in labels if str(label).strip()}
    return sorted(out)
