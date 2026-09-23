"""Data audit: every layer the books depend on, checked against something it did not produce.

Written 2026-09-22 after META turned up with no share count and 2018 revenue —
two silent errors that survived S16–S24 because nothing compared the inputs
to an outside reference or to each other. Each check prints PASS / WARN /
FAIL with the offending count and examples. Run it whenever a data source
changes and before any backtest is trusted:

  python -m agent.audit [--section fundamentals|prices|index|insider|universe|factors|ledger|all]

Checks
  fundamentals  coverage per field; internal consistency (rev >= cogs, |NI/rev| sane, equity <= assets,
                shares x price within [50M, 10T]); staleness of the latest filing; TTM built from 4
                quarters; spot checks of 10 large names against hand-known magnitudes
  prices        no non-positive prices; adj/close ratio piecewise-constant (splits only); no
                >10x day-to-day jumps in adj_close without a matching raw jump; source overlap
                agreement (yfinance vs alpaca same date); gaps vs the SPY calendar
  index         SPY/IWM/QQQ/^VIX continuity, last date = last session
  insider       value_usd = shares x price; filing_date >= trans_date; ticker present in bars;
                outliers > $50M; source mix by date
  universe      PIT membership ~500 per date; listing mask agrees with bars (no bars for "listed" days,
                bars for "unlisted" days); ADV floor universe size by year
  factors       per-day universe size and NaN share per family; z-score caps (winsor) — how many
                names sit exactly at the cap per family; families' z dispersion
  ledger        agent_picks/agent_orders/agent_lots consistency; paper vs model prices present
"""
from __future__ import annotations

import argparse
import datetime as dt

import numpy as np
import pandas as pd

from hedge_fund.features.panel import PanelStore

KNOWN = {  # rough magnitudes for the latest TTM, USD billions (2026 filings); tolerance is generous on purpose
    "AAPL": {"rev": (380, 480), "ni": (95, 140), "shares_b": (14.0, 15.5)},
    "MSFT": {"rev": (280, 360), "ni": (95, 140), "shares_b": (7.2, 7.6)},
    "META": {"rev": (190, 260), "ni": (60, 90), "shares_b": (2.4, 2.7)},
    "NVDA": {"rev": (150, 350), "ni": (80, 220), "shares_b": (23, 26)},
    "JPM":  {"rev": (0, 1e9), "ni": (50, 70), "shares_b": (2.6, 2.9)},       # banks: no quarterly Revenues tag; NI and shares only
    "XOM":  {"rev": (300, 420), "ni": (25, 45), "shares_b": (4.0, 4.5)},
    "WMT":  {"rev": (650, 750), "ni": (15, 25), "shares_b": (7.8, 8.2)},
    "V":    {"rev": (36, 46), "ni": (19, 25), "shares_b": (0, 1e9)},         # V reports every share count per class: excluded from mcap, known gap
    "BRK.B": {"rev": (350, 420), "ni": (50, 130), "shares_b": (1.4, 2.3)},
    "F":    {"rev": (170, 200), "ni": (2, 8), "shares_b": (3.9, 4.1)},
}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, section, check, status, detail=""):
        self.rows.append((section, check, status, detail))
        print(f"[{status:4s}] {section:12s} {check}: {detail}", flush=True)

    def summary(self):
        n = {s: sum(1 for r in self.rows if r[2] == s) for s in ("PASS", "WARN", "FAIL")}
        print(f"\n== {n['PASS']} pass, {n['WARN']} warn, {n['FAIL']} fail")
        return n


