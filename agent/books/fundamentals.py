"""Point-in-time factor inputs from XBRL facts.

Turns panel.xbrl_facts into, for every (ticker, filing date), the trailing
twelve-month flows and the latest balance-sheet instants that were public
as of that filing. A factor on day D then takes each name's most recent row
with filed <= D — never the row for the period that ends before D, which
is the classic look-ahead (a Q4 filed in late February is not known on
January 31).

Quarterly flows: 10-Q facts with a ~3-month duration are used directly;
the fourth quarter is derived as the 10-K annual value minus the three
10-Qs of the same fiscal year, because 10-Ks report the year, not Q4.
Companies filing 20-F/40-F (foreign) only have annual values and are
covered at annual frequency.

Tag fallbacks: revenue and cost tags changed with ASC 606 (2018); equity
with/without minority interest; shares from dei when us-gaap is missing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from hedge_fund.features.panel import PanelStore

FLOW_TAGS = {
    "ni": ["NetIncomeLoss"],
    "rev": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "gp": ["GrossProfit"],
    "cogs": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "opinc": ["OperatingIncomeLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
}
INSTANT_TAGS = {
    "assets": ["Assets"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "shares": ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"],
}


def _first_available(df: pd.DataFrame, tags: list[str]) -> pd.DataFrame:
    """Rows for the first tag (in preference order) that the company reports."""
    for t in tags:
        sub = df[df["tag"] == t]
        if not sub.empty:
            return sub
    return df.iloc[0:0]


def quarterly_flows(facts: pd.DataFrame, name: str, tags: list[str]) -> pd.DataFrame:
    """(cik, period_end, filed) -> quarterly value, with Q4 derived from the annual."""
    f = _first_available(facts, tags)
    if f.empty:
        return pd.DataFrame(columns=["cik", "period_end", "filed", name])
    f = f.assign(days=(pd.to_datetime(f["period_end"]) - pd.to_datetime(f["period_start"])).dt.days)
    q = f[(f["days"].between(75, 105))]
    a = f[(f["days"].between(350, 380))]
    # the value a quarter is known by: the earliest filing that reported it
    q = q.sort_values("filed").drop_duplicates(["cik", "period_end"], keep="first")
    a = a.sort_values("filed").drop_duplicates(["cik", "period_end"], keep="first")
    rows = [q[["cik", "period_end", "filed", "val"]].rename(columns={"val": name})]
    # Q4 = annual - the three quarters inside that fiscal year (all must already be known)
    for r in a.itertuples():
        start = pd.Timestamp(r.period_start)
        inside = q[(q["cik"] == r.cik) & (pd.to_datetime(q["period_end"]) > start)
                   & (pd.to_datetime(q["period_end"]) < pd.Timestamp(r.period_end))]
        if len(inside) == 3:
            rows.append(pd.DataFrame([{"cik": r.cik, "period_end": r.period_end,
                                       "filed": max(r.filed, inside["filed"].max()),
                                       name: r.val - inside["val"].sum()}]))
        elif len(inside) == 0 and r.form in ("20-F", "40-F"):
            rows.append(pd.DataFrame([{"cik": r.cik, "period_end": r.period_end, "filed": r.filed,
                                       name: r.val / 4.0}]))   # annual filers: spread evenly
    out = pd.concat(rows, ignore_index=True)
    return out.sort_values(["cik", "period_end", "filed"]).drop_duplicates(["cik", "period_end"], keep="first")


def latest_instants(facts: pd.DataFrame, name: str, tags: list[str]) -> pd.DataFrame:
    f = _first_available(facts[facts["is_instant"]], tags)
    if f.empty:
        return pd.DataFrame(columns=["cik", "period_end", "filed", name])
    f = f.sort_values("filed").drop_duplicates(["cik", "period_end"], keep="first")
    return f[["cik", "period_end", "filed", "val"]].rename(columns={"val": name})


def build(store: PanelStore) -> pd.DataFrame:
    """One row per (cik, filed): TTM flows + latest instants known at that filing."""
    facts = store.con.execute("SELECT cik, tag, period_start, period_end, is_instant, val, form, filed "
                              "FROM xbrl_facts WHERE unit IN ('USD','shares')").df()
    tick = store.con.execute("""SELECT CAST(cik AS INT) AS cik, ticker FROM issuer_seen
                                WHERE ticker IN (SELECT DISTINCT ticker FROM bars)
                                QUALIFY row_number() OVER (PARTITION BY cik ORDER BY quarter DESC) = 1""").df()
    parts = []
    for cik, g in facts.groupby("cik"):
        flows = None
        for name, tags in FLOW_TAGS.items():
            qf = quarterly_flows(g, name, tags)
            if qf.empty:
                continue
            qf = qf.sort_values("period_end")
            qf[f"{name}_ttm"] = qf[name].rolling(4).sum()
            # a TTM value is known when the last of its four quarters was filed
            filed = pd.to_datetime(qf["filed"])
            qf["filed_ttm"] = pd.concat([filed.shift(k) for k in range(4)], axis=1).max(axis=1)
            keep = qf[["cik", "period_end", "filed_ttm", f"{name}_ttm"]].dropna().rename(columns={"filed_ttm": "filed"})
            flows = keep if flows is None else flows.merge(keep, on=["cik", "period_end", "filed"], how="outer")
        inst = None
        for name, tags in INSTANT_TAGS.items():
            li = latest_instants(g, name, tags)
            if li.empty:
                continue
            li["filed"] = pd.to_datetime(li["filed"])
            inst = li if inst is None else inst.merge(li, on=["cik", "period_end", "filed"], how="outer")
        if flows is None and inst is None:
            continue
        df = flows if inst is None else (inst if flows is None else flows.merge(inst, on=["cik", "period_end", "filed"], how="outer"))
        df["filed"] = pd.to_datetime(df["filed"])
        df = df.sort_values("filed")
        # carry the latest known value of each item forward across filings
        for c in df.columns:
            if c not in ("cik", "period_end", "filed"):
                df[c] = df[c].ffill()
        parts.append(df.drop_duplicates("filed", keep="last"))
    out = pd.concat(parts, ignore_index=True).merge(tick, on="cik", how="inner")
    return out


def factor_inputs(store: PanelStore) -> pd.DataFrame:
    """Cached build; stores panel.fundamentals_pit."""
    df = build(store)
    df["assets_1y"] = np.nan
    df = df.sort_values(["ticker", "filed"])
    # asset growth: assets vs the value known ~one year earlier
    out = []
    for t, g in df.groupby("ticker"):
        g = g.copy()
        past = g.set_index("filed")["assets"]
        g["assets_1y"] = [past[:f - pd.Timedelta(days=330)].iloc[-1] if len(past[:f - pd.Timedelta(days=330)]) else np.nan
                          for f in g["filed"]]
        out.append(g)
    df = pd.concat(out, ignore_index=True)
    store.con.execute("DROP TABLE IF EXISTS fundamentals_pit")
    store.con.register("_f", df)
    store.con.execute("CREATE TABLE fundamentals_pit AS SELECT * FROM _f")
    store.con.unregister("_f")
    return df
