"""S53 step 1-2: fundamentals of foreign issuers for the shadow line -> fpi.db fundamentals_pit (never panel.db).

docs/AGENT_PLAN.md "### S53 补充". The long book's builder (agent.books.fundamentals.build) is reused as it is,
fed with fpi.db's facts:
  - ifrs-full tags are renamed to the us-gaap names the builder knows (IFRS_MAP); a tag reported in both
    namespaces for the same period keeps the us-gaap fact.
  - money stays in the filing's reporting currency: per filing, the non-USD unit with the most facts, else
    USD; a convenience translation (the same facts again in USD) is dropped. Each row carries `currency`;
    the shadow line converts to USD on the scoring day (agent.s53_fpi), so quality ratios never see FX.
  - share counts are in listed units (S53a): 20-F/40-F covers via ads_ratio; a 6-K or 10-Q of a depositary-
    share company takes the ratio of its latest 20-F/40-F filed on or before it (rows 'derived' in
    fpi.ads_ratio, rebuilt here).
  - annual_only: no quarterly flow (a 75-125 day period) filed in the 730 days up to the row's filing; the
    shadow line lets such rows stand for 480 days instead of 200.
  - ticker: today's, from fpi_issuers, for the whole history (Alpaca keeps a renamed ticker's bars under the
    new symbol); survivors only, pre-registered.

Usage: python -m agent.books.fundamentals_fpi
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.books import fundamentals as F
from agent.sources import fpi_data

IFRS_MAP = {
    "Revenue": "Revenues", "GrossProfit": "GrossProfit", "CostOfSales": "CostOfRevenue",
    "ProfitLossFromOperatingActivities": "OperatingIncomeLoss",
    "CashFlowsFromUsedInOperatingActivities": "NetCashProvidedByUsedInOperatingActivities",
    "ProfitLossAttributableToOwnersOfParent": "NetIncomeLoss", "ProfitLoss": "ProfitLoss", "Assets": "Assets",
    "EquityAttributableToOwnersOfParent": "StockholdersEquity",
    "Equity": "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "NoncontrollingInterests": "MinorityInterest",
    "WeightedAverageShares": "WeightedAverageNumberOfSharesOutstandingBasic",
    "AdjustedWeightedAverageShares": "WeightedAverageNumberOfDilutedSharesOutstanding",
}
NS_ORDER = {"us-gaap": 0, "dei": 0, "ifrs-full": 1}
QUARTER_DAYS = (75, 125)
ANNUAL_ONLY_LOOKBACK = 730
MONEY = ["rev_ttm", "gp_ttm", "cogs_ttm", "opinc_ttm", "cfo_ttm", "ni_ttm", "assets", "equity", "debt", "assets_1y"]


def load_facts(store) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(facts in the builder's layout, currency per (cik, accn))."""
    f = store.con.execute("""SELECT cik, ns, tag, unit, period_start, period_end, is_instant, val, form, filed, accn
                             FROM xbrl_facts WHERE period_start >= DATE '1990-01-01'
                               AND period_end BETWEEN DATE '1990-01-01' AND filed""").df()
    f["tag"] = np.where(f["ns"] == "ifrs-full", f["tag"].map(IFRS_MAP), f["tag"])
    f = f[f["tag"].notna()]
    money = f["unit"] != "shares"
    # reporting currency per filing: the non-USD unit with the most facts, else USD
    cnt = f[money].groupby(["cik", "accn", "unit"]).size().rename("n").reset_index()
    cnt["usd"] = cnt["unit"].eq("USD")
    cur = (cnt.sort_values(["cik", "accn", "usd", "n"], ascending=[True, True, True, False])
           .drop_duplicates(["cik", "accn"])[["cik", "accn", "unit"]].rename(columns={"unit": "currency"}))
    f = f.merge(cur, on=["cik", "accn"], how="left")
    f = f[~money.to_numpy() | (f["unit"] == f["currency"]).to_numpy()]
    # same tag and period in both namespaces: keep us-gaap
    f = (f.assign(_o=f["ns"].map(NS_ORDER)).sort_values(["cik", "tag", "period_start", "period_end", "filed", "_o"], kind="stable")
         .drop_duplicates(["cik", "tag", "period_start", "period_end", "filed", "accn"], keep="first").drop(columns="_o"))
    for c in ("period_start", "period_end", "filed"):
        f[c] = pd.to_datetime(f[c]).astype("datetime64[ns]")
    f["accn"] = np.where(f["tag"].isin(F.SHARE_TAGS), f["accn"], None)
    cols = ["cik", "tag", "period_start", "period_end", "is_instant", "val", "form", "filed", "accn"]
    f = f.sort_values(["cik", "tag", "period_start", "period_end", "filed"], kind="stable")
    return f[cols].reset_index(drop=True), cur


