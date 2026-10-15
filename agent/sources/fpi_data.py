"""S53 steps 1-3: data for the foreign-issuer shadow line, all in ~/.hedge-fund/agent/fpi.db (never panel.db).

docs/AGENT_PLAN.md "### S53 补充": the long book v1 reads panel.db; bars of new tickers there would move its
momentum and low-vol z-scores, so everything for the shadow line lives in its own file (a PanelStore at
another path, same table layout):
  issuers  fpi_issuers: NYSE/Nasdaq listings in SEC company_tickers_exchange.json whose filings include a
           20-F or 40-F, plus the 20-F/40-F filers already in panel.db. One ticker per CIK (today's).
           Survivors only: a foreign issuer delisted before today is not here (pre-registered limitation).
  bars     Alpaca daily bars, 2016 on, for the issuers' tickers that panel.db has no bars for -> fpi.bars
  facts    SEC companyfacts of every issuer -> fpi.xbrl_facts: us-gaap, ifrs-full and dei, every unit (EUR,
           TWD, DKK ...), forms 10-K / 10-Q / 20-F / 40-F (and /A) and 6-K (quarterly XBRL of RBC, Shell...)
  fx       daily USD per unit of every currency the facts use -> fpi.fx_daily (yfinance closes; Alpha Vantage's
           25 free calls a day are taken by the estimates job, so AV is the spot check: fx --check)
  ads      ADS ratios of the issuers' 20-F/40-F covers (agent.sources.ads_ratio, cache shared) -> fpi.ads_ratio

Usage: python -m agent.sources.fpi_data issuers|bars|facts|fx|ads|status [--limit N]
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore

FPI_DB = Path.home() / ".hedge-fund" / "agent" / "fpi.db"
EXCHANGES = ("NYSE", "Nasdaq")
FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F", "20-F/A", "40-F/A", "6-K")
SINCE = "2014-01-01"
BARS_START = "2016-01-01"
TAGS = {
    "us-gaap": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic",
                "MinorityInterest", "RedeemableNoncontrollingInterestEquityCarryingAmount",
                "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "GrossProfit",
                "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices",
                "OperatingIncomeLoss", "NetCashProvidedByUsedInOperatingActivities", "Assets", "StockholdersEquity",
                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "LongTermDebtNoncurrent",
                "LongTermDebt", "CommonStockSharesOutstanding", "WeightedAverageNumberOfDilutedSharesOutstanding",
                "WeightedAverageNumberOfSharesOutstandingBasic"],
    "ifrs-full": ["Revenue", "GrossProfit", "CostOfSales", "ProfitLossFromOperatingActivities",
                  "CashFlowsFromUsedInOperatingActivities", "ProfitLossAttributableToOwnersOfParent", "ProfitLoss",
                  "Assets", "EquityAttributableToOwnersOfParent", "Equity", "NoncontrollingInterests",
                  "WeightedAverageShares", "AdjustedWeightedAverageShares"],
    "dei": ["EntityCommonStockSharesOutstanding"],
}
# yfinance symbols: XXXUSD=X is USD per unit where quoted; else USDXXX=X inverted
FX_DIRECT = {"EUR", "GBP", "AUD", "NZD"}
PAUSE = 0.4
ISSUER_DDL = """CREATE TABLE IF NOT EXISTS fpi_issuers (cik INT PRIMARY KEY, ticker VARCHAR, exchange VARCHAR, name VARCHAR,
    n20f INT, n40f INT, n6k INT, in_panel BOOLEAN, as_of DATE)"""
FX_DDL = """CREATE TABLE IF NOT EXISTS fx_daily (currency VARCHAR, trade_date DATE, usd_per_unit DOUBLE, source VARCHAR,
    PRIMARY KEY (currency, trade_date))"""


def store(read_only: bool = False) -> PanelStore:
    return PanelStore(FPI_DB, read_only=read_only)


def _get_json(url: str, ua: str) -> dict:
    from agent.sources.ads_ratio import _get_json as get
    return get(url, ua)


# ---------------------------------------------------------------- issuers
def _submission(cik: int, ua: str) -> dict:
    time.sleep(PAUSE)
    d = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ua)
    forms = pd.Series(d["filings"]["recent"]["form"])
    return {"cik": cik, "name": d.get("name"), "n20f": int(forms.isin(["20-F", "20-F/A"]).sum()),
            "n40f": int(forms.isin(["40-F", "40-F/A"]).sum()), "n6k": int((forms == "6-K").sum())}


def issuers(workers: int = 3) -> pd.DataFrame:
    """fpi_issuers: listed NYSE/Nasdaq issuers that file 20-F/40-F; today's ticker per CIK."""
    ua = user_agent()
    d = _get_json("https://www.sec.gov/files/company_tickers_exchange.json", ua)
    lst = pd.DataFrame(d["data"], columns=d["fields"])
    lst = lst[lst["exchange"].isin(EXCHANGES)]
    with PanelStore(read_only=True) as p:
        seen = set(p.con.execute("SELECT DISTINCT CAST(cik AS BIGINT) AS cik FROM issuer_seen").df()["cik"])
        panel_fpi = set(p.con.execute("SELECT DISTINCT cik FROM xbrl_facts WHERE form IN ('20-F','40-F')").df()["cik"].astype(int))
        bars = set(p.con.execute("SELECT DISTINCT ticker FROM bars").df()["ticker"])
    cand = sorted(set(lst.loc[~lst["cik"].isin(seen), "cik"].astype(int)) | (panel_fpi & set(lst["cik"].astype(int))))
    with ThreadPoolExecutor(workers) as ex:
        subs = pd.DataFrame(list(ex.map(lambda c: _submission(c, ua), cand)))
    subs = subs[(subs["n20f"] + subs["n40f"]) > 0]
    # one ticker per CIK: the one panel.db already has bars for, else the shortest (ADS / common over preferreds)
    t = lst[lst["cik"].isin(subs["cik"])].assign(_has=lambda x: x["ticker"].isin(bars), _len=lambda x: x["ticker"].str.len())
    t = t.sort_values(["cik", "_has", "_len", "ticker"], ascending=[True, False, True, True]).drop_duplicates("cik")
    out = subs.merge(t[["cik", "ticker", "exchange"]], on="cik").assign(
        in_panel=lambda x: x["ticker"].isin(bars), as_of=pd.Timestamp.now().normalize().date())
    out = out[["cik", "ticker", "exchange", "name", "n20f", "n40f", "n6k", "in_panel", "as_of"]]
    with store() as s:
        s.con.execute(ISSUER_DDL)
        s.con.register("_i", out)
        s.con.execute("BEGIN")
        s.con.execute("DELETE FROM fpi_issuers")
        s.con.execute("INSERT INTO fpi_issuers SELECT * FROM _i")
        s.con.execute("COMMIT")
        s.con.unregister("_i")
    print(f"fpi_issuers: {len(out)} ({int(out['in_panel'].sum())} with bars in panel, {int((~out['in_panel']).sum())} new)")
    return out


