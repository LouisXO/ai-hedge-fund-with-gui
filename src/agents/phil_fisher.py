from src.graph.state import AgentState, show_agent_reasoning
from src.tools.api import (
    get_financial_metrics,
    get_market_cap,
    search_line_items,
    get_insider_trades,
    get_company_news,
)
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage
from pydantic import BaseModel
import json
from typing_extensions import Literal
from src.utils.progress import progress
from src.utils.llm import call_llm
import statistics
from typing import List





class PhilFisherSignal(BaseModel):
    signal: Literal["bullish", "bearish", "neutral"]
    confidence: float
    reasoning: str


def phil_fisher_agent(state: AgentState):
    """
    Analyzes stocks using Phil Fisher's investing principles:
      - Seek companies with long-term above-average growth potential
      - Emphasize quality of management and R&D
      - Look for strong margins, consistent growth, and manageable leverage
      - Combine fundamental 'scuttlebutt' style checks with basic sentiment and insider data
      - Willing to pay up for quality, but still mindful of valuation
      - Generally focuses on long-term compounding

    Returns a bullish/bearish/neutral signal with confidence and reasoning.
    """
    data = state["data"]
    end_date = data["end_date"]
    tickers = data["tickers"]

    analysis_data = {}
    fisher_analysis = {}

    for ticker in tickers:
        progress.update_status("phil_fisher_agent", ticker, "Fetching financial metrics")
        metrics = get_financial_metrics(ticker, end_date, period="annual", limit=5)

        progress.update_status("phil_fisher_agent", ticker, "Gathering financial line items")
        # Include relevant line items for Phil Fisher's approach:
        #   - Growth & Quality: revenue, net_income, earnings_per_share, R&D expense
        #   - Margins & Stability: operating_income, operating_margin, gross_margin
        #   - Management Efficiency & Leverage: total_debt, shareholders_equity, free_cash_flow
        #   - Valuation: net_income, free_cash_flow (for P/E, P/FCF), ebit, ebitda
        financial_line_items = search_line_items(
            ticker,
            [
                "revenue",
                "net_income",
                "earnings_per_share",
                "free_cash_flow",
                "research_and_development",
                "operating_income",
                "operating_margin",
                "gross_margin",
                "total_debt",
                "shareholders_equity",
                "cash_and_equivalents",
                "ebit",
                "ebitda",
            ],
            end_date,
            period="annual",
            limit=5,
        )

        progress.update_status("phil_fisher_agent", ticker, "Getting market cap")
        market_cap = get_market_cap(ticker, end_date)

        progress.update_status("phil_fisher_agent", ticker, "Fetching insider trades")
        insider_trades = get_insider_trades(ticker, end_date, start_date=None, limit=50)

        progress.update_status("phil_fisher_agent", ticker, "Fetching company news")
        company_news = get_company_news(ticker, end_date, start_date=None, limit=50)

        progress.update_status("phil_fisher_agent", ticker, "Analyzing growth & quality")
        growth_quality = analyze_fisher_growth_quality(financial_line_items)

        progress.update_status("phil_fisher_agent", ticker, "Analyzing margins & stability")
        margins_stability = analyze_margins_stability(financial_line_items)

        progress.update_status("phil_fisher_agent", ticker, "Analyzing management efficiency & leverage")
        mgmt_efficiency = analyze_management_efficiency_leverage(financial_line_items)

        progress.update_status("phil_fisher_agent", ticker, "Analyzing valuation (Fisher style)")
        fisher_valuation = analyze_fisher_valuation(financial_line_items, market_cap)

        progress.update_status("phil_fisher_agent", ticker, "Analyzing insider activity")
        insider_activity = analyze_insider_activity(insider_trades)

        progress.update_status("phil_fisher_agent", ticker, "Analyzing sentiment")
        sentiment_analysis = analyze_sentiment(company_news)

        # Combine partial scores with weights typical for Fisher:
        #   30% Growth & Quality
        #   25% Margins & Stability
        #   20% Management Efficiency
        #   15% Valuation
        #   5% Insider Activity
        #   5% Sentiment
        total_score = (
            growth_quality["score"] * 0.30
            + margins_stability["score"] * 0.25
            + mgmt_efficiency["score"] * 0.20
            + fisher_valuation["score"] * 0.15
            + insider_activity["score"] * 0.05
            + sentiment_analysis["score"] * 0.05
        )

        max_possible_score = 10

        # Simple bullish/neutral/bearish signal
        if total_score >= 7.5:
            signal = "bullish"
        elif total_score <= 4.5:
            signal = "bearish"
        else:
            signal = "neutral"

        analysis_data[ticker] = {
            "signal": signal,
            "score": total_score,
            "max_score": max_possible_score,
            "growth_quality": growth_quality,
            "margins_stability": margins_stability,
            "management_efficiency": mgmt_efficiency,
            "valuation_analysis": fisher_valuation,
            "insider_activity": insider_activity,
            "sentiment_analysis": sentiment_analysis,
        }

        progress.update_status("phil_fisher_agent", ticker, "Generating Phil Fisher-style analysis")
        fisher_output = generate_fisher_output(
            ticker=ticker,
            analysis_data=analysis_data,
            state=state,
        )

        fisher_analysis[ticker] = {
            "signal": fisher_output.signal,
            "confidence": fisher_output.confidence,
            "reasoning": fisher_output.reasoning,
        }

        progress.update_status("phil_fisher_agent", ticker, "Done", analysis=fisher_output.reasoning)

    # Wrap results in a single message
    message = HumanMessage(content=json.dumps(fisher_analysis), name="phil_fisher_agent")

    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(fisher_analysis, "Phil Fisher Agent")

    state["data"]["analyst_signals"]["phil_fisher_agent"] = fisher_analysis

    progress.update_status("phil_fisher_agent", None, "Done")
    
    return {"messages": [message], "data": state["data"]}


