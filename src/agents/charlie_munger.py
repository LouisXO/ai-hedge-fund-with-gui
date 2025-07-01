from src.graph.state import AgentState, show_agent_reasoning
from src.tools.api import get_financial_metrics, get_market_cap, search_line_items, get_insider_trades, get_company_news
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage
from pydantic import BaseModel
import json
from typing_extensions import Literal
from src.utils.progress import progress
from src.utils.llm import call_llm





class CharlieMungerSignal(BaseModel):
    signal: Literal["bullish", "bearish", "neutral"]
    confidence: float
    reasoning: str


def charlie_munger_agent(state: AgentState):
    """
    Analyzes stocks using Charlie Munger's investing principles and mental models.
    Focuses on moat strength, management quality, predictability, and valuation.
    """
    data = state["data"]
    end_date = data["end_date"]
    tickers = data["tickers"]
    
    analysis_data = {}
    munger_analysis = {}
    
    for ticker in tickers:
        progress.update_status("charlie_munger_agent", ticker, "Fetching financial metrics")
        metrics = get_financial_metrics(ticker, end_date, period="annual", limit=10)  # Munger looks at longer periods
        
        progress.update_status("charlie_munger_agent", ticker, "Gathering financial line items")
        financial_line_items = search_line_items(
            ticker,
            [
                "revenue",
                "net_income",
                "operating_income",
                "return_on_invested_capital",
                "gross_margin",
                "operating_margin",
                "free_cash_flow",
                "capital_expenditure",
                "cash_and_equivalents",
                "total_debt",
                "shareholders_equity",
                "outstanding_shares",
                "research_and_development",
                "goodwill_and_intangible_assets",
            ],
            end_date,
            period="annual",
            limit=10  # Munger examines long-term trends
        )
        
        progress.update_status("charlie_munger_agent", ticker, "Getting market cap")
        market_cap = get_market_cap(ticker, end_date)
        
        progress.update_status("charlie_munger_agent", ticker, "Fetching insider trades")
        # Munger values management with skin in the game
        insider_trades = get_insider_trades(
            ticker,
            end_date,
            # Look back 2 years for insider trading patterns
            start_date=None,
            limit=100
        )
        
        progress.update_status("charlie_munger_agent", ticker, "Fetching company news")
        # Munger avoids businesses with frequent negative press
        company_news = get_company_news(
            ticker,
            end_date,
            # Look back 1 year for news
            start_date=None,
            limit=100
        )
        
        progress.update_status("charlie_munger_agent", ticker, "Analyzing moat strength")
        moat_analysis = analyze_moat_strength(metrics, financial_line_items)
        
        progress.update_status("charlie_munger_agent", ticker, "Analyzing management quality")
        management_analysis = analyze_management_quality(financial_line_items, insider_trades)
        
        progress.update_status("charlie_munger_agent", ticker, "Analyzing business predictability")
        predictability_analysis = analyze_predictability(financial_line_items)
        
        progress.update_status("charlie_munger_agent", ticker, "Calculating Munger-style valuation")
        valuation_analysis = calculate_munger_valuation(financial_line_items, market_cap)
        
        # Combine partial scores with Munger's weighting preferences
        # Munger weights quality and predictability higher than current valuation
        total_score = (
            moat_analysis["score"] * 0.35 +
            management_analysis["score"] * 0.25 +
            predictability_analysis["score"] * 0.25 +
            valuation_analysis["score"] * 0.15
        )
        
        max_possible_score = 10  # Scale to 0-10
        
        # Generate a simple buy/hold/sell signal
        if total_score >= 7.5:  # Munger has very high standards
            signal = "bullish"
        elif total_score <= 4.5:
            signal = "bearish"
        else:
            signal = "neutral"
        
        analysis_data[ticker] = {
            "signal": signal,
            "score": total_score,
            "max_score": max_possible_score,
            "moat_analysis": moat_analysis,
            "management_analysis": management_analysis,
            "predictability_analysis": predictability_analysis,
            "valuation_analysis": valuation_analysis,
            # Include some qualitative assessment from news
            "news_sentiment": analyze_news_sentiment(company_news) if company_news else "No news data available"
        }
        
        progress.update_status("charlie_munger_agent", ticker, "Generating Charlie Munger analysis")
        munger_output = generate_munger_output(
            ticker=ticker, 
            analysis_data=analysis_data,
            state=state,
        )
        
        munger_analysis[ticker] = {
            "signal": munger_output.signal,
            "confidence": munger_output.confidence,
            "reasoning": munger_output.reasoning
        }
        
        progress.update_status("charlie_munger_agent", ticker, "Done", analysis=munger_output.reasoning)
    
    # Wrap results in a single message for the chain
    message = HumanMessage(
        content=json.dumps(munger_analysis),
        name="charlie_munger_agent"
    )
    
    # Show reasoning if requested
    if state["metadata"]["show_reasoning"]:
        show_agent_reasoning(munger_analysis, "Charlie Munger Agent")

    progress.update_status("charlie_munger_agent", None, "Done")
    
    # Add signals to the overall state
    state["data"]["analyst_signals"]["charlie_munger_agent"] = munger_analysis

    return {
        "messages": [message],
        "data": state["data"]
    }


