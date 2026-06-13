"""
Geocoder — returns (lat, lng) for a property location.

Strategy (fastest/most accurate first):
  1. Direct match against AREA_COORDS (hardcoded + loaded from areas.csv) — instant
  2. Nominatim (OpenStreetMap) with clean area-only query — for unknown areas
  3. Never fall back to bare city coords (that was giving us Lucknow city center for everything)
"""

import csv
import time
import logging
import requests
from pathlib import Path
from functools import lru_cache
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

logger = logging.getLogger(__name__)

# Lucknow centre — used to bias Photon results toward the city.
_LKO_LAT, _LKO_LNG = 26.85, 80.95

# Known Lucknow area coordinates (lat, lng) — curated, accurate
AREA_COORDS = {
    "gomtinagar extension":  (26.850, 81.020),
    "gomti nagar extension": (26.850, 81.020),
    "gomtinagar":            (26.858, 80.997),
    "gomti nagar":           (26.858, 80.997),
    "ashiana":               (26.790, 80.920),
    "alambagh":              (26.802, 80.908),
    "hazratganj":            (26.859, 80.946),
    "chowk":                 (26.869, 80.912),
    "thakurganj":            (26.865, 80.925),
    "para":                  (26.802, 80.908),
    "indira nagar":          (26.883, 81.002),
    "indiranagar":           (26.883, 81.002),
    "aliganj":               (26.885, 80.966),
    "mahanagar":             (26.878, 80.954),
    "rajajipuram":           (26.855, 80.883),
    "chinhat":               (26.862, 81.066),
    "vibhuti khand":         (26.862, 81.004),
    "shaheed path":          (26.840, 81.050),
    "sushant golf city":     (26.780, 80.980),
    "sultanpur road":        (26.770, 80.990),
    "faizabad road":         (26.880, 81.050),
    "kanpur road":           (26.820, 80.870),
    "sitapur road":          (26.900, 80.940),
    "hardoi road":           (26.920, 80.960),
}


