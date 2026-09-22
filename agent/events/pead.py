"""Event line: post-earnings-announcement drift in small/mid caps, from XBRL alone.

S3 found nothing in S&P 500 names (Martineau 2021: PEAD is gone in large
caps). The literature's remaining drift is in small caps, where arbitrage
is costly — exactly the ADV band the insider line already trades.

Surprise without analysts (Bernard & Thomas 1989, seasonal random walk):
  SUE = (NI_q - NI_{q-4}) / std(NI_q - NI_{q-4} over the prior 8 quarters), >= 6 obs
Event date = the day the quarter's number became public in EDGAR — the
10-Q/10-K filing date from XBRL companyfacts. That is days to weeks AFTER
the press release, so the announcement-day jump is never ours; what is
tested is the drift that follows, entered at the next open.

Rules (v1, pre-registered 2026-09-22):
  trigger   SUE >= 2 (variant: >= 1), filing within 60 days of the quarter end
  universe  ADV $3M–$500M
  hold      20 sessions goes live if it passes; 10 and 40 are report variants
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books.fundamentals import quarterly_flows
from agent.events.base import EVENT_COLS, EventLine, LineSpec
from hedge_fund.features.panel import PanelStore

NI_TAGS = ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]
SUE_MIN = 2.0
MAX_FILING_LAG_DAYS = 60


def sue_table(store: PanelStore) -> pd.DataFrame:
    facts = store.con.execute("""SELECT cik, tag, period_start, period_end, is_instant, val, form, filed
                                 FROM xbrl_facts WHERE unit = 'USD' AND tag IN (?, ?, ?)""", NI_TAGS).df()
    parts = []
    for cik, g in facts.groupby("cik"):
        q = quarterly_flows(g, "ni", NI_TAGS)
        if len(q) < 10:
            continue
        q = q.sort_values("period_end").copy()
        q["period_end"] = pd.to_datetime(q["period_end"])
        q["filed"] = pd.to_datetime(q["filed"])
        # seasonal change, then standardise by its own trailing dispersion (prior 8, excluding this one)
        q["d4"] = q["ni"] - q["ni"].shift(4)
        q["sd"] = q["d4"].shift(1).rolling(8, min_periods=6).std()
        q["sue"] = q["d4"] / q["sd"]
        q["lag_days"] = (q["filed"] - q["period_end"]).dt.days
        parts.append(q[["cik", "period_end", "filed", "ni", "d4", "sue", "lag_days"]])
    out = pd.concat(parts, ignore_index=True)
    out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=["sue"])
    return out


def attach_ticker(store: PanelStore, df: pd.DataFrame) -> pd.DataFrame:
    seen = store.con.execute("SELECT CAST(cik AS INT) AS cik, quarter, ticker FROM issuer_seen").df()
    key = seen.set_index(["cik", "quarter"])["ticker"].to_dict()
    latest = seen.sort_values("quarter").drop_duplicates("cik", keep="last").set_index("cik")["ticker"].to_dict()
    qq = df["filed"].dt.year.astype(str) + "q" + df["filed"].dt.quarter.astype(str)
    df = df.copy()
    df["ticker"] = [key.get((c, q)) or latest.get(c) for c, q in zip(df["cik"], qq)]
    return df.dropna(subset=["ticker"])


class PeadSmall(EventLine):
    spec = LineSpec(name="pead_small", version="1", hold_days=20, max_slots=20, adv_floor=3e6, adv_ceiling=5e8,
                    hypothesis="large positive earnings surprises in small/mid caps drift up for weeks after the filing")

    def __init__(self, sue_min: float = SUE_MIN):
        self.sue_min = sue_min

    def events(self, store: PanelStore, start: str, end: str) -> pd.DataFrame:
        t = attach_ticker(store, sue_table(store))
        t = t[(t["filed"] >= pd.Timestamp(start)) & (t["filed"] <= pd.Timestamp(end))
              & (t["lag_days"] <= MAX_FILING_LAG_DAYS) & (t["lag_days"] >= 0)]
        t = t[t["sue"] >= self.sue_min]
        out = pd.DataFrame({"date": t["filed"], "ticker": t["ticker"], "side": "L", "strength": t["sue"].astype(float),
                            "detail": t["sue"].map(lambda s: f"sue{s:+.1f}")})
        return out.sort_values("strength", ascending=False).drop_duplicates(["date", "ticker"])[EVENT_COLS].reset_index(drop=True)
