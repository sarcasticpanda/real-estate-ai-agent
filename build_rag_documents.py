import pandas as pd
from datetime import datetime
import json
import os

# =========================
# PATH CONFIGURATION
# =========================
csv_path = r'C:\Users\Lunar Panda\3-Main\real-estate-ai-agent\real-estate-ai-agent\csv_container'
rag_path = r'C:\Users\Lunar Panda\3-Main\real-estate-ai-agent\real-estate-ai-agent\csv_container\rag_documents'
json_filename = 'rag_properties.json'

os.makedirs(rag_path, exist_ok=True)

# =========================
# LOAD CSV FILES
# =========================
properties = pd.read_csv(os.path.join(csv_path, 'properties.csv'))
areas = pd.read_csv(os.path.join(csv_path, 'areas.csv'))
address_details = pd.read_csv(os.path.join(csv_path, 'address_details.csv'))
distance_metrics = pd.read_csv(os.path.join(csv_path, 'distance_metrics.csv'))
amenities = pd.read_csv(os.path.join(csv_path, 'amenities.csv'))
property_amenities = pd.read_csv(os.path.join(csv_path, 'property_amenities.csv'))

# =========================
# NORMALIZE COLUMN NAMES
# =========================
properties.columns = properties.columns.str.strip()

# =========================
# SAFE CAST HELPERS
# =========================
def safe_int(v):
    if pd.isna(v):
        return None
    try:
        return int(float(v))
    except:
        return None

def safe_float(v):
    if pd.isna(v):
        return None
    try:
        return float(v)
    except:
        return None

def safe_str(v):
    return str(v) if not pd.isna(v) else None

# =========================
# CLASSIFICATION LOGIC (ADD-ON)
# =========================
def classify_property(property_type):
    if not property_type:
        return {"category": None, "sub_type": None}

    p = property_type.lower().strip()

    residential = ["flat", "independent house", "villa", "plot"]
    commercial = ["shop", "office", "showroom"]

    if p in residential:
        category = "residential"
    elif p in commercial:
        category = "commercial"
    else:
        category = "other"

    return {
        "category": category,
        "sub_type": p.replace(" ", "_")
    }

# =========================
# CONNECTIVITY STATUS
# =========================
def connectivity_status(row):
    if row is None:
        return "pending_enrichment"

    keys = [
        "metro_distance_km",
        "railway_distance_km",
        "bus_stop_distance_km",
        "hospital_distance_km"
    ]

    return (
        "enriched"
        if any(pd.notna(row.get(k)) for k in keys)
        else "pending_enrichment"
    )

# =========================
# LOCATION CONTEXT (TEXT FOR RAG)
# =========================
def build_location_context(prop, area):
    ptype = safe_str(prop.get("property_type")) or "Property"
    area_name = safe_str(area.get("area_name_original"))
    city = safe_str(area.get("city"))

    if area_name and city:
        return f"{ptype} located in {area_name}, {city}."
    return f"{ptype} located in Lucknow."

# =========================
# GENERATE RAG DOCUMENTS
# =========================
rag_docs = []

for _, prop in properties.iterrows():
    prop_id = safe_str(prop.get("property_id"))

    area_df = areas[areas["area_id"] == prop.get("area_id")]
    addr_df = address_details[address_details["property_id"] == prop_id]
    dist_df = distance_metrics[distance_metrics["property_id"] == prop_id]

    area = area_df.iloc[0].to_dict() if not area_df.empty else {}
    addr = addr_df.iloc[0].to_dict() if not addr_df.empty else {}
    dist = dist_df.iloc[0].to_dict() if not dist_df.empty else None

    amenity_ids = property_amenities[
        property_amenities["property_id"] == prop_id
    ]["amenity_id"].tolist()

    amenity_names = amenities[
        amenities["amenity_id"].isin(amenity_ids)
    ]["amenity_name"].tolist()

    classification = classify_property(safe_str(prop.get("property_type")))

    doc = {
        "doc_id": f"rag_property_{prop_id}",
        "doc_type": "property",
        "schema_version": "v1",

        "classification": classification,

        "source_ids": {
            "property_id": prop_id,
            "area_id": safe_str(prop.get("area_id")),
            "address_id": safe_str(addr.get("address_id"))
        },

        "location": {
            "city": safe_str(area.get("city")),
            "state": safe_str(area.get("state")),
            "area_name": safe_str(area.get("area_name_original")),
            "area_confidence": safe_str(area.get("source_confidence")),
            "location_context": build_location_context(prop, area)
        },

        "property_profile": {
            "property_type": safe_str(prop.get("property_type")),
            "bhk": safe_int(prop.get("bhk")),
            "builtup_area_sqft": safe_int(prop.get("area_sqft")),
            "area_unit_original": safe_str(prop.get("area_unit_original")),
            "furnishing": safe_str(prop.get("furnishing")),
            "facing": safe_str(prop.get("facing")),
            "floor_info": {
                "current_floor": safe_int(prop.get("current_floor")),
                "total_floors": safe_int(prop.get("total_floors"))
            },
            "construction_age": safe_str(prop.get("construction_age_bucket"))
        },

        "pricing": {
            "total_price_inr": safe_int(prop.get("price_inr")),
            "price_per_sqft": safe_float(prop.get("price_per_sqft"))
        },

        "amenities": amenity_names,

        "connectivity": {
            "metro_distance_km": safe_float(dist.get("metro_distance_km")) if dist else None,
            "railway_distance_km": safe_float(dist.get("railway_distance_km")) if dist else None,
            "bus_stop_distance_km": safe_float(dist.get("bus_stop_distance_km")) if dist else None,
            "hospital_distance_km": safe_float(dist.get("hospital_distance_km")) if dist else None,
            "status": connectivity_status(dist)
        },

        "transaction": {
            "transaction_type": safe_str(prop.get("transaction_type")),
            "ownership": safe_str(prop.get("ownership")),
            "parking": {
                "covered": safe_int(prop.get("covered_parking")),
                "open": safe_int(prop.get("open_parking"))
            }
        },

        "metadata": {
            "source": "MagicBricks",
            "raw_source_preserved": True,
            "raw_full_address": safe_str(addr.get("full_address_text")),
            "generated_at": datetime.now().strftime("%Y-%m-%d")
        }
    }

    rag_docs.append(doc)

# =========================
# WRITE OUTPUT
# =========================
output_path = os.path.join(rag_path, json_filename)

with open(output_path, "w", encoding="utf-8") as f:
    json.dump(rag_docs, f, indent=2, ensure_ascii=False)

print(f"✅ RAG JSON generated: {len(rag_docs)} documents")
print(f"📁 Location: {output_path}")