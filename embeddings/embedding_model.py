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

    Preprocessing goals:
    - Include raw INR number AND human-readable crore/lakh/lac so the embedding
      model can match queries like "15 lakh", "15 lac", "1500000" to the same property
    - Include property-type synonyms so "flat", "apartment", "house", "bungalow" all match
    - Include area name with and without spaces ("Gomti Nagar" + "GomtiNagar")
    - Lowercase amenities for consistent tokenisation
    - Add a tier tag ("budget", "affordable", "mid-range", "premium", "luxury") so
      natural-language budget queries can find semantically similar properties
    - All important numbers in both digit and word form
    """
    profile   = prop.get("property_profile", {})
    location  = prop.get("location", {})
    pricing   = prop.get("pricing", {})
    conn      = prop.get("connectivity", {})
    amenities = prop.get("amenities", [])
    floor     = profile.get("floor_info", {})

    bhk        = profile.get("bhk", "")
    ptype      = (profile.get("property_type") or "property").lower().strip()
    area       = (location.get("area_name") or "").strip()
    city       = (location.get("city") or "Lucknow").strip()
    price      = pricing.get("total_price_inr")
    sqft       = profile.get("builtup_area_sqft", "")
    furnishing = (profile.get("furnishing") or "").strip()
    facing     = (profile.get("facing") or "").strip()
    age        = (profile.get("construction_age") or "").strip()
    cur_floor  = floor.get("current_floor", "")
    tot_floor  = floor.get("total_floors", "")

    # ── Price: raw number + crore + lakh + lac spellings ─────────────────────
    price_str = _format_price_full(price) if price else "price on request"

    # ── Property type synonyms ────────────────────────────────────────────────
    type_synonyms = {
        "flat":    "flat apartment 2d",
        "house":   "house bungalow independent house",
        "villa":   "villa luxury house independent bungalow",
        "plot":    "plot land residential plot",
        "shop":    "shop commercial space",
        "office":  "office commercial",
    }
    ptype_str = type_synonyms.get(ptype, ptype)

    # ── Area: with and without spaces ────────────────────────────────────────
    area_nospace = area.replace(" ", "")
    area_str = f"{area} {area_nospace}" if area_nospace != area else area

    # ── Furnishing in natural-language form ───────────────────────────────────
    furnishing_map = {
        "furnished":      "fully furnished furnished",
        "semi-furnished": "semi furnished semi-furnished",
        "unfurnished":    "unfurnished bare unfurnished",
    }
    furnishing_str = furnishing_map.get(furnishing.lower(), furnishing)

    # ── Amenities lowercase for consistent tokenisation ───────────────────────
    amenity_list = [a.lower().strip() for a in amenities[:14] if a]
    amenity_str = " | ".join(amenity_list) if amenity_list else "basic amenities"

    # ── POI distances ─────────────────────────────────────────────────────────
    conn_parts: list[str] = []
    _add_poi(conn_parts, conn, "metro",    "metro station")
    _add_poi(conn_parts, conn, "railway",  "railway station train")
    _add_poi(conn_parts, conn, "hospital", "hospital")
    _add_poi(conn_parts, conn, "school",   "school")
    _add_poi(conn_parts, conn, "bus_stop", "bus stop")
    _add_poi(conn_parts, conn, "market",   "market")
    _add_poi(conn_parts, conn, "park",     "park garden")
    _add_poi(conn_parts, conn, "airport",  "airport")
    conn_str = " | ".join(conn_parts) if conn_parts else ""

    # ── Landmark anchors ──────────────────────────────────────────────────────
    anchor_parts: list[str] = []
    for anchor in (conn.get("fixed_anchors") or {}).values():
        name = anchor.get("name", "")
        dist = anchor.get("distance_km")
        if name and dist is not None:
            anchor_parts.append(f"{name} {dist} km")
    anchor_str = " | ".join(anchor_parts) if anchor_parts else ""

    # ── Assemble ──────────────────────────────────────────────────────────────
    parts = [
        f"{bhk} BHK {ptype_str} in {area_str}, {city}.",
        f"Price: {price_str}.",
        f"Area: {sqft} sqft {_sqft_label(sqft)}." if sqft else "",
        f"Furnishing: {furnishing_str}." if furnishing_str else "",
        f"Floor {cur_floor} of {tot_floor}." if cur_floor and tot_floor else "",
        f"Facing: {facing}." if facing else "",
        f"Age: {age}." if age else "",
        f"Amenities: {amenity_str}.",
        f"Nearby: {conn_str}." if conn_str else "",
        f"Landmarks: {anchor_str}." if anchor_str else "",
    ]
    return " ".join(p for p in parts if p)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_price_full(price_inr: int | float) -> str:
    """
    Return price in all searchable forms:
      15000000 → "15000000 INR 15 lakh 15 lac 1.5 crore 1.5 cr"
      35000000 → "35000000 INR 3.5 crore 3.5 cr 350 lakh 350 lac"
    This lets the embedding model match whatever unit the user speaks in.
    """
    p = int(price_inr)
    crore_val = p / 10_000_000          # e.g. 1.5 for 1.5 crore
    lakh_val  = p / 100_000             # e.g. 150 for 150 lakh

    parts = [str(p), "INR"]

    # Always include lakh form
    if lakh_val == int(lakh_val):
        lakh_str = f"{int(lakh_val)}"
    else:
        lakh_str = f"{lakh_val:.1f}"
    parts += [f"{lakh_str} lakh", f"{lakh_str} lac", f"{lakh_str} lakhs"]

    # Always include crore form
    if crore_val >= 0.1:
        if crore_val == int(crore_val):
            cr_str = f"{int(crore_val)}"
        else:
            cr_str = f"{crore_val:.2g}"
        parts += [f"{cr_str} crore", f"{cr_str} cr", f"{cr_str} crores"]

    # Add tier tag based on price (helps semantic search for "budget", "luxury" etc.)
    tier = _price_tier(p)
    parts.append(tier)

    return " ".join(parts)


def _price_tier(price_inr: int) -> str:
    if price_inr < 40_00_000:       # < 40 lakh
        return "budget affordable cheap low-cost"
    elif price_inr < 1_00_00_000:   # 40L – 1Cr
        return "affordable mid-range standard"
    elif price_inr < 2_50_00_000:   # 1Cr – 2.5Cr
        return "premium mid-luxury"
    elif price_inr < 5_00_00_000:   # 2.5Cr – 5Cr
        return "luxury high-end"
    else:
        return "ultra-luxury villa premium luxury"


def _sqft_label(sqft) -> str:
    """Add qualitative size label so 'big flat' queries find large properties."""
    try:
        s = int(sqft)
        if s < 600:   return "compact small"
        if s < 1000:  return "medium"
        if s < 1500:  return "spacious large"
        return "very spacious extra large"
    except (ValueError, TypeError):
        return ""


def _add_poi(parts: list[str], conn: dict, key: str, label: str) -> None:
    dist = conn.get(f"{key}_distance_km")
    name = conn.get(f"{key}_name")
    if dist is not None:
        entry = f"{label} {dist} km"
        if name:
            entry += f" ({name})"
        parts.append(entry)