def analyze_moat_strength(metrics: list, financial_line_items: list) -> dict:
    """
    Analyze the business's competitive advantage using Munger's approach:
    - Consistent high returns on capital (ROIC)
    - Pricing power (stable/improving gross margins)
    - Low capital requirements
    - Network effects and intangible assets (R&D investments, goodwill)
    """
    score = 0
    details = []
    
    if not metrics or not financial_line_items:
        return {
            "score": 0,
            "details": "Insufficient data to analyze moat strength"
        }
    
    # 1. Return on Invested Capital (ROIC) analysis - Munger's favorite metric
    roic_values = [item.return_on_invested_capital for item in financial_line_items if item.return_on_invested_capital is not None]

    if roic_values:
        avg_roic = sum(roic_values) / len(roic_values)
        if avg_roic > 0.15:  # >15% ROIC
            score += 3
            details.append(f"Excellent ROIC: {avg_roic:.1%} average")
        elif avg_roic > 0.10:  # >10% ROIC
            score += 2
            details.append(f"Good ROIC: {avg_roic:.1%} average")
        elif avg_roic > 0.05:  # >5% ROIC
            score += 1
            details.append(f"Moderate ROIC: {avg_roic:.1%} average")
        else:
            details.append(f"Poor ROIC: {avg_roic:.1%} average")
    else:
        details.append("No ROIC data available")
    
    # 2. Gross margin analysis
    gross_margins = [item.gross_margin for item in financial_line_items if item.gross_margin is not None]

    if gross_margins and len(gross_margins) >= 3:
        avg_margin = sum(gross_margins) / len(gross_margins)
        # Check consistency/predictability of margins
        margin_volatility = max(gross_margins) - min(gross_margins)
        
        if avg_margin > 0.40 and margin_volatility < 0.10:  # High margins with low volatility
            score += 3
            details.append(f"Excellent and consistent gross margins: {avg_margin:.1%} avg")
        elif avg_margin > 0.25 and margin_volatility < 0.15:  # Good margins with reasonable stability
            score += 2
            details.append(f"Good and stable gross margins: {avg_margin:.1%} avg")
        elif avg_margin > 0.15:
            score += 1
            details.append(f"Moderate gross margins: {avg_margin:.1%} avg")
        else:
            details.append(f"Low gross margins: {avg_margin:.1%} avg")
    else:
        details.append("Insufficient gross margin data")
    
    # 3. Asset-light business model analysis
    if financial_line_items:
        latest = financial_line_items[0]
        
        # Check for asset-light characteristics
        asset_light_indicators = 0
        
        # High R&D spending (future returns without current asset accumulation)
        r_and_d = [item.research_and_development for item in financial_line_items if item.research_and_development is not None]
        revenues = [item.revenue for item in financial_line_items if item.revenue is not None]
        goodwill_and_intangible_assets = [item.goodwill_and_intangible_assets for item in financial_line_items if item.goodwill_and_intangible_assets is not None]
        
        if r_and_d and revenues:
            rd_intensity = r_and_d[0] / revenues[0]
            if rd_intensity > 0.08:  # >8% R&D intensity
                asset_light_indicators += 1
                details.append(f"High R&D intensity: {rd_intensity:.1%}")
        
        # Low goodwill relative to market cap (organic growth vs acquisitions)
        if goodwill_and_intangible_assets and revenues:
            goodwill_to_revenue = goodwill_and_intangible_assets[0] / revenues[0]
            if goodwill_to_revenue < 0.5:  # Low goodwill burden
                asset_light_indicators += 1
        
        # Award points for asset-light characteristics
        if asset_light_indicators >= 2:
            score += 2
            details.append("Strong asset-light business model")
        elif asset_light_indicators == 1:
            score += 1
            details.append("Some asset-light characteristics")
        
    return {
        "score": min(10, score),  # Cap at 10 points
        "details": "; ".join(details)
    }


