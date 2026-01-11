"""
Property Data Collection Script
Fetches property listings from search results using OpenRouter LLM for extraction
"""
import os
import json
import time
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime

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
OUTPUT_FILE = "data/properties.json"

# Search queries for each area
SEARCH_QUERIES = [
    "{area} {city} 2BHK apartment",
    "{area} {city} 3BHK flat",
    "{area} {city} property for sale"
]

# Property listing websites
TARGET_SITES = ["99acres.com", "magicbricks.com", "housing.com"]


def search_properties(area, city):
    """
    Simulate property search results
    In production, this would use a search API or web scraping
    """
    print(f"\n🔍 Searching properties in {area}, {city}...")
    
    # For demonstration, we'll create mock URLs
    # In production, replace this with actual Google Custom Search API or web scraping
    sample_urls = [
        f"https://www.99acres.com/{area.lower().replace(' ', '-')}-{city.lower()}-property",
        f"https://www.magicbricks.com/{area.lower().replace(' ', '-')}-{city.lower()}-properties",
        f"https://www.housing.com/{area.lower().replace(' ', '-')}-{city.lower()}"
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
        
        # Get text content
        text = soup.get_text(separator='\n', strip=True)
        
        # Limit text length to avoid token limits
        return text[:5000]
    
    except Exception as e:
        print(f"  ⚠️  Error fetching {url}: {e}")
        return None


def extract_property_data(page_content, area, city, source):
    """
    Use LLM to extract structured property data from page content
    """
    if not page_content:
        return None
    
    prompt = f"""
Extract property listing information from the following text. Return ONLY valid JSON, no other text.

Extract these fields:
- area_name (string)
- city (string)
- bhk (number, e.g., 2 for 2BHK)
- price (number in rupees)
- price_per_sqft (number)
- property_type (string: "Apartment", "Villa", "Studio", etc.)
- built_up_area (number in sqft)
- carpet_area (number in sqft, if available)
- floor (number, current floor)
- total_floors (number)
- property_age (string: "New", "5 years", etc.)
- status (string: "Ready", "Under Construction")
- furnishing (string: "Furnished", "Semi-furnished", "Unfurnished")
- amenities (array of strings)
- description (string, 2-3 clean lines, no sales pitch)

IMPORTANT RULES:
- Do NOT extract: owner name, phone number, exact flat/unit number
- If a field is not found, use null
- Return ONLY valid JSON, nothing else

Property is in: {area}, {city}
Source website: {source}

Text content:
{page_content}

JSON:
"""
    
    try:
        response = client.chat.completions.create(
            model="meta-llama/llama-3.1-8b-instruct:free",
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.1
        )
        
        result = response.choices[0].message.content.strip()
        
        # Try to parse JSON
        # Remove markdown code blocks if present
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        
        property_data = json.loads(result)
        property_data["source"] = source
        property_data["collected_at"] = datetime.now().isoformat()
        
        return property_data
    
    except Exception as e:
        print(f"  ⚠️  Error extracting data: {e}")
        return None


def collect_properties_for_area(area, city):
    """
    Collect property data for a specific area
    """
    properties = []
    
    # Get search results (URLs)
    urls = search_properties(area, city)
    
    for url in urls:
        print(f"  📄 Fetching: {url}")
        
        # Fetch page content
        content = fetch_page_content(url)
        
        if content:
            # Extract structured data using LLM
            source = url.split("//")[1].split("/")[0].replace("www.", "")
            property_data = extract_property_data(content, area, city, source)
            
            if property_data:
                properties.append(property_data)
                print(f"  ✅ Extracted property data")
            
            # Rate limiting
            time.sleep(2)
    
    return properties


def main():
    """
    Main execution function
    """
    print("=" * 60)
    print("PROPERTY DATA COLLECTION SCRIPT")
    print("=" * 60)
    
    # Check if data directory exists
    os.makedirs("data", exist_ok=True)
    
    all_properties = []
    
    # Collect properties for each area
    for area in TARGET_AREAS:
        area = area.strip()
        print(f"\n{'=' * 60}")
        print(f"COLLECTING DATA FOR: {area}")
        print(f"{'=' * 60}")
        
        properties = collect_properties_for_area(area, TARGET_CITY)
        all_properties.extend(properties)
        
        print(f"\n✅ Collected {len(properties)} properties from {area}")
    
    # Save to JSON file
    if all_properties:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_properties, f, indent=2, ensure_ascii=False)
        
        print(f"\n{'=' * 60}")
        print(f"✅ COLLECTION COMPLETE!")
        print(f"Total properties collected: {len(all_properties)}")
        print(f"Saved to: {OUTPUT_FILE}")
        print(f"{'=' * 60}")
    else:
        print("\n⚠️  No properties collected")


if __name__ == "__main__":
    main()
