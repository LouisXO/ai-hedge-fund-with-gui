from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any
import random
from datetime import datetime

router = APIRouter(prefix="/portfolio")

# Mock portfolio data - replace with actual database/broker integration
MOCK_HOLDINGS = [
    {
        "ticker": "AAPL",
        "shares": 50,
        "averagePrice": 180.00,
        "currentPrice": 185.25,
    },
    {
        "ticker": "MSFT", 
        "shares": 75,
        "averagePrice": 380.00,
        "currentPrice": 390.80,
    },
    {
        "ticker": "GOOGL",
        "shares": 100,
        "averagePrice": 140.00,
        "currentPrice": 145.20,
    },
    {
        "ticker": "TSLA",
        "shares": 30,
        "averagePrice": 220.00,
        "currentPrice": 205.50,
    }
]

def calculate_portfolio_metrics(holdings: List[Dict], cash_balance: float = 15420.50):
    """Calculate portfolio metrics from holdings data."""
    total_invested = 0
    total_value = cash_balance
    total_unrealized_pnl = 0
    
    processed_holdings = []
    
    for holding in holdings:
        shares = holding["shares"]
        avg_price = holding["averagePrice"]
        current_price = holding["currentPrice"]
        
        # Add some random price movement for demo
        current_price += random.uniform(-2, 2)
        
        invested_amount = shares * avg_price
        market_value = shares * current_price
        unrealized_pnl = market_value - invested_amount
        unrealized_pnl_percent = (unrealized_pnl / invested_amount) * 100
        
        total_invested += invested_amount
        total_value += market_value
        total_unrealized_pnl += unrealized_pnl
        
        processed_holdings.append({
            **holding,
            "currentPrice": round(current_price, 2),
            "totalValue": round(market_value, 2),
            "unrealizedPnL": round(unrealized_pnl, 2),
            "unrealizedPnLPercent": round(unrealized_pnl_percent, 2),
            "weight": 0  # Will calculate after we have total_value
        })
    
    # Calculate weights
    for holding in processed_holdings:
        holding["weight"] = round((holding["totalValue"] / total_value) * 100, 2)
    
    total_pnl_percent = (total_unrealized_pnl / total_invested) * 100 if total_invested > 0 else 0
    
    return {
        "totalValue": round(total_value, 2),
        "cashBalance": cash_balance,
        "totalInvested": round(total_invested, 2),
        "totalPnL": round(total_unrealized_pnl, 2),
        "totalPnLPercent": round(total_pnl_percent, 2),
        "holdings": processed_holdings,
        "lastUpdated": datetime.now().isoformat()
    }

@router.get("/")
async def get_portfolio():
    """Get current portfolio data with real-time prices."""
    try:
        portfolio_data = calculate_portfolio_metrics(MOCK_HOLDINGS)
        return portfolio_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching portfolio data: {str(e)}")

@router.get("/holdings")
async def get_holdings():
    """Get just the holdings data."""
    try:
        portfolio_data = calculate_portfolio_metrics(MOCK_HOLDINGS)
        return portfolio_data["holdings"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching holdings: {str(e)}")

@router.get("/performance")
async def get_performance():
    """Get portfolio performance metrics."""
    try:
        # Mock performance data - replace with actual calculations
        return {
            "periods": {
                "1D": {"return": 0.85, "color": "positive"},
                "1W": {"return": 2.31, "color": "positive"},
                "1M": {"return": 6.38, "color": "positive"},
                "3M": {"return": 12.45, "color": "positive"},
                "1Y": {"return": 24.76, "color": "positive"},
            },
            "metrics": {
                "sharpeRatio": 1.45,
                "maxDrawdown": -8.2,
                "volatility": 18.5,
                "beta": 1.1
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching performance data: {str(e)}")

@router.get("/alerts")
async def get_alerts():
    """Get portfolio alerts and notifications."""
    try:
        portfolio_data = calculate_portfolio_metrics(MOCK_HOLDINGS)
        alerts = []
        
        # Generate alerts based on portfolio data
        for holding in portfolio_data["holdings"]:
            if holding["unrealizedPnLPercent"] < -5:
                alerts.append({
                    "id": f"loss-{holding['ticker']}",
                    "type": "warning",
                    "title": "Significant Loss",
                    "message": f"{holding['ticker']} is down {abs(holding['unrealizedPnLPercent']):.1f}%",
                    "ticker": holding["ticker"],
                    "timestamp": datetime.now().isoformat()
                })
            
            if holding["weight"] > 30:
                alerts.append({
                    "id": f"concentration-{holding['ticker']}",
                    "type": "info", 
                    "title": "Concentration Risk",
                    "message": f"{holding['ticker']} represents {holding['weight']:.1f}% of your portfolio",
                    "ticker": holding["ticker"],
                    "timestamp": datetime.now().isoformat()
                })
        
        return alerts
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching alerts: {str(e)}") 