def analyze_management_quality(financial_line_items: list, insider_trades: list) -> dict:
    """
    Analyze management quality based on:
    1. Capital allocation efficiency
    2. Shareholder returns vs. management compensation
    3. Insider buying/selling patterns
    4. Debt management
    5. Cash management
    6. Share repurchases vs. dilution
    """
    score = 0
    details = []
    
    if not financial_line_items:
        return {"score": 0, "details": "No financial data available"}
    
    # 1. Capital allocation: Free cash flow generation vs. reinvestment
    fcf_values = [item.free_cash_flow for item in financial_line_items if item.free_cash_flow is not None]
    revenues = [item.revenue for item in financial_line_items if item.revenue is not None]
    net_income_values = [item.net_income for item in financial_line_items if item.net_income is not None]
    
    if fcf_values and net_income_values:
        # FCF conversion ratio (FCF / Net Income)
        fcf_conversion = fcf_values[0] / net_income_values[0] if net_income_values[0] != 0 else 0
        
        if fcf_conversion > 1.0:
            score += 2
            details.append(f"Excellent FCF conversion: {fcf_conversion:.1f}x")
        elif fcf_conversion > 0.8:
            score += 1
            details.append(f"Good FCF conversion: {fcf_conversion:.1f}x")
        else:
            details.append(f"Poor FCF conversion: {fcf_conversion:.1f}x")
    
    # 2. Debt management
    debt_values = [item.total_debt for item in financial_line_items if item.total_debt is not None]
    revenues = [item.revenue for item in financial_line_items if item.revenue is not None]
    equity_values = [item.shareholders_equity for item in financial_line_items if item.shareholders_equity is not None]
    
    if debt_values and equity_values:
        debt_to_equity = debt_values[0] / equity_values[0] if equity_values[0] != 0 else float('inf')
        
        if debt_to_equity < 0.3:  # Conservative debt levels
            score += 2
            details.append(f"Conservative debt management: {debt_to_equity:.2f} D/E")
        elif debt_to_equity < 0.6:
            score += 1
            details.append(f"Moderate debt levels: {debt_to_equity:.2f} D/E")
        else:
            details.append(f"High debt levels: {debt_to_equity:.2f} D/E")
    
    # 3. Cash management - avoid hoarding, but maintain adequate reserves
    cash_values = [item.cash_and_equivalents for item in financial_line_items if item.cash_and_equivalents is not None]
    revenue_values = [item.revenue for item in financial_line_items if item.revenue is not None]
    
    if cash_values and revenue_values:
        cash_to_revenue = cash_values[0] / revenue_values[0]
        
        # Sweet spot: enough cash for opportunities but not excessive hoarding
        if 0.1 <= cash_to_revenue <= 0.3:  # 10-30% of revenue in cash
            score += 2
            details.append(f"Optimal cash management: {cash_to_revenue:.1%} of revenue")
        elif cash_to_revenue > 0.5:
            details.append(f"Excessive cash hoarding: {cash_to_revenue:.1%} of revenue")
        elif cash_to_revenue < 0.05:
            details.append(f"Potentially inadequate cash reserves: {cash_to_revenue:.1%} of revenue")
        else:
            score += 1
            details.append(f"Reasonable cash management: {cash_to_revenue:.1%} of revenue")
    
    # 4. Insider trading analysis
    if insider_trades and len(insider_trades) > 0:
        # This is a placeholder - would need actual insider trading data analysis
        # For now, just note the availability of data
        details.append(f"Insider trading data available ({len(insider_trades)} transactions)")
    else:
        details.append("No insider trading data available")
    
    # 5. Share count analysis (dilution vs. buybacks)
    share_counts = [item.outstanding_shares for item in financial_line_items if item.outstanding_shares is not None]
    
    if len(share_counts) >= 3:
        # Check trend over time (remember data is in reverse chronological order)
        recent_shares = share_counts[0]
        older_shares = share_counts[-1]
        
        share_change = (recent_shares - older_shares) / older_shares
        
        if share_change < -0.05:  # >5% reduction (buybacks)
            score += 2
            details.append(f"Shareholder-friendly buybacks: {share_change:.1%} share reduction")
        elif share_change < 0:
            score += 1
            details.append(f"Some share reduction: {share_change:.1%}")
        elif share_change > 0.1:  # >10% dilution
            details.append(f"Significant share dilution: {share_change:.1%}")
        else:
            details.append(f"Stable share count: {share_change:.1%}")
    
    return {
        "score": min(10, score),  # Cap at 10 points
        "details": "; ".join(details)
    }


