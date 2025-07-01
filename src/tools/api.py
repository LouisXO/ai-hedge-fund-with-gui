import datetime
import os
import pandas as pd
import requests
import yfinance as yf
from typing import List, Dict, Optional
import time
import json
import re
import statistics

from src.data.cache import get_cache
from src.data.models import (
    CompanyNews,
    CompanyNewsResponse,
    FinancialMetrics,
    FinancialMetricsResponse,
    Price,
    PriceResponse,
    LineItem,
    LineItemResponse,
    InsiderTrade,
    InsiderTradeResponse,
    CompanyFactsResponse,
)

# Global cache instance
_cache = get_cache()


def _make_api_request(url: str, headers: dict, method: str = "GET", json_data: dict = None, max_retries: int = 3) -> requests.Response:
    """
    Make an API request with rate limiting handling and moderate backoff.
    
    Args:
        url: The URL to request
        headers: Headers to include in the request
        method: HTTP method (GET or POST)
        json_data: JSON data for POST requests
        max_retries: Maximum number of retries (default: 3)
    
    Returns:
        requests.Response: The response object
    
    Raises:
        Exception: If the request fails with a non-429 error
    """
    for attempt in range(max_retries + 1):  # +1 for initial attempt
        if method.upper() == "POST":
            response = requests.post(url, headers=headers, json=json_data)
        else:
            response = requests.get(url, headers=headers)
        
        if response.status_code == 429 and attempt < max_retries:
            # Linear backoff: 60s, 90s, 120s, 150s...
            delay = 60 + (30 * attempt)
            print(f"Rate limited (429). Attempt {attempt + 1}/{max_retries + 1}. Waiting {delay}s before retrying...")
            time.sleep(delay)
            continue
        
        # Return the response (whether success, other errors, or final 429)
        return response


def safe_json_parse(response_text: str) -> dict:
    """
    Safely parse JSON, handling invalid control characters and other common issues.
    This is more robust than the standard json.loads() for real-world API responses.
    """
    if not response_text or not isinstance(response_text, str):
        return {}
    
    try:
        # First attempt: try standard JSON parsing
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        print(f"Initial JSON parse failed: {e}")
        
        try:
            # Clean up common problematic characters
            cleaned_text = response_text
            
            # Remove or replace common control characters that cause issues
            control_chars = {
                '\x00': '',  # null
                '\x01': '',  # start of heading
                '\x02': '',  # start of text
                '\x03': '',  # end of text
                '\x04': '',  # end of transmission
                '\x05': '',  # enquiry
                '\x06': '',  # acknowledge
                '\x07': '',  # bell
                '\x08': '',  # backspace
                '\x0b': '',  # vertical tab
                '\x0c': '',  # form feed
                '\x0e': '',  # shift out
                '\x0f': '',  # shift in
                '\x10': '',  # data link escape
                '\x11': '',  # device control 1
                '\x12': '',  # device control 2
                '\x13': '',  # device control 3
                '\x14': '',  # device control 4
                '\x15': '',  # negative acknowledge
                '\x16': '',  # synchronous idle
                '\x17': '',  # end of transmission block
                '\x18': '',  # cancel
                '\x19': '',  # end of medium
                '\x1a': '',  # substitute
                '\x1b': '',  # escape
                '\x1c': '',  # file separator
                '\x1d': '',  # group separator
                '\x1e': '',  # record separator
                '\x1f': '',  # unit separator
            }
            
            # Replace control characters
            for char, replacement in control_chars.items():
                cleaned_text = cleaned_text.replace(char, replacement)
            
            # Remove any remaining non-printable characters (except allowed JSON whitespace)
            cleaned_text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned_text)
            
            # Try parsing the cleaned text
            return json.loads(cleaned_text)
            
        except json.JSONDecodeError:
            try:
                # Last resort: try to extract JSON-like content using regex
                import re
                json_pattern = r'\{.*\}'
                match = re.search(json_pattern, cleaned_text, re.DOTALL)
                if match:
                    potential_json = match.group(0)
                    return json.loads(potential_json)
            except:
                pass
            
            # If all else fails, return empty dict
            print(f"All JSON parsing attempts failed for response: {response_text[:200]}...")
            return {}
    
    except Exception as e:
        print(f"Unexpected error in safe_json_parse: {e}")
        return {}