def derive_ratios(store) -> int:
    """fpi.ads_ratio += a row per 6-K / 10-Q / 10-K filing of a depositary-share company (status 'derived'):
    the ratio of its latest 20-F/40-F filed on or before it. Earlier derived rows are replaced."""
    r = store.con.execute("SELECT * FROM ads_ratio WHERE status <> 'derived'").df()
    ads = set(r.loc[r["status"].isin(F.ADS_STATUSES), "cik"].astype(int))
    if not ads:
        return 0
    other = store.con.execute(f"""SELECT cik, accn, any_value(form) AS form, min(filed) AS filed FROM xbrl_facts
        WHERE form NOT IN ('20-F','40-F','20-F/A','40-F/A') AND cik IN ({', '.join(str(c) for c in ads)})
        GROUP BY cik, accn""").df()
    # the latest 20-F/40-F on or before, known ratio or not: a 6-K after an ADS cover without a ratio has no
    # ratio either (SMFG: its 6-K counts were ordinary shares x ADS price, 5x too small a market cap)
    annual = r[r["form"].isin(["20-F", "40-F", "20-F/A", "40-F/A"]) & r["cik"].isin(ads)][["cik", "filed", "ratio"]]
    known = annual.assign(filed=lambda d: pd.to_datetime(d["filed"])).sort_values("filed")
    other = other.assign(filed=pd.to_datetime(other["filed"])).astype({"cik": "int64"}).sort_values("filed")
    hit = pd.merge_asof(other, known.astype({"cik": "int64"}), on="filed", by="cik", direction="backward")
    hit = hit[hit["filed"] >= hit.groupby("cik")["filed"].transform("min")]           # every 6-K/10-Q of an ADS company
    hit = hit[~hit.set_index(["cik", "accn"]).index.isin(r.set_index(["cik", "accn"]).index)]
    rows = hit.assign(status="derived", cover_text="", doc="", source="latest 20-F/40-F ratio",
                      fetched_at=pd.Timestamp.now(), filed=hit["filed"].dt.date)
    cols = ["cik", "accn", "form", "filed", "ratio", "status", "cover_text", "doc", "source", "fetched_at"]
    store.con.execute("DELETE FROM ads_ratio WHERE status = 'derived'")
    store.con.register("_d", rows[cols])
    store.con.execute(f"INSERT INTO ads_ratio SELECT {', '.join(cols)} FROM _d")
    store.con.unregister("_d")
    return len(rows)


def annual_only(facts: pd.DataFrame, rows: pd.DataFrame) -> pd.Series:
    """Per row (cik, filed): no quarterly flow fact filed in the ANNUAL_ONLY_LOOKBACK days up to it."""
    flow = facts[~facts["is_instant"]]
    days = (flow["period_end"] - flow["period_start"]).dt.days
    q = flow[days.between(*QUARTER_DAYS)][["cik", "filed"]].drop_duplicates().sort_values("filed")
    q = q.rename(columns={"filed": "q_filed"}).astype({"cik": "int64"})
    left = rows[["cik", "filed"]].astype({"cik": "int64"}).reset_index().sort_values("filed")
    hit = pd.merge_asof(left, q.assign(filed=q["q_filed"]), on="filed", by="cik", direction="backward")
    recent = (hit["filed"] - hit["q_filed"]).dt.days <= ANNUAL_ONLY_LOOKBACK
    return pd.Series(~recent.to_numpy(), index=hit["index"]).reindex(rows.index)


