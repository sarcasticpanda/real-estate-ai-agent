"""
POI finder — two modes:

BATCH MODE (used during property enrichment):
  Uses local curated Lucknow POI database (lucknow_pois.py).
  Instant, offline, no API calls, no rate limits.
  find_nearby_pois(lat, lng)

QUERY-TIME MODE (used when user asks "near <specific place>"):
  Geocodes the named landmark via Overpass kumi mirror.
  geocode_landmark_overpass(name, city)
"""

import math
import logging
from typing import Any
import overpy

from enrichment.lucknow_pois import ALL_POIS, FIXED_ANCHORS

logger = logging.getLogger(__name__)

# Only used for specific named-landmark queries (not batch enrichment)
_overpass = overpy.Overpass(url="https://overpass.kumi.systems/api/interpreter")


def find_nearby_pois(lat: float, lng: float) -> dict[str, Any]:
    """
    Find nearest POIs to the given coordinates using local Lucknow POI database.
    Returns distances in km for:
      - Dynamic nearest: metro, railway, hospital, school, market, airport, bus_stop, park
      - Fixed anchors: Charbagh railway, Amausi airport, Hazratganj market,
                       Aminabad market, Phoenix Palassio, Gomti Nagar market
    Instant — no network calls needed.
    """
    result: dict[str, Any] = {"enrichment_source": "local_lucknow_db"}

    # Dynamic nearest POI per category
    for poi_type, poi_list in ALL_POIS.items():
        nearest = _find_nearest_local(lat, lng, poi_list)
        if nearest:
            dist_km, name = nearest
            result[f"{poi_type}_distance_km"] = round(dist_km, 2)
            result[f"{poi_type}_name"] = name
            logger.info(f"  {poi_type}: {dist_km:.2f} km — {name}")
        else:
            result[f"{poi_type}_distance_km"] = None
            result[f"{poi_type}_name"] = None

    # Fixed anchor distances (city-wide reference landmarks)
    anchors = {}
    for anchor_key, anchor in FIXED_ANCHORS.items():
        dist_km = round(_haversine(lat, lng, anchor["lat"], anchor["lng"]), 2)
        anchors[anchor_key] = {
            "name": anchor["name"],
            "distance_km": dist_km,
        }
        logger.info(f"  [anchor] {anchor['name']}: {dist_km:.2f} km")

    result["fixed_anchors"] = anchors
    return result


def geocode_landmark_overpass(landmark_name: str, city: str = "Lucknow") -> tuple[float, float] | None:
    """
    Get coordinates of a specific named place (e.g. "CMS school Gomti Nagar").
    Used at QUERY TIME for user's specific location queries.
    """
    area_query = f"""
[out:json][timeout:15];
area["name"="Lucknow"]["admin_level"="6"]->.searchArea;
(
  node["name"~"{landmark_name}",i](area.searchArea);
  way["name"~"{landmark_name}",i](area.searchArea);
);
out center 1;
"""
    try:
        result = _overpass.query(area_query)
        if result.nodes:
            n = result.nodes[0]
            return (float(n.lat), float(n.lon))
        if result.ways and result.ways[0].center_lat:
            w = result.ways[0]
            return (float(w.center_lat), float(w.center_lon))
    except Exception as e:
        logger.warning(f"Overpass landmark lookup failed for '{landmark_name}': {e}")

    try:
        from enrichment.geocoder import geocode_area
        return geocode_area(f"{landmark_name}, {city}")
    except Exception:
        return None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _find_nearest_local(lat: float, lng: float, poi_list: list[dict]) -> tuple[float, str] | None:
    if not poi_list:
        return None
    best = min(poi_list, key=lambda p: _haversine(lat, lng, p["lat"], p["lng"]))
    return (_haversine(lat, lng, best["lat"], best["lng"]), best["name"])


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))
