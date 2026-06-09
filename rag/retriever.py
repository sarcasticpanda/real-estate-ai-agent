"""
Hybrid RAG retriever — two search modes:

MODE 1 — General search (e.g. "near metro", "near hospital"):
  Stored distances (enriched at ingestion) + semantic similarity via pgvector.
  Fast, no extra API calls.

MODE 2 — Specific named-location search (e.g. "near CMS school Gomti Nagar"):
  Geocodes the named landmark at query time → calculates exact km distance
  from that landmark to every candidate property (using their stored lat/lng).
  Used when user names a specific place.
"""

import math
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from embeddings.embedding_model import embed_text
from database.supabase_client import search_properties
from rag.ranker import rank_properties

logger = logging.getLogger(__name__)

CRORE = 1_00_00_000


# ── Main entry point ─────────────────────────────────────────────────────────

def retrieve(
    query: str,
    requirements: dict,
    top_k: int = 5,
    match_threshold: float = 0.25,
) -> list[dict]:
    """
    Unified retrieval. Automatically handles both general and specific queries.

    requirements dict keys:
      city, area, bhk, max_budget_cr, min_budget_cr,
      amenities, nearby (list of strings),
      named_landmark (str) — set when user asks "near <specific place>"
      named_landmark_max_km (float) — max acceptable distance to that landmark
    """
    query_embedding = embed_text(query)

    max_price = int(requirements["max_budget_cr"] * CRORE) if requirements.get("max_budget_cr") else None
    min_price = int(requirements["min_budget_cr"] * CRORE) if requirements.get("min_budget_cr") else None

    # Fetch more candidates than needed so we can re-rank
    raw_results = search_properties(
        query_embedding=query_embedding,
        match_threshold=match_threshold,
        match_count=top_k * 4,
        filter_city=requirements.get("city", "Lucknow"),
        filter_max_price=max_price,
        filter_min_price=min_price,
        filter_bhk=requirements.get("bhk"),
        filter_area=requirements.get("area"),
    )

    logger.info(f"Vector search: {len(raw_results)} candidates")

    if not raw_results:
        # Broaden search — remove BHK + area filters
        logger.info("Broadening search (relaxed filters)...")
        raw_results = search_properties(
            query_embedding=query_embedding,
            match_threshold=match_threshold * 0.6,
            match_count=top_k * 3,
            filter_city=requirements.get("city", "Lucknow"),
            filter_max_price=max_price,
        )

    if not raw_results:
        return []

    # MODE 2: If user specified a named landmark, inject real-time distances
    named_landmark = requirements.get("named_landmark")
    if named_landmark:
        raw_results = _apply_named_landmark_distances(raw_results, named_landmark, requirements)

    ranked = rank_properties(raw_results, requirements)
    return ranked[:top_k]


# ── Named-landmark distance injection ────────────────────────────────────────

def _apply_named_landmark_distances(
    results: list[dict],
    landmark_name: str,
    requirements: dict,
) -> list[dict]:
    """
    Geocode the named landmark and add its exact distance to every result.
    Filters out results that are too far (> named_landmark_max_km).
    """
    from enrichment.geocoder import geocode_area

    city = requirements.get("city", "Lucknow")
    logger.info(f"Named landmark search: '{landmark_name}' in {city}")

    landmark_coords = geocode_area(f"{landmark_name}, {city}", city)
    if not landmark_coords:
        logger.warning(f"Could not geocode landmark '{landmark_name}' — skipping distance filter")
        return results

    lm_lat, lm_lng = landmark_coords
    max_km = requirements.get("named_landmark_max_km", 5.0)  # default 5 km radius
    logger.info(f"Landmark coords: {lm_lat:.4f},{lm_lng:.4f} | max radius: {max_km} km")

    filtered = []
    for r in results:
        conn = (r.get("data") or {}).get("connectivity") or {}
        prop_lat = conn.get("latitude")
        prop_lng = conn.get("longitude")

        if prop_lat and prop_lng:
            dist = _haversine(lm_lat, lm_lng, prop_lat, prop_lng)
            r["named_landmark_distance_km"] = round(dist, 2)
            r["named_landmark"] = landmark_name
            if dist <= max_km:
                filtered.append(r)
            else:
                logger.debug(f"  {r.get('id')} is {dist:.1f} km from {landmark_name} — filtered out")
        else:
            # No coords stored — include anyway (can't filter, will rank lower)
            r["named_landmark_distance_km"] = None
            filtered.append(r)

    logger.info(f"After landmark filter: {len(filtered)}/{len(results)} properties within {max_km} km of '{landmark_name}'")
    return filtered if filtered else results  # fallback: return all if everything filtered out


# ── Formatting for LLM ───────────────────────────────────────────────────────

def format_properties_for_llm(properties: list[dict]) -> str:
    """Convert ranked property list into readable text for the LLM prompt."""
    lines = []
    for i, r in enumerate(properties, 1):
        data = r.get("data", {})
        profile = data.get("property_profile", {})
        location = data.get("location", {})
        pricing = data.get("pricing", {})
        amenities = data.get("amenities", [])
        conn = data.get("connectivity", {})
        floor = profile.get("floor_info", {})

        price = pricing.get("total_price_inr")
        price_str = f"Rs.{price / CRORE:.2f} Cr" if price else "Price on request"

        conn_parts = []
        for key, label in [
            ("metro_distance_km", "Metro"),
            ("railway_distance_km", "Railway"),
            ("hospital_distance_km", "Hospital"),
            ("school_distance_km", "School"),
            ("bus_stop_distance_km", "Bus Stop"),
            ("market_distance_km", "Market"),
            ("airport_distance_km", "Airport"),
        ]:
            val = conn.get(key)
            if val is not None:
                name_key = key.replace("_distance_km", "_name")
                name = conn.get(name_key, "")
                name_str = f" ({name})" if name else ""
                conn_parts.append(f"{label}{name_str}: {val} km")

        # Named landmark distance if this was a specific-location search
        landmark_dist = r.get("named_landmark_distance_km")
        landmark_name = r.get("named_landmark")
        if landmark_dist is not None and landmark_name:
            conn_parts.insert(0, f"{landmark_name}: {landmark_dist} km")

        lines.append(
            f"Property {i}:\n"
            f"  Type: {profile.get('bhk')} BHK {profile.get('property_type')}\n"
            f"  Location: {location.get('area_name')}, {location.get('city')}\n"
            f"  Price: {price_str}\n"
            f"  Area: {profile.get('builtup_area_sqft')} sqft\n"
            f"  Floor: {floor.get('current_floor')}/{floor.get('total_floors')}\n"
            f"  Furnishing: {profile.get('furnishing')}\n"
            f"  Amenities: {', '.join(amenities[:8])}\n"
            f"  Nearby: {', '.join(conn_parts) or 'N/A'}\n"
            f"  Match Score: {r.get('score', 0):.1f}/100\n"
        )
    return "\n".join(lines)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))
