"""
Government Open Data Collector
Collects real estate data from legal government sources
- Smart Cities Data Portal (circle rates, infrastructure)
- UP Open Data Portal (housing, property data)
- data.gov.in (national datasets)
"""
import requests
import json
import os
from datetime import datetime


def fetch_smart_cities_circle_rates():
    """
    Fetch circle rate data from Smart Cities Data Portal
    https://smartcities.data.gov.in/
    """
    print("\n" + "=" * 70)
    print("FETCHING: Smart Cities Circle Rates for Lucknow")
    print("=" * 70)
    
    # API endpoint for Lucknow circle rates
    # This is a popular dataset with 13,873 views, 904 downloads
    api_url = "https://smartcities.data.gov.in/resources/circle-rate-lucknow"
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        
        print(f"📡 Requesting: {api_url}")
        response = requests.get(api_url, headers=headers, timeout=15)
        
        if response.status_code == 200:
            print("✅ Successfully fetched data")
            data = response.json() if 'json' in response.headers.get('content-type', '') else None
            
            if data:
                # Save raw data
                os.makedirs('data', exist_ok=True)
                output_file = 'data/circle_rates_smartcities.json'
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                print(f"💾 Saved to: {output_file}")
                return data
            else:
                # If JSON parsing fails, try CSV
                print("⚠️  Response is not JSON, trying CSV...")
                csv_content = response.text
                output_file = 'data/circle_rates_smartcities.csv'
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(csv_content)
                print(f"💾 Saved CSV to: {output_file}")
                return csv_content
        else:
            print(f"⚠️  HTTP {response.status_code}: {response.reason}")
            return None
            
    except requests.exceptions.Timeout:
        print("❌ Request timeout - server slow or unreachable")
        return None
    except requests.exceptions.RequestException as e:
        print(f"❌ Request error: {e}")
        return None


def fetch_up_open_data():
    """
    Fetch housing data from UP Open Data Portal
    https://up.data.gov.in/
    """
    print("\n" + "=" * 70)
    print("FETCHING: UP Open Data Portal - Housing Sector")
    print("=" * 70)
    
    # UP Open Data API endpoint
    api_url = "https://up.data.gov.in/api/3/action/package_search"
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        
        # Search for housing/real estate related datasets
        params = {
            'q': 'housing OR real estate OR property OR lucknow',
            'rows': 50
        }
        
        print(f"📡 Requesting: {api_url}")
        print(f"🔍 Search query: {params['q']}")
        
        response = requests.get(api_url, headers=headers, params=params, timeout=15)
        
        if response.status_code == 200:
            print("✅ Successfully fetched data")
            data = response.json()
            
            if data.get('success'):
                results = data.get('result', {}).get('results', [])
                print(f"📊 Found {len(results)} datasets")
                
                # Save catalog
                os.makedirs('data', exist_ok=True)
                output_file = 'data/up_opendata_housing_catalog.json'
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                print(f"💾 Saved catalog to: {output_file}")
                
                # Download first few relevant datasets
                for idx, dataset in enumerate(results[:5]):
                    print(f"\n  [{idx+1}] {dataset.get('title', 'Untitled')}")
                    print(f"      Organization: {dataset.get('organization', {}).get('title', 'N/A')}")
                    
                    resources = dataset.get('resources', [])
                    for resource in resources[:1]:  # Get first resource
                        resource_url = resource.get('url')
                        resource_format = resource.get('format', 'unknown')
                        
                        if resource_url:
                            print(f"      Format: {resource_format}")
                            print(f"      URL: {resource_url}")
                            
                            try:
                                res_response = requests.get(resource_url, headers=headers, timeout=15)
                                if res_response.status_code == 200:
                                    filename = f"data/up_dataset_{idx+1}.{resource_format.lower()}"
                                    with open(filename, 'wb') as f:
                                        f.write(res_response.content)
                                    print(f"      ✅ Downloaded to: {filename}")
                            except Exception as e:
                                print(f"      ⚠️  Download failed: {e}")
                
                return data
            else:
                print("⚠️  API returned success=False")
                return None
        else:
            print(f"⚠️  HTTP {response.status_code}: {response.reason}")
            return None
            
    except requests.exceptions.Timeout:
        print("❌ Request timeout")
        return None
    except requests.exceptions.RequestException as e:
        print(f"❌ Request error: {e}")
        return None