def analyze_fisher_growth_quality(financial_line_items: list) -> dict:
    """
    Phil Fisher emphasizes growth and quality. We'll check:
      - Revenue growth
      - EPS growth
      - R&D spending as proxy for innovation
    """
    if not financial_line_items or len(financial_line_items) < 2:
        return {
            "score": 0,
            "details": "Insufficient data for growth/quality analysis",
        }

    details = []
    raw_score = 0  # up to 6 => scale to 0-10

    # 1. Revenue growth consistency
    revenues = [item.revenue for item in financial_line_items if item.revenue is not None]

    if len(revenues) >= 3:
        growth_rates = []
        for i in range(len(revenues) - 1):
            if revenues[i + 1] != 0:
                growth_rate = (revenues[i] - revenues[i + 1]) / revenues[i + 1]
                growth_rates.append(growth_rate)

        if growth_rates:
            avg_growth = sum(growth_rates) / len(growth_rates)
            growth_consistency = len([r for r in growth_rates if r > 0])

            if avg_growth > 0.15 and growth_consistency >= 2:
                raw_score += 5
                details.append(f"Excellent revenue growth: {avg_growth:.1%} avg with consistency")
            elif avg_growth > 0.10:
                raw_score += 3
                details.append(f"Strong revenue growth: {avg_growth:.1%} avg")
            elif avg_growth > 0.05:
                raw_score += 2
                details.append(f"Good revenue growth: {avg_growth:.1%} avg")
            elif avg_growth > 0:
                raw_score += 1
                details.append(f"Modest revenue growth: {avg_growth:.1%} avg")
            else:
                details.append(f"Declining revenue: {avg_growth:.1%}")

    # 2. Earnings per share growth
    eps_values = [item.earnings_per_share for item in financial_line_items if item.earnings_per_share is not None]

    if len(eps_values) >= 3:
        eps_growth_rates = []
        for i in range(len(eps_values) - 1):
            if eps_values[i + 1] != 0:
                eps_growth = (eps_values[i] - eps_values[i + 1]) / eps_values[i + 1]
                eps_growth_rates.append(eps_growth)

        if eps_growth_rates:
            avg_eps_growth = sum(eps_growth_rates) / len(eps_growth_rates)
            if avg_eps_growth > 0.15:
                raw_score += 5
                details.append(f"Outstanding EPS growth: {avg_eps_growth:.1%} avg")
            elif avg_eps_growth > 0.10:
                raw_score += 3
                details.append(f"Strong EPS growth: {avg_eps_growth:.1%} avg")
            elif avg_eps_growth > 0:
                raw_score += 1
                details.append(f"Positive EPS growth: {avg_eps_growth:.1%} avg")

    # 3. R&D investment analysis
    rnd_values = [item.research_and_development for item in financial_line_items if item.research_and_development is not None]
    if rnd_values and revenues:
        rnd_intensity = rnd_values[0] / revenues[0] if revenues[0] != 0 else 0
        
        if rnd_intensity > 0.15:  # >15% R&D intensity
            raw_score += 4
            details.append(f"Heavy R&D investment: {rnd_intensity:.1%} of revenue")
        elif rnd_intensity > 0.08:  # >8% R&D intensity
            raw_score += 2
            details.append(f"Substantial R&D investment: {rnd_intensity:.1%} of revenue")
        elif rnd_intensity > 0.03:  # >3% R&D intensity
            raw_score += 1
            details.append(f"Moderate R&D investment: {rnd_intensity:.1%} of revenue")
        else:
            details.append(f"Low R&D investment: {rnd_intensity:.1%} of revenue")

    # scale raw_score (max 6) to 0-10
    final_score = min(10, (raw_score / 6) * 10)
    return {"score": final_score, "details": "; ".join(details)}


