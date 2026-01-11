# EXECUTION GUIDE: Real Estate Data Collection
**Complete workflow for collecting and organizing Lucknow real estate data**

---

## 🎯 OVERVIEW

This guide walks through the complete data collection process:
1. **Install Scrapy** for web scraping
2. **Inspect UP-RERA website** to update spider selectors
3. **Run UP-RERA scraper** to collect RERA projects (2-4 hours)
4. **Run DuckDuckGo collector** for real-time listings (1-2 hours)
5. **Manual locality data entry** (1 hour)
6. **Consolidate & validate** data by area

**Total Time**: 5-8 hours (mostly automated)

---

## STEP 1: INSTALL SCRAPY

**Action**: Install Scrapy framework for web scraping

**Commands**:
```powershell
cd "c:\Users\Lunar Panda\3-Main\real-estate-ai-agent\real-estate-ai-agent"
.\venv\Scripts\python.exe -m pip install scrapy
```

**Verification**:
```powershell
.\venv\Scripts\python.exe -c "import scrapy; print('Scrapy', scrapy.__version__)"
```

**Expected Output**:
```
Scrapy 2.11.x
```

**If Fails**:
- Ensure venv is activated
- Check pip is up to date: `.\venv\Scripts\python.exe -m pip install --upgrade pip`
- Try: `.\venv\Scripts\python.exe -m pip install scrapy==2.11.0`

---

## STEP 2: INSPECT UP-RERA WEBSITE ⚠️ CRITICAL

**Action**: Examine UP-RERA website structure to update spider CSS selectors

**Why**: Website HTML structure may differ from template assumptions

**Steps**:

### 2.1 Open UP-RERA Search Page
1. Open browser: https://uprera.azurewebsites.net/View_projects.aspx
2. Right-click on page → **Inspect** (or F12)
3. Click **Elements** tab (Chrome) or **Inspector** (Firefox)

### 2.2 Inspect Search Form
**Find**: District dropdown and Search button
- Right-click dropdown → Inspect
- Note the `name` attribute (e.g., `ctl00$ContentPlaceHolder1$ddlDistrict`)
- Right-click Search button → Inspect
- Note the `name` attribute

**Update in**: `scripts/uprera_spider.py` **lines 60-67**
```python
formdata = {
    '__VIEWSTATE': viewstate or '',
    '__VIEWSTATEGENERATOR': viewstate_generator or '',
    '__EVENTVALIDATION': event_validation or '',
    'ACTUAL_DISTRICT_FIELD_NAME': 'Lucknow',  # ← Update this
    'ACTUAL_BUTTON_FIELD_NAME': 'Search'      # ← Update this
}
```

### 2.3 Inspect Search Results Table
**Steps**:
1. Submit a search (select Lucknow → Search)
2. Right-click on results table → Inspect
3. Find table class (e.g., `class="GridViewStyle"`)
4. Find row class (e.g., `class="RowStyle"`)
5. Count columns: Project Name (col 2?), Registration (col 3?), Location (col 4?)

**Update in**: `scripts/uprera_spider.py` **lines 95-100**
```python
project_rows = response.css('table.ACTUAL_TABLE_CLASS tr[class*="ACTUAL_ROW_CLASS"]')

# Adjust column numbers
project_name = row.css('td:nth-child(ACTUAL_COL_NUM)::text').get()
registration_no = row.css('td:nth-child(ACTUAL_COL_NUM)::text').get()
location = row.css('td:nth-child(ACTUAL_COL_NUM)::text').get()
```

### 2.4 Inspect Project Detail Page
**Steps**:
1. Click on a project name from results
2. Right-click on each field → Inspect
3. Note the `id` or `class` of each field's `<span>` or `<label>`

**Update in**: `scripts/uprera_spider.py` **lines 125-138**
```python
'project_name': response.css('span#ACTUAL_ID::text').get(),
'registration_number': response.css('span#ACTUAL_ID::text').get(),
'promoter_name': response.css('span#ACTUAL_ID::text').get(),
# ... update all fields
```

### 2.5 Save Changes
After updating all selectors, save `scripts/uprera_spider.py`

---

## STEP 3: RUN UP-RERA SCRAPER

**Action**: Scrape registered projects from UP-RERA

