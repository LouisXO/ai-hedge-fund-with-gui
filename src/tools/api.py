import datetime
import os
import pandas as pd
import requests
import yfinance as yf
from typing import List, Dict, Optional

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
    """Fetch line items from Yahoo Finance financial statements (FREE)."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        # Get financial statements
        income_stmt = stock.financials
        balance_sheet = stock.balance_sheet
        cashflow = stock.cashflow
        
        line_items_results = []
        
        # Search through all statements for requested line items
        statements = {
            'income_statement': income_stmt,
            'balance_sheet': balance_sheet,
            'cash_flow': cashflow
        }
        
        for statement_name, df in statements.items():
            if df.empty:
                continue
                
            for line_item in line_items:
                # Search for line item in the dataframe index (case insensitive)
                matching_items = [idx for idx in df.index if line_item.lower() in idx.lower()]
                
                for match in matching_items:
                    # Get the most recent value (first column)
                    if not df.columns.empty:
                        recent_date = df.columns[0]
                        value = df.loc[match, recent_date]
                        
                        if pd.notna(value):
                            line_item_obj = LineItem(
                                ticker=ticker,
                                report_period=recent_date.strftime('%Y-%m-%d') if hasattr(recent_date, 'strftime') else str(recent_date),
                                period=period,
                                currency=info.get('currency', 'USD'),  # Required field
                                line_item=match,
                                value=float(value),
                                statement_type=statement_name
                            )
                            line_items_results.append(line_item_obj)
        
        return line_items_results[:limit]
        
    except Exception as e:
        print(f"Error fetching Yahoo Finance line items for {ticker}: {e}")
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
        
        data = response.json()
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