def get_prices(ticker: str, start_date: str, end_date: str) -> list[Price]:
    """Fetch price data from cache or Yahoo Finance API (FREE)."""
    cache_key = f"{ticker}_{start_date}_{end_date}"
    
    # Check cache first
    if cached_data := _cache.get_prices(cache_key):
        return [Price(**price) for price in cached_data]

    try:
        # Use Yahoo Finance instead of paid API
        stock = yf.Ticker(ticker)
        df = stock.history(start=start_date, end=end_date)
        
        if df.empty:
            return []
        
        # Convert Yahoo Finance data to Price objects
        prices = []
        for date, row in df.iterrows():
            price = Price(
                time=date.strftime('%Y-%m-%dT%H:%M:%S'),
                open=float(row['Open']),
                high=float(row['High']),
                low=float(row['Low']),
                close=float(row['Close']),
                volume=int(row['Volume'])
            )
            prices.append(price)
        
        # Cache the results
        _cache.set_prices(cache_key, [p.model_dump() for p in prices])
        return prices
        
    except Exception as e:
        print(f"Error fetching Yahoo Finance data for {ticker}: {e}")
        return []


def get_financial_metrics(
    ticker: str,
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
) -> list[FinancialMetrics]:
    """Fetch financial metrics from cache or Yahoo Finance (FREE)."""
    cache_key = f"{ticker}_{period}_{end_date}_{limit}"
    
    # Check cache first
    if cached_data := _cache.get_financial_metrics(cache_key):
        return [FinancialMetrics(**metric) for metric in cached_data]

    try:
        # Use Yahoo Finance for company info
        stock = yf.Ticker(ticker)
        info = stock.info
        
        if not info:
            return []
        
        # Create FinancialMetrics object from Yahoo Finance data with ALL required fields
        financial_metric = FinancialMetrics(
            ticker=ticker,
            report_period=end_date,
            period=period,
            currency=info.get('currency', 'USD'),  # Required field
            
            # Market metrics
            market_cap=info.get('marketCap'),
            enterprise_value=info.get('enterpriseValue'),
            
            # Valuation ratios
            price_to_earnings_ratio=info.get('trailingPE'),
            price_to_book_ratio=info.get('priceToBook'),
            price_to_sales_ratio=info.get('priceToSalesTrailing12Months'),
            enterprise_value_to_ebitda_ratio=info.get('enterpriseToEbitda'),
            enterprise_value_to_revenue_ratio=info.get('enterpriseToRevenue'),
            free_cash_flow_yield=None,  # Not directly available
            peg_ratio=info.get('pegRatio'),
            
            # Margins
            gross_margin=info.get('grossMargins'),
            operating_margin=info.get('operatingMargins'),
            net_margin=info.get('profitMargins'),
            
            # Returns
            return_on_equity=info.get('returnOnEquity'),
            return_on_assets=info.get('returnOnAssets'),
            return_on_invested_capital=None,  # Not directly available
            
            # Turnover ratios
            asset_turnover=None,  # Not directly available
            inventory_turnover=None,  # Not directly available
            receivables_turnover=None,  # Not directly available
            days_sales_outstanding=None,  # Not directly available
            operating_cycle=None,  # Not directly available
            working_capital_turnover=None,  # Not directly available
            
            # Liquidity ratios
            current_ratio=info.get('currentRatio'),
            quick_ratio=info.get('quickRatio'),
            cash_ratio=None,  # Not directly available
            operating_cash_flow_ratio=None,  # Not directly available
            
            # Leverage ratios
            debt_to_equity=info.get('debtToEquity'),
            debt_to_assets=None,  # Not directly available
            interest_coverage=None,  # Not directly available
            
            # Growth rates
            revenue_growth=info.get('revenueGrowth'),
            earnings_growth=info.get('earningsGrowth'),
            book_value_growth=None,  # Not directly available
            earnings_per_share_growth=None,  # Not directly available
            free_cash_flow_growth=None,  # Not directly available
            operating_income_growth=None,  # Not directly available
            ebitda_growth=None,  # Not directly available
            
            # Per share metrics
            payout_ratio=info.get('payoutRatio'),
            earnings_per_share=info.get('trailingEps'),
            book_value_per_share=info.get('bookValue'),
            free_cash_flow_per_share=None,  # Not directly available
        )
        
        financial_metrics = [financial_metric]
        
        # Cache the results
        _cache.set_financial_metrics(cache_key, [m.model_dump() for m in financial_metrics])
        return financial_metrics
        
    except Exception as e:
        print(f"Error fetching Yahoo Finance metrics for {ticker}: {e}")
        return []