def _load_areas_csv() -> None:
    """Load additional coordinates from areas.csv into AREA_COORDS."""
    csv_path = Path(__file__).resolve().parent.parent / "csv_container" / "areas.csv"
    if not csv_path.exists():
        return
    try:
        with open(csv_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                lat = row.get("latitude", "").strip()
                lng = row.get("longitude", "").strip()
                name = row.get("area_name_normalized", "").strip().replace("_", " ")
                original = row.get("area_name_original", "").strip().lower()
                if lat and lng and name:
                    AREA_COORDS[name] = (float(lat), float(lng))
                if lat and lng and original:
                    AREA_COORDS[original] = (float(lat), float(lng))
    except Exception as e:
        logger.warning(f"Could not load areas.csv: {e}")


_load_areas_csv()

_geolocator = Nominatim(user_agent="real_estate_ai_agent/1.0")


def get_area_coords(area_name: str) -> tuple[float, float] | None:
    """
    Primary function for property enrichment.
    Looks up area name in AREA_COORDS first (fast, accurate),
    then falls back to Nominatim only for genuinely unknown areas.
    """
    if not area_name:
        return None

    # Direct dict lookup (case-insensitive substring matching)
    coords = _match_area_coords(area_name)
    if coords:
        logger.info(f"Area coords (local): '{area_name}' -> {coords}")
        return coords

    # Nominatim fallback for areas not in our dict
    query = f"{area_name}, Lucknow, Uttar Pradesh, India"
    coords = _try_nominatim(query)
    if coords:
        logger.info(f"Area coords (Nominatim): '{area_name}' -> {coords}")
        AREA_COORDS[area_name.lower()] = coords  # cache for next time
        return coords

    logger.warning(f"Could not get coords for area: '{area_name}'")
    return None


def geocode_address(address: str, city: str = "Lucknow", state: str = "Uttar Pradesh") -> tuple[float, float] | None:
    """
    Geocode a full address string.
    First extracts locality from address and looks up in AREA_COORDS.
    """
    if not address:
        return None

    # Try to extract the locality (first part before comma, or before "Landmarks:")
    locality = _extract_locality(address)
    if locality:
        coords = _match_area_coords(locality)
        if coords:
            logger.info(f"Geocoded via locality '{locality}' -> {coords}")
            return coords

    # Try Nominatim with the extracted locality + city
    if locality and locality.lower() not in ("lucknow", city.lower()):
        query = f"{locality}, {city}, {state}, India"
        coords = _try_nominatim(query)
        if coords:
            return coords
        time.sleep(1.1)

    # Last resort: match anywhere in the address string
    return _match_area_coords(address)


def geocode_area(area_name: str, city: str = "Lucknow") -> tuple[float, float] | None:
    """
    Used by the named-landmark query-time geocoding ("near Tunday Kababi", "near Lulu Mall").

    Order: known-area dict → Photon (OSM, typo-tolerant POI search) → Nominatim fallback.
    Photon resolves named POIs/landmarks (restaurants, malls, stadiums, hospitals, schools)
    that Nominatim frequently misses, without any hardcoded landmark list. Successful lookups
    are cached at runtime so repeat queries are instant.
    """
    # 1. Known neighbourhood? (instant, for area-style inputs)
    coords = _match_area_coords(area_name)
    if coords:
        return coords

    # 2. Runtime cache (self-builds from Photon/Nominatim hits this process)
    key = area_name.strip().lower()
    if key in _LANDMARK_CACHE:
        return _LANDMARK_CACHE[key]

    # 3. Photon — best for named landmarks/POIs
    coords = _try_photon(f"{area_name} {city}")
    if coords:
        _LANDMARK_CACHE[key] = coords
        logger.info(f"Landmark coords (Photon): '{area_name}' -> {coords}")
        return coords

    # 4. Nominatim fallback
    time.sleep(1.1)
    coords = _try_nominatim(f"{area_name}, {city}, India")
    if coords:
        _LANDMARK_CACHE[key] = coords
        logger.info(f"Landmark coords (Nominatim): '{area_name}' -> {coords}")
    return coords


_LANDMARK_CACHE: dict[str, tuple[float, float]] = {}


def _try_photon(query: str) -> tuple[float, float] | None:
    """
    Geocode via Photon (komoot, free, OSM-based, no API key). Biased to Lucknow.
    Requires a User-Agent header or the public instance returns a non-JSON error page.
    """
    try:
        resp = requests.get(
            "https://photon.komoot.io/api",
            params={"q": query, "limit": 1, "lat": _LKO_LAT, "lon": _LKO_LNG},
            headers={"User-Agent": "real_estate_ai_agent/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        feats = resp.json().get("features") or []
        if not feats:
            return None
        lng, lat = feats[0]["geometry"]["coordinates"]  # GeoJSON is [lon, lat]
        # Sanity: reject results far outside the Lucknow region (~1.5 deg box)
        if abs(lat - _LKO_LAT) > 1.5 or abs(lng - _LKO_LNG) > 1.5:
            logger.info(f"Photon result for '{query[:40]}' outside Lucknow — rejected")
            return None
        return (float(lat), float(lng))
    except Exception as e:
        logger.warning(f"Photon error for '{query[:40]}': {type(e).__name__}: {str(e)[:80]}")
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _match_area_coords(text: str) -> tuple[float, float] | None:
    """Substring match of text against all keys in AREA_COORDS."""
    lower = text.lower()
    # Exact key match first
    if lower in AREA_COORDS:
        return AREA_COORDS[lower]
    # Substring match (longest key wins to avoid false matches)
    best_key = None
    best_len = 0
    for key in AREA_COORDS:
        if key in lower and len(key) > best_len:
            best_key = key
            best_len = len(key)
    if best_key:
        return AREA_COORDS[best_key]
    return None


def _extract_locality(address: str) -> str | None:
    """Extract the first meaningful part of an address (before comma or 'Landmarks:')."""
    # Strip everything from "Landmarks:" onwards
    for noise in ["Landmarks:", "Near ", "Opp ", "Behind "]:
        if noise in address:
            address = address[: address.index(noise)]

    parts = [p.strip() for p in address.split(",")]
    # Return first non-empty part that isn't just a state/city name
    skip = {"lucknow", "uttar pradesh", "up", "india"}
    for part in parts:
        if part and part.lower() not in skip and len(part) > 3:
            return part
    return None


def _try_nominatim(query: str) -> tuple[float, float] | None:
    try:
        location = _geolocator.geocode(query, timeout=10)
        if location:
            return (location.latitude, location.longitude)
    except (GeocoderTimedOut, GeocoderServiceError) as e:
        logger.warning(f"Nominatim error for '{query[:50]}': {e}")
    return None