# ---------------------------------------------------------------- bars
def bars(limit: int | None = None) -> dict:
    from agent.sources.alpaca_bars import _end_now, backfill
    with store() as s:
        new = s.con.execute("SELECT ticker FROM fpi_issuers WHERE NOT in_panel ORDER BY ticker").df()["ticker"].tolist()
        if limit:
            new = new[:limit]
        end = _end_now()                     # the free tier refuses an end inside the last 15 minutes
        stats = backfill(s, BARS_START, end, symbols=new, replace=True)
    print({k: v for k, v in stats.items() if k != "failed_symbols"}, "failed:", stats.get("failed_symbols", [])[:20])
    return stats


# ---------------------------------------------------------------- facts
def rows_from(cik: int, doc: dict) -> list[dict]:
    """Every kept tag, every unit, forms in FORMS, filed on or after SINCE."""
    out = []
    facts = doc.get("facts") or {}
    for ns, tags in TAGS.items():
        for tag in tags:
            units = (facts.get(ns) or {}).get(tag, {}).get("units") or {}
            for unit, items in units.items():
                if "/" in unit or unit == "pure":                # per-share amounts and ratios are not used
                    continue
                for it in items:
                    if it.get("filed", "") < SINCE or it.get("form") not in FORMS:
                        continue
                    out.append({"cik": cik, "ns": ns, "tag": tag, "unit": unit,
                                "period_start": it.get("start") or it["end"], "period_end": it["end"],
                                "is_instant": "start" not in it, "val": it["val"], "accn": it["accn"], "fy": it.get("fy"),
                                "fp": it.get("fp"), "form": it["form"], "filed": it["filed"], "frame": it.get("frame")})
    return out