def search_line_items(
    ticker: str,
    line_items: list[str],
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
) -> list[LineItem]:
    """Fetch line items from Yahoo Finance financial statements and structure them properly."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        # Get financial statements
        income_stmt = stock.financials
        balance_sheet = stock.balance_sheet
        cashflow = stock.cashflow
        quarterly_income = stock.quarterly_financials
        quarterly_balance = stock.quarterly_balance_sheet
        quarterly_cashflow = stock.quarterly_cashflow
        
        # Comprehensive mapping based on yfinance documentation
        line_item_mappings = {
            # Revenue metrics
            'revenue': [
                'Total Revenue', 'Operating Revenue', 'Revenue', 'Net Revenue'
            ],
            'operating_revenue': [
                'Operating Revenue', 'Total Revenue'
            ],
            
            # Income metrics
            'operating_income': [
                'Operating Income', 'Total Operating Income As Reported', 'Operating Profit'
            ],
            'net_income': [
                'Net Income', 'Net Income Common Stockholders', 
                'Net Income From Continuing Operation Net Minority Interest',
                'Net Income Including Noncontrolling Interests', 'Net Income Continuous Operations'
            ],
            'gross_profit': [
                'Gross Profit'
            ],
            'ebit': [
                'EBIT', 'Earnings Before Interest And Taxes'
            ],
            'ebitda': [
                'EBITDA', 'Normalized EBITDA'
            ],
            'pretax_income': [
                'Pretax Income', 'Income Before Tax'
            ],
            
            # Cash flow metrics
            'free_cash_flow': [
                'Free Cash Flow', 'Operating Cash Flow', 'Cash Flow From Operations',
                'Net Cash From Operating Activities'
            ],
            'operating_cash_flow': [
                'Operating Cash Flow', 'Net Cash From Operating Activities'
            ],
            'capital_expenditure': [
                'Capital Expenditure', 'Capital Expenditures', 'Capex'
            ],
            'cash_dividends_paid': [
                'Cash Dividends Paid', 'Common Stock Dividend Paid', 'Dividends Paid'
            ],
            'dividends_and_other_cash_distributions': [
                'Cash Dividends Paid', 'Common Stock Dividend Paid', 'Dividends Paid'
            ],
            
            # Balance sheet - Assets
            'total_assets': [
                'Total Assets'
            ],
            'current_assets': [
                'Current Assets', 'Total Current Assets'
            ],
            'cash_and_equivalents': [
                'Cash And Cash Equivalents', 'Cash Cash Equivalents And Short Term Investments',
                'Cash Financial'
            ],
            'cash_and_cash_equivalents': [
                'Cash And Cash Equivalents', 'Cash Cash Equivalents And Short Term Investments',
                'Cash Financial'
            ],
            'accounts_receivable': [
                'Accounts Receivable', 'Gross Accounts Receivable'
            ],
            'inventory': [
                'Inventory'
            ],
            'property_plant_equipment': [
                'Net PPE', 'Properties Plant And Equipment Net', 'MachineryFurnitureEquipment'
            ],
            'intangible_assets': [
                'Goodwill And Other Intangible Assets', 'Other Intangible Assets', 'Goodwill'
            ],
            
            # Balance sheet - Liabilities
            'total_liabilities': [
                'Total Liabilities Net Minority Interest', 'Total Liabilities'
            ],
            'current_liabilities': [
                'Current Liabilities', 'Total Current Liabilities'
            ],
            'accounts_payable': [
                'Accounts Payable'
            ],
            'total_debt': [
                'Total Debt', 'Net Debt', 'Long Term Debt And Capital Lease Obligation'
            ],
            'long_term_debt': [
                'Long Term Debt', 'Long Term Debt And Capital Lease Obligation'
            ],
            'current_debt': [
                'Current Debt', 'Current Debt And Capital Lease Obligation'
            ],
            
            # Balance sheet - Equity
            'shareholders_equity': [
                'Stockholders Equity', 'Total Stockholders Equity', 'Common Stockholders Equity',
                'Total Equity Gross Minority Interest'
            ],
            'retained_earnings': [
                'Retained Earnings'
            ],
            
            # Share metrics
            'outstanding_shares': [
                'Ordinary Shares Number', 'Share Issued', 'Common Stock Shares Outstanding',
                'Weighted Average Shares Outstanding', 'Basic Average Shares', 'Diluted Average Shares'
            ],
            'weighted_average_shares': [
                'Basic Average Shares', 'Diluted Average Shares', 'Weighted Average Shares Outstanding'
            ],
            
            # Expense metrics
            'cost_of_revenue': [
                'Cost Of Revenue', 'Total Costs And Expenses'
            ],
            'research_and_development': [
                'Research And Development'
            ],
            'selling_general_administrative': [
                'Selling General And Administration', 'General And Administrative Expense'
            ],
            'depreciation_and_amortization': [
                'Depreciation And Amortization', 'Depreciation', 'Amortization',
                'Depreciation Amortization Depletion'
            ],
            'interest_expense': [
                'Interest Expense', 'Interest Expense Non Operating'
            ],
            'tax_provision': [
                'Tax Provision', 'Income Tax Expense'
            ],
            
            # Calculated metrics (we'll compute these)
            'working_capital': [],  # current_assets - current_liabilities
            'operating_margin': [],  # operating_income / revenue
            'gross_margin': [],  # gross_profit / revenue
            'net_margin': [],  # net_income / revenue
            'debt_to_equity': [],  # total_debt / shareholders_equity
            'current_ratio': [],  # current_assets / current_liabilities
            'earnings_per_share': [],  # net_income / weighted_average_shares
            'book_value_per_share': [],  # shareholders_equity / outstanding_shares
            'revenue_per_share': [],  # revenue / outstanding_shares
            'free_cash_flow_per_share': [],  # free_cash_flow / outstanding_shares
            'goodwill_and_intangible_assets': [
                'Goodwill And Other Intangible Assets', 'Other Intangible Assets', 'Goodwill'
            ],
        }
        
        # Create a consolidated results list - one LineItem per period
        period_data = {}
        
        # Search through all statements (prioritize annual over quarterly for consistency)
        statements = {
            'income_statement': income_stmt,
            'balance_sheet': balance_sheet,
            'cash_flow': cashflow,
            'quarterly_income_statement': quarterly_income,
            'quarterly_balance_sheet': quarterly_balance,
            'quarterly_cash_flow': quarterly_cashflow
        }
        
        # Get all available dates across statements
        all_dates = set()
        for df in statements.values():
            if not df.empty:
                all_dates.update(df.columns)
        
        # Initialize period data structure
        for date in all_dates:
            period_data[date] = {
                'report_period': date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date),
                'raw_matches': {}
            }
        
        # Search for each mapped line item across all statements
        for statement_name, df in statements.items():
            if df.empty:
                continue
            
            # For each expected attribute, find matching line items
            for attr_name, search_terms in line_item_mappings.items():
                # Skip calculated metrics - we'll compute them later
                if not search_terms:
                    continue
                    
                # Find matching line items in this statement
                for search_term in search_terms:
                    # First try exact match
                    exact_matches = [idx for idx in df.index if search_term.lower() == idx.lower()]
                    if exact_matches:
                        match = exact_matches[0]
                    else:
                        # Then try contains match
                        contains_matches = [idx for idx in df.index if search_term.lower() in idx.lower()]
                        if contains_matches:
                            match = contains_matches[0]
                        else:
                            continue
                    
                    # Get values for all available periods
                    for date in df.columns:
                        if date in period_data:
                            value = df.loc[match, date]
                            if pd.notna(value) and value != 0:
                                # Only store if we don't already have this field or if this is from annual statements (preferred)
                                if (attr_name not in period_data[date]['raw_matches'] or 
                                    'quarterly' not in statement_name):
                                    period_data[date]['raw_matches'][attr_name] = {
                                        'line_item': match,
                                        'value': float(value),
                                        'statement': statement_name
                                    }
                    break  # Use first matching search term
        
        # Create LineItem objects for each period
        line_items_results = []
        
        # Define all expected fields that agents might access
        all_expected_fields = [
            'revenue', 'operating_revenue', 'operating_income', 'net_income', 'gross_profit',
            'ebit', 'ebitda', 'pretax_income', 'free_cash_flow', 'operating_cash_flow',
            'capital_expenditure', 'cash_dividends_paid', 'dividends_and_other_cash_distributions',
            'total_assets', 'current_assets', 'cash_and_equivalents', 'cash_and_cash_equivalents',
            'accounts_receivable', 'inventory', 'property_plant_equipment', 'intangible_assets',
            'goodwill_and_intangible_assets', 'total_liabilities', 'current_liabilities',
            'accounts_payable', 'total_debt', 'long_term_debt', 'current_debt',
            'shareholders_equity', 'retained_earnings', 'outstanding_shares', 'weighted_average_shares',
            'cost_of_revenue', 'research_and_development', 'selling_general_administrative',
            'depreciation_and_amortization', 'interest_expense', 'tax_provision',
            'working_capital', 'operating_margin', 'gross_margin', 'net_margin',
            'debt_to_equity', 'current_ratio', 'earnings_per_share', 'book_value_per_share',
            'revenue_per_share', 'free_cash_flow_per_share', 'return_on_invested_capital'
        ]
        
        for date, data in period_data.items():
            if data['raw_matches']:  # Only create if we found some data
                # Extract raw values
                line_item_values = {}
                raw_values = {}
                
                # Initialize all expected fields to None
                for field in all_expected_fields:
                    line_item_values[field] = None
                
                # Populate with actual found values
                for attr_name, match_data in data['raw_matches'].items():
                    line_item_values[attr_name] = match_data['value']
                    raw_values[match_data['line_item']] = match_data['value']
                
                # Calculate derived/computed metrics (only if base values exist)
                
                # Working capital = current_assets - current_liabilities
                if (line_item_values.get('current_assets') is not None and 
                    line_item_values.get('current_liabilities') is not None):
                    line_item_values['working_capital'] = (
                        line_item_values['current_assets'] - line_item_values['current_liabilities']
                    )
                
                # Operating margin = operating_income / revenue
                if (line_item_values.get('operating_income') is not None and 
                    line_item_values.get('revenue') is not None and 
                    line_item_values['revenue'] != 0):
                    line_item_values['operating_margin'] = (
                        line_item_values['operating_income'] / line_item_values['revenue']
                    )
                
                # Gross margin = gross_profit / revenue
                if (line_item_values.get('gross_profit') is not None and 
                    line_item_values.get('revenue') is not None and 
                    line_item_values['revenue'] != 0):
                    line_item_values['gross_margin'] = (
                        line_item_values['gross_profit'] / line_item_values['revenue']
                    )
                
                # Net margin = net_income / revenue
                if (line_item_values.get('net_income') is not None and 
                    line_item_values.get('revenue') is not None and 
                    line_item_values['revenue'] != 0):
                    line_item_values['net_margin'] = (
                        line_item_values['net_income'] / line_item_values['revenue']
                    )
                
                # Debt-to-equity = total_debt / shareholders_equity
                if (line_item_values.get('total_debt') is not None and 
                    line_item_values.get('shareholders_equity') is not None and 
                    line_item_values['shareholders_equity'] != 0):
                    line_item_values['debt_to_equity'] = (
                        line_item_values['total_debt'] / line_item_values['shareholders_equity']
                    )
                
                # Current ratio = current_assets / current_liabilities
                if (line_item_values.get('current_assets') is not None and 
                    line_item_values.get('current_liabilities') is not None and 
                    line_item_values['current_liabilities'] != 0):
                    line_item_values['current_ratio'] = (
                        line_item_values['current_assets'] / line_item_values['current_liabilities']
                    )
                
                # Calculate free cash flow if not available directly
                if (line_item_values.get('free_cash_flow') is None and 
                    line_item_values.get('operating_cash_flow') is not None and 
                    line_item_values.get('capital_expenditure') is not None):
                    line_item_values['free_cash_flow'] = (
                        line_item_values['operating_cash_flow'] - abs(line_item_values['capital_expenditure'])
                    )
                
                # Per-share metrics calculations
                shares = line_item_values.get('outstanding_shares') or line_item_values.get('weighted_average_shares')
                if shares and shares > 0:
                    # Earnings per share = net_income / shares
                    if line_item_values.get('net_income') is not None:
                        line_item_values['earnings_per_share'] = line_item_values['net_income'] / shares
                    
                    # Book value per share = shareholders_equity / shares
                    if line_item_values.get('shareholders_equity') is not None:
                        line_item_values['book_value_per_share'] = line_item_values['shareholders_equity'] / shares
                    
                    # Revenue per share = revenue / shares
                    if line_item_values.get('revenue') is not None:
                        line_item_values['revenue_per_share'] = line_item_values['revenue'] / shares
                    
                    # Free cash flow per share = free_cash_flow / shares
                    if line_item_values.get('free_cash_flow') is not None:
                        line_item_values['free_cash_flow_per_share'] = line_item_values['free_cash_flow'] / shares
                
                # Create LineItem object with all expected fields (including None values)
                line_item_obj = LineItem(
                    ticker=ticker,
                    report_period=data['report_period'],
                    period=period,
                    currency=info.get('currency', 'USD'),
                    raw_line_items=raw_values,
                    **line_item_values  # Unpack all values (including None for missing fields)
                )
                line_items_results.append(line_item_obj)
        
        # Sort by date (newest first) and return limited results
        line_items_results.sort(
            key=lambda x: x.report_period, 
            reverse=True
        )
        
        return line_items_results[:limit]
        
    except Exception as e:
        print(f"Error fetching Yahoo Finance line items for {ticker}: {e}")
        import traceback
        traceback.print_exc()
        return []


def get_insider_trades(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
) -> list[InsiderTrade]:
    """Fetch insider trades - Using SEC EDGAR API (FREE) - Limited functionality."""
    cache_key = f"{ticker}_{start_date or 'none'}_{end_date}_{limit}"
    
    # Check cache first
    if cached_data := _cache.get_insider_trades(cache_key):
        return [InsiderTrade(**trade) for trade in cached_data]

    try:
        # Note: SEC EDGAR insider trading data requires complex parsing
        # For now, return empty list - you might want to implement SEC Form 4 parsing
        print(f"Insider trades for {ticker}: SEC EDGAR parsing not implemented yet")
        return []
        
        # TODO: Implement SEC Form 4 parsing or use alternative free source
        # This would require parsing XML from SEC EDGAR which is complex
        
    except Exception as e:
        print(f"Error fetching insider trades for {ticker}: {e}")
        return []


def get_company_news(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
) -> list[CompanyNews]:
    """Fetch company news using NewsAPI (FREE - 100 requests/day)."""
    cache_key = f"{ticker}_{start_date or 'none'}_{end_date}_{limit}"
    
    # Check cache first
    if cached_data := _cache.get_company_news(cache_key):
        return [CompanyNews(**news) for news in cached_data]

    # Check if NewsAPI key is available
    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key:
        print("NewsAPI key not found. Get free key from newsapi.org")
        return []

    try:
        # Calculate date range
        if start_date:
            from_date = start_date
        else:
            # Default to 30 days back
            from_dt = datetime.datetime.strptime(end_date, '%Y-%m-%d') - datetime.timedelta(days=30)
            from_date = from_dt.strftime('%Y-%m-%d')
        
        url = "https://newsapi.org/v2/everything"
        params = {
            'q': f'"{ticker}" OR "{ticker} stock"',
            'from': from_date,
            'to': end_date,
            'sortBy': 'publishedAt',
            'apiKey': api_key,
            'pageSize': min(limit, 100)  # NewsAPI max is 100 per request
        }
        
        response = requests.get(url, params=params)
        if response.status_code != 200:
            print(f"NewsAPI error: {response.status_code}")
            return []
        
        # Use safe JSON parsing to handle control characters
        data = safe_json_parse(response.text)
        articles = data.get('articles', [])
        
        # Convert to CompanyNews objects
        company_news = []
        for article in articles:
            # Handle missing author field - provide default value
            author = article.get('author')
            if not author or author.strip() == '':
                # Use source name as fallback author
                source_info = article.get('source', {})
                author = source_info.get('name', 'Unknown')
            
            news = CompanyNews(
                ticker=ticker,
                title=article.get('title', ''),
                author=author,  # Now guaranteed to have a value
                url=article.get('url', ''),
                date=article.get('publishedAt', ''),
                source=article.get('source', {}).get('name', ''),
                sentiment=None  # NewsAPI doesn't provide sentiment
            )
            company_news.append(news)
        
        # Cache the results
        _cache.set_company_news(cache_key, [news.model_dump() for news in company_news])
        return company_news
        
    except Exception as e:
        print(f"Error fetching news for {ticker}: {e}")
        return []


def get_market_cap(
    ticker: str,
    end_date: str,
) -> float | None:
    """Fetch market cap using Yahoo Finance (FREE)."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        market_cap = info.get('marketCap')
        if market_cap:
            return float(market_cap)
        
        # Alternative calculation: shares outstanding * current price
        shares = info.get('sharesOutstanding')
        current_price = info.get('currentPrice')
        
        if shares and current_price:
            return float(shares * current_price)
        
        return None
        
    except Exception as e:
        print(f"Error fetching market cap for {ticker}: {e}")
        return None


