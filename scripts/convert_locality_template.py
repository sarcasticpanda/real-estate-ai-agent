"""
Helper script to convert the filled locality template to JSON format
"""
import json
import re


def parse_locality_template(template_file):
    """
    Parse the filled template file and convert to JSON
    """
    with open(template_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    localities = []
    
    # Split by area sections
    area_sections = re.split(r'={40,}\nAREA \d+: (.+?)\n={40,}', content)
    
    # Process each area (skip first empty split)
    for i in range(1, len(area_sections), 2):
        if i + 1 >= len(area_sections):
            break
        
        area_name = area_sections[i].strip()
        area_content = area_sections[i + 1]
        
        # Initialize area data
        locality_data = {
            "area": area_name,
            "nearest_metros": [],
            "schools": [],
            "hospitals": []
        }
        
        # Extract metro stations
        metro_pattern = r'Metro \d+.*?:\s*\n\s*Name:\s*(.+?)\n\s*Distance.*?:\s*(.+?)\n\s*Travel Time.*?:\s*(.+?)\n'
        metros = re.findall(metro_pattern, area_content)
        
        for name, distance, travel_time in metros:
            name = name.strip()
            distance = distance.strip()
            travel_time = travel_time.strip()
            
            # Skip if empty
            if name and name != "____________________":
                try:
                    distance_km = float(re.findall(r'\d+\.?\d*', distance)[0]) if re.findall(r'\d+\.?\d*', distance) else 0
                    travel_min = int(re.findall(r'\d+', travel_time)[0]) if re.findall(r'\d+', travel_time) else 0
                    
                    locality_data["nearest_metros"].append({
                        "name": name,
                        "distance_km": distance_km,
                        "travel_time_min": travel_min
                    })
                except:
                    pass
        
        # Extract schools
        school_pattern = r'TOP SCHOOLS.*?\n((?:\d+\.\s*.+?\n)+)'
        school_match = re.search(school_pattern, area_content)
        if school_match:
            schools = re.findall(r'\d+\.\s*(.+)', school_match.group(1))
            for school in schools:
                school = school.strip()
                if school and school != "____________________" and "(optional)" not in school:
                    locality_data["schools"].append(school)
        
        # Extract hospitals
        hospital_pattern = r'TOP HOSPITALS.*?\n((?:\d+\.\s*.+?\n)+)'
        hospital_match = re.search(hospital_pattern, area_content)
        if hospital_match:
            hospitals = re.findall(r'\d+\.\s*(.+)', hospital_match.group(1))
            for hospital in hospitals:
                hospital = hospital.strip()
                if hospital and hospital != "____________________" and "(optional)" not in hospital:
                    locality_data["hospitals"].append(hospital)
        
        # Only add if we have some data
        if locality_data["nearest_metros"] or locality_data["schools"] or locality_data["hospitals"]:
            localities.append(locality_data)
    
    return localities


def main():
    """
    Main conversion function
    """
    print("=" * 60)
    print("LOCALITY TEMPLATE TO JSON CONVERTER")
    print("=" * 60)
    
    template_file = "LOCALITY_DATA_TEMPLATE.txt"
    output_file = "data/locality_info.json"
    
    print(f"\n📄 Reading template: {template_file}")
    
    try:
        localities = parse_locality_template(template_file)
        
        if localities:
            # Ensure data directory exists
            import os
            os.makedirs("data", exist_ok=True)
            
            # Save to JSON
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(localities, f, indent=2, ensure_ascii=False)
            
            print(f"\n✅ Conversion complete!")
            print(f"Total localities processed: {len(localities)}")
            print(f"Saved to: {output_file}")
            
            # Display summary
            print(f"\n{'=' * 60}")
            print("SUMMARY:")
            print(f"{'=' * 60}")
            for loc in localities:
                print(f"\n{loc['area']}:")
                print(f"  Metro stations: {len(loc['nearest_metros'])}")
                print(f"  Schools: {len(loc['schools'])}")
                print(f"  Hospitals: {len(loc['hospitals'])}")
        else:
            print("\n⚠️  No data found in template. Please fill the template first.")
    
    except FileNotFoundError:
        print(f"\n❌ Template file not found: {template_file}")
        print("Please make sure LOCALITY_DATA_TEMPLATE.txt exists and is filled out.")
    except Exception as e:
        print(f"\n❌ Error: {e}")


if __name__ == "__main__":
    main()
