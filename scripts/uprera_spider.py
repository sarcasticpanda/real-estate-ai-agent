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
        
        # Form field names from UP-RERA website inspection (Jan 2026)
        formdata = {
            '__VIEWSTATE': viewstate or '',
            '__VIEWSTATEGENERATOR': viewstate_generator or '',
            '__EVENTVALIDATION': event_validation or '',
            'ctl00$ContentPlaceHolder1$DdlprojectDistrict': 'Lucknow',
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
        """
        self.logger.info("Parsing search results")
        
        # Debug: Save response HTML
        with open('debug_results.html', 'w', encoding='utf-8') as f:
            f.write(response.text)
        self.logger.info("Saved response to debug_results.html")
        
        # Try multiple possible table selectors
        table_selectors = [
            'table#ctl00_ContentPlaceHolder1_GridView1 tr',
            'table[id*="GridView"] tr',
            'table.table-bordered tr',
            'table tr'
        ]
        
        project_rows = []
        for selector in table_selectors:
            project_rows = response.css(selector)
            if project_rows:
                self.logger.info(f"Found {len(project_rows)} rows using selector: {selector}")
                break
        
        if not project_rows:
            self.logger.warning("No table rows found with any selector")
            return
        
        for idx, row in enumerate(project_rows):
            # Skip header row (has <th> elements or Orange background)
            if row.css('th'):
                self.logger.info(f"Row {idx}: Skipping header row")
                continue
            if 'Orange' in (row.css('::attr(style)').get() or ''):
                self.logger.info(f"Row {idx}: Skipping Orange header row")
                continue
            
            # Extract data from columns
            cells = row.css('td')
            self.logger.info(f"Row {idx}: Found {len(cells)} cells")
            
            if len(cells) < 8:
                self.logger.warning(f"Row {idx}: Not enough cells, skipping")
                continue
            
            # Column structure: S.No(1), Reg.Number(2), Project Name(3), Promoter(4), District(5), ProjectType(6), Approval(7), ViewDetails(8)
            registration_no = cells[1].css('::text').get()
            project_name = cells[2].css('::text').get()
            promoter_name = cells[3].css('::text').get()
            district = cells[4].css('::text').get()
            project_type = cells[5].css('::text').get()
            
            self.logger.info(f"Row {idx}: Project='{project_name}', Reg='{registration_no}'")
            
            # Get "View Details" link from column 8 (index 7)
            detail_link = cells[7].css('a::attr(href)').get()
            
            if detail_link:
                self.logger.info(f"Row {idx}: Following detail link {detail_link}")
                yield response.follow(
                    detail_link,
                    callback=self.parse_project_detail,
                    errback=self.handle_error,
                    meta={
                        'project_name': project_name,
                        'registration_no': registration_no,
                        'promoter_name': promoter_name,
                        'district': district,
                        'project_type': project_type
                    }
                )
            else:
                self.logger.warning(f"Row {idx}: No detail link found")
        
        # Handle pagination
        next_page = response.css('a[title="Next Page"]::attr(href)').get()
        if next_page:
            yield response.follow(next_page, callback=self.parse_results_page)
    
    def parse_project_detail(self, response):
        """
        Parse detailed project information with flexible field extraction
        """
        # Handle 404 errors
        if response.status == 404:
            self.logger.warning(f"404 error for {response.url}")
            return
        
        # Get data from meta (already collected from table)
        project_name = response.meta.get('project_name', '').strip()
        registration_no = response.meta.get('registration_no', '').strip()
        promoter_name = response.meta.get('promoter_name', '').strip()
        district = response.meta.get('district', 'Lucknow').strip()
        project_type = response.meta.get('project_type', '').strip()
        
        # Try to extract additional details from page text (flexible approach)
        page_text = response.css('body').get() or ''
        
        # Extract project address from text
        location = ''
        address_patterns = [
            r'Project Address\s*:\s*([^\n]+)',
            r'Address\s*:\s*([^\n]+)',
        ]
        for pattern in address_patterns:
            import re
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                location = match.group(1).strip()
                break
        
        # Extract completion date
        completion_date = ''
        date_patterns = [
            r'Proposed Completion Date\s*:\s*([0-9-/]+)',
            r'Completion Date\s*:\s*([0-9-/]+)',
        ]
        for pattern in date_patterns:
            import re
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                completion_date = match.group(1).strip()
                break
        
        # Build project data
        project_data = {
            'project_name': project_name,
            'registration_number': registration_no,
            'location': location or 'Lucknow',
            'district': district,
            'promoter_name': promoter_name,
            'project_type': project_type,
            'completion_date': completion_date,
            
            # Metadata
            'source': 'UP-RERA',
            'source_url': response.url,
            'scraped_at': datetime.now().isoformat()
        }
        
        # Determine area from location
        area = self.extract_area(location or project_name)
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
    
    def handle_error(self, failure):
        """
        Handle request errors (404, timeouts, etc.)
        """
        self.logger.error(f"Request failed: {failure.request.url}")
        self.logger.error(f"Error: {failure.value}")


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
