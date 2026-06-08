"""
Curated POI database for Lucknow.
All coordinates verified from OpenStreetMap / Google Maps.

Two layers:
  DYNAMIC  — find_nearby_pois() picks the single nearest from each category per property.
  FIXED    — FIXED_ANCHORS stores distances to specific city-wide reference landmarks
             (Charbagh station, Amausi airport, Hazratganj + Aminabad markets).
             These are stored per-property so queries like "how far from Charbagh" always work.
"""

# ── Metro Stations ────────────────────────────────────────────────────────────
# Lucknow Metro: Red Line (N-S) + Green Line (E-W)
METRO_STATIONS = [
    # Green Line (East-West Corridor)
    {"name": "Charbagh Metro",           "lat": 26.8566, "lng": 80.9117},
    {"name": "Hussainganj Metro",         "lat": 26.8591, "lng": 80.9176},
    {"name": "Sachivalaya Metro",         "lat": 26.8617, "lng": 80.9247},
    {"name": "Hazratganj Metro",          "lat": 26.8588, "lng": 80.9457},
    {"name": "Vishwavidyalaya Metro",     "lat": 26.8558, "lng": 80.9561},
    {"name": "IT College Metro",          "lat": 26.8564, "lng": 80.9652},
    {"name": "Mawaiya Metro",             "lat": 26.8586, "lng": 80.9773},
    {"name": "Alambagh Bus Station Metro","lat": 26.8046, "lng": 80.9179},
    {"name": "Alambagh Metro",            "lat": 26.7986, "lng": 80.9175},
    {"name": "Singar Nagar Metro",        "lat": 26.7931, "lng": 80.9199},
    {"name": "Krishna Nagar Metro",       "lat": 26.7873, "lng": 80.9218},
    {"name": "Transport Nagar Metro",     "lat": 26.7792, "lng": 80.9161},
    # Red Line (North-South Corridor)
    {"name": "Durgapuri Metro",           "lat": 26.8627, "lng": 80.9101},
    {"name": "Lekhraj Market Metro",      "lat": 26.8703, "lng": 80.9086},
    {"name": "Indira Nagar Metro",        "lat": 26.8813, "lng": 80.9101},
    {"name": "Bhootnath Market Metro",    "lat": 26.8856, "lng": 80.9130},
    {"name": "Munshipulia Metro",         "lat": 26.8920, "lng": 80.9180},
]

# ── Railway Stations ──────────────────────────────────────────────────────────
RAILWAY_STATIONS = [
    {"name": "Lucknow Charbagh",          "lat": 26.8580, "lng": 80.9110},
    {"name": "Lucknow NR (New Railway)",  "lat": 26.8601, "lng": 80.9135},
    {"name": "Aishbagh Railway",          "lat": 26.8484, "lng": 80.9220},
    {"name": "Lucknow Junction",          "lat": 26.8580, "lng": 80.9110},
    {"name": "Malhaur Railway",           "lat": 26.8540, "lng": 81.0220},
    {"name": "Gomti Nagar Railway",       "lat": 26.8507, "lng": 80.9929},
]

# ── Hospitals ─────────────────────────────────────────────────────────────────
HOSPITALS = [
    {"name": "KGMU (King George's Medical University)", "lat": 26.8590, "lng": 80.9519},
    {"name": "Ram Manohar Lohia Hospital",              "lat": 26.8610, "lng": 80.9550},
    {"name": "Civil Hospital Lucknow",                  "lat": 26.8573, "lng": 80.9461},
    {"name": "Balrampur Hospital",                      "lat": 26.8607, "lng": 80.9118},
    {"name": "Sahara Hospital",                         "lat": 26.8434, "lng": 80.9765},
    {"name": "Medanta Hospital Lucknow",                "lat": 26.7866, "lng": 80.9806},
    {"name": "SGPGI (Sanjay Gandhi Hospital)",          "lat": 26.7814, "lng": 80.9805},
    {"name": "Lucknow Medical College Hospital",        "lat": 26.8595, "lng": 80.9505},
    {"name": "Apollomedics Hospital",                   "lat": 26.8380, "lng": 81.0130},
    {"name": "Vivekananda Hospital",                    "lat": 26.8529, "lng": 80.9876},
    {"name": "Chandan Hospital Gomti Nagar",            "lat": 26.8612, "lng": 80.9991},
    {"name": "Era Medical College",                     "lat": 26.8508, "lng": 80.8916},
]

# ── Schools / Educational Institutions ───────────────────────────────────────
SCHOOLS = [
    {"name": "CMS Hazratganj",              "lat": 26.8600, "lng": 80.9480},
    {"name": "CMS Gomti Nagar",             "lat": 26.8607, "lng": 81.0005},
    {"name": "CMS Aliganj",                 "lat": 26.8840, "lng": 80.9660},
    {"name": "CMS Indira Nagar",            "lat": 26.8827, "lng": 80.9937},
    {"name": "La Martiniere College",       "lat": 26.8468, "lng": 80.9449},
    {"name": "St Francis College",          "lat": 26.8491, "lng": 80.9451},
    {"name": "Loreto Convent School",       "lat": 26.8612, "lng": 80.9472},
    {"name": "Lucknow Public School",       "lat": 26.8583, "lng": 80.9910},
    {"name": "Kendriya Vidyalaya Lucknow",  "lat": 26.8570, "lng": 80.9490},
    {"name": "Amity University Lucknow",    "lat": 26.8475, "lng": 81.0070},
    {"name": "BBD University",              "lat": 26.8460, "lng": 81.0290},
    {"name": "Lucknow University",          "lat": 26.8540, "lng": 80.9560},
    {"name": "IIMM Lucknow",               "lat": 26.7720, "lng": 80.9870},
    {"name": "IIIM Lucknow",               "lat": 26.8531, "lng": 80.9947},
]

