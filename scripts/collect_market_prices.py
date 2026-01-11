"""
Market Price Data Collection Script
Collects average price per sqft trends for target areas
"""
import os
import json
import time
import requests
from bs4 import BeautifulSoup
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
OUTPUT_FILE = "data/market_prices.json"


def search_market_prices(area, city):
    """
    Simulate market price search
    In production, this would use search API or scraping
    """
    print(f"\n🔍 Searching market prices for {area}, {city}...")
    
    # Mock URLs for demonstration
    sample_urls = [
        f"https://www.magicbricks.com/{area.lower().replace(' ', '-')}-{city.lower()}-price-trends",
        f"https://www.99acres.com/{area.lower().replace(' ', '-')}-{city.lower()}-property-rates"
    ]
    
    return sample_urls


def fetch_page_content(url):
    """
    Fetch page content from URL
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
        
        text = soup.get_text(separator='\n', strip=True)
        return text[:3000]
    
    except Exception as e:
        print(f"  ⚠️  Error fetching {url}: {e}")
        return None


def extract_market_data(page_content, area):
    """
    Use LLM to extract market price data
    """
    if not page_content:
        return None
    
    prompt = f"""
Extract market price information for {area} from the following text. Return ONLY valid JSON, no other text.

Extract these fields:
- area (string)
- avg_price_per_sqft (number in rupees)
- min_price_per_sqft (number in rupees)
- max_price_per_sqft (number in rupees)
- trend (string: "up", "down", or "stable")

If exact values not found, provide reasonable estimates based on context.
Return ONLY valid JSON, nothing else.

Text content:
{page_content}

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
        
        # Remove markdown code blocks if present
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        
        market_data = json.loads(result)
        return market_data
    
    except Exception as e:
        print(f"  ⚠️  Error extracting market data: {e}")
        return None


def collect_market_prices_for_area(area, city):
    """
    Collect market price data for a specific area
    """
    urls = search_market_prices(area, city)
    
    for url in urls:
        print(f"  📄 Fetching: {url}")
        
        content = fetch_page_content(url)
        
        if content:
            market_data = extract_market_data(content, area)
            
            if market_data:
                print(f"  ✅ Extracted market data for {area}")
                time.sleep(2)
                return market_data
            
            time.sleep(2)
    
    return None


def main():
    """
    Main execution function
    """
    print("=" * 60)
    print("MARKET PRICE DATA COLLECTION SCRIPT")
    print("=" * 60)
    
    os.makedirs("data", exist_ok=True)
    
    all_market_data = []
    
    for area in TARGET_AREAS:
        area = area.strip()
        print(f"\n{'=' * 60}")
        print(f"COLLECTING MARKET DATA FOR: {area}")
        print(f"{'=' * 60}")
        
        market_data = collect_market_prices_for_area(area, TARGET_CITY)
        
        if market_data:
            all_market_data.append(market_data)
    
    # Save to JSON file
    if all_market_data:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_market_data, f, indent=2, ensure_ascii=False)
        
        print(f"\n{'=' * 60}")
        print(f"✅ COLLECTION COMPLETE!")
        print(f"Total areas covered: {len(all_market_data)}")
        print(f"Saved to: {OUTPUT_FILE}")
        print(f"{'=' * 60}")
    else:
        print("\n⚠️  No market data collected")


if __name__ == "__main__":
    main()
