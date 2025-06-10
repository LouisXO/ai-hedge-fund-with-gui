import yfinance as yf
import requests
import pandas as pd
from datetime import datetime, timedelta
import os
from typing import List, Dict, Optional

# Free API alternatives to Financial Datasets

def get_stock_data_free(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Get stock price data using Yahoo Finance (completely free)"""
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(start=start_date, end=end_date)
        return df
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return pd.DataFrame()

def get_company_info_free(ticker: str) -> Dict:
    """Get company fundamental data using Yahoo Finance"""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        # Extract key financial metrics
        return {
            'market_cap': info.get('marketCap'),
            'pe_ratio': info.get('trailingPE'),
            'forward_pe': info.get('forwardPE'),
            'price_to_book': info.get('priceToBook'),
            'price_to_sales': info.get('priceToSalesTrailing12Months'),
            'profit_margin': info.get('profitMargins'),
            'operating_margin': info.get('operatingMargins'),
            'roe': info.get('returnOnEquity'),
            'debt_to_equity': info.get('debtToEquity'),
            'current_ratio': info.get('currentRatio'),
            'revenue_growth': info.get('revenueGrowth'),
            'earnings_growth': info.get('earningsGrowth'),
            'sector': info.get('sector'),
            'industry': info.get('industry'),
        }
    except Exception as e:
        print(f"Error fetching company info for {ticker}: {e}")
        return {}

def get_financial_statements_free(ticker: str) -> Dict:
    """Get financial statements using Yahoo Finance"""
    try:
        stock = yf.Ticker(ticker)
        
        return {
            'income_statement': stock.financials,
            'balance_sheet': stock.balance_sheet,
            'cash_flow': stock.cashflow,
        }
    except Exception as e:
        print(f"Error fetching financial statements for {ticker}: {e}")
        return {}

def get_sec_company_facts_free(ticker: str) -> Dict:
    """Get company facts from SEC EDGAR API (completely free, no API key needed)"""
    try:
        # First get CIK from ticker
        cik_url = f"https://www.sec.gov/files/company_tickers.json"
        headers = {'User-Agent': 'YourApp/1.0 (your-email@example.com)'}  # SEC requires user agent
        
        response = requests.get(cik_url, headers=headers)
        if response.status_code != 200:
            return {}
            
        companies = response.json()
        cik = None
        
        for company_data in companies.values():
            if company_data['ticker'].upper() == ticker.upper():
                cik = str(company_data['cik_str']).zfill(10)
                break
        
        if not cik:
            return {}
        
        # Get company facts
        facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        response = requests.get(facts_url, headers=headers)
        
        if response.status_code == 200:
            return response.json()
        return {}
        
    except Exception as e:
        print(f"Error fetching SEC data for {ticker}: {e}")
        return {}

def get_news_free(ticker: str, days_back: int = 7) -> List[Dict]:
    """Get news using NewsAPI (100 requests/day free)"""
    api_key = os.environ.get("NEWSAPI_KEY")  # Get free key from newsapi.org
    if not api_key:
        return []
    
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days_back)
        
        url = f"https://newsapi.org/v2/everything"
        params = {
            'q': f'"{ticker}" OR "{ticker} stock"',
            'from': start_date.strftime('%Y-%m-%d'),
            'to': end_date.strftime('%Y-%m-%d'),
            'sortBy': 'publishedAt',
            'apiKey': api_key,
            'pageSize': 50
        }
        
        response = requests.get(url, params=params)
        if response.status_code == 200:
            data = response.json()
            return data.get('articles', [])
        return []
        
    except Exception as e:
        print(f"Error fetching news for {ticker}: {e}")
        return []

def get_alpha_vantage_overview_free(ticker: str) -> Dict:
    """Get company overview using Alpha Vantage (500 calls/day free)"""
    api_key = os.environ.get("ALPHA_VANTAGE_KEY")  # Get free key from alphavantage.co
    if not api_key:
        return {}
    
    try:
        url = f"https://www.alphavantage.co/query"
        params = {
            'function': 'OVERVIEW',
            'symbol': ticker,
            'apikey': api_key
        }
        
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json()
        return {}
        
    except Exception as e:
        print(f"Error fetching Alpha Vantage data for {ticker}: {e}")
        return {}

# Wrapper function to combine all free sources
def get_comprehensive_stock_data_free(ticker: str, start_date: str, end_date: str) -> Dict:
    """Get comprehensive stock data from multiple free sources"""
    
    return {
        'price_data': get_stock_data_free(ticker, start_date, end_date),
        'company_info': get_company_info_free(ticker),
        'financial_statements': get_financial_statements_free(ticker),
        'sec_facts': get_sec_company_facts_free(ticker),
        'news': get_news_free(ticker),
        'alpha_vantage': get_alpha_vantage_overview_free(ticker)
    }

# Example usage
if __name__ == "__main__":
    data = get_comprehensive_stock_data_free("AAPL", "2024-01-01", "2024-12-31")
    print("Price data shape:", data['price_data'].shape)
    print("Market cap:", data['company_info'].get('market_cap'))
    print("News articles:", len(data['news'])) 