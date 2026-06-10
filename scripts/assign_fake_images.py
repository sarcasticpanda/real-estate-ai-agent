"""
Assign placeholder house/apartment images to all properties in Supabase.

Images are chosen based on PRICE TIER (not just property type):
  < 40L        → budget apartment / studio
  40L - 1Cr    → standard flat / mid-range apartment
  1Cr - 2.5Cr  → premium flat / independent house
  2.5Cr - 5Cr  → luxury apartment / large independent house
  > 5Cr        → villa / bungalow / kothi

All photos are real Indian-style residential photos from Unsplash CDN (free, no API key).

Run:
    python scripts/assign_fake_images.py
    python scripts/assign_fake_images.py --dry-run   # preview without writing
    python scripts/assign_fake_images.py --reset      # clear existing images first
"""

import sys
import argparse
import random
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from database.supabase_client import get_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE = "https://images.unsplash.com/photo-"
W = "?w=800&q=80&fit=crop&auto=format"

# ── Budget tier: < 40L — small/basic apartment ───────────────────────────────
BUDGET_APARTMENT = [
    f"{BASE}1560448204-e02f11c3d0e2{W}",   # compact apartment interior
    f"{BASE}1522708323590-d24dbb6b0267{W}", # small studio living
    f"{BASE}1484154218962-a197022b5858{W}", # basic kitchen
    f"{BASE}1493809842364-78817add7ffb{W}", # modest bedroom
    f"{BASE}1614082595249-46e1379ffc35{W}", # affordable flat
    f"{BASE}1484101403633-562f891dc89a{W}", # simple room
]

# ── Standard tier: 40L - 1Cr — typical 2-3 BHK flat ─────────────────────────
STANDARD_FLAT = [
    f"{BASE}1502672260266-1c1ef2d93688{W}", # neat 2BHK living room
    f"{BASE}1556909114-f6e7ad7d3136{W}",    # standard kitchen
    f"{BASE}1583608205776-bfd35f0d9f83{W}", # typical Indian flat interior
    f"{BASE}1545324418-cc1a3fa10c00{W}",    # mid-range apartment exterior
    f"{BASE}1486325212027-8081e485255e{W}", # residential building
    f"{BASE}1507089947277-3535aef98b24{W}", # family living room
    f"{BASE}1512917774080-9991f1c4c750{W}", # apartment block
    f"{BASE}1618221195710-dd6b41faaea6{W}", # modern bedroom
]

# ── Premium tier: 1Cr - 2.5Cr — premium flat / independent floor ─────────────
PREMIUM_FLAT = [
    f"{BASE}1600210492493-0946911123ea{W}", # premium apartment interior
    f"{BASE}1600566753086-00f18fb6b3ea{W}", # large bedroom
    f"{BASE}1556909172-54557c7e4fb7{W}",    # premium kitchen
    f"{BASE}1570129477492-45c003edd2be{W}", # independent house exterior
    f"{BASE}1560185893-a55372865169{W}",    # clean house front
    f"{BASE}1505691938895-1758d7feb511{W}", # independent home
    f"{BASE}1615529328331-f8917597711f{W}", # spacious living room
    f"{BASE}1567538096630-e531ab52987d{W}", # dining room
]

# ── Luxury tier: 2.5Cr - 5Cr — large house / luxury flat ─────────────────────
LUXURY_HOUSE = [
    f"{BASE}1564013799919-ab600027ffc6{W}", # large house exterior
    f"{BASE}1568605114967-8130f3a36994{W}", # beautiful house front
    f"{BASE}1600047509807-ba8f99d2cdde{W}", # upscale residential
    f"{BASE}1600585154340-be6161a56a0c{W}", # large house with lawn
    f"{BASE}1571939228382-b2f2b585ce15{W}", # luxury living room
    f"{BASE}1600210492493-0946911123ea{W}", # high-end interior
    f"{BASE}1615529328331-f8917597711f{W}", # grand room
]