def analyze_margins_stability(financial_line_items: list) -> dict:
    """
    Evaluate margins & stability:
      - Check operating margin trend
      - Look at gross margin level
      - Multi-year margin stability
    """
    if not financial_line_items:
        return {
            "score": 0,
            "details": "No financial data for margin analysis",
        }

    details = []
    raw_score = 0

    # 1. Operating Margin
    # Use safe attribute access for dynamic fields
    op_margins = [item.operating_margin for item in financial_line_items if item.operating_margin is not None]

    if op_margins:
        recent_om = op_margins[0]
        if recent_om > 0.2:
            raw_score += 2
            details.append(f"Strong operating margin: {recent_om:.1%}")
        elif recent_om > 0.1:
            raw_score += 1
            details.append(f"Moderate operating margin: {recent_om:.1%}")
        else:
            details.append(f"Operating margin may be negative or uncertain")
    else:
        details.append("Not enough operating margin data points")

    # 2. Gross Margin Level
    # Use safe attribute access for dynamic fields
    gm_values = [item.gross_margin for item in financial_line_items if item.gross_margin is not None]

    if gm_values:
        # We'll just take the most recent
        recent_gm = gm_values[0]
        if recent_gm > 0.5:
            raw_score += 2
            details.append(f"Strong gross margin: {recent_gm:.1%}")
        elif recent_gm > 0.3:
            raw_score += 1
            details.append(f"Moderate gross margin: {recent_gm:.1%}")
        else:
            details.append(f"Low gross margin: {recent_gm:.1%}")
    else:
        details.append("No gross margin data available")

    # 3. Multi-year Margin Stability
    #   e.g. if we have at least 3 data points, see if standard deviation is low.
    if len(op_margins) >= 3:
        stdev = statistics.pstdev(op_margins)
        if stdev < 0.02:
            raw_score += 2
            details.append("Operating margin extremely stable over multiple years")
        elif stdev < 0.05:
            raw_score += 1
            details.append("Operating margin reasonably stable")
        else:
            details.append("Operating margin volatility is high")
    else:
        details.append("Not enough margin data points for volatility check")

    # scale raw_score (max 6) to 0-10
    final_score = min(10, (raw_score / 6) * 10)
    return {"score": final_score, "details": "; ".join(details)}


