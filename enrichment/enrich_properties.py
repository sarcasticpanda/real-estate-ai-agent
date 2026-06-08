"""
Enrichment orchestrator.

Run this script to fill the NULL connectivity data (metro, hospital, school,
railway, bus stop, market, airport distances) for all properties in
rag_properties.json using free OpenStreetMap APIs.

Usage:
    python enrichment/enrich_properties.py                  # enrich all pending
    python enrichment/enrich_properties.py --limit 5        # test with 5 properties
    python enrichment/enrich_properties.py --force          # re-enrich already enriched
    python enrichment/enrich_properties.py --property PROP001  # single property

Time: ~110 properties × ~8 sec each ≈ ~15 minutes total
"""

import json
import time
import argparse
import logging
import sys
from pathlib import Path

# Allow running from project root or from enrichment/ subfolder
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from enrichment.geocoder import geocode_address, get_area_coords
from enrichment.poi_finder import find_nearby_pois

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

RAG_JSON_PATH = ROOT / "csv_container" / "rag_documents" / "rag_properties.json"
PROGRESS_PATH = ROOT / "csv_container" / "rag_documents" / "enrichment_progress.json"


def load_properties() -> list[dict]:
    with open(RAG_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_properties(properties: list[dict]) -> None:
    with open(RAG_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(properties, f, ensure_ascii=False, indent=2)


def load_progress() -> dict:
    """Track which properties have been enriched so we can resume after interruption."""
    if PROGRESS_PATH.exists():
        with open(PROGRESS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_progress(progress: dict) -> None:
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


def get_coordinates(prop: dict) -> tuple[float, float] | None:
    """
    Get lat/lng for a property.
    Strategy:
      1. Use raw_full_address + city to geocode
      2. Fall back to area_name geocoding
      3. Fall back to hardcoded area coordinates
    """
    location = prop.get("location", {})
    area_name = location.get("area_name", "")
    city = location.get("city", "Lucknow")
    state = location.get("state", "Uttar Pradesh")
    raw_address = prop.get("metadata", {}).get("raw_full_address", "")

    # Try area name lookup first (fast, accurate for known Lucknow areas)
    if area_name:
        coords = get_area_coords(area_name)
        if coords:
            return coords

    # Fall back to full address geocoding via Nominatim
    if raw_address:
        coords = geocode_address(raw_address, city, state)
        if coords:
            return coords

    logger.warning(f"Could not geocode property {prop.get('doc_id')} — skipping enrichment")
    return None


def enrich_single(prop: dict) -> dict:
    """Enrich one property with POI distances. Returns updated connectivity dict."""
    doc_id = prop.get("doc_id", "unknown")
    logger.info(f"\n{'='*50}")
    logger.info(f"Enriching: {doc_id}")

    coords = get_coordinates(prop)
    if not coords:
        return prop

    lat, lng = coords
    logger.info(f"Coordinates: {lat:.4f}, {lng:.4f}")

    poi_data = find_nearby_pois(lat, lng)

    # Update connectivity section
    prop["connectivity"] = {
        "latitude": lat,
        "longitude": lng,
        # Dynamic nearest POIs (closest of each category)
        "metro_distance_km": poi_data.get("metro_distance_km"),
        "metro_name": poi_data.get("metro_name"),
        "railway_distance_km": poi_data.get("railway_distance_km"),
        "railway_name": poi_data.get("railway_name"),
        "bus_stop_distance_km": poi_data.get("bus_stop_distance_km"),
        "bus_stop_name": poi_data.get("bus_stop_name"),
        "hospital_distance_km": poi_data.get("hospital_distance_km"),
        "hospital_name": poi_data.get("hospital_name"),
        "school_distance_km": poi_data.get("school_distance_km"),
        "school_name": poi_data.get("school_name"),
        "market_distance_km": poi_data.get("market_distance_km"),
        "market_name": poi_data.get("market_name"),
        "airport_distance_km": poi_data.get("airport_distance_km"),
        "airport_name": poi_data.get("airport_name"),
        "park_distance_km": poi_data.get("park_distance_km"),
        "park_name": poi_data.get("park_name"),
        # Fixed city-wide reference landmarks (always present)
        "fixed_anchors": poi_data.get("fixed_anchors", {}),
        "status": "enriched",
        "enrichment_source": poi_data.get("enrichment_source", "local_lucknow_db"),
    }

    return prop


def run_enrichment(limit: int | None = None, force: bool = False, target_id: str | None = None) -> None:
    properties = load_properties()
    progress = load_progress()

    total = 0
    skipped = 0
    enriched = 0
    failed = 0

    for i, prop in enumerate(properties):
        doc_id = prop.get("doc_id", f"prop_{i}")

        # Filter by target property if specified
        if target_id and target_id not in doc_id:
            continue

        # Skip already enriched unless --force
        current_status = prop.get("connectivity", {}).get("status")
        if current_status == "enriched" and not force:
            skipped += 1
            continue

        # Stop at limit
        if limit and total >= limit:
            break

        total += 1
        try:
            properties[i] = enrich_single(prop)
            enriched += 1
            progress[doc_id] = "enriched"
        except Exception as e:
            logger.error(f"Failed to enrich {doc_id}: {e}")
            failed += 1
            progress[doc_id] = "failed"

        # Save after every property so progress is never lost
        save_properties(properties)
        save_progress(progress)

        # Brief pause between properties
        time.sleep(1.5)

    logger.info(f"\n{'='*50}")
    logger.info(f"Enrichment complete.")
    logger.info(f"  Enriched: {enriched}")
    logger.info(f"  Skipped (already done): {skipped}")
    logger.info(f"  Failed: {failed}")
    logger.info(f"  Total processed: {total}")
    logger.info(f"Output: {RAG_JSON_PATH}")


def print_summary(properties: list[dict]) -> None:
    """Print enrichment status summary."""
    pending = sum(1 for p in properties if p.get("connectivity", {}).get("status") != "enriched")
    done = len(properties) - pending
    print(f"\nEnrichment Status: {done}/{len(properties)} properties enriched, {pending} pending\n")
    for p in properties[:5]:
        conn = p.get("connectivity", {})
        print(f"  {p.get('doc_id')}: metro={conn.get('metro_distance_km')} km, "
              f"hospital={conn.get('hospital_distance_km')} km, "
              f"school={conn.get('school_distance_km')} km")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enrich properties with POI distances")
    parser.add_argument("--limit", type=int, help="Process only N properties (for testing)")
    parser.add_argument("--force", action="store_true", help="Re-enrich already enriched properties")
    parser.add_argument("--property", type=str, help="Enrich a single property by ID (e.g. PROP001)")
    parser.add_argument("--status", action="store_true", help="Show enrichment status and exit")
    args = parser.parse_args()

    if args.status:
        props = load_properties()
        print_summary(props)
        sys.exit(0)

    run_enrichment(limit=args.limit, force=args.force, target_id=args.property)
