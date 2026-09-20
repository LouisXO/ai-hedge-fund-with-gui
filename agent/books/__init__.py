"""Two stock books on one engine — short-term (event-driven) and long-term (monthly).

Everything the last two days established is baked in rather than optional:
point-in-time universe (listing_status + issuer_seen), prices that include
delisted names (Alpaca), real quoted spreads by liquidity bucket and year
(spread_grid), an explicit execution assumption (fraction of the spread
paid), rules written down before the backtest, and SPY as the yardstick
with alpha measured by regression, not by eyeballing total return.
"""
