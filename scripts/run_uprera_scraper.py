"""
Run UP-RERA Scraper
Helper script to run Scrapy spider programmatically
"""
import os
import subprocess
import sys


def run_scrapy_spider():
    """
    Run the UP-RERA spider using Scrapy
    """
    print("=" * 70)
    print("UP-RERA PROJECT SCRAPER (Scrapy)")
    print("=" * 70)
    
    # Check if scrapy is installed
    try:
        import scrapy
        print("✅ Scrapy is installed")
    except ImportError:
        print("\n❌ Scrapy is not installed!")
        print("\nInstall with:")
        print("  pip install scrapy")
        return
    
    # Create necessary directories
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    
    print("\n⚠️  IMPORTANT: Before running, ensure you have:")
    print("   1. Inspected UP-RERA website structure")
    print("   2. Updated CSS selectors in scripts/uprera_spider.py")
    print("   3. Updated form field names for search submission")
    
    response = input("\nHave you updated the spider selectors? (y/n): ")
    
    if response.lower() != 'y':
        print("\n⚠️  Please update the spider first!")
        print("\nSteps:")
        print("1. Open https://uprera.azurewebsites.net/View_projects.aspx")
        print("2. Right-click > Inspect")
        print("3. Find form fields, table structure, detail page layout")
        print("4. Update selectors in scripts/uprera_spider.py")
        print("5. Run this script again")
        return
    
    print("\n🚀 Starting UP-RERA scraper...")
    print("⏱️  This may take 2-4 hours depending on data volume")
    print("📊 Progress will be logged to console")
    print("\n" + "=" * 70)
    
    # Run scrapy spider
    spider_path = os.path.join("scripts", "uprera_spider.py")
    output_path = os.path.join("data", "rera_projects.json")
    
    cmd = [
        sys.executable,
        "-m",
        "scrapy",
        "runspider",
        spider_path,
        "-o",
        output_path,
        "--loglevel=INFO"
    ]
    
    try:
        subprocess.run(cmd, check=True)
        
        print("\n" + "=" * 70)
        print("✅ SCRAPING COMPLETE!")
        print(f"Data saved to: {output_path}")
        print("=" * 70)
        
        # Show collected data count
        try:
            import json
            with open(output_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                print(f"\nTotal projects collected: {len(data)}")
        except:
            pass
    
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Scraping failed: {e}")
        print("\nPossible issues:")
        print("  - Incorrect CSS selectors")
        print("  - Website structure changed")
        print("  - Network/connection issues")
        print("  - CAPTCHA blocking")
    except KeyboardInterrupt:
        print("\n\n⚠️  Scraping interrupted by user")
        print("Partial data may have been saved to:", output_path)
    except Exception as e:
        print(f"\n❌ Error: {e}")


if __name__ == "__main__":
    run_scrapy_spider()