def prices_to_df(prices: list[Price]) -> pd.DataFrame:
    """Convert prices to a DataFrame."""
    df = pd.DataFrame([p.model_dump() for p in prices])
    df["Date"] = pd.to_datetime(df["time"])
    df.set_index("Date", inplace=True)
    numeric_cols = ["open", "close", "high", "low", "volume"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.sort_index(inplace=True)
    return df


def get_price_data(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Get price data using free Yahoo Finance API."""
    prices = get_prices(ticker, start_date, end_date)
    return prices_to_df(prices)


# Additional helper functions for enhanced free data
def get_company_info_detailed(ticker: str) -> Dict:
    """Get detailed company information using Yahoo Finance."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        return {
            'sector': info.get('sector'),
            'industry': info.get('industry'),
            'employees': info.get('fullTimeEmployees'),
            'summary': info.get('longBusinessSummary'),
            'website': info.get('website'),
            'headquarters': {
                'city': info.get('city'),
                'state': info.get('state'),
                'country': info.get('country')
            },
            'metrics': {
                'beta': info.get('beta'),
                'dividend_yield': info.get('dividendYield'),
                'ex_dividend_date': info.get('exDividendDate'),
                'payout_ratio': info.get('payoutRatio'),
                'profit_margin': info.get('profitMargins'),
                'operating_margin': info.get('operatingMargins'),
                'return_on_assets': info.get('returnOnAssets'),
                'return_on_equity': info.get('returnOnEquity'),
                'revenue_per_share': info.get('revenuePerShare'),
                'debt_to_equity': info.get('debtToEquity'),
                'current_ratio': info.get('currentRatio'),
                'book_value': info.get('bookValue'),
                'price_to_book': info.get('priceToBook'),
                'enterprise_value': info.get('enterpriseValue'),
                'forward_pe': info.get('forwardPE'),
                'trailing_pe': info.get('trailingPE')
            }
        }
    except Exception as e:
        print(f"Error fetching detailed company info for {ticker}: {e}")
        return {}