def analyze_predictability(financial_line_items: list) -> dict:
    """
    Analyze business predictability - Munger's key criterion.
    Look for:
    1. Revenue predictability and growth
    2. Margin stability
    3. Cash flow consistency
    4. Earnings quality
    """
    score = 0
    details = []
    
    if not financial_line_items or len(financial_line_items) < 3:
        return {"score": 0, "details": "Insufficient data for predictability analysis"}
    
    # 1. Revenue growth predictability
    revenues = [item.revenue for item in financial_line_items if item.revenue is not None]
    
    if len(revenues) >= 5:
        # Calculate year-over-year growth rates (remember reverse chronological order)
        growth_rates = []
        for i in range(len(revenues) - 1):
            if revenues[i + 1] != 0:
                growth_rate = (revenues[i] - revenues[i + 1]) / revenues[i + 1]
                growth_rates.append(growth_rate)
        
        if growth_rates:
            avg_growth = sum(growth_rates) / len(growth_rates)
            growth_volatility = max(growth_rates) - min(growth_rates)
            
            # Reward consistent, positive growth
            if avg_growth > 0.05 and growth_volatility < 0.20:  # >5% growth with <20% volatility
                score += 3
                details.append(f"Highly predictable revenue growth: {avg_growth:.1%} avg, low volatility")
            elif avg_growth > 0 and growth_volatility < 0.30:
                score += 2
                details.append(f"Predictable positive revenue growth: {avg_growth:.1%} avg")
            elif avg_growth > 0:
                score += 1
                details.append(f"Positive but volatile revenue growth: {avg_growth:.1%} avg")
            else:
                details.append(f"Unpredictable or declining revenue: {avg_growth:.1%} avg")
    else:
        details.append("Insufficient revenue history")
    
    # 2. Operating income predictability
    op_income = [item.operating_income for item in financial_line_items if item.operating_income is not None]
    
    if len(op_income) >= 4:
        # Count positive operating income periods
        positive_periods = sum(1 for income in op_income if income > 0)
        
        if positive_periods == len(op_income):
            score += 2
            details.append("Consistently positive operating income")
        elif positive_periods >= len(op_income) * 0.8:
            score += 1
            details.append(f"Mostly positive operating income: {positive_periods}/{len(op_income)} periods")
        else:
            details.append(f"Inconsistent operating income: {positive_periods}/{len(op_income)} positive periods")
    else:
        details.append("Insufficient operating income history")
    
    # 3. Operating margin stability
    op_margins = [item.operating_margin for item in financial_line_items if item.operating_margin is not None]
    
    if len(op_margins) >= 4:
        avg_margin = sum(op_margins) / len(op_margins)
        margin_volatility = max(op_margins) - min(op_margins)
        
        if avg_margin > 0.15 and margin_volatility < 0.05:  # >15% margins with <5% volatility
            score += 3
            details.append(f"Highly predictable margins: {avg_margin:.1%} avg with minimal volatility")
        elif margin_volatility < 0.07:  # Moderately stable margins
            score += 1
            details.append(f"Moderately predictable margins: {avg_margin:.1%} avg with some volatility")
        else:
            details.append(f"Unpredictable margins: {avg_margin:.1%} avg with high volatility ({margin_volatility:.1%})")
    else:
        details.append("Insufficient margin history")
    
    # 4. Cash generation reliability
    fcf_values = [item.free_cash_flow for item in financial_line_items if item.free_cash_flow is not None]
    
    if fcf_values and len(fcf_values) >= 5:
        # Count positive FCF periods
        positive_fcf_periods = sum(1 for fcf in fcf_values if fcf > 0)
        
        if positive_fcf_periods == len(fcf_values):
            # Consistently positive FCF
            score += 2
            details.append("Highly predictable cash generation: Positive FCF in all periods")
        elif positive_fcf_periods >= len(fcf_values) * 0.8:
            # Mostly positive FCF
            score += 1
            details.append(f"Predictable cash generation: Positive FCF in {positive_fcf_periods}/{len(fcf_values)} periods")
        else:
            details.append(f"Unpredictable cash generation: Positive FCF in only {positive_fcf_periods}/{len(fcf_values)} periods")
    else:
        details.append("Insufficient free cash flow history")
    
    # Scale score to 0-10 range
    # Maximum possible raw score would be 10 (3+3+2+2)
    final_score = min(10, score * 10 / 10)
    
    return {
        "score": final_score,
        "details": "; ".join(details)
    }