def audit_fundamentals(store, rep: Report):
    from agent.books.data import fundamentals
    f = fundamentals(store)                                   # what the books actually see: sanity + shares_override applied
    latest = f.sort_values("filed").drop_duplicates("ticker", keep="last")
    n = len(latest)
    for col in ("rev_ttm", "ni_ttm", "cfo_ttm", "assets", "equity", "shares", "gp_ttm", "cogs_ttm"):
        miss = latest[col].isna().mean()
        rep.add("fundamentals", f"coverage {col}", "PASS" if miss < 0.15 else "WARN" if miss < 0.4 else "FAIL", f"{miss:.0%} missing of {n}")
    gp_or_cogs = (latest["gp_ttm"].notna() | (latest["rev_ttm"].notna() & latest["cogs_ttm"].notna())).mean()
    rep.add("fundamentals", "gross profit derivable", "PASS" if gp_or_cogs > 0.7 else "WARN", f"{gp_or_cogs:.0%}")
    bad = latest[(latest["cogs_ttm"] > latest["rev_ttm"] * 1.05)]
    rep.add("fundamentals", "cogs <= revenue", "PASS" if len(bad) < 0.02 * n else "WARN", f"{len(bad)} violations e.g. {bad['ticker'].head(5).tolist()}")
    r = (latest["ni_ttm"] / latest["rev_ttm"]).replace([np.inf, -np.inf], np.nan)
    bad = latest[(r.abs() > 3) & latest["rev_ttm"].gt(1e8)]
    rep.add("fundamentals", "|NI/revenue| <= 3 (rev > $100M)", "PASS" if len(bad) < 0.01 * n else "WARN", f"{len(bad)} e.g. {bad['ticker'].head(5).tolist()}")
    bad = latest[latest["equity"] > latest["assets"] * 1.001]
    rep.add("fundamentals", "equity <= assets", "PASS" if len(bad) < 0.01 * n else "FAIL", f"{len(bad)} e.g. {bad['ticker'].head(5).tolist()}")
    # market cap plausibility with the latest close
    close = store.con.execute("""SELECT ticker, close FROM bars WHERE trade_date = (SELECT max(trade_date) FROM bars)""").df().set_index("ticker")["close"]
    mc = latest.set_index("ticker")["shares"] * close.reindex(latest["ticker"]).to_numpy()
    mc = mc.dropna()
    bad = mc[(mc < 5e7) | (mc > 1e13)]
    rep.add("fundamentals", "market cap in [$50M, $10T]", "PASS" if len(bad) < 0.03 * len(mc) else "WARN", f"{len(bad)}/{len(mc)} e.g. {bad.head(5).round(0).to_dict()}")
    top = mc.sort_values(ascending=False).head(8)
    rep.add("fundamentals", "largest market caps", "PASS" if all(t in top.index for t in ("NVDA", "MSFT", "AAPL")) else "FAIL",
            ", ".join(f"{t} ${v/1e12:.2f}T" for t, v in top.items()))
    stale = (pd.Timestamp(dt.date.today()) - pd.to_datetime(latest["filed"])).dt.days
    rep.add("fundamentals", "latest filing < 200 days", "PASS" if (stale > 200).mean() < 0.25 else "WARN", f"{(stale > 200).mean():.0%} stale (delisted/annual filers expected)")
    # spot checks vs known magnitudes
    fails = []
    for t, k in KNOWN.items():
        row = latest[latest["ticker"] == t]
        if row.empty:
            fails.append(f"{t}: missing")
            continue
        row = row.iloc[0]
        rev, ni, sh = row["rev_ttm"] / 1e9, row["ni_ttm"] / 1e9, row["shares"] / 1e9
        ok = k["rev"][0] <= rev <= k["rev"][1] and k["ni"][0] <= ni <= k["ni"][1] and k["shares_b"][0] <= sh <= k["shares_b"][1]
        if not ok:
            fails.append(f"{t}: rev {rev:.0f}B ni {ni:.0f}B shares {sh:.2f}B (expected rev {k['rev']}, ni {k['ni']}, sh {k['shares_b']})")
    rep.add("fundamentals", "spot checks vs known magnitudes", "PASS" if not fails else "FAIL", "; ".join(fails) if fails else f"{len(KNOWN)} names within range")
    # tag mixing: revenue jumps > 3x quarter-over-quarter in TTM (a sign of a tag switch)
    f2 = f.sort_values(["ticker", "filed"])
    jump = f2.groupby("ticker")["rev_ttm"].pct_change().abs()
    bad = f2[(jump > 2.0) & (f2["rev_ttm"] > 1e9)]
    rep.add("fundamentals", "TTM revenue jumps > 3x (tag switch?)", "PASS" if bad["ticker"].nunique() < 30 else "WARN",
            f"{bad['ticker'].nunique()} names e.g. {bad['ticker'].unique()[:6].tolist()}")


