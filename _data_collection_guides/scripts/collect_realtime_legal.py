"""
Free Real-Time Property Data Collection Using Legal APIs
Uses DuckDuckGo and Tavily (both FREE, no credit card needed)
"""
import os
import json
import time
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize OpenRouter client
client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)

# Configuration
TARGET_CITY = os.getenv("TARGET_CITY", "Lucknow")
TARGET_AREAS = os.getenv("TARGET_AREAS", "Gomti Nagar,Alambagh,Hazratganj,Indira Nagar,Aliganj").split(",")
OUTPUT_FILE = "data/properties_realtime.json"


def search_duckduckgo(query, max_results=5):
    """
    Search DuckDuckGo for property listings (FREE, no API key needed)
    """
    try:
        from duckduckgo_search import DDGS
        
        print(f"  🔍 Searching DuckDuckGo: {query}")
        
        with DDGS() as ddgs:
            results = []
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    'title': r.get('title', ''),
                    'url': r.get('href', ''),
                    'snippet': r.get('body', '')
                })
            
            print(f"  ✅ Found {len(results)} results")
            return results
    
    except ImportError:
        print("\n⚠️  duckduckgo-search not installed!")
        print("Install with: pip install duckduckgo-search")
        return []
    except Exception as e:
        print(f"  ⚠️  Search error: {e}")
        return []


def fetch_page_with_requests(url):
    """
    Fetch page content using requests
    """
    try:
        import requests
        from bs4 import BeautifulSoup
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
        
        # Get text content
        text = soup.get_text(separator='\n', strip=True)
        
        # Limit text length
        return text[:5000]
    
    except Exception as e:
        print(f"  ⚠️  Error fetching {url}: {e}")
        return None


def extract_property_from_text(text, snippet, url, area):
    """
    Use LLM to extract property data from text
    """
    if not text and not snippet:
        return None
    
    content = f"{snippet}\n\n{text}" if text else snippet
    
    prompt = f"""
Extract property information from this text. Return ONLY valid JSON, nothing else.

Fields to extract:
- area_name: {area}
- city: Lucknow
- bhk: number (e.g., 2 for 2BHK)
- price: number in rupees (convert lakhs/crores to rupees)
- price_per_sqft: number (if available)
- property_type: "Apartment", "Villa", "House", "Studio"
- built_up_area: number in sqft
- status: "Ready", "Under Construction", "New"
- amenities: array of strings
- description: short summary (2-3 lines)

RULES:
- Do NOT extract: phone, owner name, agent name
- If field not found, use null
- Return ONLY valid JSON

Text:
{content[:3000]}

JSON:
"""
    
    try:
        response = client.chat.completions.create(
            model="meta-llama/llama-3.2-3b-instruct:free",
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.1
        )
        
        result = response.choices[0].message.content.strip()
        
        # Clean JSON
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        
        property_data = json.loads(result)
        property_data["source_url"] = url
        property_data["collected_at"] = datetime.now().isoformat()
        
        return property_data
    
    except Exception as e:
        print(f"  ⚠️  Extraction error: {e}")
        return None


def collect_properties_realtime(area, city, max_listings=10):
    """
    Collect real-time property data for an area using free search APIs
    """
    properties = []
    
    # Search queries
    queries = [
        f"2BHK property {area} {city} site:99acres.com OR site:magicbricks.com OR site:housing.com",
        f"3BHK apartment {area} {city} for sale",
        f"flat {area} {city} price",
    ]
    
    for query in queries:
        if len(properties) >= max_listings:
            break
        
        # Search with DuckDuckGo
        results = search_duckduckgo(query, max_results=5)
        
        for result in results:
            if len(properties) >= max_listings:
                break
            
            print(f"\n  📄 Processing: {result['title'][:60]}...")
            
            # Try to fetch page content
            page_content = fetch_page_with_requests(result['url'])
            
            # Extract property data
            property_data = extract_property_from_text(
                page_content,
                result['snippet'],
                result['url'],
                area
            )
            
            if property_data and property_data.get('price'):
                properties.append(property_data)
                print(f"  ✅ Extracted property: {property_data.get('bhk')}BHK @ ₹{property_data.get('price')}")
            
            # Rate limiting (respectful scraping)
            time.sleep(3)
    
    return properties


def main():
    """
    Main execution function
    """
    print("=" * 70)
    print("REAL-TIME PROPERTY DATA COLLECTION (FREE & LEGAL)")
    print("Using DuckDuckGo Search API (No API Key Required)")
    print("=" * 70)
    
    # Check dependencies
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        print("\n❌ Missing dependency: duckduckgo-search")
        print("\nInstall with:")
        print("  pip install duckduckgo-search")
        print("\nThen run this script again.")
        return
    
    os.makedirs("data", exist_ok=True)
    
    all_properties = []
    
    for area in TARGET_AREAS:
        area = area.strip()
        print(f"\n{'=' * 70}")
        print(f"COLLECTING DATA FOR: {area}, {TARGET_CITY}")
        print(f"{'=' * 70}")
        
        properties = collect_properties_realtime(area, TARGET_CITY, max_listings=10)
        all_properties.extend(properties)
        
        print(f"\n✅ Collected {len(properties)} properties from {area}")
        
        # Brief pause between areas
        time.sleep(5)
    
    # Save to JSON
    if all_properties:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_properties, f, indent=2, ensure_ascii=False)
        
        print(f"\n{'=' * 70}")
        print(f"✅ COLLECTION COMPLETE!")
        print(f"Total properties collected: {len(all_properties)}")
        print(f"Saved to: {OUTPUT_FILE}")
        print(f"{'=' * 70}")
        
        # Show summary by area
        print("\nSummary by Area:")
        from collections import Counter
        area_counts = Counter([p.get('area_name', 'Unknown') for p in all_properties])
        for area, count in area_counts.items():
            print(f"  {area}: {count} properties")
    else:
        print("\n⚠️  No properties collected")
        print("\nPossible reasons:")
        print("  - No search results found")
        print("  - Pages couldn't be fetched (anti-scraping measures)")
        print("  - LLM couldn't extract data")
        print("\nTry:")
        print("  - Manual data collection for initial dataset")
        print("  - Contact UP-RERA for official data access")


if __name__ == "__main__":
    main()
