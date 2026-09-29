"""Measurement of the paper books: the shared baselines every page uses, and the evaluation quantities.

Two parts, kept apart on purpose.

1. baselines() — what the pages show (S48, review item measure #3). Each book is measured from
   the close BEFORE its first fill (long 09-22, insider 09-23, core 09-25): the NAV rows written
   before a book owned anything are not part of its record, and SPY / QQQ are taken from the
   same close, on adj_close (total return). The combined index chains the daily returns of the
   books that have started, so a book joining does not move it. Three bases, always labelled:
     sim         the simulator's NAV (agent_book_nav): what the paper account shows, no dividends
     sim_tr      sim + the dividends the lots were entitled to (agent/dividends.py): total return,
                 the same basis as SPY / QQQ adj_close
     auction_tr  every fill re-priced at the opening cross, net of regulatory fees, plus
                 dividends (agent_auction_nav.equity_auction_tr): the basis the evaluation reads
   Next to every return: the average invested share (market value / NAV since the first fill),
   because idle cash earns nothing on paper while the backtests park it in SPY.

2. evaluate() — the pre-registered evaluation's inputs (agent/config.yaml), computed from the
   record every day and written to out/agent/evaluate.json ONLY. config.yaml: no verdict before
   the evaluation point, so no page shows these as a headline; a page may show counts (progress).
   Counted from evaluate_from (S47, 2026-09-29):
     tracking      paper NAV (auction_tr) vs the same-window rule replay (agent/drift.py writes it
                   into drift.json): daily difference, its mean, Newey-West t (lag 5), cumulative
     alpha2        of the paper NAV and of the replay, SPY + (IWM - SPY), NW t — file only
     execution     fill vs the opening cross (agent/auction_basis.py fill_rows), per book and side,
                   standard error clustered by trading day (fills of one morning move together);
                   fills whose cross was beyond the limit, and fills without a cross, counted apart
     fill_rate     orders planned from evaluate_from that reached a final state: share filled
     insider       entry kinds (on_time / late / retry) of the orders and lots
     closed_lots   per book: count, mean, sd, and for each lot the SPY return over the same opens

Usage: python -m agent.evaluate [--db PATH] [--out PATH] [--drift PATH] [--auction PATH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os

import numpy as np
import pandas as pd

from agent import ledger
from hedge_fund.validation.stats import newey_west_t

OUT = "/Users/louis/optradar/out/agent/evaluate.json"
DRIFT = "/Users/louis/optradar/out/agent/drift.json"
AUCTION = "/Users/louis/optradar/out/agent/auction_basis.json"
CONFIG = os.path.join(os.path.dirname(__file__), "config.yaml")
BOOK_LABEL = {"long": "长线综合因子", "insider": "内部人短线", "core": "SPY 核心仓"}
BOOK_ORDER = ("long", "insider", "core")
BENCH = ("SPY", "QQQ")
BASES = ("sim", "sim_tr", "auction_tr")
BASIS_LABEL = {"sim": "模拟器口径(不含分红)", "sim_tr": "模拟器口径含分红", "auction_tr": "竞价口径含分红、扣监管费"}
FINAL = ("filled", "expired", "canceled", "rejected", "done_for_day", "replaced")


# ================================================================ 1. baselines (pages) ====
def load_books(con) -> tuple[pd.DataFrame, dict, dict]:
    """(NAV per day and book on every basis, {book: first fill day}, {book: alloc_usd})."""
    nav = con.execute("SELECT as_of, book, equity_usd, market_value_usd, n_positions FROM agent_book_nav").df()
    try:
        auc = con.execute("SELECT * FROM agent_auction_nav").df()
    except Exception:                                            # a ledger without the auction basis yet
        auc = pd.DataFrame(columns=["as_of", "book"])
    for col in ("equity_auction", "div_cum", "equity_auction_tr"):
        if col not in auc.columns:
            auc[col] = np.nan
    if len(nav):
        nav["as_of"] = pd.to_datetime(nav["as_of"]).dt.date
    if len(auc):
        auc["as_of"] = pd.to_datetime(auc["as_of"]).dt.date
    df = nav.merge(auc[["as_of", "book", "equity_auction", "div_cum", "equity_auction_tr"]], on=["as_of", "book"], how="left")
    df["div_cum"] = df["div_cum"].fillna(0.0)
    df["sim"] = df["equity_usd"]
    df["sim_tr"] = df["equity_usd"] + df["div_cum"]
    df["auction_tr"] = df["equity_auction_tr"].fillna(df["equity_auction"].fillna(df["equity_usd"]) + df["div_cum"])
    first = {b: pd.Timestamp(d).date() for b, d in con.execute(
        """SELECT book, min(CAST(filled_at AS DATE)) FROM agent_orders
           WHERE dry_run = FALSE AND filled_qty > 0 GROUP BY 1""").fetchall() if d is not None}
    alloc = dict(con.execute("SELECT book, alloc_usd FROM agent_books").fetchall())
    return df.sort_values(["as_of", "book"]).reset_index(drop=True), first, alloc


def bench_prices(store, symbols=BENCH, since: dt.date | None = None) -> dict[str, dict[dt.date, float]]:
    """adj_close (total return) of the benchmark ETFs from index_daily."""
    q = f"SELECT symbol, trade_date, adj_close FROM index_daily WHERE symbol IN ({','.join('?' * len(symbols))})"
    args = list(symbols)
    if since:
        q += " AND trade_date >= ?"
        args.append(since)
    out: dict[str, dict] = {s: {} for s in symbols}
    for s, d, p in store.con.execute(q, args).fetchall():
        if p is not None:
            out[s][pd.Timestamp(d).date()] = float(p)
    return out


def _at_or_before(series: dict, day: dt.date) -> float | None:
    ks = [k for k in series if k <= day]
    return series[max(ks)] if ks else None


def _pct(a, b) -> float | None:
    return None if a is None or b is None or not b else (a / b - 1) * 100


def baselines(nav: pd.DataFrame, first_fill: dict, bench: dict, end: dt.date | None = None) -> dict:
    """Per book and combined: returns since the close before the first fill, on every basis, with SPY / QQQ.

    nav: load_books()'s frame; first_fill: {book: first fill day}; bench: {symbol: {day: adj_close}}.
    Books without a fill are left out (they have no record yet).
    """
    if end is not None:
        nav = nav[nav["as_of"] <= end]
    books, series = {}, {}
    for b in [b for b in BOOK_ORDER if b in first_fill] + sorted(set(first_fill) - set(BOOK_ORDER)):
        g = nav[nav["book"] == b].set_index("as_of").sort_index()
        if g.empty:
            continue
        before = [d for d in g.index if d < first_fill[b]]
        base = before[-1] if before else g.index[0]
        g = g.loc[base:]
        last = g.index[-1]
        held = g.loc[g.index > base]
        expo = (held["market_value_usd"] / held["equity_usd"]).mean() * 100 if len(held) else None
        books[b] = {"label": BOOK_LABEL.get(b, b), "first_fill": str(first_fill[b]), "base_day": str(base),
                    "last_day": str(last), "n_sessions": int(len(g) - 1), "base_equity": float(g["sim"].iloc[0]),
                    "ret": {k: _pct(float(g[k].iloc[-1]), float(g[k].iloc[0])) for k in BASES},
                    "bench": {s: _pct(_at_or_before(bench.get(s, {}), last), _at_or_before(bench.get(s, {}), base)) for s in BENCH},
                    "exposure_avg_pct": None if expo is None or pd.isna(expo) else float(expo),
                    "div_cum": float(g["div_cum"].iloc[-1] - g["div_cum"].iloc[0])}
        series[b] = g
    if not books:
        return {"books": {}, "combined": None, "bases": BASIS_LABEL}
    start = min(pd.Timestamp(v["base_day"]).date() for v in books.values())
    days = sorted({d for g in series.values() for d in g.index if d >= start})
    idx = {k: [100.0] for k in BASES}
    expo = []
    for p, t in zip(days, days[1:]):
        live = [b for b, g in series.items() if p in g.index and t in g.index]
        for k in BASES:
            e0 = sum(float(series[b].at[p, k]) for b in live)
            e1 = sum(float(series[b].at[t, k]) for b in live)
            idx[k].append(idx[k][-1] * (e1 / e0 if e0 else 1.0))
        inv = [b for b in live if t >= pd.Timestamp(books[b]["first_fill"]).date()]      # invested share since the first fill
        eq = sum(float(series[b].at[t, "equity_usd"]) for b in inv)
        if eq:
            expo.append(sum(float(series[b].at[t, "market_value_usd"]) for b in inv) / eq * 100)
    b0 = {s: _at_or_before(bench.get(s, {}), start) for s in BENCH}
    comb = {"base_day": str(start), "last_day": str(days[-1]), "days": [str(d) for d in days], "books": list(books),
            "index": {k: [round(v, 3) for v in idx[k]] for k in BASES},
            "bench": {s: [None if b0[s] is None or _at_or_before(bench.get(s, {}), d) is None
                          else round(_at_or_before(bench[s], d) / b0[s] * 100, 3) for d in days] for s in BENCH},
            "ret": {k: idx[k][-1] - 100 for k in BASES},
            "bench_ret": {s: _pct(_at_or_before(bench.get(s, {}), days[-1]), b0[s]) for s in BENCH},
            "exposure_avg_pct": float(np.mean(expo)) if expo else None,
            "series": {b: {k: [round(float(g.at[d, k]) / float(g[k].iloc[0]) * 100, 3) if d in g.index else None for d in days]
                           for k in BASES} for b, g in series.items()}}
    return {"books": books, "combined": comb, "bases": BASIS_LABEL}


def page_baselines(con, store, end: dt.date | None = None) -> dict:
    """baselines() straight from a ledger connection and a PanelStore: the one call the pages make."""
    nav, first, _ = load_books(con)
    since = min(first.values()) - dt.timedelta(days=10) if first else None
    return baselines(nav, first, bench_prices(store, BENCH, since), end)


def fill_gaps(fills: list, rows: list[dict]) -> list[dict]:
    """Each fill with its gap to the OPENING CROSS (auction_basis fill_rows; the evaluation's definition) and its order kind.

    kind: 'opg' (market-on-open, fills in the cross), 'day_market' (the simulator's first ask after the open),
    'day_limit'. The gap to the bars' open (model_px) is kept as gap_model for reference only."""
    cross = {(r["book"], r["ticker"], r["side"], r["day"]): r for r in rows}
    out = []
    for b, t, s, day, ts, px, m, ot, tif in fills:
        r = cross.get((b, t, s, str(day)), {})
        kind = "opg" if (tif or "").lower() == "opg" else "day_market" if ot == "market" else "day_limit"
        out.append({"book": b, "ticker": t, "side": s, "day": str(day), "time": str(ts)[11:19], "type": ot, "tif": tif, "kind": kind,
                    "gap": None if r.get("gap_pct") is None else round(r["gap_pct"], 3), "gap_fill": bool(r.get("gap_fill")),
                    "gap_model": round((px / m - 1) * 100 * (1 if s == "buy" else -1), 3) if m else None})
    return out


def bench_by_year(s, start: str, end: str) -> tuple[dict, float | None]:
    """Calendar-year total returns and CAGR of an adj_close series over [start, end], as the backtest reports SPY:
    the first year from the first close in the window, later years from the previous year's last close."""
    s = s.loc[start:end].dropna()
    if s.empty:
        return {}, None
    out, prev = {}, float(s.iloc[0])
    for y, g in s.groupby(s.index.year):
        out[str(y)] = float(g.iloc[-1]) / prev - 1
        prev = float(g.iloc[-1])
    years = (s.index[-1] - s.index[0]).days / 365.25
    return out, ((float(s.iloc[-1]) / float(s.iloc[0])) ** (1 / years) - 1) * 100 if years > 0 else None


def backtest_bench(store, valid_dir: str) -> dict | None:
    """The two live books' backtests by year (S33 report) next to SPY and QQQ over the same window. The comparison
    shown is against SPY and QQQ (CAGR, excess over SPY); alpha2 stays in the research reports (S48)."""
    files = sorted(glob.glob(os.path.join(valid_dir, "s33_negative_filters_*.json")))
    if not files:
        return None
    full = json.load(open(files[-1]))
    j = full["books"]
    lb, ib = j["long_base"], j["insider_base"]
    years = sorted(lb["by_year"])
    qqq_y, qqq_cagr = bench_by_year(store.index_series("QQQ"), full.get("start", "2017-01-01"), full.get("end", "2026-08-31"))
    keys = ("cagr_pct", "spy_cagr_pct", "excess_cagr_pct", "active_t_nw", "max_drawdown_pct", "sharpe", "avg_exposure")
    return {"years": years, "long": [round(lb["by_year"][y] * 100, 1) for y in years],
            "insider": [round(ib["by_year"].get(y, 0) * 100, 1) for y in years],
            "spy": [round(lb["spy_by_year"][y] * 100, 1) for y in years],
            "qqq": [round(qqq_y[y] * 100, 1) if y in qqq_y else None for y in years], "qqq_cagr_pct": qqq_cagr,
            "long_meta": {k: lb.get(k) for k in keys}, "insider_meta": {k: ib.get(k) for k in keys},
            "start": full.get("start"), "end": full.get("end"), "report": os.path.basename(files[-1])}


# ================================================================ 2. evaluation (file only) ====
def _config(path: str = CONFIG) -> dict:
    try:
        import yaml
        return yaml.safe_load(open(path)) or {}
    except Exception:
        return {}


def clustered_mean(x: np.ndarray, groups: list) -> dict:
    """Mean of x with its standard error clustered by group (fills of one trading day move together)."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n == 0:
        return {"n": 0, "n_clusters": 0, "mean": None, "se": None, "t": None}
    m = float(x.mean())
    g = pd.Series(x - m).groupby(list(groups)).sum()
    k = len(g)
    se = math.sqrt(float((g ** 2).sum()) * (k / (k - 1) if k > 1 else 1.0)) / n if k > 1 else None
    return {"n": n, "n_clusters": k, "mean": m, "se": se, "t": (m / se) if se else None}


def alpha2(ret: pd.Series, spy: pd.Series, iwm: pd.Series) -> dict:
    """Daily returns on SPY and IWM - SPY: annualised intercept and its Newey-West t (lag 5)."""
    df = pd.concat({"y": ret, "m": spy, "s": iwm - spy}, axis=1).dropna()
    if len(df) < 20:
        return {"n": int(len(df)), "alpha_ann_pct": None, "t_nw": None}
    X = np.column_stack([np.ones(len(df)), df["m"], df["s"]])
    beta, *_ = np.linalg.lstsq(X, df["y"].to_numpy(), rcond=None)
    resid = df["y"].to_numpy() - X[:, 1:] @ beta[1:]
    return {"n": int(len(df)), "alpha_ann_pct": float(beta[0] * 252 * 100), "t_nw": float(newey_west_t(resid, lag=5)),
            "beta_mkt": float(beta[1]), "beta_size": float(beta[2])}


def tracking(paper: pd.Series, replay: pd.Series, since: dt.date) -> dict:
    """Paper vs replay NAV (both total return), daily returns after `since`'s close."""
    df = pd.concat({"p": paper, "r": replay}, axis=1).dropna().sort_index()
    df = df[df.index >= since]
    rets = df.pct_change().dropna()
    d = (rets["p"] - rets["r"]).to_numpy()
    if not len(d):
        return {"n_days": 0, "mean_bp": None, "t_nw": None, "cum_pct": None}
    return {"n_days": int(len(d)), "mean_bp": float(d.mean() * 1e4), "sd_bp": float(d.std(ddof=1) * 1e4) if len(d) > 1 else None,
            "t_nw": None if len(d) < 3 else float(newey_west_t(d, lag=5)),
            "cum_pct": float((df["p"].iloc[-1] / df["p"].iloc[0]) / (df["r"].iloc[-1] / df["r"].iloc[0]) * 100 - 100),
            "from": str(pd.Timestamp(df.index[0]).date()), "to": str(pd.Timestamp(df.index[-1]).date())}


def execution(fill_rows: list[dict], since: dt.date) -> dict:
    """Fill vs opening cross per book and side, for fills after `since` (orders planned from it fill the next morning)."""
    fr = pd.DataFrame(fill_rows)
    if fr.empty:
        return {}
    fr = fr[pd.to_datetime(fr["day"]).dt.date > since]
    out = {}
    for (b, s), g in fr.groupby(["book", "side"]):
        ok = g[g["gap_pct"].notna()]
        out[f"{b}/{s}"] = dict(clustered_mean(ok["gap_pct"].to_numpy(dtype=float), list(ok["day"])),
                               n_fills=int(len(g)), n_gap_fills=int(g.get("gap_fill", pd.Series(False, index=g.index)).fillna(False).sum()),
                               n_no_cross=int(g["cross"].isna().sum()) if "cross" in g else 0)
    return out


def fill_rate(orders: pd.DataFrame, since: dt.date) -> dict:
    o = orders[(orders["as_of"] >= since) & ((orders["filled_qty"].fillna(0) > 0) | orders["status"].isin(FINAL))]
    out = {}
    for (b, s), g in o.groupby(["book", "side"]):
        out[f"{b}/{s}"] = {"n": int(len(g)), "n_filled": int((g["filled_qty"].fillna(0) > 0).sum()),
                           "share": float((g["filled_qty"].fillna(0) > 0).mean()),
                           "qty_share": float(g["filled_qty"].fillna(0).sum() / g["qty"].sum()) if g["qty"].sum() else None}
    return out


def closed_lots(lots: pd.DataFrame, spy_open: dict, since: dt.date) -> dict:
    """Per book, the closed lots entered from `since`: count, mean, sd; each with SPY over the same two opens."""
    out = {}
    c = lots[(lots["status"] == "closed") & (lots["order_as_of"] >= since)]
    for b, g in c.groupby("book"):
        r = g["ret_pct"].astype(float)
        spy = [_pct(spy_open.get(x), spy_open.get(e)) for e, x in zip(g["entry_day"], g["exit_day"])]
        ex = [a - s for a, s in zip(r, spy) if s is not None]
        out[b] = {"n": int(len(g)), "mean_ret_pct": float(r.mean()), "sd_ret_pct": float(r.std(ddof=1)) if len(r) > 1 else None,
                  "mean_excess_spy_pct": float(np.mean(ex)) if ex else None, "n_with_spy": len(ex),
                  "by_entry_kind": {str(k): int(v) for k, v in g["entry_kind"].fillna("unknown").value_counts().items()}}
    return out


def evaluate(con, store, since: dt.date, replay: dict | None, fill_rows: list[dict], cfg: dict) -> dict:
    nav, first, alloc = load_books(con)
    orders = con.execute("""SELECT book, as_of, side, qty, status, filled_qty, entry_kind FROM agent_orders WHERE dry_run = FALSE""").df()
    lots = con.execute("""SELECT l.book, l.status, l.entry_day, l.exit_day, l.ret_pct, l.entry_kind, o.as_of AS order_as_of
                          FROM agent_lots l LEFT JOIN agent_orders o ON o.client_order_id = l.entry_order""").df()
    for df_, cols in ((orders, ["as_of"]), (lots, ["entry_day", "exit_day", "order_as_of"])):
        for col in cols:
            df_[col] = [None if pd.isna(x) else pd.Timestamp(x).date() for x in df_[col]]
    lots = lots[lots["order_as_of"].notna()]
    idx = store.con.execute("""SELECT symbol, trade_date, adj_close, open * adj_close / close FROM index_daily
                               WHERE symbol IN ('SPY', 'IWM') AND trade_date >= ?""", [since - dt.timedelta(days=10)]).fetchall()
    px = pd.DataFrame(idx, columns=["s", "d", "adj", "adj_open"])
    px["d"] = pd.to_datetime(px["d"])
    adj = px.pivot(index="d", columns="s", values="adj")
    spy_open = {k.date(): float(v) for k, v in px[px["s"] == "SPY"].set_index("d")["adj_open"].items()}
    mret = adj.pct_change()
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "evaluate_from": str(since),
           "note": "评估点之前只写文件,不上任何页面的头条(agent/config.yaml);页面最多显示计数", "books": {}}
    for b in sorted(set(nav["book"])):
        g = nav[nav["book"] == b].set_index("as_of").sort_index()
        paper = pd.Series(g["auction_tr"].to_numpy(dtype=float), index=pd.to_datetime(g.index))
        rec = {"alloc_usd": alloc.get(b), "first_fill": str(first.get(b)) if b in first else None}
        pr = paper[paper.index >= pd.Timestamp(since)].pct_change().dropna()
        if len(mret):
            rec["alpha2_paper"] = alpha2(pr, mret.get("SPY", pd.Series(dtype=float)), mret.get("IWM", pd.Series(dtype=float)))
        rp = (replay or {}).get(b)
        if rp and rp.get("days"):
            rs = pd.Series(rp["nav"], index=pd.to_datetime(rp["days"]), dtype=float)
            rec["tracking"] = tracking(paper, rs, pd.Timestamp(since))
            rec["alpha2_replay"] = alpha2(rs[rs.index >= pd.Timestamp(since)].pct_change().dropna(),
                                          mret.get("SPY", pd.Series(dtype=float)), mret.get("IWM", pd.Series(dtype=float)))
        out["books"][b] = rec
    out["execution"] = execution(fill_rows, since)
    out["fill_rate"] = fill_rate(orders, since)
    ins = orders[(orders["book"] == "insider") & (orders["side"] == "buy") & (orders["as_of"] >= since)]
    il = lots[(lots["book"] == "insider") & (lots["order_as_of"] >= since)]
    out["insider_entries"] = {"orders": {str(k): int(v) for k, v in ins["entry_kind"].fillna("unknown").value_counts().items()},
                              "lots": {str(k): int(v) for k, v in il["entry_kind"].fillna("unknown").value_counts().items()}}
    out["closed_lots"] = closed_lots(lots, spy_open, since)
    ev = cfg.get("evaluate_at") or {}
    out["progress"] = {b: {"closed": out["closed_lots"].get(b, {}).get("n", 0),
                           "on_time": out["closed_lots"].get(b, {}).get("by_entry_kind", {}).get("on_time") if b == "insider" else None,
                           "target": e.get("n_closed_lots"), "or_date": str(e.get("or_date"))}
                       for b, e in ev.items() if isinstance(e, dict)}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=ledger.OPTRADAR_DB)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--drift", default=DRIFT, help="drift.json with the rule replay's NAV")
    ap.add_argument("--auction", default=AUCTION, help="auction_basis.json with the fill rows")
    args = ap.parse_args(argv)
    from hedge_fund.features.panel import PanelStore
    cfg = _config()
    since = pd.Timestamp(str(cfg.get("evaluate_from") or "2026-09-29")).date()
    try:
        replay = json.load(open(args.drift)).get("replay")
    except Exception:
        replay = None
    try:
        rows = json.load(open(args.auction)).get("fill_rows", [])
    except Exception:
        rows = []
    con = ledger.connect(args.db, read_only=True)
    try:
        with PanelStore(read_only=True) as store:
            out = evaluate(con, store, since, replay, rows, cfg)
    finally:
        con.close()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(f"evaluate: from {since}; " + "; ".join(f"{b} closed {p['closed']}/{p['target']}" for b, p in out["progress"].items())
          + f"; written {args.out} (file only, no page headline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
