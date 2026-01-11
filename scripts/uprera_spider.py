"""
UP-RERA Scrapy Spider
Scrapes registered real estate projects from UP-RERA portal for Lucknow
"""
import scrapy
import json
from datetime import datetime


class UPReraSpider(scrapy.Spider):
    name = 'uprera_lucknow'
    allowed_domains = ['uprera.azurewebsites.net', 'up-rera.in']
    
    # Custom settings for polite scraping
    custom_settings = {
        'DOWNLOAD_DELAY': 3,  # 3 seconds between requests
        'CONCURRENT_REQUESTS': 1,  # One request at a time
        'CONCURRENT_REQUESTS_PER_DOMAIN': 1,
        'ROBOTSTXT_OBEY': True,
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'AUTOTHROTTLE_ENABLED': True,
        'AUTOTHROTTLE_START_DELAY': 2,
        'AUTOTHROTTLE_MAX_DELAY': 10,
    }
    
    # Target areas in Lucknow
    target_areas = ['Gomti Nagar', 'Alambagh', 'Hazratganj', 'Indira Nagar', 'Aliganj']
    
    def start_requests(self):
        """
        Start by accessing the project search page
        """
        # UP-RERA registered projects search URL
        base_url = 'https://uprera.azurewebsites.net/View_projects.aspx'
        
        yield scrapy.Request(
            url=base_url,
            callback=self.parse_search_page,
            dont_filter=True
        )
    
    def parse_search_page(self, response):
        """
        Parse the search page and submit search for Lucknow district
        
        NOTE: This is a template. You MUST inspect UP-RERA website and update:
        1. Form field names (lines 60-67)
        2. ASP.NET ViewState fields
        3. Submit button name
        
        To inspect:
        1. Open https://uprera.azurewebsites.net/View_projects.aspx in browser
        2. Right-click > Inspect > Elements tab
        3. Find the search form and note all input field names
        4. Find the district dropdown and submit button names
        """
        self.logger.info("Accessed UP-RERA search page")
        
        # Extract ASP.NET form data
        viewstate = response.css('input#__VIEWSTATE::attr(value)').get()
        viewstate_generator = response.css('input#__VIEWSTATEGENERATOR::attr(value)').get()
        event_validation = response.css('input#__EVENTVALIDATION::attr(value)').get()
        
        # TODO: Update these field names by inspecting the actual website
        formdata = {
            '__VIEWSTATE': viewstate or '',
            '__VIEWSTATEGENERATOR': viewstate_generator or '',
            '__EVENTVALIDATION': event_validation or '',
            # REPLACE THESE with actual form field names from UP-RERA
            'ctl00$ContentPlaceHolder1$ddlDistrict': 'Lucknow',
            'ctl00$ContentPlaceHolder1$btnSearch': 'Search'
        }
        
        # Submit search form
        yield scrapy.FormRequest.from_response(
            response,
            formdata=formdata,
            callback=self.parse_results_page
        )
    
    def parse_results_page(self, response):
        """
        Parse the search results page and extract project links
        
        NOTE: You MUST update these CSS selectors by inspecting the results table
        
        To inspect:
        1. Submit a search on UP-RERA website
        2. Right-click on results table > Inspect
        3. Note the table class, row class, and cell structure
        4. Update selectors below
        """
        self.logger.info("Parsing search results")
        
        # TODO: Update these selectors based on actual HTML structure
        project_rows = response.css('table.GridViewStyle tr[class*="RowStyle"]')
        
        for row in project_rows:
            # TODO: Adjust column indices based on actual table structure
            project_link = row.css('a::attr(href)').get()
            project_name = row.css('td:nth-child(2)::text').get()
            registration_no = row.css('td:nth-child(3)::text').get()
            location = row.css('td:nth-child(4)::text').get()
            
            if project_link:
                yield response.follow(
                    project_link,
                    callback=self.parse_project_detail,
                    meta={
                        'project_name': project_name,
                        'registration_no': registration_no,
                        'location': location
                    }
                )
        
        # Handle pagination
        next_page = response.css('a[title="Next Page"]::attr(href)').get()
        if next_page:
            yield response.follow(next_page, callback=self.parse_results_page)
    
    def parse_project_detail(self, response):
        """
        Parse detailed project information
        
        NOTE: You MUST update these CSS selectors by inspecting a project detail page
        
        To inspect:
        1. Click on a project from search results
        2. Right-click on each field > Inspect
        3. Note the span/label IDs and classes
        4. Update selectors below
        """
        project_name = response.meta.get('project_name', '')
        registration_no = response.meta.get('registration_no', '')
        location = response.meta.get('location', '')
        
        # TODO: Update all these selectors based on actual page structure
        project_data = {
            'project_name': project_name or response.css('span#lblProjectName::text').get(),
            'registration_number': registration_no or response.css('span#lblRegistrationNo::text').get(),
            'location': location or response.css('span#lblLocation::text').get(),
            'district': 'Lucknow',
            'promoter_name': response.css('span#lblPromoterName::text').get(),
            'project_type': response.css('span#lblProjectType::text').get(),
            'project_area': response.css('span#lblProjectArea::text').get(),
            'total_units': response.css('span#lblTotalUnits::text').get(),
            'status': response.css('span#lblStatus::text').get(),
            'completion_date': response.css('span#lblCompletionDate::text').get(),
            'amenities': response.css('span#lblAmenities::text').get(),
            'description': response.css('span#lblDescription::text').get(),
            'sanctions': response.css('span#lblSanctions::text').get(),
            
            # Metadata
            'source': 'UP-RERA',
            'source_url': response.url,
            'scraped_at': datetime.now().isoformat()
        }
        
        # Determine area from location
        area = self.extract_area(location or project_data.get('location', ''))
        project_data['area'] = area
        
        yield project_data
    
    def extract_area(self, location_text):
        """
        Extract area name from location text
        """
        if not location_text:
            return 'Unknown'
        
        location_lower = location_text.lower()
        
        for area in self.target_areas:
            if area.lower() in location_lower:
                return area
        
        return 'Other Lucknow'


# Pipeline to save data to JSON
class UPReraPipeline:
    def open_spider(self, spider):
        import os
        os.makedirs('data', exist_ok=True)
        self.file = open('data/rera_projects.json', 'w', encoding='utf-8')
        self.projects = []
    
    def close_spider(self, spider):
        json.dump(self.projects, self.file, indent=2, ensure_ascii=False)
        self.file.close()
        spider.logger.info(f"Saved {len(self.projects)} projects to data/rera_projects.json")
    
    def process_item(self, item, spider):
        self.projects.append(dict(item))
        return item
