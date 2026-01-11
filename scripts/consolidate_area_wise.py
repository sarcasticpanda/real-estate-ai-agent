"""
Data Consolidation Script
Merges data from all sources and organizes by area
"""
import os
import json
from collections import defaultdict
from datetime import datetime


def load_json_file(filepath):
    """Load JSON file"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"⚠️  File not found: {filepath}")
        return None
    except json.JSONDecodeError as e:
        print(f"⚠️  Invalid JSON in {filepath}: {e}")
        return None


def consolidate_data():
    """
    Main consolidation function
    """
    print("=" * 70)
    print("DATA CONSOLIDATION & AREA-WISE ORGANIZATION")
    print("=" * 70)
    
    # Target areas
    target_areas = ['Gomti Nagar', 'Alambagh', 'Hazratganj', 'Indira Nagar', 'Aliganj']
    
    # Initialize area data
    area_data = {area: {
        'area_name': area,
        'city': 'Lucknow',
        'rera_projects': [],
        'property_listings': [],
        'locality_info': {},
        'market_prices': {},
        'osm_data': {},
        'statistics': {
            'total_projects': 0,
            'total_listings': 0,
            'avg_price': 0,
            'collected_at': datetime.now().isoformat()
        }
    } for area in target_areas}
    
    # Add "Other Lucknow" for unclassified areas
    area_data['Other Lucknow'] = {
        'area_name': 'Other Lucknow',
        'city': 'Lucknow',
        'rera_projects': [],
        'property_listings': [],
        'locality_info': {},
        'market_prices': {},
        'osm_data': {},
        'statistics': {
            'total_projects': 0,
            'total_listings': 0,
            'avg_price': 0,
            'collected_at': datetime.now().isoformat()
        }
    }
    
    # Load RERA projects
    print("\n📂 Loading RERA projects...")
    rera_projects = load_json_file("data/rera_projects.json")
    if rera_projects:
        for project in rera_projects:
            area = project.get('area', 'Other Lucknow')
            if area not in area_data:
                area = 'Other Lucknow'
            area_data[area]['rera_projects'].append(project)
            area_data[area]['statistics']['total_projects'] += 1
        print(f"✅ Loaded {len(rera_projects)} RERA projects")
    
    # Load property listings
    print("\n📂 Loading property listings...")
    properties = load_json_file("data/properties_realtime.json")
    if properties:
        for prop in properties:
            area = prop.get('area_name', 'Unknown')
            if area not in area_data:
                area = 'Other Lucknow'
            area_data[area]['property_listings'].append(prop)
            area_data[area]['statistics']['total_listings'] += 1
        print(f"✅ Loaded {len(properties)} property listings")
    
    # Load locality info
    print("\n📂 Loading locality information...")
    locality_info = load_json_file("data/locality_info.json")
    if locality_info:
        for loc in locality_info:
            area = loc.get('area', '')
            if area in area_data:
                area_data[area]['locality_info'] = loc
        print(f"✅ Loaded locality data for {len(locality_info)} areas")
    
    # Load market prices
    print("\n📂 Loading market prices...")
    market_prices = load_json_file("data/market_prices.json")
    if market_prices:
        for price in market_prices:
            area = price.get('area', '')
            if area in area_data:
                area_data[area]['market_prices'] = price
        print(f"✅ Loaded market data for {len(market_prices)} areas")
    
    # Calculate statistics
    print("\n📊 Calculating statistics...")
    for area_name, data in area_data.items():
        listings = data['property_listings']
        if listings:
            prices = [p.get('price', 0) for p in listings if p.get('price')]
            if prices:
                data['statistics']['avg_price'] = sum(prices) / len(prices)
                data['statistics']['min_price'] = min(prices)
                data['statistics']['max_price'] = max(prices)
    
    # Save consolidated data
    print("\n💾 Saving area-wise data...")
    os.makedirs("cleaned_data/area_wise", exist_ok=True)
    
    for area_name, data in area_data.items():
        # Skip areas with no data
        if (data['statistics']['total_projects'] == 0 and 
            data['statistics']['total_listings'] == 0):
            continue
        
        # Create filename-safe area name
        filename = area_name.lower().replace(' ', '_') + '.json'
        filepath = os.path.join("cleaned_data", "area_wise", filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"  ✅ {area_name}: {filepath}")
        print(f"     Projects: {data['statistics']['total_projects']}, " 
              f"Listings: {data['statistics']['total_listings']}")
    
    # Save summary
    summary = {
        'total_areas': len([a for a in area_data.values() if a['statistics']['total_projects'] > 0 or a['statistics']['total_listings'] > 0]),
        'areas': [a for a in area_data.keys() if area_data[a]['statistics']['total_projects'] > 0 or area_data[a]['statistics']['total_listings'] > 0],
        'total_rera_projects': sum(d['statistics']['total_projects'] for d in area_data.values()),
        'total_property_listings': sum(d['statistics']['total_listings'] for d in area_data.values()),
        'consolidated_at': datetime.now().isoformat()
    }
    
    with open("cleaned_data/area_wise/summary.json", 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    
    print("\n" + "=" * 70)
    print("✅ CONSOLIDATION COMPLETE!")
    print("=" * 70)
    print(f"\nTotal areas with data: {summary['total_areas']}")
    print(f"Total RERA projects: {summary['total_rera_projects']}")
    print(f"Total property listings: {summary['total_property_listings']}")
    print(f"\nArea-wise files saved to: cleaned_data/area_wise/")
    print("=" * 70)


if __name__ == "__main__":
    consolidate_data()