def facts(workers: int = 4, limit: int | None = None) -> dict:
    from agent.sources.sec_xbrl import DDL as XBRL_DDL
    from agent.sources.sec_xbrl import fetch as fetch_company
    ua = user_agent()
    with store(read_only=True) as s:
        ciks = s.con.execute("SELECT cik FROM fpi_issuers ORDER BY cik").df()["cik"].astype(int).tolist()[:limit]

    def one(cik):
        time.sleep(PAUSE)
        return cik, fetch_company(cik, ua)

    stats = {"ciks": len(ciks), "ok": 0, "missing": 0, "rows": 0}
    frames = []
    with ThreadPoolExecutor(workers) as ex:
        for cik, doc in ex.map(one, ciks):
            if doc is None:
                stats["missing"] += 1
                continue
            r = rows_from(cik, doc)
            stats["ok"] += 1
            if r:
                frames.append(pd.DataFrame(r))
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    stats["rows"] = len(df)
    if len(df):
        for c in ("period_start", "period_end", "filed"):
            df[c] = pd.to_datetime(df[c]).dt.date
        df = df.drop_duplicates(["cik", "ns", "tag", "unit", "period_start", "period_end", "accn"], keep="last")
        with store() as s:
            for stmt in XBRL_DDL:
                s.con.execute(stmt)
            # unit is part of the key here: the same fact in EUR and in USD (a convenience translation)
            s.con.execute("DROP TABLE IF EXISTS xbrl_facts")
            s.con.execute("""CREATE TABLE xbrl_facts (cik INT, ns VARCHAR, tag VARCHAR, unit VARCHAR, period_start DATE,
                period_end DATE, is_instant BOOLEAN, val DOUBLE, accn VARCHAR, fy INT, fp VARCHAR, form VARCHAR, filed DATE,
                frame VARCHAR, PRIMARY KEY (cik, ns, tag, unit, period_start, period_end, accn))""")
            s.con.register("_f", df)
            s.con.execute("INSERT INTO xbrl_facts SELECT cik, ns, tag, unit, period_start, period_end, is_instant, val, accn, "
                          "fy, fp, form, filed, frame FROM _f")
            s.con.unregister("_f")
    print(stats)
    return stats


# ---------------------------------------------------------------- fx
def currencies() -> list[str]:
    with store(read_only=True) as s:
        u = s.con.execute("SELECT DISTINCT unit FROM xbrl_facts").df()["unit"].tolist()
    return sorted(x for x in u if x not in ("USD", "shares") and len(x) == 3 and x.isalpha() and x.isupper())


def fx(start: str = "2015-01-01") -> dict:
    import yfinance as yf
    out, missing = [], []
    for cur in currencies():
        sym = f"{cur}USD=X" if cur in FX_DIRECT else f"USD{cur}=X"
        try:
            h = yf.Ticker(sym).history(start=start, auto_adjust=False)["Close"].dropna()
        except Exception:
            h = pd.Series(dtype=float)
        h = h[h > 0]
        if h.empty:
            missing.append(cur)
            continue
        usd = h if cur in FX_DIRECT else 1.0 / h
        out.append(pd.DataFrame({"currency": cur, "trade_date": pd.to_datetime(usd.index).tz_localize(None).date,
                                 "usd_per_unit": usd.to_numpy(), "source": f"yfinance:{sym}"}))
    df = pd.concat(out, ignore_index=True).drop_duplicates(["currency", "trade_date"], keep="last")
    with store() as s:
        s.con.execute(FX_DDL)
        s.con.register("_x", df)
        s.con.execute("BEGIN")
        s.con.execute("DELETE FROM fx_daily")
        s.con.execute("INSERT INTO fx_daily SELECT * FROM _x")
        s.con.execute("COMMIT")
        s.con.unregister("_x")
    stats = {"currencies": int(df["currency"].nunique()), "rows": len(df), "missing": missing}
    print(stats)
    return stats


# ---------------------------------------------------------------- ADS ratios
def ads(workers: int = 4) -> None:
    """Covers of the issuers' 20-F/40-F filings not read yet (the cache is shared with panel's), then
    fpi.ads_ratio = cache + agent/ads_ratio.yaml."""
    from agent.sources import ads_ratio as A
    with store(read_only=True) as s:
        todo = s.con.execute(f"""SELECT cik, accn, any_value(form) AS form, min(filed) AS filed FROM xbrl_facts
            WHERE form IN ({', '.join(repr(f) for f in A.FORMS)}) GROUP BY cik, accn ORDER BY cik, filed""").df()
    A.fetch_rows(todo, workers=workers)
    A.commit(FPI_DB)


def status() -> None:
    with store(read_only=True) as s:
        have = set(s.con.execute("SELECT table_name FROM information_schema.tables").df()["table_name"])
        for t in ("fpi_issuers", "bars", "xbrl_facts", "fx_daily", "ads_ratio", "fundamentals_pit"):
            if t in have:
                print(t, s.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["issuers", "bars", "facts", "fx", "ads", "status"])
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    {"issuers": issuers, "bars": lambda: bars(a.limit), "facts": lambda: facts(limit=a.limit), "fx": fx,
     "ads": ads, "status": status}[a.cmd]()


if __name__ == "__main__":
    main()