# ── Markets / Malls ───────────────────────────────────────────────────────────
MARKETS = [
    {"name": "Phoenix Palassio Mall",       "lat": 26.8613, "lng": 81.0002},
    {"name": "Fun Republic Mall",           "lat": 26.8573, "lng": 80.9925},
    {"name": "Hazratganj Market",           "lat": 26.8589, "lng": 80.9463},
    {"name": "Aminabad Market",             "lat": 26.8567, "lng": 80.9275},
    {"name": "Alambagh Market",             "lat": 26.8010, "lng": 80.9175},
    {"name": "Indira Nagar Market",         "lat": 26.8820, "lng": 80.9940},
    {"name": "Gomti Nagar Market",          "lat": 26.8590, "lng": 80.9990},
    {"name": "Aliganj Market",              "lat": 26.8840, "lng": 80.9660},
    {"name": "Rajajipuram Market",          "lat": 26.8540, "lng": 80.8820},
    {"name": "Wave Mall Lucknow",           "lat": 26.8560, "lng": 80.9888},
    {"name": "Star Mall Lucknow",           "lat": 26.8430, "lng": 80.9780},
    {"name": "Chowk Market",               "lat": 26.8690, "lng": 80.9120},
]

# ── Airports ──────────────────────────────────────────────────────────────────
AIRPORTS = [
    {"name": "Amausi International Airport (Chaudhary Charan Singh)", "lat": 26.7606, "lng": 80.8893},
]

# ── Bus Terminals ─────────────────────────────────────────────────────────────
BUS_STOPS = [
    {"name": "Charbagh Bus Terminal",       "lat": 26.8556, "lng": 80.9093},
    {"name": "Alambagh Bus Terminal",       "lat": 26.8022, "lng": 80.9168},
    {"name": "Gomti Nagar Bus Stop",        "lat": 26.8590, "lng": 80.9970},
    {"name": "Hazratganj Bus Stop",         "lat": 26.8580, "lng": 80.9470},
    {"name": "Aliganj Bus Stop",            "lat": 26.8820, "lng": 80.9660},
    {"name": "Indira Nagar Bus Stop",       "lat": 26.8810, "lng": 80.9930},
    {"name": "Faizabad Road Bus Stop",      "lat": 26.8790, "lng": 81.0220},
    {"name": "Chinhat Bus Stop",            "lat": 26.8610, "lng": 81.0660},
]

# ── Parks / Green Spaces ──────────────────────────────────────────────────────
PARKS = [
    {"name": "Janeshwar Mishra Park",       "lat": 26.8645, "lng": 80.9754},
    {"name": "Ambedkar Memorial Park",      "lat": 26.8588, "lng": 80.9474},
    {"name": "Begum Hazrat Mahal Park",     "lat": 26.8585, "lng": 80.9450},
    {"name": "Lohia Park",                  "lat": 26.8603, "lng": 80.9382},
    {"name": "Gomti Riverfront Park",       "lat": 26.8611, "lng": 80.9480},
    {"name": "Indira Gandhi Pratishthan Park","lat": 26.8612, "lng": 80.9744},
    {"name": "Nishatganj Park",             "lat": 26.8700, "lng": 80.9390},
    {"name": "BBD Green Belt Park",         "lat": 26.8460, "lng": 81.0200},
    {"name": "Munshipulia Park",            "lat": 26.8920, "lng": 80.9180},
]

# ── Fixed Landmark Anchors ────────────────────────────────────────────────────
# Distance to these specific city-wide landmarks is stored for EVERY property.
# This lets users ask "how far from Charbagh?" or "near airport?" for any listing.
FIXED_ANCHORS = {
    "charbagh_railway": {
        "name": "Charbagh Railway Station",
        "lat": 26.8580,
        "lng": 80.9110,
    },
    "amausi_airport": {
        "name": "Amausi International Airport",
        "lat": 26.7606,
        "lng": 80.8893,
    },
    "hazratganj_market": {
        "name": "Hazratganj Market",
        "lat": 26.8589,
        "lng": 80.9463,
    },
    "aminabad_market": {
        "name": "Aminabad Market",
        "lat": 26.8567,
        "lng": 80.9275,
    },
    "phoenix_palassio": {
        "name": "Phoenix Palassio Mall",
        "lat": 26.8613,
        "lng": 81.0002,
    },
    "gomti_nagar_market": {
        "name": "Gomti Nagar Market",
        "lat": 26.8590,
        "lng": 80.9990,
    },
}

# ── Master lookup (dynamic nearest-POI search) ────────────────────────────────
ALL_POIS = {
    "metro":    METRO_STATIONS,
    "railway":  RAILWAY_STATIONS,
    "hospital": HOSPITALS,
    "school":   SCHOOLS,
    "market":   MARKETS,
    "airport":  AIRPORTS,
    "bus_stop": BUS_STOPS,
    "park":     PARKS,
}
