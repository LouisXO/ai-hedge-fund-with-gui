from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Dict, List, Optional
import uuid
import hashlib
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from app.backend.services.portfolio import create_portfolio

router = APIRouter(prefix="/portfolio")

class CreatePortfolioRequest(BaseModel):
    initial_cash: float = 100000.0
    margin_requirement: float = 0.5
    tickers: List[str]

class PortfolioPosition(BaseModel):
    ticker: str
    long_shares: int = 0
    short_shares: int = 0
    long_cost_basis: float = 0.0
    short_cost_basis: float = 0.0

class UpdatePortfolioRequest(BaseModel):
    positions: List[PortfolioPosition]

class StockDataRequest(BaseModel):
    tickers: List[str]
    period: str = "1mo"  # 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max

# Store portfolio in memory (in production, use a database)
_portfolio_store: Dict[str, dict] = {}

def generate_session_portfolio_id(request: Request) -> str:
    """Generate a unique portfolio ID based on client session"""
    # Use client IP + user agent as session identifier
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    
    # Create a hash-based session ID
    session_data = f"{client_ip}_{user_agent}"
    session_hash = hashlib.md5(session_data.encode()).hexdigest()[:12]
    return f"portfolio_{session_hash}"

@router.post("/create")
async def create_new_portfolio(request: CreatePortfolioRequest, http_request: Request):
    """Create a new portfolio with initial cash and tickers"""
    try:
        portfolio = create_portfolio(
            initial_cash=request.initial_cash,
            margin_requirement=request.margin_requirement,
            tickers=request.tickers
        )
        
        # Generate unique portfolio ID for this session
        portfolio_id = generate_session_portfolio_id(http_request)
        _portfolio_store[portfolio_id] = portfolio
        
        return {
            "portfolio_id": portfolio_id,
            "portfolio": portfolio,
            "message": "Portfolio created successfully"
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/")
async def get_portfolio(http_request: Request, portfolio_id: Optional[str] = None):
    """Get current portfolio"""
    # Use session-based ID if no specific ID provided
    if portfolio_id is None:
        portfolio_id = generate_session_portfolio_id(http_request)
    
    # Auto-create portfolio if it doesn't exist for this session
    if portfolio_id not in _portfolio_store:
        print(f"Portfolio {portfolio_id} not found, creating session portfolio...")
        session_portfolio = create_portfolio(
            initial_cash=100000.0,
            margin_requirement=0.5,
            tickers=[]
        )
        _portfolio_store[portfolio_id] = session_portfolio
    
    return {
        "portfolio_id": portfolio_id,
        "portfolio": _portfolio_store[portfolio_id]
    }

@router.put("/positions")
async def update_portfolio_positions(
    request: UpdatePortfolioRequest, 
    http_request: Request,
    portfolio_id: Optional[str] = None
):
    """Update portfolio positions"""
    # Use session-based ID if no specific ID provided
    if portfolio_id is None:
        portfolio_id = generate_session_portfolio_id(http_request)
    
    # Auto-create portfolio if it doesn't exist for this session
    if portfolio_id not in _portfolio_store:
        print(f"Portfolio {portfolio_id} not found, creating session portfolio...")
        session_portfolio = create_portfolio(
            initial_cash=100000.0,
            margin_requirement=0.5,
            tickers=[]  # Will be populated as positions are added
        )
        _portfolio_store[portfolio_id] = session_portfolio
    
    portfolio = _portfolio_store[portfolio_id]
    
    for position in request.positions:
        # Create position entry if it doesn't exist (for new tickers)
        if position.ticker not in portfolio["positions"]:
            portfolio["positions"][position.ticker] = {
                "long": 0,
                "short": 0,
                "long_cost_basis": 0.0,
                "short_cost_basis": 0.0,
                "short_margin_used": 0.0,
            }
            # Also initialize realized gains for new ticker
            portfolio["realized_gains"][position.ticker] = {
                "long": 0.0,
                "short": 0.0,
            }
        
        # Update position values
        portfolio["positions"][position.ticker]["long"] = position.long_shares
        portfolio["positions"][position.ticker]["short"] = position.short_shares
        portfolio["positions"][position.ticker]["long_cost_basis"] = position.long_cost_basis
        portfolio["positions"][position.ticker]["short_cost_basis"] = position.short_cost_basis
    
    return {
        "message": "Portfolio positions updated",
        "portfolio_id": portfolio_id,
        "portfolio": portfolio
    }

@router.get("/summary")
async def get_portfolio_summary(http_request: Request, portfolio_id: Optional[str] = None):
    """Get portfolio summary with current values"""
    # Use session-based ID if no specific ID provided
    if portfolio_id is None:
        portfolio_id = generate_session_portfolio_id(http_request)
    
    # Auto-create portfolio if it doesn't exist for this session
    if portfolio_id not in _portfolio_store:
        print(f"Portfolio {portfolio_id} not found, creating session portfolio...")
        session_portfolio = create_portfolio(
            initial_cash=100000.0,
            margin_requirement=0.5,
            tickers=[]
        )
        _portfolio_store[portfolio_id] = session_portfolio
    
    portfolio = _portfolio_store[portfolio_id]
    
    # Calculate total positions value (you might want to fetch real-time prices)
    total_long_value = 0
    total_short_value = 0
    
    for ticker, position in portfolio["positions"].items():
        # For demo purposes, using cost basis. In production, use current market prices
        total_long_value += position["long"] * position["long_cost_basis"]
        total_short_value += position["short"] * position["short_cost_basis"]
    
    total_value = portfolio["cash"] + total_long_value - total_short_value
    
    return {
        "portfolio_id": portfolio_id,
        "cash": portfolio["cash"],
        "total_long_value": total_long_value,
        "total_short_value": total_short_value,
        "total_portfolio_value": total_value,
        "margin_used": portfolio["margin_used"],
        "margin_requirement": portfolio["margin_requirement"],
        "positions_count": len([p for p in portfolio["positions"].values() if p["long"] > 0 or p["short"] > 0])
    }

@router.post("/stock-data")
async def get_stock_data(request: StockDataRequest):
    """Get comprehensive stock data including charts, current prices, and market details"""
    try:
        stock_data = {}
        
        for ticker in request.tickers:
            try:
                stock = yf.Ticker(ticker)
                
                # Get basic info
                info = stock.info
                
                # Get historical data for charts
                hist = stock.history(period=request.period)
                
                # Get current market data
                current_data = stock.history(period="1d")
                
                # Prepare chart data
                chart_data = []
                if not hist.empty:
                    for date, row in hist.iterrows():
                        chart_data.append({
                            "date": date.strftime('%Y-%m-%d'),
                            "timestamp": int(date.timestamp() * 1000),  # For JavaScript Date
                            "open": float(row['Open']),
                            "high": float(row['High']),
                            "low": float(row['Low']),
                            "close": float(row['Close']),
                            "volume": int(row['Volume'])
                        })
                
                # Get current price info
                current_price = None
                price_change = None
                price_change_percent = None
                
                if not current_data.empty:
                    current_price = float(current_data['Close'].iloc[-1])
                    if len(current_data) > 1:
                        prev_close = float(current_data['Close'].iloc[-2])
                        price_change = current_price - prev_close
                        price_change_percent = (price_change / prev_close) * 100
                
                # Compile comprehensive stock data
                stock_data[ticker] = {
                    # Basic info
                    "symbol": ticker,
                    "company_name": info.get('longName', ticker),
                    "sector": info.get('sector'),
                    "industry": info.get('industry'),
                    
                    # Current market data
                    "current_price": current_price,
                    "price_change": price_change,
                    "price_change_percent": price_change_percent,
                    "volume": info.get('volume'),
                    "avg_volume": info.get('averageVolume'),
                    
                    # Market metrics
                    "market_cap": info.get('marketCap'),
                    "enterprise_value": info.get('enterpriseValue'),
                    "shares_outstanding": info.get('sharesOutstanding'),
                    "float_shares": info.get('floatShares'),
                    
                    # Valuation ratios
                    "pe_ratio": info.get('trailingPE'),
                    "forward_pe": info.get('forwardPE'),
                    "pb_ratio": info.get('priceToBook'),
                    "ps_ratio": info.get('priceToSalesTrailing12Months'),
                    "peg_ratio": info.get('pegRatio'),
                    
                    # Financial health
                    "debt_to_equity": info.get('debtToEquity'),
                    "current_ratio": info.get('currentRatio'),
                    "quick_ratio": info.get('quickRatio'),
                    "return_on_equity": info.get('returnOnEquity'),
                    "return_on_assets": info.get('returnOnAssets'),
                    
                    # Margins
                    "profit_margin": info.get('profitMargins'),
                    "operating_margin": info.get('operatingMargins'),
                    "gross_margin": info.get('grossMargins'),
                    
                    # Growth
                    "revenue_growth": info.get('revenueGrowth'),
                    "earnings_growth": info.get('earningsGrowth'),
                    
                    # Dividends
                    "dividend_yield": info.get('dividendYield'),
                    "payout_ratio": info.get('payoutRatio'),
                    "ex_dividend_date": info.get('exDividendDate'),
                    
                    # Trading ranges
                    "day_high": info.get('dayHigh'),
                    "day_low": info.get('dayLow'),
                    "week_52_high": info.get('fiftyTwoWeekHigh'),
                    "week_52_low": info.get('fiftyTwoWeekLow'),
                    
                    # Chart data
                    "chart_data": chart_data,
                    "period": request.period,
                    
                    # Additional info
                    "beta": info.get('beta'),
                    "employees": info.get('fullTimeEmployees'),
                    "headquarters": {
                        "city": info.get('city'),
                        "state": info.get('state'),
                        "country": info.get('country')
                    },
                    "website": info.get('website'),
                    
                    # Last updated
                    "last_updated": datetime.now().isoformat()
                }
                
            except Exception as e:
                # If individual stock fails, include error info
                stock_data[ticker] = {
                    "symbol": ticker,
                    "error": f"Failed to fetch data: {str(e)}",
                    "chart_data": [],
                    "last_updated": datetime.now().isoformat()
                }
        
        return {
            "stock_data": stock_data,
            "period": request.period,
            "total_stocks": len(request.tickers),
            "successful": len([s for s in stock_data.values() if "error" not in s]),
            "failed": len([s for s in stock_data.values() if "error" in s])
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching stock data: {str(e)}")

@router.get("/holdings-data")
async def get_holdings_data(http_request: Request, portfolio_id: Optional[str] = None, period: str = "1mo"):
    """Get comprehensive data for all holdings in the portfolio"""
    # Get portfolio first
    portfolio_response = await get_portfolio(http_request, portfolio_id)
    portfolio = portfolio_response["portfolio"]
    
    # Get tickers from active positions
    active_tickers = [
        ticker for ticker, position in portfolio["positions"].items()
        if position["long"] > 0 or position["short"] > 0
    ]
    
    if not active_tickers:
        return {
            "portfolio_id": portfolio_response["portfolio_id"],
            "holdings": {},
            "message": "No active positions found"
        }
    
    # Get stock data for all holdings
    stock_request = StockDataRequest(tickers=active_tickers, period=period)
    stock_data_response = await get_stock_data(stock_request)
    
    # Combine with position data
    holdings_data = {}
    for ticker in active_tickers:
        position = portfolio["positions"][ticker]
        stock_info = stock_data_response["stock_data"].get(ticker, {})
        
        # Calculate position values
        current_price = stock_info.get("current_price", 0)
        long_value = position["long"] * position["long_cost_basis"]
        short_value = position["short"] * position["short_cost_basis"]
        
        # Calculate unrealized gains
        if current_price:
            current_long_value = position["long"] * current_price
            current_short_value = position["short"] * current_price
            long_unrealized = current_long_value - long_value if position["long"] > 0 else 0
            short_unrealized = short_value - current_short_value if position["short"] > 0 else 0
            total_unrealized = long_unrealized + short_unrealized
        else:
            current_long_value = long_value
            current_short_value = short_value
            total_unrealized = 0
        
        holdings_data[ticker] = {
            **stock_info,  # Include all stock data
            "position": {
                "long_shares": position["long"],
                "short_shares": position["short"],
                "long_cost_basis": position["long_cost_basis"],
                "short_cost_basis": position["short_cost_basis"],
                "long_value": long_value,
                "short_value": short_value,
                "current_long_value": current_long_value,
                "current_short_value": current_short_value,
                "total_unrealized_gain": total_unrealized,
                "unrealized_gain_percent": (total_unrealized / (long_value + short_value)) * 100 if (long_value + short_value) > 0 else 0
            }
        }
    
    return {
        "portfolio_id": portfolio_response["portfolio_id"],
        "holdings": holdings_data,
        "period": period,
        "total_holdings": len(holdings_data)
    }

@router.get("/list")
async def list_portfolios():
    """Debug endpoint: List all portfolios (remove in production)"""
    return {
        "portfolios": list(_portfolio_store.keys()),
        "total_count": len(_portfolio_store)
    } 