def analyze_management_efficiency_leverage(financial_line_items: list) -> dict:
    """
    Evaluate management efficiency & leverage:
      - Return on Equity (ROE)
      - Debt-to-Equity ratio
      - Possibly check if free cash flow is consistently positive
    """
    if not financial_line_items:
        return {
            "score": 0,
            "details": "No financial data for management efficiency analysis",
        }

    details = []
    raw_score = 0  # up to 6 => scale to 0–10

    # 1. Return on Equity (ROE)
    ni_values = [item.net_income for item in financial_line_items if item.net_income is not None]
    eq_values = [item.shareholders_equity for item in financial_line_items if item.shareholders_equity is not None]

    if ni_values and eq_values and len(ni_values) == len(eq_values):
        recent_ni = ni_values[0]
        recent_eq = eq_values[0] if eq_values[0] else 1e-9
        if recent_ni > 0:
            roe = recent_ni / recent_eq
            if roe > 0.2:
                raw_score += 3
                details.append(f"High ROE: {roe:.1%}")
            elif roe > 0.1:
                raw_score += 2
                details.append(f"Moderate ROE: {roe:.1%}")
            elif roe > 0:
                raw_score += 1
                details.append(f"Positive but low ROE: {roe:.1%}")
            else:
                details.append(f"ROE is near zero or negative: {roe:.1%}")
        else:
            details.append("Recent net income is zero or negative, hurting ROE")
    else:
        details.append("Insufficient data for ROE calculation")

    # 2. Debt-to-Equity
    debt_values = [item.total_debt for item in financial_line_items if item.total_debt is not None]

    if debt_values and eq_values and len(debt_values) == len(eq_values):
        recent_debt = debt_values[0]
        recent_equity = eq_values[0] if eq_values[0] else 1e-9
        dte = recent_debt / recent_equity
        if dte < 0.3:
            raw_score += 2
            details.append(f"Low debt-to-equity: {dte:.2f}")
        elif dte < 1.0:
            raw_score += 1
            details.append(f"Manageable debt-to-equity: {dte:.2f}")
        else:
            details.append(f"High debt-to-equity: {dte:.2f}")
    else:
        details.append("Insufficient data for debt/equity analysis")

    # 3. FCF Consistency
    fcf_values = [item.free_cash_flow for item in financial_line_items if item.free_cash_flow is not None]

    if fcf_values and len(fcf_values) >= 2:
        # Check if FCF is positive in recent years
        positive_fcf_count = sum(1 for x in fcf_values if x and x > 0)
        # We'll be simplistic: if most are positive, reward
        ratio = positive_fcf_count / len(fcf_values)
        if ratio > 0.8:
            raw_score += 1
            details.append(f"Majority of periods have positive FCF ({positive_fcf_count}/{len(fcf_values)})")
        else:
            details.append(f"Free cash flow is inconsistent or often negative")
    else:
        details.append("Insufficient or no FCF data to check consistency")

    final_score = min(10, (raw_score / 6) * 10)
    return {"score": final_score, "details": "; ".join(details)}


def analyze_fisher_valuation(financial_line_items: list, market_cap: float | None) -> dict:
    """
    Phil Fisher is willing to pay for quality and growth, but still checks:
      - P/E
      - P/FCF
      - (Optionally) Enterprise Value metrics, but simpler approach is typical
    We will grant up to 2 points for each of two metrics => max 4 raw => scale to 0–10.
    """
    if not financial_line_items or market_cap is None:
        return {"score": 0, "details": "Insufficient data to perform valuation"}

    details = []
    raw_score = 0

    # Gather needed data
    net_incomes = [item.net_income for item in financial_line_items if item.net_income is not None]
    fcf_values = [item.free_cash_flow for item in financial_line_items if item.free_cash_flow is not None]

    # 1) P/E
    recent_net_income = net_incomes[0] if net_incomes else None
    if recent_net_income and recent_net_income > 0:
        pe = market_cap / recent_net_income
        pe_points = 0
        if pe < 20:
            pe_points = 2
            details.append(f"Reasonably attractive P/E: {pe:.2f}")
        elif pe < 30:
            pe_points = 1
            details.append(f"Somewhat high but possibly justifiable P/E: {pe:.2f}")
        else:
            details.append(f"Very high P/E: {pe:.2f}")
        raw_score += pe_points
    else:
        details.append("No positive net income for P/E calculation")

    # 2) P/FCF
    recent_fcf = fcf_values[0] if fcf_values else None
    if recent_fcf and recent_fcf > 0:
        pfcf = market_cap / recent_fcf
        pfcf_points = 0
        if pfcf < 20:
            pfcf_points = 2
            details.append(f"Reasonable P/FCF: {pfcf:.2f}")
        elif pfcf < 30:
            pfcf_points = 1
            details.append(f"Somewhat high P/FCF: {pfcf:.2f}")
        else:
            details.append(f"Excessively high P/FCF: {pfcf:.2f}")
        raw_score += pfcf_points
    else:
        details.append("No positive free cash flow for P/FCF calculation")

    # scale raw_score (max 4) to 0–10
    final_score = min(10, (raw_score / 4) * 10)
    return {"score": final_score, "details": "; ".join(details)}


