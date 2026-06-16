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
    exclude_ids: list | None = None,
) -> list[dict]:
    """
    Unified retrieval. Automatically handles both general and specific queries.

    requirements dict keys:
      city, area, bhk, max_budget_cr, min_budget_cr,
      amenities, nearby (list of strings),
      named_landmark (str) — set when user asks "near <specific place>"
      named_landmark_max_km (float) — max acceptable distance to that landmark

    exclude_ids: property IDs to skip (already shown to user in this session).
    """
    query_embedding = embed_text(query)

    max_price = int(requirements["max_budget_cr"] * CRORE) if requirements.get("max_budget_cr") else None
    min_price = int(requirements["min_budget_cr"] * CRORE) if requirements.get("min_budget_cr") else None
    # 25% buffer so nearby-priced properties are visible; ranker penalises over-budget ones.
    max_price_search = int(max_price * 1.25) if max_price else None

    city = requirements.get("city", "Lucknow") or "Lucknow"
    if city.lower() != "lucknow":
        city = "Lucknow"

    # Strip spaces so "Gomti Nagar" → "GomtiNagar" for ILIKE matching
    area = requirements.get("area")
    if area:
        area = area.replace(" ", "")

    logger.info(f"Retrieve: area={area} bhk={requirements.get('bhk')} budget≤{max_price} exclude={len(exclude_ids or [])}")

    # For a "near <landmark>" search, GEOGRAPHIC distance — not vector similarity — is the
    # ranking signal. If we only fetched the top-N most semantically similar, the actually
    # closest homes could be cut off before their distance is ever computed. So cast a wide
    # net (essentially the whole city's matching inventory) and lower the threshold; the
    # haversine distance filter does the real selecting.
    has_landmark = bool(requirements.get("named_landmark"))
    if has_landmark:
        fetch_count = 500
        effective_threshold = min(match_threshold, 0.05)
    else:
        fetch_count = max(top_k * 6, 30)
        effective_threshold = match_threshold

    raw_results = search_properties(
        query_embedding=query_embedding,
        match_threshold=effective_threshold,
        match_count=fetch_count,
        filter_city=city,
        filter_max_price=max_price_search,
        filter_min_price=min_price,
        filter_bhk=requirements.get("bhk"),
        filter_area=area,
    )

    logger.info(f"Vector search: {len(raw_results)} candidates (with area filter)")

    if not raw_results:
        # Broaden: drop area + BHK filters, keep budget
        logger.info("Broadening search (no area/BHK filter)...")
        raw_results = search_properties(
            query_embedding=query_embedding,
            match_threshold=effective_threshold * 0.6,
            match_count=fetch_count,
            filter_city=city,
            filter_max_price=max_price_search,
        )

    if not raw_results:
        return []

    # Deduplicate by property ID (DB may have duplicate rows after re-embedding)
    seen: set = set()
    deduped = []
    for r in raw_results:
        pid = r.get("id")
        if pid and pid not in seen:
            seen.add(pid)
            deduped.append(r)
    raw_results = deduped

    # Exclude already-shown property IDs so "more options" returns fresh results
    if exclude_ids:
        excl = set(exclude_ids)
        raw_results = [r for r in raw_results if r.get("id") not in excl]
        logger.info(f"After exclusion: {len(raw_results)} candidates remaining")

    if not raw_results:
        return []

    # MODE 2: Named landmark → inject real-time distances
    named_landmark = requirements.get("named_landmark")
    landmark_found = False
    if named_landmark:
        filtered = _apply_named_landmark_distances(raw_results, named_landmark, requirements)
        # Tag results so the LLM knows if landmark was found or not
        if filtered and any(r.get("named_landmark_distance_km") is not None for r in filtered):
            raw_results = filtered  # landmark found and distances injected
            landmark_found = True
        else:
            # Geocoding failed — tag results so LLM can explain gracefully
            for r in raw_results:
                r["named_landmark_not_found"] = named_landmark
            raw_results = filtered

    ranked = rank_properties(raw_results, requirements)
    ranked = _apply_connectivity_filter(ranked, requirements)

    # For a "near <landmark>" search the buyer wants the CLOSEST homes first.
    # Sort by the live-computed distance (properties without a distance go last).
    if landmark_found:
        ranked.sort(key=lambda r: r.get("named_landmark_distance_km")
                    if r.get("named_landmark_distance_km") is not None else 1e9)

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
        # Tag every result so the agent/LLM can honestly tell the buyer we couldn't
        # locate the landmark, rather than silently showing unrelated properties.
        for r in results:
            r["named_landmark_not_found"] = landmark_name
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
        if price:
            if price >= CRORE:
                cr = price / CRORE
                price_str = f"Rs.{cr:.2g} Cr" if cr != int(cr) else f"Rs.{int(cr)} Cr"
            else:
                lakh = price / 100_000
                price_str = f"Rs.{lakh:.0f} lakh" if lakh == int(lakh) else f"Rs.{lakh:.1f} lakh"
        else:
            price_str = "Price on request"

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
            f"  Furnishing: {profile.get('furnishing')}\n"
            f"  Top amenities: {', '.join(amenities[:4])}\n"
            f"  Nearby: {', '.join(conn_parts[:3]) or 'N/A'}\n"
        )
    return "\n".join(lines)


