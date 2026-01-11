# Real-Time Data Collection Guide (FREE & LEGAL)

## 🚀 Quick Start

### Install Additional Dependencies
```bash
.\venv\Scripts\pip.exe install duckduckgo-search
```

### Run Real-Time Collection (Legal & Free)
```bash
.\venv\Scripts\python.exe scripts\collect_realtime_legal.py
```

This will:
- Search DuckDuckGo for Lucknow properties (FREE, no API key)
- Extract property data using AI
- Save to `data/properties_realtime.json`

---

## 📊 Data Collection Options

### Option 1: Real-Time Automated (DuckDuckGo) ⚡
**Status**: ✅ Implemented  
**Script**: `collect_realtime_legal.py`  
**Cost**: FREE  
**Credit Card**: NOT required  
**Legal**: ✅ Using public search API

**Limitations**:
- May get blocked by property sites
- Results depend on search quality
- ~5-10 properties per area

**Usage**:
```bash
python scripts\collect_realtime_legal.py
```

---

### Option 2: UP-RERA Official Data 🏛️ (BEST)
**Status**: 📝 Manual process  
**Script**: `rera_guide.py`  
**Cost**: FREE (₹10 RTI fee)  
**Credit Card**: NOT required  
**Legal**: ✅✅✅ 100% Legal - official govt data

**Steps**:
1. Run guide: `python scripts\rera_guide.py`
2. File RTI request OR email UP-RERA
3. Wait 2-4 weeks for response
4. Get verified project data

**Data Quality**: ⭐⭐⭐⭐⭐ (Official, verified)

---

### Option 3: Manual Collection 📝
**Status**: ✅ Template ready  
**File**: `LOCALITY_DATA_TEMPLATE.txt`  
**Cost**: FREE  
**Time**: ~2-4 hours  

**For**:
- Locality infrastructure (metro, schools, hospitals)
- Initial property samples
- Data verification

---

## ⚠️ What NOT To Do

### ❌ Selenium Scraping
- Violates ToS of 99acres, Magicbricks, Housing.com
- Legal risk: Could face lawsuits
- Ethical: Not respectful of site owners
- **DO NOT USE SELENIUM FOR PROPERTY SITES**

### ❌ Unauthorized Web Scraping
- robots.txt violations
- Aggressive crawling
- Ignoring rate limits

---

## ✅ Legal Data Collection Checklist

- [x] DuckDuckGo search API (public search)
- [x] OpenAI/OpenRouter for extraction
- [ ] UP-RERA official data (file RTI/email)
- [ ] data.gov.in government datasets
- [ ] OpenStreetMap for location data
- [x] Manual collection with proper attribution

---

## 📈 Expected Results

### With DuckDuckGo (Automated)
- **Properties**: 5-15 per area
- **Time**: 30-60 mins
- **Quality**: Medium (depends on what's accessible)

### With UP-RERA (Official)
- **Properties**: Hundreds of verified projects
- **Time**: 2-4 weeks (waiting for response)
- **Quality**: HIGH (official data)

### With Manual Collection
- **Properties**: As many as you collect
- **Time**: ~10 mins per property
- **Quality**: HIGH (verified by you)

---

## 🎯 Recommended Workflow

**Week 1**:
1. ✅ Run `collect_realtime_legal.py` for initial data
2. ✅ Fill `LOCALITY_DATA_TEMPLATE.txt` manually
3. 📧 File RTI with UP-RERA
4. 📧 Email UP-RERA for API access

**Week 2-3**:
- Manually collect 20-30 sample properties for quality baseline
- Validate automated collection results
- Enrich with OpenStreetMap data

**Week 4+**:
- Receive UP-RERA data (if approved)
- Build full dataset from official sources
- Automate updates with legal APIs

---

## 🆘 Troubleshooting

### "duckduckgo-search not installed"
```bash
.\venv\Scripts\pip.exe install duckduckgo-search
```

### "No properties collected"
- Property sites may block automated access
- Try manual collection for initial dataset
- Focus on UP-RERA official data request

### "API rate limit"
- DuckDuckGo has built-in rate limiting
- Script includes 3-5 sec delays
- Reduce max_listings if needed

---

## 💡 Pro Tips

1. **Start with manual collection** for 10-20 properties to understand data structure
2. **File RTI immediately** - it takes 2-4 weeks to get response
3. **Combine sources**: Manual + DuckDuckGo + RERA = comprehensive dataset
4. **Focus on quality over quantity** - 50 verified properties > 500 scraped ones
5. **Document sources** - always note where each data point came from

---

## 📞 Support

For issues or questions:
1. Check script comments for integration notes
2. Review `SETUP_COMPLETE.md` for setup issues
3. Run `python scripts\test_api.py` to verify API connection

---

**Remember**: Legal and ethical data collection builds a sustainable project. The best data comes from official sources like UP-RERA! 🏠✨
