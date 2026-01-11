"""
Data Validation Script
Validates and cleans collected data, moves to cleaned_data folder
"""
import os
import json
from datetime import datetime


def load_json_file(filepath):
    """
    Load JSON file
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"⚠️  File not found: {filepath}")
        return None
    except json.JSONDecodeError as e:
        print(f"⚠️  Invalid JSON in {filepath}: {e}")
        return None


def validate_properties(properties):
    """
    Validate property listings
    """
    print("\n" + "=" * 60)
    print("VALIDATING PROPERTY LISTINGS")
    print("=" * 60)
    
    if not properties:
        print("⚠️  No properties to validate")
        return []
    
    valid_properties = []
    issues = []
    
    required_fields = ['area_name', 'city', 'bhk', 'price', 'property_type', 'status']
    
    for idx, prop in enumerate(properties):
        prop_issues = []
        
        # Check required fields
        for field in required_fields:
            if field not in prop or prop[field] is None:
                prop_issues.append(f"Missing {field}")
        
        # Check for forbidden data (phone, owner name)
        prop_str = json.dumps(prop).lower()
        if any(keyword in prop_str for keyword in ['phone', 'contact', 'owner name', 'call']):
            prop_issues.append("Contains forbidden contact info")
        
        # Validate price
        if 'price' in prop and prop['price']:
            try:
                price = float(prop['price'])
                if price <= 0 or price > 1000000000:  # 100 crore max
                    prop_issues.append(f"Invalid price: {price}")
            except:
                prop_issues.append("Invalid price format")
        
        # Validate BHK
        if 'bhk' in prop and prop['bhk']:
            try:
                bhk = int(prop['bhk'])
                if bhk < 1 or bhk > 10:
                    prop_issues.append(f"Invalid BHK: {bhk}")
            except:
                prop_issues.append("Invalid BHK format")
        
        if prop_issues:
            issues.append(f"Property {idx + 1}: {', '.join(prop_issues)}")
        else:
            valid_properties.append(prop)
    
    print(f"\n✅ Valid properties: {len(valid_properties)}/{len(properties)}")
    
    if issues:
        print(f"\n⚠️  Issues found:")
        for issue in issues[:10]:  # Show first 10 issues
            print(f"  - {issue}")
        if len(issues) > 10:
            print(f"  ... and {len(issues) - 10} more issues")
    
    return valid_properties


def validate_market_prices(market_data):
    """
    Validate market price data
    """
    print("\n" + "=" * 60)
    print("VALIDATING MARKET PRICE DATA")
    print("=" * 60)
    
    if not market_data:
        print("⚠️  No market data to validate")
        return []
    
    valid_data = []
    
    for data in market_data:
        if 'area' in data and 'avg_price_per_sqft' in data:
            valid_data.append(data)
    
    print(f"\n✅ Valid market data entries: {len(valid_data)}/{len(market_data)}")
    
    return valid_data


def validate_locality_info(locality_data):
    """
    Validate locality infrastructure data
    """
    print("\n" + "=" * 60)
    print("VALIDATING LOCALITY INFRASTRUCTURE DATA")
    print("=" * 60)
    
    if not locality_data:
        print("⚠️  No locality data to validate")
        return []
    
    valid_data = []
    
    for data in locality_data:
        if 'area' in data and (data.get('nearest_metros') or data.get('schools') or data.get('hospitals')):
            valid_data.append(data)
    
    print(f"\n✅ Valid locality entries: {len(valid_data)}/{len(locality_data)}")
    
    return valid_data


def validate_circle_rates(circle_data):
    """
    Validate circle rate data
    """
    print("\n" + "=" * 60)
    print("VALIDATING CIRCLE RATE DATA")
    print("=" * 60)
    
    if not circle_data:
        print("⚠️  No circle rate data to validate")
        return []
    
    valid_data = []
    
    for data in circle_data:
        if 'area' in data and 'circle_rate' in data:
            valid_data.append(data)
    
    print(f"\n✅ Valid circle rate entries: {len(valid_data)}/{len(circle_data)}")
    
    return valid_data


def save_cleaned_data(data, filename):
    """
    Save cleaned data to cleaned_data folder
    """
    os.makedirs("cleaned_data", exist_ok=True)
    
    filepath = os.path.join("cleaned_data", filename)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"💾 Saved to: {filepath}")


def generate_summary_report(stats):
    """
    Generate validation summary report
    """
    print("\n" + "=" * 60)
    print("VALIDATION SUMMARY REPORT")
    print("=" * 60)
    print(f"\nValidation completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    for data_type, counts in stats.items():
        print(f"\n{data_type}:")
        print(f"  Total: {counts['total']}")
        print(f"  Valid: {counts['valid']}")
        print(f"  Invalid: {counts['total'] - counts['valid']}")
        if counts['total'] > 0:
            print(f"  Success rate: {(counts['valid'] / counts['total']) * 100:.1f}%")


def main():
    """
    Main validation function
    """
    print("=" * 60)
    print("DATA VALIDATION & CLEANING SCRIPT")
    print("=" * 60)
    
    stats = {}
    
    # Validate properties
    properties = load_json_file("data/properties.json")
    if properties:
        valid_properties = validate_properties(properties)
        save_cleaned_data(valid_properties, "properties.json")
        stats["Properties"] = {"total": len(properties), "valid": len(valid_properties)}
    
    # Validate market prices
    market_data = load_json_file("data/market_prices.json")
    if market_data:
        valid_market = validate_market_prices(market_data)
        save_cleaned_data(valid_market, "market_prices.json")
        stats["Market Prices"] = {"total": len(market_data), "valid": len(valid_market)}
    
    # Validate locality info
    locality_data = load_json_file("data/locality_info.json")
    if locality_data:
        valid_locality = validate_locality_info(locality_data)
        save_cleaned_data(valid_locality, "locality_info.json")
        stats["Locality Info"] = {"total": len(locality_data), "valid": len(valid_locality)}
    
    # Validate circle rates
    circle_data = load_json_file("data/circle_rates.json")
    if circle_data:
        valid_circle = validate_circle_rates(circle_data)
        save_cleaned_data(valid_circle, "circle_rates.json")
        stats["Circle Rates"] = {"total": len(circle_data), "valid": len(valid_circle)}
    
    # Generate summary report
    if stats:
        generate_summary_report(stats)
    
    print("\n" + "=" * 60)
    print("✅ VALIDATION COMPLETE!")
    print("=" * 60)


if __name__ == "__main__":
    main()
