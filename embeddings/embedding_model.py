"""
HuggingFace sentence-transformer wrapper.
Model: all-MiniLM-L6-v2 (384 dims, runs fully local, free).
First run downloads ~80MB model to ~/.cache/huggingface/
"""

import logging
from functools import lru_cache
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


@lru_cache(maxsize=1)
def get_model() -> SentenceTransformer:
    logger.info(f"Loading embedding model: {MODEL_NAME}")
    return SentenceTransformer(MODEL_NAME)


def embed_text(text: str) -> list[float]:
    """Embed a single text string → 384-dim float list."""
    model = get_model()
    vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed a list of texts efficiently in batches."""
    model = get_model()
    vectors = model.encode(
        texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=True
    )
    return [v.tolist() for v in vectors]


def build_semantic_text(prop: dict) -> str:
    """
    Build the text string that gets embedded for a property.
    Information-dense: BHK, price (human-readable crore/lakh), area, furnishing,
    amenities, nearest POIs, fixed landmark distances.
    """
    profile  = prop.get("property_profile", {})
    location = prop.get("location", {})
    pricing  = prop.get("pricing", {})
    conn     = prop.get("connectivity", {})
    amenities = prop.get("amenities", [])
    floor    = profile.get("floor_info", {})

    bhk        = profile.get("bhk", "")
    ptype      = profile.get("property_type", "Property")
    area       = location.get("area_name", "")
    city       = location.get("city", "Lucknow")
    price      = pricing.get("total_price_inr")
    sqft       = profile.get("builtup_area_sqft", "")
    furnishing = profile.get("furnishing", "")
    facing     = profile.get("facing", "")
    age        = profile.get("construction_age", "")
    cur_floor  = floor.get("current_floor", "")
    tot_floor  = floor.get("total_floors", "")

    price_str  = _format_price(price) if price else "price on request"
    amenity_str = ", ".join(amenities[:12]) if amenities else "basic amenities"

    # Dynamic nearest POIs
    conn_parts = []
    _add_poi(conn_parts, conn, "metro",    "metro")
    _add_poi(conn_parts, conn, "railway",  "railway station")
    _add_poi(conn_parts, conn, "hospital", "hospital")
    _add_poi(conn_parts, conn, "school",   "school")
    _add_poi(conn_parts, conn, "bus_stop", "bus stop")
    _add_poi(conn_parts, conn, "market",   "market")
    _add_poi(conn_parts, conn, "park",     "park")
    conn_str = ", ".join(conn_parts) if conn_parts else "connectivity details not available"

    # Fixed landmark anchors (city-wide reference distances)
    anchor_parts = []
    anchors = conn.get("fixed_anchors", {})
    for key, anchor in anchors.items():
        name = anchor.get("name", key)
        dist = anchor.get("distance_km")
        if dist is not None:
            anchor_parts.append(f"{name} {dist} km")
    anchor_str = ", ".join(anchor_parts) if anchor_parts else ""

    parts = [
        f"{bhk} BHK {ptype} in {area}, {city}.",
        f"Price: {price_str}.",
        f"Area: {sqft} sqft." if sqft else "",
        f"Furnishing: {furnishing}." if furnishing else "",
        f"Floor {cur_floor} of {tot_floor}." if cur_floor and tot_floor else "",
        f"Facing {facing}." if facing else "",
        f"Age: {age}." if age else "",
        f"Amenities: {amenity_str}.",
        f"Nearby: {conn_str}.",
        f"Landmarks: {anchor_str}." if anchor_str else "",
    ]

    return " ".join(p for p in parts if p)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_price(price_inr: int | float) -> str:
    """Convert raw INR to human-readable crore/lakh string for semantic search.
    No currency symbol — users query '1.5 crore flat' not symbol variants."""
    p = int(price_inr)
    if p >= 10_000_000:
        val = p / 10_000_000
        s = f"{val:.2g}" if val != int(val) else str(int(val))
        return f"{s} crore"
    if p >= 100_000:
        val = p / 100_000
        s = f"{val:.2g}" if val != int(val) else str(int(val))
        return f"{s} lakh"
    return str(p)


def _add_poi(parts: list[str], conn: dict, key: str, label: str) -> None:
    dist = conn.get(f"{key}_distance_km")
    name = conn.get(f"{key}_name")
    if dist is not None:
        entry = f"{label} {dist} km"
        if name:
            entry += f" ({name})"
        parts.append(entry)