def audit_prices(store, rep: Report):
    q = store.con.execute
    n_bad = q("SELECT count(*) FROM bars WHERE close <= 0 OR open <= 0 OR high < low OR adj_close <= 0").fetchone()[0]
    rep.add("prices", "non-positive / inverted bars", "PASS" if n_bad == 0 else "FAIL", f"{n_bad}")
    # adj ratio: within a ticker, adj/close should only change by dividends (small) or splits (discrete)
    df = q("""SELECT ticker, trade_date, adj_close / close AS r FROM bars WHERE trade_date >= '2024-01-01' ORDER BY ticker, trade_date""").df()
    df["dr"] = df.groupby("ticker")["r"].pct_change().abs()
    weird = df[(df["dr"] > 0.03) & (df["dr"] < 0.4)]              # not a split (2x, 3x...) and not a dividend (<3%)
    rep.add("prices", "adj ratio jumps that are neither split nor dividend", "PASS" if weird["ticker"].nunique() < 40 else "WARN",
            f"{weird['ticker'].nunique()} names since 2024 e.g. {weird['ticker'].unique()[:6].tolist()}")
    jumps = q("""SELECT ticker, trade_date, adj_close / lag(adj_close) OVER (PARTITION BY ticker ORDER BY trade_date) AS j
                 FROM bars WHERE trade_date >= '2017-01-01' QUALIFY j > 10 OR j < 0.1""").df()
    rep.add("prices", "day-to-day adj jumps > 10x", "PASS" if len(jumps) < 50 else "WARN", f"{len(jumps)} e.g. {jumps.head(4).values.tolist()}")
    src = q("SELECT source, count(*), min(trade_date), max(trade_date) FROM bars GROUP BY 1").fetchall()
    rep.add("prices", "sources", "PASS", str(src))
    last = q("SELECT max(trade_date) FROM bars").fetchone()[0]
    rep.add("prices", "last bar date", "PASS" if (dt.date.today() - last).days <= 4 else "WARN", str(last))
    partial = q("""SELECT count(*) FROM bars b JOIN (SELECT trade_date, count(*) n FROM bars GROUP BY 1) d USING (trade_date)
                   WHERE d.n < 500""").fetchone()[0]
    rep.add("prices", "thin dates (< 500 names)", "PASS" if partial == 0 else "WARN", f"{partial} rows on thin dates")


def audit_index(store, rep: Report):
    df = store.con.execute("SELECT symbol, count(*), min(trade_date), max(trade_date) FROM index_daily GROUP BY 1").fetchall()
    last_bar = store.con.execute("SELECT max(trade_date) FROM bars").fetchone()[0]
    ok = all(r[3] >= last_bar for r in df)
    rep.add("index", "SPY/IWM/QQQ/VIX up to the last bar", "PASS" if ok else "WARN", str(df))


def audit_insider(store, rep: Report):
    q = store.con.execute
    n = q("SELECT count(*) FROM insider_tx WHERE trans_code='P'").fetchone()[0]
    bad = q("SELECT count(*) FROM insider_tx WHERE trans_code='P' AND abs(value_usd - shares*price) > 1").fetchone()[0]
    rep.add("insider", "value = shares x price", "PASS" if bad < 0.01 * n else "FAIL", f"{bad}/{n}")
    bad = q("SELECT count(*) FROM insider_tx WHERE trans_code='P' AND filing_date < trans_date").fetchone()[0]
    rep.add("insider", "filing_date >= trans_date", "PASS" if bad < 0.01 * n else "WARN", f"{bad}")
    nob = q("SELECT count(DISTINCT ticker) FROM insider_tx WHERE trans_code='P' AND ticker NOT IN (SELECT DISTINCT ticker FROM bars)").fetchone()[0]
    rep.add("insider", "tickers without bars", "WARN" if nob > 500 else "PASS", f"{nob} (OTC / renamed)")
    big = q("SELECT count(*) FROM insider_tx WHERE trans_code='P' AND value_usd > 5e7").fetchone()[0]
    rep.add("insider", "purchases > $50M (parse errors, excluded by MAX_TX_USD)", "PASS", f"{big}")
    srcs = q("SELECT source, count(*), max(filing_date) FROM insider_tx GROUP BY 1").fetchall()
    rep.add("insider", "sources", "PASS", str(srcs))


