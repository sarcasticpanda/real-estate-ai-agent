"""
Test OpenRouter API Connection
"""
import os
from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def test_api_connection():
    """
    Test if OpenRouter API key works
    """
    print("=" * 60)
    print("TESTING OPENROUTER API CONNECTION")
    print("=" * 60)
    
    api_key = os.getenv("OPENROUTER_API_KEY")
    
    if not api_key:
        print("\n❌ No API key found in .env file")
        return False
    
    print(f"\n🔑 API Key found: {api_key[:20]}...")
    
    try:
        # Initialize client
        client = OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1"
        )
        
        print("\n📡 Sending test request...")
        
        # Test API call
        response = client.chat.completions.create(
            model="meta-llama/llama-3.1-8b-instruct:free",
            messages=[
                {"role": "user", "content": "Say 'API connection successful!' if you can read this."}
            ]
        )
        
        result = response.choices[0].message.content
        
        print(f"\n✅ API Response received:")
        print(f"   {result}")
        print("\n" + "=" * 60)
        print("✅ API CONNECTION SUCCESSFUL!")
        print("=" * 60)
        
        return True
    
    except Exception as e:
        print(f"\n❌ API Connection Failed: {e}")
        print("\nPossible issues:")
        print("  - Invalid API key")
        print("  - Network connection problem")
        print("  - OpenRouter service issue")
        return False


if __name__ == "__main__":
    test_api_connection()