# ── Ultra-luxury tier: > 5Cr — villa / bungalow / kothi ─────────────────────
VILLA = [
    f"{BASE}1580587771525-78b9dba3b914{W}", # modern villa front
    f"{BASE}1613490493576-4d0d40671a48{W}", # luxury villa pool
    f"{BASE}1600607688969-a5bfcd646154{W}", # villa garden
    f"{BASE}1600596542815-0c2d0e980bce{W}", # bungalow exterior
    f"{BASE}1613545325278-f24b0cae1224{W}", # villa interior
    f"{BASE}1616594039964-ae9021a400a4{W}", # luxury bedroom
    f"{BASE}1571896349842-33c89424de2d{W}", # grand living space
]

# ── Plots (no price dependence) ───────────────────────────────────────────────
PLOT = [
    f"{BASE}1500076656116-558758c991c1{W}", # open land
    f"{BASE}1464082354059-1c7f8a9cadd9{W}", # vacant plot
    f"{BASE}1590402494610-2c378a9114c6{W}", # residential layout
]


def _price_tier(price_inr: int | None, property_type: str) -> str:
    pt = (property_type or "").lower()
    if "plot" in pt or "land" in pt:
        return "plot"
    if price_inr is None:
        return "standard"
    cr = price_inr / 1_00_00_000
    if cr < 0.40:
        return "budget"
    if cr < 1.0:
        return "standard"
    if cr < 2.5:
        return "premium"
    if cr < 5.0:
        return "luxury"
    return "villa"


TIER_POOLS = {
    "budget":   BUDGET_APARTMENT,
    "standard": STANDARD_FLAT,
    "premium":  PREMIUM_FLAT,
    "luxury":   LUXURY_HOUSE,
    "villa":    VILLA,
    "plot":     PLOT,
}


def _pick_images(price_inr: int | None, property_type: str, n: int = 4) -> list[str]:
    tier  = _price_tier(price_inr, property_type)
    pool  = TIER_POOLS[tier]
    count = min(n, len(pool))
    imgs  = random.sample(pool, count)
    logger.debug(f"  tier={tier} price={price_inr} → {count} images")
    return imgs


def assign_images(dry_run: bool = False, reset: bool = False) -> None:
    client = get_client()

    result = client.table("properties").select("id, data, property_type, price_inr").execute()
    properties = result.data or []
    logger.info(f"Found {len(properties)} properties")

    updated = 0
    skipped = 0

    for prop in properties:
        prop_id  = prop["id"]
        data     = prop.get("data") or {}
        existing = data.get("images") or []

        if existing and not reset:
            skipped += 1
            continue

        pt        = prop.get("property_type") or data.get("property_profile", {}).get("property_type", "apartment")
        price_inr = prop.get("price_inr") or data.get("pricing", {}).get("total_price_inr")
        if price_inr:
            price_inr = int(price_inr)

        imgs = _pick_images(price_inr, pt, n=4)

        if dry_run:
            tier = _price_tier(price_inr, pt)
            cr = round(price_inr / 1_00_00_000, 2) if price_inr else "?"
            logger.info(f"[DRY-RUN] {prop_id} | {pt} | ₹{cr}Cr | tier={tier} | {len(imgs)} images")
            updated += 1
            continue

        data["images"] = imgs
        client.table("properties").update({"data": data}).eq("id", prop_id).execute()
        updated += 1
        if updated % 20 == 0:
            logger.info(f"  Updated {updated} ...")

    logger.info(
        f"\nDone.  Updated: {updated}  |  Skipped (already had images): {skipped}"
        + ("  [DRY-RUN — nothing written]" if dry_run else "")
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    parser.add_argument("--reset",   action="store_true", help="Overwrite existing images too")
    args = parser.parse_args()

    random.seed(42)
    assign_images(dry_run=args.dry_run, reset=args.reset)
