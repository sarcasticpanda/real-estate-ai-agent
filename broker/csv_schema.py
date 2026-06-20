"""
Defines the expected broker CSV format and validates uploads.
"""

# Required columns in the broker upload CSV
REQUIRED_COLUMNS = {
    "property_type",   # flat | house | villa | plot | shop | office
    "price_inr",       # total price in rupees (e.g. 17000000)
    "address",         # full address or locality name
}

OPTIONAL_COLUMNS = {
    "bhk",                # integer; optional for plots/commercial listings
    "area_sqft",          # built-up area in sqft
    "city",               # defaults to Lucknow
    "furnishing",         # Furnished | Unfurnished | Semi-Furnished
    "facing",             # East | West | North | South
    "current_floor",      # integer
    "total_floors",       # integer
    "construction_age",   # New Construction | 5-10 years | etc.
    "covered_parking",    # integer
    "open_parking",       # integer
    "transaction_type",   # Resale | New
    "ownership",          # Freehold | Leasehold
    "amenities",          # comma-separated: "Lift, Gym, Pool"
    "broker_name",        # broker's name
    "broker_phone",       # broker's contact
    "description",        # free-text property description
}

VALID_PROPERTY_TYPES = {"flat", "house", "villa", "plot", "shop", "office", "apartment"}
VALID_FURNISHING = {"furnished", "unfurnished", "semi-furnished", "semi furnished"}
MAX_PRICE_INR = 100_00_00_000   # ₹100 Cr upper bound sanity check
MIN_PRICE_INR = 1_00_000        # ₹1 Lakh lower bound


def validate_row(row: dict, row_num: int) -> list[str]:
    """
    Validate a single CSV row. Returns list of error messages (empty = valid).
    """
    errors = []

    # Check required columns have values
    for col in REQUIRED_COLUMNS:
        if not str(row.get(col, "")).strip():
            errors.append(f"Row {row_num}: missing required field '{col}'")

    # Validate BHK
    bhk_val = row.get("bhk", "")
    try:
        bhk = int(bhk_val)
        if not (1 <= bhk <= 10):
            errors.append(f"Row {row_num}: bhk must be between 1 and 10 (got {bhk})")
    except (ValueError, TypeError):
        if bhk_val:
            errors.append(f"Row {row_num}: bhk must be a number (got '{bhk_val}')")

    # Validate price
    price_val = row.get("price_inr", "")
    try:
        price = float(str(price_val).replace(",", ""))
        if not (MIN_PRICE_INR <= price <= MAX_PRICE_INR):
            errors.append(f"Row {row_num}: price_inr {price} is out of valid range")
    except (ValueError, TypeError):
        if price_val:
            errors.append(f"Row {row_num}: price_inr must be a number (got '{price_val}')")

    # Validate property type
    ptype = str(row.get("property_type", "")).strip().lower()
    if ptype and ptype not in VALID_PROPERTY_TYPES:
        errors.append(f"Row {row_num}: unknown property_type '{ptype}' (valid: {VALID_PROPERTY_TYPES})")

    # Validate furnishing
    furnish = str(row.get("furnishing", "")).strip().lower()
    if furnish and furnish not in VALID_FURNISHING:
        errors.append(f"Row {row_num}: unknown furnishing '{furnish}' (valid: {VALID_FURNISHING})")

    return errors
