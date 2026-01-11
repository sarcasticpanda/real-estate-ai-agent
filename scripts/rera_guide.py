"""
RERA Data Collection Helper
Guide for collecting official property data from UP-RERA portal
"""

RERA_INFO = """
============================================================
UP-RERA OFFICIAL DATA COLLECTION GUIDE
============================================================

UP-RERA (Uttar Pradesh Real Estate Regulatory Authority) is the 
official government source for verified property data in Lucknow.

WEBSITE: https://www.up-rera.in/

============================================================
WHAT DATA IS AVAILABLE:
============================================================

1. Registered Projects
   - Project name and location
   - Builder/promoter details
   - Project status
   - Total units
   - Approval details
   - Timeline

2. Approved Layouts
   - Building plans
   - Unit configurations
   - Amenities planned

3. Complaints & Resolutions
   - Consumer complaints
   - Resolution status
   - Builder compliance

============================================================
HOW TO ACCESS DATA:
============================================================

OPTION 1: Manual Collection from Portal
1. Visit https://www.up-rera.in/
2. Navigate to "Registered Projects"
3. Filter by: Lucknow district
4. Export available data
5. Copy project details systematically

OPTION 2: File RTI Request
1. Draft RTI application requesting:
   - Bulk property data for Lucknow
   - Machine-readable format (CSV/JSON)
   - Purpose: Research/AI development
2. Submit at: https://rtionline.gov.in/
3. Fee: ₹10 via online payment
4. Expected response: 30 days

OPTION 3: Official API Request (Best)
1. Draft formal email to UP-RERA
2. Email: info@up-rera.in
3. Request API access or bulk data export
4. Mention:
   - Your project purpose (AI for consumer benefit)
   - Data fields needed
   - How it helps property buyers
   - Commit to proper attribution

============================================================
SAMPLE RTI APPLICATION:
============================================================

To,
The Public Information Officer,
UP Real Estate Regulatory Authority,
Uttar Pradesh

Subject: Request for Property Data under RTI Act 2005

Dear Sir/Madam,

Under the Right to Information Act 2005, I request the following 
information:

1. List of all registered real estate projects in Lucknow district
2. Project details including:
   - Project name and registration number
   - Location/address
   - Builder/promoter name
   - Project type (residential/commercial)
   - Total units
   - Status (ongoing/completed)
   - Expected completion date

3. Preferred format: CSV or JSON (machine-readable)

Purpose: Developing an AI-powered consumer assistance tool to help 
property buyers make informed decisions.

I am willing to pay the prescribed fee.

Name: [Your Name]
Contact: [Your Email/Phone]
Address: [Your Address]

Date: [Date]

============================================================
SAMPLE EMAIL TO UP-RERA:
============================================================

Subject: Request for API Access / Bulk Data for Research Project

Dear UP-RERA Team,

I am developing an AI-powered real estate assistant to help property 
buyers in Lucknow make informed decisions. The system will use RERA-
registered project data to:

1. Verify project authenticity
2. Provide accurate project information
3. Guide buyers on legal compliance
4. Promote transparency in real estate

I respectfully request:
- API access to RERA registered projects database, OR
- Bulk data export for Lucknow projects in CSV/JSON format

Data fields needed:
- Project name, registration number, location
- Builder details, project status
- Unit types, expected completion

I commit to:
✓ Proper attribution to UP-RERA
✓ Non-commercial use for consumer benefit
✓ Compliance with data usage terms

This project aims to empower buyers and align with RERA's mission 
of consumer protection.

Thank you for considering this request.

Best regards,
[Your Name]
[Your Contact]
[Your Project Details]

============================================================
NEXT STEPS:
============================================================

Week 1:
□ File RTI request online
□ Send email to UP-RERA
□ Start manual data collection from portal

Week 2-4:
□ Follow up on RTI/email
□ Continue manual collection
□ Structure data in JSON format

Week 5+:
□ Process received data
□ Integrate into your system
□ Maintain updates

============================================================
LEGAL & ETHICAL NOTES:
============================================================

✅ RERA data is public information
✅ Using for consumer benefit aligns with RERA mission
✅ Proper attribution required
✅ Non-commercial use preferred initially
✅ Regular updates needed as projects change

============================================================

For questions or help with RTI/email drafts, contact your project lead.

"""

def print_guide():
    print(RERA_INFO)

def generate_rti_template():
    """Generate RTI application template"""
    template = """
To,
The Public Information Officer,
UP Real Estate Regulatory Authority,
Uttar Pradesh

Subject: Request for Property Data under RTI Act 2005

Dear Sir/Madam,

Under the Right to Information Act 2005, I request the following information:

1. List of all registered real estate projects in Lucknow district
2. Project details including:
   - Project name and registration number
   - Location/address
   - Builder/promoter name
   - Project type (residential/commercial)
   - Total units
   - Status (ongoing/completed)
   - Expected completion date

3. Preferred format: CSV or JSON (machine-readable)

Purpose: Developing an AI-powered consumer assistance tool to help property 
buyers make informed decisions.

I am willing to pay the prescribed fee.

Name: [Your Name]
Contact: [Your Email/Phone]
Address: [Your Address]

Date: [Today's Date]
"""
    
    filename = "RTI_Application_Template.txt"
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(template)
    
    print(f"✅ RTI template saved to: {filename}")
    print("Fill in your details and submit at: https://rtionline.gov.in/")

if __name__ == "__main__":
    print_guide()
    print("\n" + "=" * 60)
    generate_rti_template()