def calculate_munger_valuation(financial_line_items: list, market_cap: float) -> dict:
    """
    Calculate intrinsic value using Munger's approach:
    - Focus on owner earnings (approximated by FCF)
    - Simple multiple on normalized earnings
    - Prefer paying a fair price for a wonderful business
    """
    score = 0
    details = []
    
    if not financial_line_items or market_cap is None:
        return {
            "score": 0,
            "details": "Insufficient data to perform valuation"
        }
    
    # Get FCF values (Munger's preferred "owner earnings" metric)
    fcf_values = [item.free_cash_flow for item in financial_line_items if item.free_cash_flow is not None]
    
    if not fcf_values or len(fcf_values) < 3:
        return {
            "score": 0,
            "details": "Insufficient free cash flow data for valuation"
        }
    
    # 1. Normalize earnings by taking average of last 3-5 years
    # (Munger prefers to normalize earnings to avoid over/under-valuation based on cyclical factors)
    normalized_fcf = sum(fcf_values[:min(5, len(fcf_values))]) / min(5, len(fcf_values))
    
    if normalized_fcf <= 0:
        return {
            "score": 0,
            "details": f"Negative or zero normalized FCF ({normalized_fcf}), cannot value",
            "intrinsic_value": None
        }
    
    # 2. Calculate FCF yield (inverse of P/FCF multiple)
    if market_cap <= 0:
        return {
            "score": 0,
            "details": f"Invalid market cap ({market_cap}), cannot value"
        }
    
    fcf_yield = normalized_fcf / market_cap
    
    # 3. Apply Munger's FCF multiple based on business quality
    # Munger would pay higher multiples for wonderful businesses
    # Let's use a sliding scale where higher FCF yields are more attractive
    if fcf_yield > 0.08:  # >8% FCF yield (P/FCF < 12.5x)
        score += 4
        details.append(f"Excellent value: {fcf_yield:.1%} FCF yield")
    elif fcf_yield > 0.05:  # >5% FCF yield (P/FCF < 20x)
        score += 3
        details.append(f"Good value: {fcf_yield:.1%} FCF yield")
    elif fcf_yield > 0.03:  # >3% FCF yield (P/FCF < 33x)
        score += 1
        details.append(f"Fair value: {fcf_yield:.1%} FCF yield")
    else:
        details.append(f"Expensive: Only {fcf_yield:.1%} FCF yield")
    
    # 4. Calculate simple intrinsic value range
    # Munger tends to use straightforward valuations, avoiding complex DCF models
    conservative_value = normalized_fcf * 10  # 10x FCF = 10% yield
    reasonable_value = normalized_fcf * 15    # 15x FCF ≈ 6.7% yield
    optimistic_value = normalized_fcf * 20    # 20x FCF = 5% yield
    
    # 5. Calculate margins of safety
    current_to_reasonable = (reasonable_value - market_cap) / market_cap
    
    if current_to_reasonable > 0.3:  # >30% upside
        score += 3
        details.append(f"Large margin of safety: {current_to_reasonable:.1%} upside to reasonable value")
    elif current_to_reasonable > 0.1:  # >10% upside
        score += 2
        details.append(f"Moderate margin of safety: {current_to_reasonable:.1%} upside to reasonable value")
    elif current_to_reasonable > -0.1:  # Within 10% of reasonable value
        score += 1
        details.append(f"Fair price: Within 10% of reasonable value ({current_to_reasonable:.1%})")
    else:
        details.append(f"Expensive: {-current_to_reasonable:.1%} premium to reasonable value")
    
    # 6. Check earnings trajectory for additional context
    # Munger likes growing owner earnings
    if len(fcf_values) >= 3:
        recent_avg = sum(fcf_values[:3]) / 3
        older_avg = sum(fcf_values[-3:]) / 3 if len(fcf_values) >= 6 else fcf_values[-1]
        
        if recent_avg > older_avg * 1.2:  # >20% growth in FCF
            score += 3
            details.append("Growing FCF trend adds to intrinsic value")
        elif recent_avg > older_avg:
            score += 2
            details.append("Stable to growing FCF supports valuation")
        else:
            details.append("Declining FCF trend is concerning")
    
    # Scale score to 0-10 range
    # Maximum possible raw score would be 10 (4+3+3)
    final_score = min(10, score * 10 / 10) 
    
    return {
        "score": final_score,
        "details": "; ".join(details),
        "intrinsic_value_range": {
            "conservative": conservative_value,
            "reasonable": reasonable_value,
            "optimistic": optimistic_value
        },
        "fcf_yield": fcf_yield,
        "normalized_fcf": normalized_fcf
    }


