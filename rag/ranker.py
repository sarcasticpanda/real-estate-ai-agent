"""
Re-ranks vector search results using a composite score.
Combines: semantic similarity + budget fit + location match + amenity match.
"""


def rank_properties(
    results: list[dict],
    requirements: dict,
) -> list[dict]:
    """
    Score and sort properties by relevance to requirements.
    Each result dict comes from Supabase match_properties RPC and has:
      - similarity (float 0-1, from pgvector cosine)
      - data (full property JSON)
    Returns sorted list with added 'score' key.
    """
    scored = []
    for r in results:
        score = _compute_score(r, requirements)
        r["score"] = score
        scored.append(r)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def _compute_score(result: dict, req: dict) -> float:
    """Weighted composite score (0–100)."""
    data = result.get("data", {})
    similarity = result.get("similarity", 0.0)

    weights = {
        "similarity": 35,
        "budget": 25,
        "bhk": 10,
        "type": 15,
        "location": 10,
        "amenities": 5,
    }

    scores = {
        "similarity": similarity * 100,
        "budget": _budget_score(data, req),
        "bhk": _bhk_score(data, req),
        "type": _type_score(data, req),
        "location": _location_score(data, req),
        "amenities": _amenity_score(data, req),
    }

    total = sum(scores[k] * weights[k] / 100 for k in weights)
    return round(total, 2)


def _budget_score(data: dict, req: dict) -> float:
    """Full score if within budget, decaying score if over/under."""
    price = (data.get("pricing") or {}).get("total_price_inr")
    max_budget = req.get("max_budget_cr")
    min_budget = req.get("min_budget_cr")

    if price is None:
        return 50.0  # unknown price — neutral

    price_cr = price / 1_00_00_000

    if max_budget and price_cr > max_budget:
        overshoot = (price_cr - max_budget) / max_budget
        return max(0, 100 - overshoot * 200)  # penalty for going over budget

    if min_budget and price_cr < min_budget * 0.5:
        return 60.0  # suspiciously cheap — slight penalty

    # Far BELOW the stated budget: a buyer asking around ₹1.5 Cr shouldn't be led
    # with ₹12 L flats. Gently de-rank well-under-budget homes so in-budget options
    # float to the top — but keep cheaper ones visible (floor ~55), since "under X"
    # buyers still legitimately want value picks.
    if max_budget:
        ratio = price_cr / max_budget
        if ratio < 0.5:
            return max(55.0, 55.0 + ratio * 90.0)  # 0.5→100, 0.25→~78, 0.08→~62

    return 100.0


def _bhk_score(data: dict, req: dict) -> float:
    req_bhk = req.get("bhk")
    if req_bhk is None:
        return 100.0
    prop_bhk = (data.get("property_profile") or {}).get("bhk")
    if prop_bhk is None:
        return 50.0
    diff = abs(prop_bhk - req_bhk)
    return max(0, 100 - diff * 30)


def _type_score(data: dict, req: dict) -> float:
    req_type = (req.get("property_type") or "").lower().strip()
    if not req_type:
        return 100.0
    prop_type = ((data.get("property_profile") or {}).get("property_type") or "").lower().strip()
    if not prop_type:
        return 50.0
    if req_type == prop_type:
        return 100.0
    # Villa/house/bungalow are close relatives
    villa_group = {"villa", "house", "bungalow", "independent house", "independent"}
    flat_group = {"flat", "apartment"}
    if req_type in villa_group and prop_type in villa_group:
        return 75.0
    if req_type in flat_group and prop_type in flat_group:
        return 75.0
    return 15.0  # wrong category — heavy penalty to push non-matching types to the back


def _location_score(data: dict, req: dict) -> float:
    req_area = (req.get("area") or "").lower()
    if not req_area:
        return 100.0
    prop_area = ((data.get("location") or {}).get("area_name") or "").lower()
    if req_area in prop_area or prop_area in req_area:
        return 100.0
    return 40.0


def _amenity_score(data: dict, req: dict) -> float:
    """Score based on how many requested amenities the property has."""
    req_amenities = [a.lower() for a in (req.get("amenities") or [])]
    req_nearby = [n.lower() for n in (req.get("nearby") or [])]
    all_wanted = req_amenities + req_nearby

    if not all_wanted:
        return 100.0

    prop_amenities = [a.lower() for a in (data.get("amenities") or [])]
    conn = data.get("connectivity") or {}

    matches = 0
    for want in all_wanted:
        # Check property amenities list
        if any(want in a for a in prop_amenities):
            matches += 1
            continue
        # Check connectivity (metro, railway, hospital, school)
        if "metro" in want and conn.get("metro_distance_km") is not None and conn["metro_distance_km"] < 3:
            matches += 1
        elif "hospital" in want and conn.get("hospital_distance_km") is not None and conn["hospital_distance_km"] < 3:
            matches += 1
        elif "school" in want and conn.get("school_distance_km") is not None and conn["school_distance_km"] < 2:
            matches += 1
        elif "railway" in want and conn.get("railway_distance_km") is not None and conn["railway_distance_km"] < 5:
            matches += 1

    return (matches / len(all_wanted)) * 100
