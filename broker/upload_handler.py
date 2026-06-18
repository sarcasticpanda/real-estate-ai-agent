"""
Broker CSV upload pipeline.

When a broker uploads a CSV file:
  1. Parse and validate all rows
  2. Normalize to the standard JSON schema
  3. Geocode each address (Nominatim)
  4. Enrich with nearby POIs (Overpass API)
  5. Generate embedding (HuggingFace)
  6. Upsert into Supabase

Usage:
    python broker/upload_handler.py path/to/broker_listings.csv
    python broker/upload_handler.py path/to/broker_listings.csv --broker-id BROKER001
"""

import csv
import json
import uuid
import hashlib
import sys
import logging
import argparse
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from broker.csv_schema import validate_row, VALID_PROPERTY_TYPES
from enrichment.geocoder import geocode_address
from enrichment.poi_finder import find_nearby_pois
from embeddings.embedding_model import build_semantic_text, embed_text
from database.supabase_client import upsert_property

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

CRORE = 1_00_00_000


def parse_amenities(amenities_str: str) -> list[str]:
    if not amenities_str:
        return []
    return [a.strip().title() for a in amenities_str.split(",") if a.strip()]


def normalize_row(row: dict, broker_id: str | None = None) -> dict:
    """Convert a CSV row dict into the standard property JSON schema."""
    # Idempotent uploads: if the broker provides their own stable reference
    # (external_ref / ref / listing_id), derive the id from it so re-uploading the
    # same listing UPDATES it instead of creating a duplicate. Otherwise mint a uuid.
    ext_ref = str(row.get("external_ref") or row.get("ref") or row.get("listing_id") or "").strip()
    if ext_ref:
        prop_id = f"BROKER_{hashlib.md5(ext_ref.encode()).hexdigest()[:10].upper()}"
    else:
        prop_id = f"BROKER_{uuid.uuid4().hex[:8].upper()}"
    ptype = str(row.get("property_type", "")).strip().title()
    city = str(row.get("city", "Lucknow")).strip()
    address = str(row.get("address", "")).strip()

    try:
        price = int(float(str(row.get("price_inr", 0)).replace(",", "")))
    except (ValueError, TypeError):
        price = 0

    try:
        sqft = float(str(row.get("area_sqft", 0)).replace(",", ""))
    except (ValueError, TypeError):
        sqft = 0

    price_per_sqft = round(price / sqft, 2) if sqft > 0 else None

    return {
        "doc_id": f"rag_property_{prop_id}",
        "doc_type": "property",
        "schema_version": "v1",
        "classification": {
            "category": "commercial" if ptype.lower() in {"shop", "office"} else "residential",
            "sub_type": ptype.lower(),
        },
        "source_ids": {
            "property_id": prop_id,
            "broker_id": broker_id,
        },
        "location": {
            "city": city,
            "state": "Uttar Pradesh",
            "area_name": address.split(",")[0].strip() if "," in address else address,
            "area_confidence": "approximate",
            "location_context": f"{ptype} located in {address}, {city}.",
        },
        "property_profile": {
            "property_type": ptype,
            "bhk": _safe_int(row.get("bhk")),
            "builtup_area_sqft": sqft,
            "area_unit_original": "Sqft",
            "furnishing": str(row.get("furnishing", "")).strip().title() or None,
            "facing": str(row.get("facing", "")).strip().title() or None,
            "floor_info": {
                "current_floor": _safe_int(row.get("current_floor")),
                "total_floors": _safe_int(row.get("total_floors")),
            },
            "construction_age": str(row.get("construction_age", "")).strip() or None,
        },
        "pricing": {
            "total_price_inr": price,
            "price_per_sqft": price_per_sqft,
        },
        "amenities": parse_amenities(str(row.get("amenities", ""))),
        "connectivity": {
            "status": "pending_enrichment",
        },
        "transaction": {
            "transaction_type": str(row.get("transaction_type", "Resale")).strip().title(),
            "ownership": str(row.get("ownership", "Freehold")).strip().title(),
            "parking": {
                "covered": _safe_int(row.get("covered_parking", 0)),
                "open": _safe_int(row.get("open_parking", 0)),
            },
        },
        "metadata": {
            "source": "broker_upload",
            "broker_name": str(row.get("broker_name", "")).strip() or None,
            "broker_phone": str(row.get("broker_phone", "")).strip() or None,
            "description": str(row.get("description", "")).strip() or None,
            "raw_full_address": address,
            "generated_at": datetime.now().strftime("%Y-%m-%d"),
        },
    }