def tick_map(store) -> pd.DataFrame:
    iss = store.con.execute("SELECT cik, ticker FROM fpi_issuers").df()
    quarters = [f"{y}q{q}" for y in range(2010, pd.Timestamp.now().year + 1) for q in range(1, 5)]
    return pd.DataFrame([(int(c), qu, t, 1) for c, t in iss.itertuples(index=False) for qu in quarters],
                        columns=["cik", "quarter", "ticker", "n_filings"])


def build_fpi(store) -> pd.DataFrame:
    n = derive_ratios(store)
    facts, cur = load_facts(store)
    out = F.build(store, facts=facts, tick_pit=tick_map(store))
    # the row's currency: its own filing's (several filings on one day: the most common)
    fc = facts[["cik", "filed"]].drop_duplicates()
    acc = store.con.execute("SELECT cik, accn, min(filed) AS filed FROM xbrl_facts GROUP BY cik, accn").df()
    acc["filed"] = pd.to_datetime(acc["filed"]).astype("datetime64[ns]")
    rc = (acc.merge(cur, on=["cik", "accn"]).groupby(["cik", "filed"])["currency"]
          .agg(lambda s: s.value_counts().index[0]).reset_index().astype({"cik": "int64"}))
    out = out.merge(rc, on=["cik", "filed"], how="left")
    # a filing with no money facts (only a share count: AMX 2026-04-28) carries the previous row's money, so
    # also its currency; was: USD, which read Mexican pesos as dollars
    out = out.sort_values(["cik", "filed"], kind="stable")
    out["currency"] = out.groupby("cik")["currency"].ffill().fillna("USD")
    # a count 100x below the company's other rows is a unit error (RY 2019 in thousands, UGP 2018)
    med = out.groupby("ticker")["shares"].transform("median")
    unit = out["shares"] < med / 100
    out.loc[unit, "shares"] = np.nan
    out.loc[unit, "shares_flag"] = "unit"
    out["annual_only"] = annual_only(facts.assign(cik=facts["cik"].astype("int64")), out)
    del fc
    print(f"derived ratios {n}; rows {len(out)}, names {out['ticker'].nunique()}, currencies {out['currency'].value_counts().head(8).to_dict()}")
    return out


def write(store, df: pd.DataFrame) -> pd.DataFrame:
    """fpi.fundamentals_pit: the build plus assets_1y (as factor_inputs, without its share review list, which
    belongs to panel.db)."""
    df = df.drop(columns=[c for c in F.REVIEW_ONLY if c in df.columns]).sort_values(["ticker", "filed"])
    out = []
    for _, g in df.groupby("ticker"):
        g = g.copy()
        past = g.set_index("filed")["assets"]
        g["assets_1y"] = [past[:f - pd.Timedelta(days=330)].iloc[-1] if len(past[:f - pd.Timedelta(days=330)]) else np.nan
                          for f in g["filed"]]
        out.append(g)
    df = pd.concat(out, ignore_index=True)
    store.con.register("_f", df)
    try:
        store.con.execute("CREATE OR REPLACE TABLE fundamentals_pit AS SELECT * FROM _f")
    finally:
        store.con.unregister("_f")
    return df


def main() -> None:
    with fpi_data.store() as s:
        df = write(s, build_fpi(s))
    print(f"fpi fundamentals_pit: {len(df)} rows, {df['ticker'].nunique()} names, "
          f"with shares {int(df['shares'].notna().sum())}, annual_only rows {int(df['annual_only'].sum())}")


if __name__ == "__main__":
    main()