def analyze_insider_activity(insider_trades: list) -> dict:
    """
    Simple insider-trade analysis:
      - If there's heavy insider buying, we nudge the score up.
      - If there's mostly selling, we reduce it.
      - Otherwise, neutral.
    """
    # Default is neutral (5/10).
    score = 5
    details = []

    if not insider_trades:
        details.append("No insider trades data; defaulting to neutral")
        return {"score": score, "details": "; ".join(details)}

    buys, sells = 0, 0
    for trade in insider_trades:
        if trade.transaction_shares is not None:
            if trade.transaction_shares > 0:
                buys += 1
            elif trade.transaction_shares < 0:
                sells += 1

    total = buys + sells
    if total == 0:
        details.append("No buy/sell transactions found; neutral")
        return {"score": score, "details": "; ".join(details)}

    buy_ratio = buys / total
    if buy_ratio > 0.7:
        score = 8
        details.append(f"Heavy insider buying: {buys} buys vs. {sells} sells")
    elif buy_ratio > 0.4:
        score = 6
        details.append(f"Moderate insider buying: {buys} buys vs. {sells} sells")
    else:
        score = 4
        details.append(f"Mostly insider selling: {buys} buys vs. {sells} sells")

    return {"score": score, "details": "; ".join(details)}


def analyze_sentiment(news_items: list) -> dict:
    """
    Basic news sentiment: negative keyword check vs. overall volume.
    """
    if not news_items:
        return {"score": 5, "details": "No news data; defaulting to neutral sentiment"}

    negative_keywords = ["lawsuit", "fraud", "negative", "downturn", "decline", "investigation", "recall"]
    negative_count = 0
    for news in news_items:
        title_lower = (news.title or "").lower()
        if any(word in title_lower for word in negative_keywords):
            negative_count += 1

    details = []
    if negative_count > len(news_items) * 0.3:
        score = 3
        details.append(f"High proportion of negative headlines: {negative_count}/{len(news_items)}")
    elif negative_count > 0:
        score = 6
        details.append(f"Some negative headlines: {negative_count}/{len(news_items)}")
    else:
        score = 8
        details.append("Mostly positive/neutral headlines")

    return {"score": score, "details": "; ".join(details)}


def generate_fisher_output(
    ticker: str,
    analysis_data: dict[str, any],
    state: AgentState,
) -> PhilFisherSignal:
    """
    Generates a JSON signal in the style of Phil Fisher.
    """
    template = ChatPromptTemplate.from_messages(
        [
            (
              "system",
              """You are a Phil Fisher AI agent, making investment decisions using his principles:
  
              1. Emphasize long-term growth potential and quality of management.
              2. Focus on companies investing in R&D for future products/services.
              3. Look for strong profitability and consistent margins.
              4. Willing to pay more for exceptional companies but still mindful of valuation.
              5. Rely on thorough research (scuttlebutt) and thorough fundamental checks.
              
              When providing your reasoning, be thorough and specific by:
              1. Discussing the company's growth prospects in detail with specific metrics and trends
              2. Evaluating management quality and their capital allocation decisions
              3. Highlighting R&D investments and product pipeline that could drive future growth
              4. Assessing consistency of margins and profitability metrics with precise numbers
              5. Explaining competitive advantages that could sustain growth over 3-5+ years
              6. Using Phil Fisher's methodical, growth-focused, and long-term oriented voice
              
              For example, if bullish: "This company exhibits the sustained growth characteristics we seek, with revenue increasing at 18% annually over five years. Management has demonstrated exceptional foresight by allocating 15% of revenue to R&D, which has produced three promising new product lines. The consistent operating margins of 22-24% indicate pricing power and operational efficiency that should continue to..."
              
              For example, if bearish: "Despite operating in a growing industry, management has failed to translate R&D investments (only 5% of revenue) into meaningful new products. Margins have fluctuated between 10-15%, showing inconsistent operational execution. The company faces increasing competition from three larger competitors with superior distribution networks. Given these concerns about long-term growth sustainability..."
              
              You must output a JSON object with:
                - "signal": "bullish" or "bearish" or "neutral"
                - "confidence": a float between 0 and 100
                - "reasoning": a detailed explanation
              """,
            ),
            (
              "human",
              """Based on the following analysis, create a Phil Fisher-style investment signal.

              Analysis Data for {ticker}:
              {analysis_data}

              Return the trading signal in this JSON format:
              {{
                "signal": "bullish/bearish/neutral",
                "confidence": float (0-100),
                "reasoning": "string"
              }}
              """,
            ),
        ]
    )

    prompt = template.invoke({"analysis_data": json.dumps(analysis_data, indent=2), "ticker": ticker})

    def create_default_signal():
        return PhilFisherSignal(
            signal="neutral",
            confidence=0.0,
            reasoning="Error in analysis, defaulting to neutral"
        )

    return call_llm(
        prompt=prompt,
        pydantic_model=PhilFisherSignal,
        state=state,
        agent_name="phil_fisher_agent",
        default_factory=create_default_signal,
    )