def enrich_property(prop: dict) -> dict:
    """Add geocoding and POI distances to a normalized property."""
    address = prop["metadata"].get("raw_full_address", "")
    city = prop["location"].get("city", "Lucknow")
    state = prop["location"].get("state", "Uttar Pradesh")

    coords = geocode_address(address, city, state)
    if not coords:
        logger.warning(f"Could not geocode {prop['doc_id']} — saving without distances")
        return prop

    lat, lng = coords
    poi_data = find_nearby_pois(lat, lng)

    prop["connectivity"] = {
        "latitude": lat,
        "longitude": lng,
        "metro_distance_km": poi_data.get("metro_distance_km"),
        "metro_name": poi_data.get("metro_name"),
        "railway_distance_km": poi_data.get("railway_distance_km"),
        "railway_name": poi_data.get("railway_name"),
        "bus_stop_distance_km": poi_data.get("bus_stop_distance_km"),
        "hospital_distance_km": poi_data.get("hospital_distance_km"),
        "hospital_name": poi_data.get("hospital_name"),
        "school_distance_km": poi_data.get("school_distance_km"),
        "school_name": poi_data.get("school_name"),
        "market_distance_km": poi_data.get("market_distance_km"),
        "airport_distance_km": poi_data.get("airport_distance_km"),
        "status": "enriched",
        "enrichment_source": "overpass_api",
    }
    return prop


def process_csv(filepath: str, broker_id: str | None = None) -> dict:
    """
    Full pipeline: CSV file → validate → normalize → enrich → embed → Supabase.
    Returns a summary dict.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {filepath}")

    results = {"total": 0, "success": 0, "failed": 0, "errors": []}

    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    logger.info(f"Processing {len(rows)} rows from {path.name}")

    for i, row in enumerate(rows, 1):
        results["total"] += 1
        errors = validate_row(row, i)

        if errors:
            logger.warning(f"Validation errors: {errors}")
            results["errors"].extend(errors)
            results["failed"] += 1
            continue

        try:
            # Normalize
            prop = normalize_row(row, broker_id)
            logger.info(f"[{i}/{len(rows)}] Processing {prop['doc_id']}")

            # Enrich with geo + POI data
            prop = enrich_property(prop)

            # Build semantic text + generate embedding
            semantic_text = build_semantic_text(prop)
            embedding = embed_text(semantic_text)

            # Upload to Supabase
            upsert_property(prop, semantic_text, embedding)

            results["success"] += 1
            logger.info(f"  ✓ Uploaded {prop['doc_id']}")

        except Exception as e:
            logger.error(f"Row {i} failed: {e}")
            results["errors"].append(f"Row {i}: {e}")
            results["failed"] += 1

        # Rate limit: be respectful to Nominatim + Overpass
        time.sleep(2.0)

    logger.info(f"\nUpload complete: {results['success']} success, {results['failed']} failed out of {results['total']}")
    return results


def create_property_from_fields(fields: dict, broker_id: str | None = None) -> dict:
    """
    Add ONE property from broker form fields (same pipeline as a CSV row).
    Returns {ok, property_id, doc_id, area, error}. Used by the broker 'Add property' UI.
    `fields` keys mirror the CSV columns: property_type, bhk, price_inr, area_sqft,
    furnishing, address, city, amenities, broker_name, broker_phone, description, etc.
    """
    errors = validate_row(fields, 1)
    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    try:
        prop = normalize_row(fields, broker_id)
        prop = enrich_property(prop)
        semantic_text = build_semantic_text(prop)
        embedding = embed_text(semantic_text)
        upsert_property(prop, semantic_text, embedding)
        return {
            "ok": True,
            "property_id": prop["doc_id"],          # this is the DB row id ("rag_property_BROKER_XXXX")
            "internal_id": prop["source_ids"]["property_id"],
            "area": prop["location"].get("area_name"),
        }
    except Exception as e:
        logger.error(f"create_property_from_fields failed: {e}")
        return {"ok": False, "error": str(e)}


def _safe_int(val) -> int | None:
    try:
        return int(float(str(val))) if val not in (None, "", "nan") else None
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload broker property CSV to the knowledge base")
    parser.add_argument("csv_file", help="Path to broker CSV file")
    parser.add_argument("--broker-id", help="Broker ID to associate with these properties")
    args = parser.parse_args()

    summary = process_csv(args.csv_file, broker_id=args.broker_id)
    print(json.dumps(summary, indent=2))
