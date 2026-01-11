# Real Estate AI Agent - Data Collection Setup

## ✅ Setup Complete!

Your data collection environment is ready. Here's what has been set up:

### 📁 Project Structure
```
real-estate-ai-agent/
├── venv/                          # Python virtual environment
├── .env                           # API keys (OpenRouter)
├── requirements.txt               # Dependencies
├── .gitignore                     # Git ignore rules
├── LOCALITY_DATA_TEMPLATE.txt     # Manual data entry template
├── scripts/
│   ├── test_api.py                # Test API connection
│   ├── collect_properties.py      # Collect property listings
│   ├── collect_market_prices.py   # Collect market price data
│   ├── convert_locality_template.py  # Convert filled template to JSON
│   └── validate_data.py           # Validate and clean data
├── data/                          # Raw collected data (will be created)
├── cleaned_data/                  # Validated data (will be created)
├── chunks/                        # For RAG ingestion
└── vector_db/                     # For RAG system
```

### 🔑 API Configuration
- ✅ OpenRouter API key configured (FREE tier)
- ✅ Using model: `meta-llama/llama-3.2-3b-instruct:free`
- ✅ Connection tested and working

### 📋 Next Steps

#### **STEP 1: Manual Locality Data Collection (30-60 mins)**
1. Open `LOCALITY_DATA_TEMPLATE.txt`
2. For each area (Gomti Nagar, Alambagh, Hazratganj, Indira Nagar, Aliganj):
   - Search on Google Maps
   - Fill in nearest metro stations (name, distance, travel time)
   - Fill in top 3-5 schools
   - Fill in top 3-5 hospitals
3. Save the file when done
4. Run: `venv\Scripts\python.exe scripts\convert_locality_template.py`

#### **STEP 2: Automated Property Data Collection**
Run the property collection script:
```bash
venv\Scripts\python.exe scripts\collect_properties.py
```
This will:
- Search for properties in all 5 target areas
- Extract structured data using LLM
- Save to `data/properties.json`

**Note:** The current script uses mock URLs for demonstration. For production:
- Use Google Custom Search API (100 free queries/day)
- Or implement direct web scraping with BeautifulSoup
- See script comments for integration points

#### **STEP 3: Market Price Data Collection**
Run the market price collection script:
```bash
venv\Scripts\python.exe scripts\collect_market_prices.py
```

#### **STEP 4: Data Validation**
After collecting all data, validate and clean it:
```bash
venv\Scripts\python.exe scripts\validate_data.py
```
This will:
- Check data completeness
- Remove invalid entries
- Verify no personal info (phone, owner name)
- Save cleaned data to `cleaned_data/`

### 🎯 Target Data Structure

**properties.json** (50-200 entries):
```json
{
  "area_name": "Gomti Nagar",
  "city": "Lucknow",
  "bhk": 2,
  "price": 5800000,
  "price_per_sqft": 5200,
  "property_type": "Apartment",
  "built_up_area": 1100,
  "amenities": ["Lift", "Parking", "Security"],
  "status": "Ready",
  "source": "99acres.com"
}
```

**locality_info.json** (5 entries, one per area):
```json
{
  "area": "Gomti Nagar",
  "nearest_metros": [
    {"name": "Indira Nagar", "distance_km": 2.1, "travel_time_min": 8}
  ],
  "schools": ["Delhi Public School", "CMS", "St. Francis"],
  "hospitals": ["Mayo Hospital", "Medanta", "Sahara"]
}
```

**market_prices.json** (5-20 entries):
```json
{
  "area": "Gomti Nagar",
  "avg_price_per_sqft": 5200,
  "min_price_per_sqft": 4800,
  "max_price_per_sqft": 6000,
  "trend": "up"
}
```

### ⚙️ Configuration
Edit `.env` to change target areas:
```
TARGET_CITY=Lucknow
TARGET_AREAS=Gomti Nagar,Alambagh,Hazratganj,Indira Nagar,Aliganj
```

### 🚀 Running Scripts
Always use the virtual environment Python:
```bash
# Activate venv (optional, for interactive work)
.\venv\Scripts\activate

# Or run directly
.\venv\Scripts\python.exe scripts\<script_name>.py
```

### 📊 Data Usage
Once collected and validated, this data will be used for:
1. **RAG System** - Load into vector database for semantic search
2. **Property Matching** - Match buyer requirements with listings
3. **Area Intelligence** - Provide locality insights to buyers
4. **Market Analysis** - Price trends and recommendations

### 🔒 Legal & Ethical Notes
- ✅ Only public data is collected
- ✅ No personal info (phone, owner name, exact flat number)
- ✅ Using legal APIs and respecting ToS
- ✅ Rate limiting implemented (2 sec between requests)

### 🆘 Troubleshooting

**API Connection Failed?**
- Check internet connection
- Verify API key in `.env` file
- Run: `venv\Scripts\python.exe scripts\test_api.py`

**Import Errors?**
- Ensure venv is activated or use full path
- Reinstall: `venv\Scripts\pip.exe install -r requirements.txt`

**No Data Collected?**
- Current scripts use mock URLs for demo
- Implement actual search API or web scraping
- See script comments for integration guidance

---

## 🎉 You're Ready!

Start with **STEP 1** (manual locality data collection) and work through the steps.

Good luck with your data collection! 🏠🤖