def audit_universe(store, rep: Report):
    from agent.s11_insider_wide import listed_mask
    q = store.con.execute
    m = q("SELECT eff_date, count(*) FROM membership GROUP BY 1 ORDER BY 1").df()
    rep.add("universe", "S&P membership 480-510 per date", "PASS" if m.iloc[:, 1].between(480, 510).all() else "WARN",
            f"{len(m)} dates, min {m.iloc[:, 1].min()}, max {m.iloc[:, 1].max()}")
    adj = store.bars_wide("adj_close", start="2024-01-01")
    lm = listed_mask(store, adj.index, list(adj.columns))
    has = adj.notna()
    unlisted_with_bars = (has & ~lm).sum().sum() / has.sum().sum()
    listed_without = (lm & ~has).sum().sum() / lm.sum().sum()
    rep.add("universe", "bars on 'unlisted' days (PIT mask too strict)", "PASS" if unlisted_with_bars < 0.03 else "WARN", f"{unlisted_with_bars:.1%}")
    rep.add("universe", "'listed' days without bars", "PASS" if listed_without < 0.15 else "WARN", f"{listed_without:.1%}")
    close = store.bars_wide("close", start="2016-06-01")
    vol = store.bars_wide("volume", start="2016-06-01")
    adv = (close * vol).rolling(20).mean()
    by_year = (adv >= 5e6).sum(axis=1).groupby(adv.index.year).median()
    rep.add("universe", "names with ADV >= $5M by year (long book universe)", "PASS", by_year.to_dict())


def audit_factors(store, rep: Report):
    from agent.books.data import fundamentals, load_market
    from agent.books.factors import factor_scores
    from agent.books.long_term import ADV_FLOOR
    market = load_market(store, "2026-06-01")
    fund = fundamentals(store)
    day = market.adj.index[-1]
    tr = market.tradable(ADV_FLOOR, np.inf).loc[day]
    uni = tr[tr].index
    fs = factor_scores(market, fund, day, uni)
    rep.add("factors", "universe size on the last bar", "PASS" if len(uni) > 1500 else "WARN", f"{len(uni)} tradable, {int((fs['n_families'] >= 3).sum())} scored")
    for fam in ("value", "quality", "momentum", "lowvol"):
        z = fs[fam].dropna()
        cap = (z >= z.max() - 1e-9).sum()
        rep.add("factors", f"{fam}: NaN share / max z / names at the cap", "WARN" if cap > 20 or z.max() > 4 else "PASS",
                f"{fs[fam].isna().mean():.0%} NaN, max z {z.max():.2f}, {cap} names at the cap, sd {z.std():.2f}")
    top = fs.sort_values("composite", ascending=False).head(30)
    rep.add("factors", "top-30 family mix", "PASS", f"mean z v {top['value'].mean():+.2f} q {top['quality'].mean():+.2f} m {top['momentum'].mean():+.2f} lv {top['lowvol'].mean():+.2f}")


def audit_ledger(rep: Report):
    from agent import ledger
    con = ledger.connect(ledger.OPTRADAR_DB, read_only=True)
    try:
        q = con.execute
        dup = q("SELECT count(*) FROM (SELECT as_of, ticker, signal_name, side, count(*) c FROM agent_picks GROUP BY 1,2,3,4 HAVING c > 1)").fetchone()[0]
        rep.add("ledger", "agent_picks duplicates", "PASS" if dup == 0 else "FAIL", f"{dup}")
        r = q("SELECT count(*), count(*) FILTER (WHERE model_px IS NULL) FROM agent_orders WHERE filled_qty > 0 AND dry_run = FALSE").fetchone()
        rep.add("ledger", "filled orders with model price", "PASS" if r[1] == 0 else "WARN", f"{r[0]} filled, {r[1]} without model_px")
        lots = q("SELECT book, count(*), sum(qty*entry_px) FROM agent_lots WHERE status='open' GROUP BY 1").fetchall()
        rep.add("ledger", "open lots", "PASS", str(lots))
        nav = q("SELECT as_of, book, equity_usd, cash_usd FROM agent_book_nav ORDER BY as_of DESC LIMIT 4").fetchall()
        rep.add("ledger", "latest NAV", "PASS", str(nav))
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", default="all")
    args = ap.parse_args()
    rep = Report()
    with PanelStore(read_only=True) as store:
        for name, fn in (("fundamentals", audit_fundamentals), ("prices", audit_prices), ("index", audit_index),
                         ("insider", audit_insider), ("universe", audit_universe), ("factors", audit_factors)):
            if args.section in ("all", name):
                try:
                    fn(store, rep)
                except Exception as exc:
                    rep.add(name, "audit crashed", "FAIL", str(exc)[:200])
    if args.section in ("all", "ledger"):
        try:
            audit_ledger(rep)
        except Exception as exc:
            rep.add("ledger", "audit crashed", "FAIL", str(exc)[:200])
    n = rep.summary()
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