def fetch_national_data_portal():
    """
    Fetch data from National Open Data Portal
    https://data.gov.in/
    """
    print("\n" + "=" * 70)
    print("FETCHING: National Data Portal - Lucknow Real Estate")
    print("=" * 70)
    
    # data.gov.in API endpoint
    api_url = "https://data.gov.in/api/datastore/resource.json"
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        
        # Example: Property registration statistics
        params = {
            'resource_id': '6176ee09-3d56-4a3b-8115-21841bdd3ae9',  # Example resource ID
            'limit': 1000
        }
        
        print(f"📡 Requesting: {api_url}")
        
        response = requests.get(api_url, headers=headers, params=params, timeout=15)
        
        if response.status_code == 200:
            print("✅ Successfully fetched data")
            data = response.json()
            
            records = data.get('records', [])
            print(f"📊 Found {len(records)} records")
            
            if records:
                # Save data
                os.makedirs('data', exist_ok=True)
                output_file = 'data/national_portal_data.json'
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                print(f"💾 Saved to: {output_file}")
                return data
            else:
                print("⚠️  No records found")
                return None
        else:
            print(f"⚠️  HTTP {response.status_code}: {response.reason}")
            print("💡 Note: This endpoint requires a valid resource_id")
            print("   Visit https://data.gov.in/ to browse and find specific dataset IDs")
            return None
            
    except requests.exceptions.Timeout:
        print("❌ Request timeout")
        return None
    except requests.exceptions.RequestException as e:
        print(f"❌ Request error: {e}")
        return None


def search_data_gov_catalog():
    """
    Search data.gov.in catalog for Lucknow real estate datasets
    """
    print("\n" + "=" * 70)
    print("SEARCHING: data.gov.in Catalog for Lucknow Datasets")
    print("=" * 70)
    
    # Catalog search endpoint
    api_url = "https://data.gov.in/api/3/action/package_search"
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        
        params = {
            'q': 'lucknow property OR lucknow housing OR lucknow circle rate OR lucknow registration',
            'rows': 20,
            'start': 0
        }
        
        print(f"📡 Requesting: {api_url}")
        print(f"🔍 Search query: {params['q']}")
        
        response = requests.get(api_url, headers=headers, params=params, timeout=15)
        
        if response.status_code == 200:
            print("✅ Successfully fetched catalog")
            data = response.json()
            
            if data.get('success'):
                results = data.get('result', {}).get('results', [])
                total_count = data.get('result', {}).get('count', 0)
                print(f"📊 Found {total_count} total datasets, showing first {len(results)}")
                
                print("\n📋 AVAILABLE DATASETS:")
                print("-" * 70)
                
                for idx, dataset in enumerate(results, 1):
                    title = dataset.get('title', 'Untitled')
                    org = dataset.get('organization', {}).get('title', 'N/A')
                    resources = len(dataset.get('resources', []))
                    
                    print(f"\n{idx}. {title}")
                    print(f"   Organization: {org}")
                    print(f"   Resources: {resources}")
                    
                    # Show resource details
                    for resource in dataset.get('resources', [])[:2]:
                        res_format = resource.get('format', 'unknown')
                        res_url = resource.get('url', '')
                        print(f"   - Format: {res_format}, URL: {res_url[:60]}...")
                
                # Save catalog
                os.makedirs('data', exist_ok=True)
                output_file = 'data/datagovin_catalog.json'
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                print(f"\n💾 Full catalog saved to: {output_file}")
                
                return data
            else:
                print("⚠️  API returned success=False")
                return None
        else:
            print(f"⚠️  HTTP {response.status_code}: {response.reason}")
            return None
            
    except requests.exceptions.Timeout:
        print("❌ Request timeout")
        return None
    except requests.exceptions.RequestException as e:
        print(f"❌ Request error: {e}")
        return None


def main():
    """
    Main function to orchestrate data collection
    """
    print("\n" + "=" * 70)
    print("GOVERNMENT OPEN DATA COLLECTOR")
    print("Legal, Free, and Public Real Estate Data Sources")
    print("=" * 70)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = {}
    
    # 1. Smart Cities Data Portal
    print("\n[1/4] Smart Cities Data Portal...")
    results['smart_cities'] = fetch_smart_cities_circle_rates()
    
    # 2. UP Open Data Portal
    print("\n[2/4] UP Open Data Portal...")
    results['up_opendata'] = fetch_up_open_data()
    
    # 3. Search National Data Portal Catalog
    print("\n[3/4] National Data Portal Catalog Search...")
    results['datagovin_catalog'] = search_data_gov_catalog()
    
    # 4. Try fetching from National Data Portal (if resource_id available)
    print("\n[4/4] National Data Portal (Example Resource)...")
    results['national_portal'] = fetch_national_data_portal()
    
    # Summary
    print("\n" + "=" * 70)
    print("✅ DATA COLLECTION COMPLETE!")
    print("=" * 70)
    
    successful = sum(1 for v in results.values() if v is not None)
    total = len(results)
    
    print(f"\nSuccessfully collected: {successful}/{total} sources")
    print("\n📁 Check 'data/' folder for downloaded files:")
    print("   - circle_rates_smartcities.json/csv")
    print("   - up_opendata_housing_catalog.json")
    print("   - up_dataset_*.json/csv")
    print("   - datagovin_catalog.json")
    print("   - national_portal_data.json")
    
    print("\n💡 NEXT STEPS:")
    print("   1. Review downloaded data files")
    print("   2. Run consolidation script to merge with existing data")
    print("   3. Validate and clean data")
    print("=" * 70)


if __name__ == "__main__":
    main()
