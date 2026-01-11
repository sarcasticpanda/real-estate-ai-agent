# 🚀 QUICK START GUIDE

**Ready to collect Lucknow real estate data!**

---

## ⚡ NEXT STEPS (In Order)

### STEP 1: Inspect UP-RERA Website (15-30 minutes)
**⚠️ CRITICAL - Do this before running scraper**

1. Open: https://uprera.azurewebsites.net/View_projects.aspx
2. Right-click → Inspect (F12)
3. Update CSS selectors in `scripts/uprera_spider.py`

**What to update**:
- Lines 60-67: Form field names (district dropdown, search button)
- Lines 95-100: Results table structure (table class, row class, columns)
- Lines 125-138: Project detail page fields (all project info fields)

**See**: [EXECUTION_GUIDE.md](EXECUTION_GUIDE.md) Step 2 for detailed instructions

---

### STEP 2: Run UP-RERA Scraper (2-4 hours automated)

```powershell
# Option 1 - Using helper (recommended)
.\venv\Scripts\python.exe scripts\run_uprera_scraper.py

# Option 2 - Direct Scrapy
.\venv\Scripts\scrapy.exe runspider scripts\uprera_spider.py -o data/rera_projects.json
```

**Expected**: 500-1000 RERA registered projects  
**Output**: `data/rera_projects.json`

---

### STEP 3: Run DuckDuckGo Collector (1-2 hours automated)

```powershell
.\venv\Scripts\python.exe scripts\collect_realtime_legal.py
```

**Expected**: 50-100 active property listings  
**Output**: `data/properties_realtime.json`

---

### STEP 4: Fill Locality Data (1 hour manual)

1. Open `LOCALITY_DATA_TEMPLATE.txt`
2. Use Google Maps to fill:
   - Metro stations (name, distance, time)
   - Schools (3-5 per area)
   - Hospitals (3-5 per area)
3. Save and convert:

```powershell
.\venv\Scripts\python.exe scripts\convert_locality_template.py
```

**Output**: `data/locality_info.json`

---

### STEP 5: Consolidate & Validate (5 minutes automated)

```powershell
# Merge all data sources by area
.\venv\Scripts\python.exe scripts\consolidate_area_wise.py

# Clean and validate
.\venv\Scripts\python.exe scripts\validate_data.py
```

**Output**: `cleaned_data/area_wise/*.json` (gomti_nagar.json, alambagh.json, etc.)

---

## ✅ COMPLETION CHECKLIST

- [ ] Scrapy installed (✅ Already done!)
- [ ] UP-RERA spider CSS selectors updated
- [ ] RERA projects collected (500+ projects)
- [ ] Real-time listings collected (50+ listings)
- [ ] Locality data filled (5 areas)
- [ ] Data consolidated by area
- [ ] Data validated and cleaned

---

## 📁 FINAL DATA STRUCTURE

```
cleaned_data/area_wise/
├── gomti_nagar.json      (180 projects, 22 listings, locality info)
├── alambagh.json         (140 projects, 15 listings, locality info)
├── hazratganj.json
├── indira_nagar.json
├── aliganj.json
├── other_lucknow.json
└── summary.json
```

Each area file contains:
- `rera_projects[]`: Official RERA registered projects
- `property_listings[]`: Real-time active listings
- `locality_info{}`: Metro, schools, hospitals
- `market_prices{}`: Price trends
- `statistics{}`: Averages, counts

---

## 🎯 TOTAL TIME ESTIMATE

- **Automated**: 4-7 hours (scraping, you can leave running)
- **Manual**: 1-2 hours (website inspection + locality data)
- **Total**: 5-8 hours

---

## 🆘 TROUBLESHOOTING

### Spider collects no data
→ CSS selectors are wrong, go back to Step 1

### CAPTCHA appears
→ Pause scraper, solve manually, increase delay to 5-10 sec

### DuckDuckGo times out
→ Wait 10 minutes, try again (rate limiting)

### LLM extraction fails
→ Check OpenRouter API key in `.env`

**Full troubleshooting**: [EXECUTION_GUIDE.md](EXECUTION_GUIDE.md)

---

## 📖 DETAILED GUIDES

- **Complete workflow**: [EXECUTION_GUIDE.md](EXECUTION_GUIDE.md)
- **Setup details**: [SETUP_COMPLETE.md](SETUP_COMPLETE.md)
- **Legal data sources**: [REALTIME_DATA_GUIDE.md](REALTIME_DATA_GUIDE.md)

---

## ⏭️ AFTER DATA COLLECTION

**Phase 2: RAG Agent Development**
1. Chunk data for embeddings
2. Build vector database (ChromaDB/FAISS)
3. Create AI agent for property queries
4. Add buyer qualification logic
5. Implement scheduling automation

---

**Questions?** Check [EXECUTION_GUIDE.md](EXECUTION_GUIDE.md) for step-by-step details!