**Command** (Option 1 - Using helper script):
```powershell
.\venv\Scripts\python.exe scripts\run_uprera_scraper.py
```

**Command** (Option 2 - Direct Scrapy):
```powershell
.\venv\Scripts\scrapy.exe runspider scripts\uprera_spider.py -o data/rera_projects.json
```

**Expected Duration**: 2-4 hours
- Polite crawling: 3-second delay between requests
- Estimated: 500-1000 projects for Lucknow

**Expected Output**:
```
2025-01-11 10:00:00 [scrapy.core.engine] INFO: Spider opened
2025-01-11 10:00:03 [uprera_lucknow] INFO: Accessed UP-RERA search page
2025-01-11 10:00:06 [uprera_lucknow] INFO: Parsing search results
...
2025-01-11 13:00:00 [scrapy.core.engine] INFO: Closing spider (finished)
2025-01-11 13:00:00 [uprera_lucknow] INFO: Saved 750 projects to data/rera_projects.json
```

**Output File**: `data/rera_projects.json`

**If CAPTCHA Appears**:
1. **STOP** the scraper (Ctrl+C)
2. Manually solve CAPTCHA in browser
3. Restart scraper (it will resume)
4. Consider increasing `DOWNLOAD_DELAY` to 5-10 seconds

**Troubleshooting**:
- **No data collected**: CSS selectors incorrect (go back to Step 2)
- **Empty fields**: Check selector accuracy on detail pages
- **HTTP 403/429**: Increase delay, respect rate limits

---

## STEP 4: RUN DUCKDUCKGO COLLECTOR

**Action**: Collect real-time property listings from web search

**Command**:
```powershell
.\venv\Scripts\python.exe scripts\collect_realtime_legal.py
```

**Expected Duration**: 1-2 hours
- Searches for each area + property type combination
- LLM extraction of structured data

**Expected Output**:
```
=== REAL-TIME PROPERTY DATA COLLECTION (LEGAL) ===
Collecting data for 5 areas...

[1/5] Gomti Nagar - 2 BHK apartments...
  ✓ Found 3 search results
  ✓ Extracted 2 properties

[1/5] Gomti Nagar - 3 BHK apartments...
  ✓ Found 5 search results
  ✓ Extracted 4 properties

...

✅ Collection complete!
Total properties collected: 87
Saved to: data/properties_realtime.json
```

**Output File**: `data/properties_realtime.json`

**Troubleshooting**:
- **API timeout**: Wait a few minutes, retry
- **Low extraction rate**: Check LLM model availability
- **No results**: DuckDuckGo may rate limit, try later

---

## STEP 5: MANUAL LOCALITY DATA ENTRY

**Action**: Fill in metro, schools, hospitals for 5 target areas

**File**: `LOCALITY_DATA_TEMPLATE.txt`

**Time**: ~1 hour (all 5 areas)

**Steps**:
1. Open `LOCALITY_DATA_TEMPLATE.txt`
2. For each area (Gomti Nagar, Alambagh, etc.):
   - Use Google Maps to find nearest metro station
   - List 3-5 reputable schools
   - List 3-5 hospitals/clinics
   - Measure distances using Google Maps
3. Save template
4. Convert to JSON:

**Command**:
```powershell
.\venv\Scripts\python.exe scripts\convert_locality_template.py
```

**Output File**: `data/locality_info.json`

---

## STEP 6: CONSOLIDATE DATA

**Action**: Merge all data sources and organize by area

**Command**:
```powershell
.\venv\Scripts\python.exe scripts\consolidate_area_wise.py
```

**Expected Output**:
```
=== DATA CONSOLIDATION & AREA-WISE ORGANIZATION ===

📂 Loading RERA projects...
✅ Loaded 750 RERA projects

📂 Loading property listings...
✅ Loaded 87 property listings

📂 Loading locality information...
✅ Loaded locality data for 5 areas

📊 Calculating statistics...

💾 Saving area-wise data...
  ✅ Gomti Nagar: cleaned_data/area_wise/gomti_nagar.json
     Projects: 180, Listings: 22
  ✅ Alambagh: cleaned_data/area_wise/alambagh.json
     Projects: 140, Listings: 15
  ...

✅ CONSOLIDATION COMPLETE!
Total areas with data: 6
Total RERA projects: 750
Total property listings: 87
```