def to_card(r: dict) -> dict:
    """Convert a ranked result into a structured property card dict for the web UI."""
    data = r.get("data", {})
    profile  = data.get("property_profile", {})
    location = data.get("location", {})
    pricing  = data.get("pricing", {})
    conn     = data.get("connectivity", {})
    amenities = data.get("amenities", [])
    floor    = profile.get("floor_info", {})

    price = pricing.get("total_price_inr")
    if price:
        if price >= CRORE:
            cr = price / CRORE
            price_str = f"Rs.{cr:.2g} Cr" if cr != int(cr) else f"Rs.{int(cr)} Cr"
        else:
            lakh = price / 100_000
            price_str = f"Rs.{lakh:.0f} lakh" if lakh == int(lakh) else f"Rs.{lakh:.1f} lakh"
    else:
        price_str = "Price on request"

    # Ensure Unsplash images have CDN params so browsers load them without redirect
    raw_images = data.get("images") or []
    images = []
    for url in raw_images[:4]:
        if "unsplash.com/photo-" in url and "?" not in url:
            url = url + "?w=800&q=80&fit=crop&auto=format"
        images.append(url)

    connectivity = {}
    for key, label in [("metro", "Metro"), ("hospital", "Hospital"), ("school", "School"), ("market", "Market"), ("bus_stop", "Bus")]:
        dist = conn.get(f"{key}_distance_km")
        if dist is not None:
            connectivity[label] = f"{dist} km"

    cur_f = floor.get("current_floor")
    tot_f = floor.get("total_floors")

    card = {
        "id": r.get("id", ""),
        "bhk": profile.get("bhk"),
        "property_type": profile.get("property_type", ""),
        "area": location.get("area_name", ""),
        "city": location.get("city", "Lucknow"),
        "price_str": price_str,
        "price_inr": int(price) if price else None,
        "sqft": profile.get("builtup_area_sqft"),
        "furnishing": profile.get("furnishing"),
        "floor": f"{cur_f}/{tot_f}" if cur_f and tot_f else None,
        "facing": profile.get("facing"),
        "age": profile.get("construction_age"),
        "top_amenities": [a for a in amenities[:10] if a],
        "connectivity": connectivity,
        "images": images,
        "score": round(r.get("score", 0), 1),
    }

    # Live distance to a buyer-named landmark (computed this query) — show it prominently.
    lm_name = r.get("named_landmark")
    lm_dist = r.get("named_landmark_distance_km")
    if lm_name and lm_dist is not None:
        card["landmark_name"] = lm_name.title()  # tidy display ("cms ..." → "Cms ...")
        card["landmark_distance_km"] = lm_dist

    # Broker-uploaded documents (floor plan, brochure, papers) — shown to the buyer.
    docs = data.get("documents") or []
    if docs:
        card["documents"] = [{"url": d.get("url"), "label": d.get("label", "Document")}
                             for d in docs if d.get("url")]

    # A map link the buyer can tap (uses exact coords if we have them, else the area).
    lat, lng = conn.get("latitude"), conn.get("longitude")
    if lat and lng:
        card["map_url"] = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
    elif location.get("area_name"):
        q = f"{location.get('area_name')}, {location.get('city', 'Lucknow')}".replace(" ", "+")
        card["map_url"] = f"https://www.google.com/maps/search/?api=1&query={q}"

    return card


_CONNECTIVITY_LIMITS = {
    "metro": 3.0,
    "railway": 5.0,
    "hospital": 3.0,
    "school": 2.0,
    "market": 3.0,
    "bus": 2.0,
    "bus stop": 2.0,
    "airport": 15.0,
}


def _apply_connectivity_filter(results: list[dict], requirements: dict) -> list[dict]:
    """
    Hard post-filter: remove properties that are too far from requested nearby places.
    Only applies when nearby list is non-empty and at least some properties satisfy it.
    """
    nearby = [n.lower() for n in (requirements.get("nearby") or [])]
    if not nearby:
        return results

    conn_key_map = {
        "metro": "metro_distance_km",
        "railway": "railway_distance_km",
        "hospital": "hospital_distance_km",
        "school": "school_distance_km",
        "market": "market_distance_km",
        "bus": "bus_stop_distance_km",
        "bus stop": "bus_stop_distance_km",
        "airport": "airport_distance_km",
    }

    filtered = []
    for r in results:
        conn = (r.get("data") or {}).get("connectivity") or {}
        passes = True
        for want in nearby:
            for keyword, conn_key in conn_key_map.items():
                if keyword in want:
                    limit = _CONNECTIVITY_LIMITS.get(keyword, 5.0)
                    dist = conn.get(conn_key)
                    if dist is not None and dist > limit:
                        passes = False
                    break
        if passes:
            filtered.append(r)

    # Only apply filter if at least 2 properties pass — otherwise return unfiltered
    return filtered if len(filtered) >= 2 else results


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))