def analyze_news_sentiment(news_items: list) -> str:
    """
    Simple qualitative analysis of recent news.
    Munger pays attention to significant news but doesn't overreact to short-term stories.
    """
    if not news_items or len(news_items) == 0:
        return "No news data available"
    
    # Just return a simple count for now - in a real implementation, this would use NLP
    return f"Qualitative review of {len(news_items)} recent news items would be needed"


def generate_munger_output(
    ticker: str,
    analysis_data: dict[str, any],
    state: AgentState,
) -> CharlieMungerSignal:
    """
    Generates investment decisions in the style of Charlie Munger.
    """
    template = ChatPromptTemplate.from_messages([
        (
            "system",
            """You are a Charlie Munger AI agent, making investment decisions using his principles:

            1. Focus on the quality and predictability of the business.
            2. Rely on mental models from multiple disciplines to analyze investments.
            3. Look for strong, durable competitive advantages (moats).
            4. Emphasize long-term thinking and patience.
            5. Value management integrity and competence.
            6. Prioritize businesses with high returns on invested capital.
            7. Pay a fair price for wonderful businesses.
            8. Never overpay, always demand a margin of safety.
            9. Avoid complexity and businesses you don't understand.
            10. "Invert, always invert" - focus on avoiding stupidity rather than seeking brilliance.
            
            Rules:
            - Praise businesses with predictable, consistent operations and cash flows.
            - Value businesses with high ROIC and pricing power.
            - Prefer simple businesses with understandable economics.
            - Admire management with skin in the game and shareholder-friendly capital allocation.
            - Focus on long-term economics rather than short-term metrics.
            - Be skeptical of businesses with rapidly changing dynamics or excessive share dilution.
            - Avoid excessive leverage or financial engineering.
            - Provide a rational, data-driven recommendation (bullish, bearish, or neutral).
            
            When providing your reasoning, be thorough and specific by:
            1. Explaining the key factors that influenced your decision the most (both positive and negative)
            2. Applying at least 2-3 specific mental models or disciplines to explain your thinking
            3. Providing quantitative evidence where relevant (e.g., specific ROIC values, margin trends)
            4. Citing what you would "avoid" in your analysis (invert the problem)
            5. Using Charlie Munger's direct, pithy conversational style in your explanation
            
            For example, if bullish: "The high ROIC of 22% demonstrates the company's moat. When applying basic microeconomics, we can see that competitors would struggle to..."
            For example, if bearish: "I see this business making a classic mistake in capital allocation. As I've often said about [relevant Mungerism], this company appears to be..."
            """
        ),
        (
            "human",
            """Based on the following analysis, create a Munger-style investment signal.

            Analysis Data for {ticker}:
            {analysis_data}

            Return the trading signal in this JSON format:
            {{
              "signal": "bullish/bearish/neutral",
              "confidence": float (0-100),
              "reasoning": "string"
            }}
            """
        )
    ])

    prompt = template.invoke({
        "analysis_data": json.dumps(analysis_data, indent=2),
        "ticker": ticker
    })

    def create_default_charlie_munger_signal():
        return CharlieMungerSignal(
            signal="neutral",
            confidence=0.0,
            reasoning="Error in analysis, defaulting to neutral"
        )

    return call_llm(
        prompt=prompt,
        state=state,
        pydantic_model=CharlieMungerSignal, 
        agent_name="charlie_munger_agent", 
        default_factory=create_default_charlie_munger_signal,
    )