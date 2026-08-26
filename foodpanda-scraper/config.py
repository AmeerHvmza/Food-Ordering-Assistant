"""Project-wide constants and defaults for the Foodpanda scraper."""

from __future__ import annotations

# Site / region
BASE_URL = "https://www.foodpanda.pk"
COUNTRY_CODE = "pk"

# Default scrape target (Karachi)
DEFAULT_CITY = "Karachi"
DEFAULT_LAT = 24.8607
DEFAULT_LNG = 67.0011
DEFAULT_COUNT = 15
DEFAULT_DB_PATH = "foodpanda.db"

# Rate limiting between restaurant scrapes / navigations
MIN_DELAY_SEC = 1.5
MAX_DELAY_SEC = 3.0

# Playwright
NAV_TIMEOUT_MS = 15_000
DEFAULT_HEADLESS = True

# Logging
LOG_FILE = "scrape.log"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Internal API candidates (verified / updated via NOTES.md after discovery)
DISCO_VENDORS_URL = "https://disco.deliveryhero.io/listing/api/v1/pandora/vendors"
FD_API_VENDOR_URL = "https://pk.fd-api.com/api/v5/vendors/{vendor_code}"
VENDOR_MENU_URL_TEMPLATE = f"{BASE_URL}/api/v5/vendors/{{vendor_code}}/menu"

# HTTP
REQUEST_TIMEOUT_SEC = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def listing_page_url(lat: float, lng: float) -> str:
    """Build the restaurant listing page URL for a lat/lng."""
    return (
        f"{BASE_URL}/restaurants/new"
        f"?lat={lat}&lng={lng}&vertical=restaurants"
    )
