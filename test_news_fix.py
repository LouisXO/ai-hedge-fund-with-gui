#!/usr/bin/env python3
"""
Test script to verify that the CompanyNews validation error is fixed
"""
import sys
sys.path.append('.')

from src.tools.api import get_company_news
from src.data.models import CompanyNews

def test_company_news_validation():
    """Test that CompanyNews objects can be created without validation errors"""
    print("🧪 Testing CompanyNews validation fix...")
    
    # Test creating CompanyNews objects manually with different author scenarios
    test_cases = [
        {
            "ticker": "AAPL",
            "title": "Apple Stock Rises",
            "author": "John Doe",
            "source": "Reuters",
            "date": "2024-01-15T10:30:00Z",
            "url": "https://example.com/article1",
            "sentiment": None
        },
        {
            "ticker": "AAPL", 
            "title": "Apple Earnings Report",
            "author": "Reuters",  # Using source as author fallback
            "source": "Reuters",
            "date": "2024-01-15T11:00:00Z", 
            "url": "https://example.com/article2",
            "sentiment": None
        },
        {
            "ticker": "AAPL",
            "title": "Apple Market Analysis", 
            "author": "Unknown",  # Default fallback
            "source": "Financial Times",
            "date": "2024-01-15T12:00:00Z",
            "url": "https://example.com/article3",
            "sentiment": None
        }
    ]
    
    print("\n📋 Testing CompanyNews object creation:")
    for i, test_data in enumerate(test_cases, 1):
        try:
            news = CompanyNews(**test_data)
            print(f"✅ Test {i}: Successfully created CompanyNews object")
            print(f"   └── Author: '{news.author}', Source: '{news.source}'")
        except Exception as e:
            print(f"❌ Test {i}: Failed to create CompanyNews object - {e}")
            return False
    
    print("\n🔍 Testing actual get_company_news function:")
    try:
        # This should not throw validation errors even without API key
        news_list = get_company_news("AAPL", "2024-01-15")
        print(f"✅ get_company_news executed successfully, returned {len(news_list)} articles")
    except Exception as e:
        print(f"❌ get_company_news failed: {e}")
        return False
    
    print("\n🎉 All tests passed! The validation error is fixed.")
    return True

if __name__ == "__main__":
    success = test_company_news_validation()
    if success:
        print("\n✅ CompanyNews validation fix verified!")
    else:
        print("\n❌ Tests failed - there may still be validation issues") 