**Output Files**:
- `cleaned_data/area_wise/gomti_nagar.json`
- `cleaned_data/area_wise/alambagh.json`
- `cleaned_data/area_wise/hazratganj.json`
- `cleaned_data/area_wise/indira_nagar.json`
- `cleaned_data/area_wise/aliganj.json`
- `cleaned_data/area_wise/other_lucknow.json`
- `cleaned_data/area_wise/summary.json`

---

## STEP 7: VALIDATE DATA

**Action**: Clean and validate all collected data

**Command**:
```powershell
.\venv\Scripts\python.exe scripts\validate_data.py
```

**Expected Output**:
```
=== DATA VALIDATION & CLEANING ===

Validating properties_realtime.json...
  ✓ 87 properties validated
  ⚠ Removed 3 properties with missing required fields

Validating rera_projects.json...
  ✓ 750 projects validated
  ⚠ Removed 12 projects with invalid data

✅ VALIDATION COMPLETE!
Clean data saved to: cleaned_data/
```

---

## 📊 FINAL VERIFICATION

**Check collected data**:
```powershell
# Count RERA projects
.\venv\Scripts\python.exe -c "import json; print('RERA Projects:', len(json.load(open('data/rera_projects.json', encoding='utf-8'))))"

# Count property listings
.\venv\Scripts\python.exe -c "import json; print('Property Listings:', len(json.load(open('data/properties_realtime.json', encoding='utf-8'))))"

# Show area-wise summary
.\venv\Scripts\python.exe -c "import json; print(json.dumps(json.load(open('cleaned_data/area_wise/summary.json', encoding='utf-8')), indent=2))"
```

---

## ✅ SUCCESS CRITERIA

**Data Collection Complete When**:
- ✅ `data/rera_projects.json` has 500+ projects
- ✅ `data/properties_realtime.json` has 50+ listings
- ✅ `data/locality_info.json` has data for 5 areas
- ✅ `cleaned_data/area_wise/` has 5-6 area JSON files
- ✅ Each area file has RERA projects + listings + locality info

---

## 🚨 COMMON ISSUES

### Issue: Scrapy spider collects no data
**Solution**: CSS selectors are incorrect
- Go back to Step 2
- Carefully inspect UP-RERA website
- Update all selectors in `uprera_spider.py`

### Issue: CAPTCHA blocks scraping
**Solution**: Manual intervention
- Pause scraper (Ctrl+C)
- Solve CAPTCHA in browser
- Increase `DOWNLOAD_DELAY` to 5-10 seconds
- Resume scraper

### Issue: DuckDuckGo returns no results
**Solution**: Rate limiting
- Wait 10-15 minutes
- Run script again
- Consider reducing areas/property types

### Issue: LLM extraction fails
**Solution**: API issues
- Check OpenRouter API key is valid
- Check internet connection
- Try different model: `meta-llama/llama-3.2-1b-instruct:free`

---

## 📁 EXPECTED FOLDER STRUCTURE (After Completion)

```
real-estate-ai-agent/
├── data/
│   ├── rera_projects.json          (500-1000 projects)
│   ├── properties_realtime.json    (50-100 listings)
│   ├── locality_info.json          (5 areas)
│   └── market_prices.json          (optional)
│
├── cleaned_data/
│   └── area_wise/
│       ├── gomti_nagar.json        (180 projects, 22 listings)
│       ├── alambagh.json
│       ├── hazratganj.json
│       ├── indira_nagar.json
│       ├── aliganj.json
│       ├── other_lucknow.json
│       └── summary.json
│
├── scripts/
│   ├── uprera_spider.py            (UPDATED with selectors)
│   ├── collect_realtime_legal.py
│   ├── consolidate_area_wise.py
│   ├── validate_data.py
│   └── run_uprera_scraper.py
│
└── logs/
    └── scraping.log
```

---

## 🎯 NEXT PHASE: RAG AGENT

After data collection is complete:
1. **Chunk data** for RAG (embeddings)
2. **Build vector database** (ChromaDB/FAISS)
3. **Create AI agent** for property queries
4. **Add buyer qualification** logic
5. **Implement scheduling** automation

---

**Need Help?**
- Check console output for specific errors
- Review logs in `logs/` directory
- Verify CSS selectors match current UP-RERA website structure
