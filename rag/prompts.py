"""
Prompt templates for the property recommendation AI agent.
"""

SYSTEM_PROMPT = """You are a helpful real estate assistant for Lucknow properties.
Your job is to help buyers find properties that match their needs and connect them with brokers.

Guidelines:
- Be friendly, concise, and helpful
- Answer only property-related questions
- When presenting properties, always include price, area, BHK, key amenities, and nearby facilities
- Format prices in lakhs/crores (Indian style): ₹1,70,00,000 = ₹1.70 Cr
- If the user seems interested in a property, gently ask for their name and phone to connect them with the broker
- Do not fabricate property details — only use the information provided to you
"""

INTENT_EXTRACTION_PROMPT = """Extract the user's real estate requirements from the message below.
Return ONLY a valid JSON object with these fields (use null for unspecified):

{{
  "city": string or null,
  "area": string or null,
  "bhk": integer or null,
  "min_budget_cr": float or null,
  "max_budget_cr": float or null,
  "property_type": "flat" | "house" | "villa" | "plot" | "shop" | null,
  "furnishing": "furnished" | "unfurnished" | "semi-furnished" | null,
  "amenities": list of strings or [],
  "nearby": list of strings or [],
  "named_landmark": string or null,
  "named_landmark_max_km": float or null,
  "intent": "buy" | "rent" | "invest" | null,
  "is_lead_ready": boolean
}}

Rules:
- Convert budget mentions to crores: "50 lakh" = 0.5, "2 crore" = 2.0, "20 lakh" = 0.2
- Generic: "near metro", "near hospital" → nearby: ["metro"] / ["hospital"], named_landmark = null
- Specific named place: "near CMS school", "near Charbagh station", "near Phoenix mall" → named_landmark = "CMS school" (or full name), nearby = []
- named_landmark_max_km: if user says "within 2 km of X" → 2.0, else null (system defaults to 5 km)
- Set is_lead_ready to true ONLY if user clearly wants to visit, book, or contact a broker
- Keep all values null if not mentioned

User message: {message}"""

PROPERTY_RECOMMENDATION_PROMPT = """Based on the user's requirements, here are the matching properties from our database.
Format a helpful, natural response presenting the top {count} properties.

User requirements: {requirements}

Properties found:
{properties_text}

Instructions:
- Present each property clearly with price in crores/lakhs, location, BHK, key amenities
- Mention relevant nearby facilities (metro, hospital, school distances if available)
- Be conversational, not robotic
- End with: "Would you like to schedule a visit or get more details on any of these?"
- If fewer than 3 properties, say you're showing the best matches available"""

LEAD_CAPTURE_PROMPT = """The user wants to proceed further (visit/contact broker).
Politely ask for their name and phone number.

Previous context: {context}

Generate a short, friendly message asking for:
1. Full name
2. Phone number (for the broker to contact)

Keep it under 2 sentences."""

NO_RESULTS_PROMPT = """No properties were found matching the user's exact requirements.
User asked for: {requirements}

Generate a helpful response that:
1. Acknowledges we don't have an exact match
2. Suggests slightly relaxing one or two constraints (e.g., nearby area, slightly higher budget)
3. Asks if they'd like to see similar properties"""
