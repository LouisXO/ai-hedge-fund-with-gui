"""Daily post-close review: what happened today in the paper books and the real account,
one line per trade, and the rule checks that turn today's mistakes into tomorrow's vetoes.

Runs at the end of agent/bin/execute.sh (after fills are synced, bars updated, books marked
and the moomoo snapshot taken). Writes out/agent/review_<date>.json / .html, review_latest.json,
appends the lessons to out/agent/lessons.jsonl (the cumulative "same mistake again" counter), and
sends a clickable Mac notification that leads with the number of events needing action. Private site
only: the real account is in here.

Since 2026-09-29 (S48):
  - real account: the deals after the previous review day through this one, plus any earlier deal no
    review has shown (afternoon fills that reached acct_deals late, Saturday expiry settlements).
    Days to expiry and the price context use each deal's own date; its lessons carry that date.
    A read-only summary of the last 30 days' option positions by days to expiry at the first buy.
  - paper: a table of the lots closed today (fills vs the opening cross, SPY/IWM and the rule replay
    over the same window).
  - rule checks in classes: events needing action, the real account's behaviour, known simulator
    behaviour (shown, not a lesson), info.

No LLM. Every sentence is a template over numbers we computed; the rules are our own
findings (S12/S13 spreads, S25 big moves, S26 option cheapness, S31 entry timing, S33 vetoes).

Usage: python -m agent.review [--date YYYY-MM-DD] [--no-notify]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import html
import json
import os
import re
import subprocess
import sys

import duckdb
import numpy as np
import pandas as pd
import yaml

from agent import ledger
from agent.sources.alpaca_auctions import AUCTIONS_DB
from agent.sources.alpaca_news import NEWS_DB
from agent.sources.retail_heat import RETAIL_DB
from agent.watch import WATCHLIST
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/optradar/out/agent"
SITE = "https://optradar.tail5b470b.ts.net"
LESSONS = os.path.join(OUT_DIR, "lessons.jsonl")
OPT_CODE = re.compile(r"^US\.([A-Z.]+?)(\d{6})([CP])(\d+)$")
BIG_SIGMA, BIG_ABS, SD_WIN, VETO_WIN = 3.0, 5.0, 60, 5
# One lot's day loss that needs a look: the 1st percentile of one-day returns in the tradable pond
# (panel.db bars 2016-01 to 2026-09, close >= $2, dollar volume >= $1M, 8.4M stock-days: -9.31%;
# computed 2026-09-29, S48). A descriptive tail of the names the books hold, not a backtest statistic.
LOT_DAY_TAIL_PCT = -9.3
# Rule classes (S48, loop #7): only "action" and "real" go into lessons.jsonl.
#   action  something to fix or check today (rejected order, lots vs broker mismatch, market entry, tail loss)
#   sim     known simulator behaviour already explained (S27 fill after the open, S36 unfilled limits): counted
#           by the drift monitor, not a lesson
#   real    the user's own trading in the real account
#   info    context only
SIM_RULES = {"paper_unfilled", "paper_exec_gap", "paper_insider_limit"}

sys.path.insert(0, "/Users/louis/optradar/bin")
import site_theme  # noqa: E402


# ------------------------------------------------------------------ helpers ----
def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def news_link(h) -> str:
    if isinstance(h, str):
        return esc(h[:80])
    t = esc(h.get("zh") or h.get("en", ""))
    return f"<a href='{esc(h['url'])}' target='_blank' rel='noopener'>{t} ↗</a>" if h.get("url") else t


def pct(x, nd=2) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    cls = "pos" if x > 0 else "neg" if x < 0 else ""
    return f"<span class='{cls}'>{x:+.{nd}f}%</span>"


def usd(x) -> str:
    return "—" if x is None else f"${x:,.0f}"


def parse_code(code: str) -> dict:
    m = OPT_CODE.match(code or "")
    if not m:
        return {"ticker": (code or "").replace("US.", ""), "is_option": False}
    und, ymd, cp, k = m.groups()
    return {"ticker": und, "is_option": True, "expiry": dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])),
            "cp": cp, "strike": int(k) / 1000.0}


def sessions(store: PanelStore) -> list[dt.date]:
    return [r[0] for r in store.con.execute("SELECT trade_date FROM index_daily WHERE symbol = 'SPY' ORDER BY trade_date").fetchall()]


def prev_session(sess: list[dt.date], day: dt.date) -> dt.date | None:
    before = [d for d in sess if d < day]
    return before[-1] if before else None


def bars(store: PanelStore, tickers: list[str], start: dt.date, end: dt.date) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame(columns=["ticker", "trade_date", "open", "high", "low", "close", "adj_close"])
    q = f"SELECT ticker, trade_date, open, high, low, close, adj_close FROM bars WHERE trade_date BETWEEN ? AND ? AND ticker IN ({','.join('?' * len(tickers))}) ORDER BY ticker, trade_date"
    df = store.con.execute(q, [start, end] + list(tickers)).df()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def index_day(store: PanelStore, symbol: str, day: dt.date, prev: dt.date | None) -> dict:
    r = store.con.execute("SELECT trade_date, close FROM index_daily WHERE symbol = ? AND trade_date IN (?, ?) ORDER BY trade_date", [symbol, prev, day]).fetchall()
    d = {row[0]: row[1] for row in r}
    if day in d and prev in d and d[prev]:
        return {"close": d[day], "ret": (d[day] / d[prev] - 1) * 100}
    return {"close": d.get(day), "ret": None}


def day_context(hist: pd.DataFrame, spy: pd.Series, ticker: str, day: dt.date) -> dict:
    """Today's OHLC, day return, position of prices in the range, and the S25 big-move flag (last 5 sessions)."""
    g = hist[hist["ticker"] == ticker].set_index("trade_date").sort_index()
    if g.empty or day not in g.index:
        return {}
    row = g.loc[day]
    idx = list(g.index)
    i = idx.index(day)
    prev_close = float(g["adj_close"].iloc[i - 1]) if i > 0 else None
    ret = (float(row["adj_close"]) / prev_close - 1) * 100 if prev_close else None
    r = g["adj_close"].pct_change() * 100
    abn = r - spy.reindex(g.index).pct_change().to_numpy() * 100
    sd = r.shift(1).rolling(SD_WIN, min_periods=40).std()
    z = abn / sd
    big = ((z.abs() >= BIG_SIGMA) & (abn.abs() >= BIG_ABS)).fillna(False)
    recent = big.iloc[max(0, i - VETO_WIN + 1): i + 1]
    big_days = [d.isoformat() for d, b in recent.items() if b]
    rv20 = float(r.iloc[max(0, i - 19): i + 1].std() * np.sqrt(252)) if i >= 10 else None
    return {"open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]),
            "prev_close": prev_close, "ret": ret, "abn": float(abn.iloc[i]) if pd.notna(abn.iloc[i]) else None,
            "big_move_days": big_days, "rv20": rv20,
            "ret5": (float(g["adj_close"].iloc[i]) / float(g["adj_close"].iloc[i - 5]) - 1) * 100 if i >= 5 else None}


def range_pos(px: float, ctx: dict) -> float | None:
    if not ctx or ctx.get("high") is None or ctx["high"] == ctx["low"]:
        return None
    return (px - ctx["low"]) / (ctx["high"] - ctx["low"]) * 100


def headlines(symbols: list[str], day: dt.date) -> dict[str, list[dict]]:
    out: dict[str, list[str]] = {}
    if not symbols:
        return out
    try:
        n = duckdb.connect(str(NEWS_DB), read_only=True)
        rows = n.execute(f"""SELECT s.symbol, i.headline, i.url FROM news_symbols s JOIN news_items i USING (id)
                             WHERE s.symbol IN ({','.join('?' * len(symbols))}) AND i.n_symbols <= 3
                               AND i.created_at >= ? AND i.created_at < ? ORDER BY i.created_at DESC""",
                       list(symbols) + [pd.Timestamp(day) - pd.Timedelta(hours=8), pd.Timestamp(day) + pd.Timedelta(hours=24)]).fetchall()
        n.close()
        for s, h, u in rows:
            out.setdefault(s, [])
            if len(out[s]) < 3:
                out[s].append({"en": h, "url": u})
        from agent.translate import translate
        zh = translate([h["en"] for v in out.values() for h in v])
        for v in out.values():
            for h in v:
                h["zh"] = zh.get(h["en"], h["en"])
    except Exception:
        pass
    return out


def retail(symbols: list[str], day: dt.date) -> dict[str, dict]:
    try:
        r = duckdb.connect(str(RETAIL_DB), read_only=True)
        rows = r.execute("SELECT ticker, rank, mentions, mentions_24h_ago FROM apewisdom_daily WHERE day = ?", [day]).fetchall()
        r.close()
        return {t: {"rank": rk, "mentions": m, "prev": p} for t, rk, m, p in rows if t in symbols}
    except Exception:
        return {}


def insiders_30d(store: PanelStore, tickers: list[str], day: dt.date) -> dict[str, dict]:
    if not tickers:
        return {}
    rows = store.con.execute(f"""SELECT ticker, trans_code, count(*), sum(value_usd) FROM insider_tx
                                 WHERE ticker IN ({','.join('?' * len(tickers))}) AND filing_date BETWEEN ? AND ? AND trans_code IN ('P','S')
                                 GROUP BY 1, 2""", list(tickers) + [day - dt.timedelta(days=30), day]).fetchall()
    out: dict[str, dict] = {}
    for t, c, n, v in rows:
        out.setdefault(t, {})[c] = {"n": int(n), "usd": float(v or 0)}
    return out


def long_ranks(con) -> dict[str, int]:
    try:
        return dict(con.execute("""SELECT ticker, rank FROM agent_picks WHERE signal_name = 'composite_long'
                                   AND as_of = (SELECT max(as_of) FROM agent_picks WHERE signal_name = 'composite_long')""").fetchall())
    except Exception:
        return {}


def watch_notes() -> dict[str, str]:
    try:
        return {t: (c.get("note") or "") for t, c in yaml.safe_load(open(WATCHLIST))["watch"].items()}
    except Exception:
        return {}


def system_view(t: str, ctx: dict, ranks: dict, ins: dict, heat: dict, notes: dict) -> list[str]:
    """What our own systems say about a name today — the part a discretionary trade can check against."""
    v = []
    if t in ranks:
        v.append(f"长线书排名 {ranks[t]}" + ("(持有区)" if ranks[t] <= 30 else "(保留区)"))
    i = ins.get(t, {})
    if i.get("P"):
        v.append(f"内部人 30 日买入 {i['P']['n']} 笔 ${i['P']['usd']:,.0f}")
    if i.get("S"):
        v.append(f"内部人 30 日卖出 {i['S']['n']} 笔 ${i['S']['usd']:,.0f}")
    if ctx.get("big_move_days"):
        v.append(f"S25 区间:近 5 日大动 {', '.join(ctx['big_move_days'])}")
    h = heat.get(t)
    if h:
        v.append(f"散户榜第 {h['rank']}(提及 {h['mentions']},前日 {h['prev']})")
    if t in notes:
        v.append(f"关注名单:{notes[t][:50]}")
    return v


# ------------------------------------------------------------------ paper ----
def paper_section(con, store, day: dt.date, prev: dt.date | None, spy_ret: float | None) -> dict:
    out: dict = {"books": [], "orders": [], "positions": [], "flags": []}
    nav = con.execute("SELECT book, equity_usd, cash_usd, n_positions FROM agent_book_nav WHERE as_of = ? ORDER BY book", [day]).fetchall()
    nav_prev = dict(con.execute("SELECT book, equity_usd FROM agent_book_nav WHERE as_of = ?", [prev]).fetchall()) if prev else {}
    alloc = dict(con.execute("SELECT book, alloc_usd FROM agent_books").fetchall())
    for b, eq, cash, n in nav:
        pe = nav_prev.get(b)
        out["books"].append({"book": b, "equity": eq, "cash": cash, "n": n, "day_ret": (eq / pe - 1) * 100 if pe else None,
                             "since_start": (eq / alloc[b] - 1) * 100 if alloc.get(b) else None,
                             "vs_spy": ((eq / pe - 1) * 100 - spy_ret) if pe and spy_ret is not None else None})
    orders = con.execute("""SELECT book, ticker, side, qty, order_type, tif, limit_price, ref_close, reason, status, filled_qty,
                                   filled_avg_px, filled_at, model_px FROM agent_orders
                            WHERE dry_run = FALSE AND (as_of = ? OR CAST(filled_at AS DATE) = ?) ORDER BY book, side, ticker""", [prev, day]).fetchall()
    # the lots held at the day's close (same as status = 'open' today; a rerun of an earlier day sees that day's holdings)
    lots = con.execute("""SELECT book, ticker, qty, entry_day, entry_px, entry_model_px, hold_until FROM agent_lots
                          WHERE entry_day <= ? AND (status = 'open' OR exit_day > ?)""", [day, day]).fetchall()
    tickers = sorted({o[1] for o in orders} | {l[1] for l in lots})
    hist = bars(store, tickers, day - dt.timedelta(days=120), day)
    spy = store.index_series("SPY", "adj_close")
    spy.index = spy.index.date
    ctxs = {t: day_context(hist, spy, t, day) for t in tickers}
    news = headlines(tickers, day)

    for b, t, side, qty, otype, tif, lim, ref, reason, status, fq, fpx, fat, mpx in orders:
        c = ctxs.get(t, {})
        fq = float(fq or 0)
        row = {"book": b, "ticker": t, "side": side, "qty": int(qty), "type": otype, "tif": tif, "limit": lim, "ref_close": ref, "reason": reason,
               "status": status, "filled_qty": fq, "fill_px": fpx, "model_px": mpx, "open": c.get("open"), "close": c.get("close"),
               "gap_pct": ((fpx / mpx - 1) * 100 * (1 if side == "buy" else -1)) if fq and fpx and mpx else None,
               "day1_pct": ((c["close"] / fpx - 1) * 100 * (1 if side == "buy" else -1)) if fq and fpx and c.get("close") else None,
               "tags": []}
        if fq == 0 and status in ("expired", "canceled", "rejected"):
            row["tags"].append("未成交")
            if otype == "limit" and c.get("open") and lim and c["open"] <= lim:
                row["tags"].append("开盘价在限价内仍未成交(模拟器按卖一)")
        elif fq and fq < qty:
            row["tags"].append(f"部分成交 {int(fq)}/{int(qty)}")
        if row["gap_pct"] is not None and abs(row["gap_pct"]) >= 0.5:
            row["tags"].append(f"执行偏差 {row['gap_pct']:+.2f}%")
        if side == "buy" and otype == "market" and str(day) >= "2026-09-29":     # orders planned from 2026-09-28 on are limits (S36c)
            row["tags"].append("入场用了市价单(S36c:所有入场都应是限价单)")
        if reason == "entry_retry":
            row["tags"].append("重试单(上次没成交,晚一天补买;单独统计)")
        out["orders"].append(row)
    for o in submit_errors(prev):                         # refused at the POST: never in agent_orders
        out["orders"].append({"book": o.get("book"), "ticker": o.get("ticker"), "side": o.get("side"), "qty": int(o.get("qty") or 0),
                              "type": o.get("order_type"), "tif": o.get("tif"), "limit": o.get("limit_price"), "ref_close": o.get("ref_close"),
                              "reason": o.get("reason"), "status": str(o.get("status")), "filled_qty": 0.0, "fill_px": None, "model_px": None,
                              "open": None, "close": None, "gap_pct": None, "day1_pct": None, "tags": ["提交时被拒"]})

    for b, t, qty, eday, epx, empx, hu in lots:
        c = ctxs.get(t, {})
        row = {"book": b, "ticker": t, "qty": int(qty), "entry_day": str(eday), "entry_px": epx, "close": c.get("close"),
               "day_ret": c.get("ret"), "abn": c.get("abn"), "since_entry": (c["close"] / epx - 1) * 100 if c.get("close") and epx else None,
               "hold_until": str(hu) if hu else None, "news": news.get(t, []), "tags": []}
        if row["day_ret"] is not None and abs(row["day_ret"]) >= 5:
            row["tags"].append(f"当日 {row['day_ret']:+.1f}%" + ("(有新闻)" if news.get(t) else "(无新闻)"))
        if row["since_entry"] is not None and row["since_entry"] <= -10:
            row["tags"].append("入场以来 ≤ −10%(v1 无止损,S24 已证明止损不帮忙;只记录)")
        out["positions"].append(row)
    out["positions"].sort(key=lambda r: (r["day_ret"] if r["day_ret"] is not None else 0))

    out["closed"] = closed_lots(con, store, day)

    # rule checks, in three classes (see SIM_RULES)
    def flag(rule: str, text: str, cls: str) -> None:
        out["flags"].append({"rule": rule, "text": text, "cls": cls, "date": str(day), "key": rule, **({"info": True} if cls == "info" else {})})

    rejected = [o for o in out["orders"] if o["status"] == "rejected" or "提交时被拒" in o["tags"]]
    if rejected:
        flag("paper_rejected", f"{len(rejected)} 张单被券商拒绝:" + ", ".join(o["ticker"] + ("(提交时被拒)" if "提交时被拒" in o["tags"] else "")
                                                                          for o in rejected), "action")
    rec = reconcile_msgs(day)
    if rec:
        flag("paper_reconcile", f"账本对券商持仓不一致 {len(rec)} 条:" + ";".join(str(m) for m in rec[:3]), "action")
    tail = [p for p in out["positions"] if p["day_ret"] is not None and p["day_ret"] <= LOT_DAY_TAIL_PCT]
    if tail:
        flag("paper_lot_tail", f"单个持仓当日跌幅超过股票池 1% 分位({LOT_DAY_TAIL_PCT:.1f}%):"
             + ", ".join(f"{p['book']} {p['ticker']} {p['day_ret']:+.1f}%" + ("(有新闻)" if p["news"] else "(无新闻)") for p in tail), "action")
    mkt_in = [o for o in out["orders"] if any(t.startswith("入场用了市价单") for t in o["tags"])]
    if mkt_in:
        flag("paper_market_entry", f"{len(mkt_in)} 张市价入场单(规则 S36c:限价 = 前收盘 + 3%)", "action")
    unfilled = [o for o in out["orders"] if "未成交" in o["tags"] and o["status"] != "rejected"]
    if unfilled:
        flag("paper_unfilled", f"{len(unfilled)} 张单未成交:{', '.join(o['ticker'] for o in unfilled)}", "sim")
    gaps = [o["gap_pct"] for o in out["orders"] if o["gap_pct"] is not None]
    if gaps and np.mean(np.abs(gaps)) >= 0.5:
        flag("paper_exec_gap", f"执行偏差绝对值平均 {np.mean(np.abs(gaps)):.2f}%/边、带符号平均 {np.mean(gaps):+.2f}%/边({len(gaps)} 笔),超过 0.5%", "sim")
    big = [p for p in out["positions"] if p["day_ret"] is not None and abs(p["day_ret"]) >= 5 and p not in tail]
    if big:
        flag("paper_big_move", "持仓大动:" + ", ".join(f"{p['ticker']} {p['day_ret']:+.1f}%" for p in big[:6]), "info")
    return out


def submit_errors(prev: dt.date | None) -> list[dict]:
    """Orders the broker refused at submission in the previous session's 16:10 run (the one that planned the
    orders filled today). agent.execute keeps only accepted POSTs in agent_orders; a refused one is only in
    exec_<date>.json, with status 'error: ...'."""
    if prev is None:
        return []
    try:
        j = json.load(open(os.path.join(OUT_DIR, f"exec_{prev}.json")))
    except Exception:
        return []
    return [o for o in (j.get("orders") or []) if str(o.get("status") or "").startswith("error")]


def reconcile_msgs(day: dt.date) -> list:
    """Lots-vs-broker messages from the day's sync run (agent.execute --sync-only at 13:25), else the 16:10 run."""
    for name in (f"sync_{day}.json", f"exec_{day}.json"):
        try:
            j = json.load(open(os.path.join(OUT_DIR, name)))
        except Exception:
            continue
        r = j.get("reconcile") or []
        return list(r) if isinstance(r, (list, tuple)) else [r]
    return []


def auction_cross(pairs: set) -> dict:
    """Opening-cross price per (ticker, day) from auctions.db, read-only (agent.auction_basis loads it before the review)."""
    if not pairs:
        return {}
    try:
        a = duckdb.connect(str(AUCTIONS_DB), read_only=True)
        rows = a.execute("SELECT ticker, day, open_px FROM auctions WHERE ticker IN ({})".format(",".join("?" * len({t for t, _ in pairs}))),
                         sorted({t for t, _ in pairs})).fetchall()
        a.close()
    except Exception:
        return {}
    return {(t, pd.Timestamp(d).date()): float(p) for t, d, p in rows if p is not None and (t, pd.Timestamp(d).date()) in pairs}


def closed_lots(con, store, day: dt.date) -> list[dict]:
    """Lots closed today: fills against the opening cross, holding sessions, return, SPY and IWM open to open over the
    same window, and the rule replay of the same lot (the engine's fills: the adjusted open on the entry and exit days),
    which is there to catch implementation errors, not to judge the rule."""
    lots = con.execute("""SELECT book, ticker, qty, entry_day, entry_px, exit_day, exit_px, ret_pct FROM agent_lots
                          WHERE status = 'closed' AND exit_day = ? ORDER BY book, ticker""", [day]).fetchall()
    if not lots:
        return []
    cross = auction_cross({(t, e) for _, t, _, e, *_ in lots} | {(t, x) for _, t, _, _, _, x, *_ in lots})
    e0 = min(l[3] for l in lots)
    hist = bars(store, sorted({l[1] for l in lots}), e0, day)
    adj_open = {(t, d): o * a / c for t, d, o, a, c in zip(hist["ticker"], hist["trade_date"], hist["open"], hist["adj_close"], hist["close"]) if o and c}
    idx = store.con.execute("""SELECT symbol, trade_date, open * adj_close / close FROM index_daily WHERE symbol IN ('SPY', 'IWM')
                               AND trade_date BETWEEN ? AND ?""", [e0, day]).fetchall()
    io = {(s, d): float(v) for s, d, v in idx if v is not None}
    sess = [d for d in sessions(store) if e0 <= d <= day]

    def oo(key: str, e: dt.date, x: dt.date, px: dict):
        return (px[(key, x)] / px[(key, e)] - 1) * 100 if (key, e) in px and (key, x) in px else None

    out = []
    for b, t, qty, e, epx, x, xpx, ret in lots:
        ce, cx = cross.get((t, e)), cross.get((t, x))
        r = ret if ret is not None else ((xpx / epx - 1) * 100 if epx and xpx else None)
        out.append({"book": b, "ticker": t, "qty": qty, "entry_day": str(e), "entry_px": epx, "exit_px": xpx,
                    "entry_vs_cross_pct": (epx / ce - 1) * 100 if epx and ce else None,
                    "exit_vs_cross_pct": (xpx / cx - 1) * 100 if xpx and cx else None,
                    "hold_sessions": sum(1 for d in sess if e < d <= x), "ret_pct": r,
                    "spy_pct": oo("SPY", e, x, io), "iwm_pct": oo("IWM", e, x, io),
                    "replay_pct": oo(t, e, x, adj_open)})
    return out


# ------------------------------------------------------------------ real ----
def _deal_keys(x: dict) -> set[str]:
    """A deal's identities: deal_id (reviews from 2026-09-29 on) and time|code (older review files have no deal_id)."""
    k = {f"{str(x.get('ts'))[:19]}|{x.get('code')}"}
    if x.get("deal_id"):
        k.add(str(x["deal_id"]))
    return k


def earlier_reviews(day: dt.date) -> tuple[set[str], dt.date | None, dt.date | None]:
    """Deal keys shown by the review files dated before `day`, the latest such date and the first one."""
    keys: set[str] = set()
    dates = []
    for p in sorted(glob.glob(os.path.join(OUT_DIR, "review_????-??-??.json"))):
        d = dt.date.fromisoformat(os.path.basename(p)[7:17])
        if d >= day:
            continue
        try:
            j = json.load(open(p))
        except Exception:
            continue
        dates.append(d)
        for x in (j.get("real") or {}).get("deals", []):
            keys |= _deal_keys(x)
    return keys, (dates[-1] if dates else None), (dates[0] if dates else None)


def select_deals(dl: pd.DataFrame, day: dt.date, prev: dt.date | None, reviewed: set[str],
                 last_review: dt.date | None, first_review: dt.date | None) -> pd.DataFrame:
    """The deals this review covers: dated after the previous review day up to `day` (a Saturday expiry
    settlement lands in Monday's review), plus deals dated earlier — from the first review on — that no
    earlier review showed (a fill that reached acct_deals a day late). Before 2026-09-29 only deals dated
    `day` were read, and the afternoon's fills and weekend settlements were never reviewed (S48)."""
    if dl.empty:
        return dl
    start = last_review or prev or (day - dt.timedelta(days=1))
    seen = dl.apply(lambda r: bool(_deal_keys({"ts": r["ts"], "code": r["code"], "deal_id": r["deal_id"]}) & reviewed), axis=1)
    window = (dl["day"] > start) & (dl["day"] <= day)
    late = (dl["day"] <= start) & (dl["day"] >= (first_review or start)) if first_review else pd.Series(False, index=dl.index)
    return dl[(window | late) & ~seen]


def fifo_round_trips(dl: pd.DataFrame) -> list[dict]:
    """Every SELL matched first-in-first-out against the earlier BUYs of the same code (a price-0 SELL of an
    option is the broker's expiry settlement)."""
    out = []
    for code, g in dl.sort_values("ts").groupby("code", sort=False):
        q: list[list] = []                                   # [qty left, price, ts]
        for _, d in g.iterrows():
            if d["side"] == "BUY":
                q.append([float(d["qty"]), float(d["price"]), d["ts"]])
                continue
            if d["side"] != "SELL":
                continue
            left, cost, first = float(d["qty"]), 0.0, None
            while left > 1e-9 and q:
                take = min(left, q[0][0])
                cost += take * q[0][1]
                first = first if first is not None else q[0][2]
                q[0][0] -= take
                left -= take
                if q[0][0] <= 1e-9:
                    q.pop(0)
            matched = float(d["qty"]) - left
            if matched > 0:
                out.append({"code": code, "sell_ts": d["ts"], "sell_id": str(d["deal_id"]), "qty": matched, "entry_ts": first,
                            "avg_cost": cost / matched, "exit_px": float(d["price"])})
    return out


def dte_bucket(dte: int) -> str:
    return "0–1 天" if dte <= 1 else "2–10 天" if dte <= 10 else "> 10 天"


def options_summary(dl: pd.DataFrame, day: dt.date, positions: list[tuple], days: int = 30) -> list[dict]:
    """Read-only: option positions opened in the last `days` days, by days to expiry at the first buy.
    Per bucket: positions, premium paid, premium lost to worthless expiry, realized net P&L, still open."""
    opt = dl[(dl["day"] <= day) & dl["code"].map(lambda c: parse_code(c)["is_option"])]
    if opt.empty:
        return []
    marks = {p[0]: float(p[5] or 0) for p in positions}                 # code -> market value today
    agg: dict[str, dict] = {}
    for code, g in opt.sort_values("ts").groupby("code"):
        buys = g[g["side"] == "BUY"]
        if buys.empty or buys["day"].iloc[0] < day - dt.timedelta(days=days):
            continue
        p = parse_code(code)
        b = agg.setdefault(dte_bucket((p["expiry"] - buys["day"].iloc[0]).days),
                           {"n": 0, "premium": 0.0, "expired_worthless": 0.0, "realized": 0.0, "n_open": 0, "open_mark_pnl": 0.0})
        b["n"] += 1
        b["premium"] += float((buys["qty"] * buys["price"]).sum()) * 100
        closed = 0.0
        for rt in fifo_round_trips(g):
            closed += rt["qty"]
            b["realized"] += (rt["exit_px"] - rt["avg_cost"]) * rt["qty"] * 100
            if rt["exit_px"] == 0:
                b["expired_worthless"] += rt["avg_cost"] * rt["qty"] * 100
        left = float(buys["qty"].sum()) - closed
        if left > 1e-9:
            b["n_open"] += 1
            open_cost = float((buys["qty"] * buys["price"]).sum()) * 100 * left / float(buys["qty"].sum())
            if code in marks:
                b["open_mark_pnl"] += marks[code] - open_cost
    order = ["0–1 天", "2–10 天", "> 10 天"]
    return [{"bucket": k, **agg[k]} for k in order if k in agg]


def real_section(con, store, day: dt.date, prev: dt.date | None, spy_ret: float | None, ranks: dict, notes: dict) -> dict:
    out: dict = {"nav": {}, "deals": [], "round_trips": [], "positions": [], "flags": [], "options_30d": []}
    navs = con.execute("SELECT date, sum(total_assets), sum(cash) FROM acct_nav GROUP BY 1 ORDER BY 1").fetchall()
    nv = {d: (a, c) for d, a, c in navs}
    flows = dict(con.execute("SELECT date, sum(amount_usd) FROM acct_flows GROUP BY 1").fetchall())
    if day in nv:
        a, c = nv[day]
        pa = nv.get(prev, (None, None))[0]
        pnl = (a - pa - flows.get(day, 0.0)) if pa else None
        out["nav"] = {"total": a, "cash": c, "pnl": pnl, "ret": (pnl / pa * 100) if pnl is not None and pa else None,
                      "vs_spy": (pnl / pa * 100 - spy_ret) if pnl is not None and pa and spy_ret is not None else None}
    deals = con.execute("SELECT deal_id, ts, code, name, side, qty, price, is_option, underlying FROM acct_deals ORDER BY ts").fetchall()
    dl = pd.DataFrame(deals, columns=["deal_id", "ts", "code", "name", "side", "qty", "price", "is_option", "underlying"])
    dl["day"] = pd.to_datetime(dl["ts"]).dt.date
    reviewed, last_review, first_review = earlier_reviews(day)
    today = select_deals(dl, day, prev, reviewed, last_review, first_review)
    out["window"] = {"after": str(last_review or prev), "through": str(day), "late": int((today["day"] <= (last_review or prev or day)).sum()) if len(today) else 0}
    pos = con.execute("""SELECT code, name, qty, cost_price, price, market_val, pl_val, is_option, underlying FROM acct_positions
                         WHERE date = ? ORDER BY market_val DESC""", [day]).fetchall()
    tickers = sorted({parse_code(c)["ticker"] for c in list(today["code"]) + [p[0] for p in pos]})
    d0 = min([day] + list(today["day"]))
    hist = bars(store, tickers, d0 - dt.timedelta(days=120), day)
    spy = store.index_series("SPY", "adj_close")
    spy.index = spy.index.date
    _ctx: dict = {}

    def ctx(t: str, d: dt.date) -> dict:                  # price context of the deal's own date (not the review day)
        if (t, d) not in _ctx:
            _ctx[(t, d)] = day_context(hist, spy, t, d)
        return _ctx[(t, d)]

    news: dict = {}
    for d, g in today.groupby("day"):
        for t, h in headlines(sorted({parse_code(c)["ticker"] for c in g["code"]}), d).items():
            news[(t, d)] = h
    pos_news = headlines(sorted({parse_code(p[0])["ticker"] for p in pos}), day)
    heat = retail(tickers, day)
    ins = insiders_30d(store, tickers, day)
    trips = {rt["sell_id"]: rt for rt in fifo_round_trips(dl)}

    for _, d in today.iterrows():
        p = parse_code(d["code"])
        t = p["ticker"]
        dd = d["day"]
        c = ctx(t, dd)
        row = {"deal_id": str(d["deal_id"]), "ts": str(d["ts"]), "day": str(dd), "code": d["code"], "name": d["name"], "ticker": t, "side": d["side"],
               "qty": float(d["qty"]), "price": float(d["price"]),
               "is_option": bool(p["is_option"]), "u_open": c.get("open"), "u_close": c.get("close"), "u_ret": c.get("ret"), "u_ret5": c.get("ret5"),
               "range_pos": range_pos(float(d["price"]), c) if not p["is_option"] else None,
               "after_pct": None, "system": system_view(t, c, ranks, ins, heat, notes), "news": news.get((t, dd), []), "tags": []}
        if not p["is_option"] and c.get("close"):
            row["after_pct"] = (c["close"] / row["price"] - 1) * 100 * (1 if d["side"] == "BUY" else -1)
        if p["is_option"]:
            dte = (p["expiry"] - dd).days
            spot = c.get("close")
            row.update({"expiry": str(p["expiry"]), "cp": p["cp"], "strike": p["strike"], "dte": dte,
                        "moneyness_pct": ((p["strike"] / spot - 1) * 100 * (1 if p["cp"] == "C" else -1)) if spot else None, "rv20": c.get("rv20")})
            if d["side"] == "SELL" and float(d["price"]) == 0 and dte <= 0:
                row["tags"].append("到期作废(券商结算记录)")
            if d["side"] == "BUY":
                if dte <= 0:
                    row["tags"].append("当日到期(0DTE):回本要求标的在剩余几小时反向走完整个权利金")
                elif dte <= 10:
                    row["tags"].append(f"买入时只剩 {dte} 天(theta 区)")
                if row["moneyness_pct"] is not None and row["moneyness_pct"] >= 10:
                    row["tags"].append(f"虚值 {row['moneyness_pct']:.0f}%(彩票结构)")
                row["tags"].append("S26:期权买方没有验证过的入场;IV 与 RV 的比较要看入场时的链")
        if d["side"] == "BUY" and c.get("big_move_days"):
            row["tags"].append("S25 区间内买入(近 5 日大动;全市场平均后续跑输)")
        if d["side"] == "BUY" and ins.get(t, {}).get("S", {}).get("usd", 0) >= 1e6:
            row["tags"].append(f"内部人 30 日净卖出 ${ins[t]['S']['usd']:,.0f}")
        if d["side"] == "BUY" and ins.get(t, {}).get("P"):
            row["tags"].append("内部人 30 日有公开市场买入(与我们的短线线同向)")
        if row["range_pos"] is not None:
            if d["side"] == "BUY" and row["range_pos"] >= 80:
                row["tags"].append(f"买在当日区间 {row['range_pos']:.0f}% 位置(接近高点)")
            if d["side"] == "SELL" and row["range_pos"] <= 20:
                row["tags"].append(f"卖在当日区间 {row['range_pos']:.0f}% 位置(接近低点)")
        out["deals"].append(row)
        rt = trips.get(str(d["deal_id"])) if d["side"] == "SELL" else None
        if rt:
            q, avg, first = rt["qty"], rt["avg_cost"], rt["entry_ts"]
            mult = 100 if p["is_option"] else 1
            ret = (float(d["price"]) / avg - 1) * 100 if avg else None
            hold = (pd.Timestamp(d["ts"]) - pd.Timestamp(first)).days
            # the underlying over the same hold, for the leverage check (last bar on or before each date)
            g = hist[hist["ticker"] == t].set_index("trade_date")["adj_close"]
            u0, u1 = g[g.index <= pd.Timestamp(first).date()], g[g.index <= dd]
            u_ret = (float(u1.iloc[-1]) / float(u0.iloc[-1]) - 1) * 100 if not u0.empty and not u1.empty else None
            out["round_trips"].append({"code": d["code"], "ticker": t, "is_option": bool(p["is_option"]), "qty": q, "entry_ts": str(first),
                                       "exit_day": str(dd), "entry_px": avg, "exit_px": float(d["price"]), "ret_pct": ret, "pnl_usd": (float(d["price"]) - avg) * q * mult,
                                       "hold_days": hold, "underlying_ret_pct": u_ret,
                                       "lesson": (f"标的同期 {u_ret:+.1f}%,期权 {ret:+.0f}%:杠杆 {ret / u_ret:.1f}x" if p["is_option"] and u_ret and ret is not None and abs(u_ret) > 0.5 else "")})

    for code, name, qty, cost, px, mv, pl, is_opt, und in pos:
        p = parse_code(code)
        t = p["ticker"]
        c = ctx(t, day)
        total = out["nav"].get("total") or 0
        row = {"code": code, "name": name, "ticker": t, "qty": qty, "cost": cost, "price": px, "market_val": mv, "pl": pl,
               "pl_pct": (px / cost - 1) * 100 if cost and cost > 0 else None, "is_option": bool(is_opt), "u_ret": c.get("ret"),
               "weight_pct": (mv / total * 100) if total else None, "system": system_view(t, c, ranks, ins, heat, notes), "news": pos_news.get(t, []), "tags": []}
        if p["is_option"]:
            row["dte"] = (p["expiry"] - day).days
            if row["dte"] <= 7:
                row["tags"].append(f"到期 {row['dte']} 天")
        if row["weight_pct"] and row["weight_pct"] >= 40:
            row["tags"].append(f"占净值 {row['weight_pct']:.0f}%")
        out["positions"].append(row)

    # rule checks, dated by the deal's own day; one flag per rule, contract, side and day (two fills of one order = one lesson)
    def flag(rule: str, text: str, d: dt.date, key: str) -> None:
        out["flags"].append({"rule": rule, "text": text, "cls": "real", "date": str(d), "key": key})

    buys = today[today["side"] == "BUY"]
    for (code, dd), g in buys.groupby(["code", "day"]):
        if len(g) >= 2:
            flag("real_add_same_day", f"{parse_code(code)['ticker']} 同一合约当日买入 {len(g)} 次({', '.join(f'{p:.2f}' for p in g['price'])})", dd, code)
    for dd, g in today.groupby("day"):
        if len(g) >= 5:
            flag("real_overtrading", f"{dd} 当日 {len(g)} 笔成交", dd, "all")
    seen: dict = {}
    for d in out["deals"]:
        for tg in d["tags"]:
            key = ("real_buy_after_jump" if tg.startswith("S25") else "real_0dte" if tg.startswith("当日到期") else "real_short_dte" if "只剩" in tg else "real_otm_lottery" if "虚值" in tg
                   else "real_buy_high" if tg.startswith("买在") else "real_sell_low" if tg.startswith("卖在") else "real_vs_insiders" if "净卖出" in tg else None)
            if key:
                k = (key, d["code"], d["side"], d["day"])
                seen.setdefault(k, [d, tg, 0])[2] += 1
    for (key, code, side, dd), (d, tg, n) in seen.items():
        flag(key, f"{d['ticker']} {side}" + (f" ×{n}" if n > 1 else "") + f"({dd}):{tg}", dt.date.fromisoformat(dd), f"{code}|{side}")
    conc = [p for p in out["positions"] if p["weight_pct"] and p["weight_pct"] >= 40]
    if conc:
        out["flags"].append({"rule": "real_concentration", "text": "单一持仓占比 ≥ 40%:" + ", ".join(f"{p['ticker']} {p['weight_pct']:.0f}%" for p in conc),
                             "info": True, "cls": "info", "date": str(day), "key": "conc"})
    out["options_30d"] = options_summary(dl, day, pos)
    return out


# ------------------------------------------------------------------ lessons ----
def recorded(f: dict) -> bool:
    """Lessons are the events needing action and the real account's behaviour; simulator behaviour and info are not."""
    return not f.get("info") and f.get("cls", "sim" if f["rule"] in SIM_RULES else "action") in ("action", "real")


def record_lessons(day: dt.date, flags: list[dict]) -> dict[str, int]:
    """Write this review's lessons, dated by the event (a deal's own day), and return the 30-day count per rule.

    Rerun-safe: the rows this review wrote before are replaced (rows from before 2026-09-29 carry no
    'review' and are matched by their date). A lesson another review already recorded (same date, rule
    and key) is not written twice."""
    rows = []
    if os.path.exists(LESSONS):
        with open(LESSONS) as f:
            rows = [json.loads(l) for l in f if l.strip()]
    rows = [r for r in rows if r.get("review", r.get("date")) != day.isoformat()]
    have = {(r["date"], r["rule"], r.get("key") or r.get("text")): r for r in rows}
    for f in flags:
        if not recorded(f):
            continue
        r = {"date": f.get("date") or day.isoformat(), "rule": f["rule"], "text": f["text"], "review": day.isoformat(), "key": f.get("key") or f["text"]}
        old = have.get((r["date"], r["rule"], r["key"]))
        if old is not None:
            # a later review recorded it first (reruns out of order): the lesson moves to this earlier review,
            # which is the one that shows the deal from now on, so a rerun of the later day does not drop it
            if old.get("review", old["date"]) > day.isoformat():
                old["review"] = day.isoformat()
            continue
        have[(r["date"], r["rule"], r["key"])] = r
        rows.append(r)
    rows.sort(key=lambda r: (r["date"], r.get("review", r["date"])))
    with open(LESSONS, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    cutoff = (day - dt.timedelta(days=30)).isoformat()
    counts: dict[str, int] = {}
    for r in rows:
        if r["date"] >= cutoff:
            counts[r["rule"]] = counts.get(r["rule"], 0) + 1
    return counts


RULE_NAMES = {"paper_rejected": "模拟盘订单被拒", "paper_reconcile": "账本对券商持仓不一致", "paper_lot_tail": "单个持仓当日跌幅超过股票池 1% 分位",
              "paper_unfilled": "模拟盘未成交", "paper_exec_gap": "执行偏差 > 0.5%", "paper_insider_limit": "内部人限价入场(旧规则,2026-09-28 作废)", "paper_market_entry": "市价入场", "paper_big_move": "持仓大动",
              "real_overtrading": "实盘当日 ≥ 5 笔", "real_buy_after_jump": "实盘大动后追买(S25)", "real_short_dte": "实盘买临期期权", "real_0dte": "实盘买当日到期期权",
              "real_otm_lottery": "实盘买深虚值", "real_buy_high": "实盘买在日高附近", "real_sell_low": "实盘卖在日低附近",
              "real_vs_insiders": "实盘逆内部人卖出买入", "real_concentration": "实盘单一持仓 ≥ 40%", "real_add_same_day": "实盘同一合约当日加仓"}


def balder_block() -> str:
    """Trades forwarded from Balder's X posts (agent/balder_log.py), scored 1/5/20 sessions vs SPY. Private site only."""
    try:
        b = json.load(open(os.path.join(OUT_DIR, "balder_latest.json")))
    except Exception:
        return "<p class='muted'>还没有记录。把 Balder 的帖子以文本分享到 iCloud Drive 的 Balder 文件夹,收盘后自动解析记分。</p>"
    if not b.get("n"):
        return "<p class='muted'>还没有记录(把帖子文本放进 iCloud Drive/Balder,每天 13:25 解析)。</p>"
    score = ""
    for book, s in b.get("by_book", {}).items():
        cells = "".join(f"<td>{v['n']}</td><td>{pct(v['mean'])}</td><td>{pct(v['mean_abn'])}</td><td>{'' if v['hit'] is None else format(v['hit'], '.0%')}</td>" for v in (s["h1"], s["h5"], s["h20"]))
        score += f"<tr><td>{esc(book)}</td>{cells}</tr>"
    rows = "".join(f"<tr><td>{esc(r['posted'])}</td><td>{esc(r['book'])}</td><td><b>{esc(r['ticker'])}</b></td><td>{esc(r['action'])}</td>"
                   f"<td>{'' if r.get('price') is None else format(r['price'], '.2f')}</td><td class='muted'>{esc(r.get('strategy') or '')}</td>"
                   f"<td>{pct(r.get('ret1'))}</td><td>{pct(r.get('ret5'))}</td><td>{pct(r.get('ret20'))}</td><td class='muted'>{esc(r.get('note') or '')}</td></tr>"
                   for r in b.get("recent", []))
    return (f"<p class='muted'>来源:Balder 在 X 的订阅帖子,由用户转存;只做记分对照,不进任何信号,不上公开站。开仓从帖子次日开盘起算收益。共 {b['n']} 条。</p>"
            f"<div class='tbl'><table><tr><th>书</th><th>1日 n</th><th>均值</th><th>超额</th><th>胜率</th><th>5日 n</th><th>均值</th><th>超额</th><th>胜率</th><th>20日 n</th><th>均值</th><th>超额</th><th>胜率</th></tr>{score}</table></div>"
            f"<div class='tbl'><table><tr><th>日期</th><th>书</th><th>代码</th><th>动作</th><th>价格</th><th>策略</th><th>1日</th><th>5日</th><th>20日</th><th>摘要</th></tr>{rows}</table></div>")


def auction_block(rep: dict) -> str:
    """Auction-basis NAV next to the simulator's, and the shadow record of unfilled entries (agent/auction_basis.py)."""
    try:
        a = json.load(open(os.path.join(OUT_DIR, "auction_basis.json")))
    except Exception:
        return "<p class='muted'>还没有竞价口径数据(收盘后生成)</p>"
    rows = "".join(f"<tr><td>{esc(b)}</td><td>{usd(v['equity_sim'])}</td><td>{usd(v['equity_auction'])}</td><td>{v['adj_cum']:+,.0f}</td><td>{v['n_fills']}</td></tr>"
                   for b, v in a.get("books", {}).items())
    f = a.get("fills", {})
    gap = f"成交价平均比开盘竞价价贵 {f['mean_gap_pct']:+.2f}%/边(中位 {f['median_gap_pct']:+.2f}%,{f['with_cross']} 笔)。" if f.get("mean_gap_pct") is not None else ""
    ms = a.get("missed", [])
    mrows = "".join(f"<tr><td><b>{esc(m['ticker'])}</b></td><td>{esc(m['entry_day'])}</td><td>{'—' if m['entry_cross'] is None else format(m['entry_cross'], '.2f')}</td>"
                    f"<td>{esc(m['exit_day'] or '—')}</td><td>{esc(m['status'])}</td><td>{pct(m['ret_pct'] if m['ret_pct'] is not None else m.get('mark_pct'))}</td></tr>" for m in ms)
    sm = a.get("missed_summary", {})
    return (f"<p class='muted'>模拟器按开盘后的卖一成交;真实账户的开盘单按开盘竞价价成交(S27b)。竞价口径 = 每笔成交按当天开盘竞价价重新记账。{gap}"
            "评估点按竞价口径判断,模拟器口径作为保守下限。</p>"
            f"<div class='tbl'><table><tr><th>书</th><th>模拟器口径</th><th>竞价口径</th><th>差额 $</th><th>成交笔数</th></tr>{rows}</table></div>"
            + (f"<h3>错过的交易(模拟器没成交,按开盘竞价价虚拟入场,5 天后虚拟卖出;不计入净值)</h3>"
               f"<p class='muted'>{sm.get('n', 0)} 笔,已结束 {sm.get('closed', 0)} 笔" + (f",平均 {sm['mean_ret_pct']:+.2f}%,超额 {sm['mean_abn_pct']:+.2f}%" if sm.get("mean_ret_pct") is not None else "") + ";未结束的按最新收盘价估值。</p>"
               f"<div class='tbl'><table><tr><th>股票</th><th>虚拟入场日</th><th>竞价价</th><th>虚拟卖出日</th><th>状态</th><th>收益</th></tr>{mrows}</table></div>" if ms else ""))


# ------------------------------------------------------------------ render ----
def flag_classes(rep: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"action": [], "real": [], "sim": [], "info": []}
    for f in rep["paper"]["flags"] + rep["real"]["flags"]:
        c = "info" if f.get("info") else f.get("cls") or ("sim" if f["rule"] in SIM_RULES else "action")
        out[c].append(f)
    return out


def notify_text(rep: dict) -> str:
    """The notification leads with the number of events needing action, then the first one and the real account's day."""
    fc = flag_classes(rep)
    head = f"需要处理 {len(fc['action'])} 条" + (f":{fc['action'][0]['text'][:60]}" if fc["action"] else "")
    parts = [head]
    n = rep["real"]["nav"]
    if n and n.get("pnl") is not None:
        parts.append(f"实盘当日 {n['pnl']:+,.0f}")
    if fc["real"]:
        parts.append(f"实盘行为 {len(fc['real'])} 条")
    parts += [f"{b['book']} {b['day_ret']:+.2f}%" for b in rep["paper"]["books"] if b.get("day_ret") is not None]
    return " · ".join(parts)[:180]


def summary_lines(rep: dict) -> list[str]:
    L = []
    m = rep["market"]
    L.append(f"SPY {m['spy']['ret']:+.2f}% · IWM {m['iwm']['ret']:+.2f}% · VIX {m['vix']['close']:.1f}" if m["spy"].get("ret") is not None else "市场数据缺失")
    for b in rep["paper"]["books"]:
        L.append(f"模拟盘 {b['book']}:{usd(b['equity'])}({b['day_ret']:+.2f}% 当日,{b['since_start']:+.2f}% 自起始)" if b["day_ret"] is not None
                 else f"模拟盘 {b['book']}:{usd(b['equity'])}({b['since_start']:+.2f}% 自起始)")
    o = rep["paper"]["orders"]
    if o:
        filled = [x for x in o if x["filled_qty"]]
        L.append(f"模拟盘订单 {len(o)} 张,成交 {len(filled)},未成交 {sum(1 for x in o if '未成交' in x['tags'])}" +
                 (f",平均偏差 {np.mean([x['gap_pct'] for x in filled if x['gap_pct'] is not None]):+.2f}%/边" if any(x['gap_pct'] is not None for x in filled) else ""))
    r = rep["real"]
    if r["nav"]:
        n = r["nav"]
        L.append(f"实盘 {usd(n['total'])}" + (f",当日 {n['pnl']:+,.0f}({n['ret']:+.2f}%" + (f",对 SPY {n['vs_spy']:+.2f}%" if n.get("vs_spy") is not None else "") + ")"
                                             if n.get("pnl") is not None else ""))
    cl = rep["paper"].get("closed") or []
    if cl:
        L.append(f"模拟盘平仓 {len(cl)} 笔:" + "; ".join(f"{x['book']} {x['ticker']} " + (f"{x['ret_pct']:+.1f}%" if x.get("ret_pct") is not None else "—")
                                                        for x in cl))
    if r["deals"]:
        L.append(f"实盘成交 {len(r['deals'])} 笔(上次复盘之后)" + (f",平仓 {len(r['round_trips'])} 笔:" + "; ".join(f"{x['ticker']} {x['ret_pct']:+.0f}%" for x in r["round_trips"]) if r["round_trips"] else ""))
    else:
        L.append("实盘上次复盘之后无新成交")
    fc = flag_classes(rep)
    L.append(f"需要处理 {len(fc['action'])} 条 · 实盘行为 {len(fc['real'])} 条 · 模拟器已知现象 {len(fc['sim'])} 条(不计入教训)")
    return L


def render_html(rep: dict) -> str:
    d = rep["date"]
    m = rep["market"]
    cards = f"""<div class='cards'>
<div class='card'><div class='k'>SPY</div><div class='v'>{pct(m['spy'].get('ret'))}</div><div class='s'>{m['spy'].get('close') or '—'}</div></div>
<div class='card'><div class='k'>IWM</div><div class='v'>{pct(m['iwm'].get('ret'))}</div><div class='s'>小盘</div></div>
<div class='card'><div class='k'>VIX</div><div class='v'>{(m['vix'].get('close') or 0):.1f}</div><div class='s'>{pct(m['vix'].get('ret'))}</div></div>"""
    for b in rep["paper"]["books"]:
        cards += f"<div class='card'><div class='k'>模拟盘 {esc(b['book'])}</div><div class='v'>{pct(b['day_ret'])}</div><div class='s'>{usd(b['equity'])} · 自起始 {pct(b['since_start'])} · {b['n']} 仓</div></div>"
    n = rep["real"]["nav"]
    if n:
        cards += f"<div class='card'><div class='k'>实盘 moomoo</div><div class='v'>{pct(n.get('ret'))}</div><div class='s'>{usd(n['total'])} · 当日 {n['pnl']:+,.0f} · 现金 {usd(n['cash'])}</div></div>" if n.get("pnl") is not None \
            else f"<div class='card'><div class='k'>实盘 moomoo</div><div class='v'>{usd(n['total'])}</div><div class='s'>现金 {usd(n['cash'])}</div></div>"
    cards += "</div>"

    def tags(ts):
        return " ".join(f"<span class='flag'>{esc(t)}</span>" for t in ts)

    # paper orders
    po = rep["paper"]["orders"]
    porows = "".join(f"<tr><td>{esc(o['book'])}</td><td><b>{esc(o['ticker'])}</b></td><td>{'买' if o['side']=='buy' else '卖'} {o['qty']}</td>"
                     f"<td>{esc(o['type'])}{(' ' + str(o['limit'])) if o['limit'] else ''}</td><td>{esc(o['status'])}</td>"
                     f"<td>{(f'{int(o['filled_qty'])} @ {o['fill_px']:.2f}') if o['filled_qty'] else '—'}</td><td>{(f'{o['model_px']:.2f}') if o['model_px'] else '—'}</td>"
                     f"<td>{pct(o['gap_pct'])}</td><td>{pct(o['day1_pct'])}</td><td class='muted'>{esc(o['reason'])} {tags(o['tags'])}</td></tr>" for o in po)
    paper_orders = (f"<div class='tbl'><table><tr><th>书</th><th>标的</th><th>方向</th><th>单型</th><th>状态</th><th>成交</th><th>模型价(开盘)</th><th>对开盘偏差<br><span class='muted'>模拟器逐单撮合</span></th><th>首日</th><th>原因 / 标记</th></tr>{porows}</table>"
                    if po else "<p class='muted'>今日无订单</p>")
    pp = rep["paper"]["positions"]
    movers = [p for p in pp if p["day_ret"] is not None]
    show = movers[:4] + movers[-4:] if len(movers) > 8 else movers
    prows = "".join(f"<tr><td>{esc(p['book'])}</td><td><b>{esc(p['ticker'])}</b></td><td>{pct(p['day_ret'])}</td><td>{pct(p['abn'])}</td><td>{pct(p['since_entry'])}</td>"
                    f"<td>{esc(p['entry_day'])}</td><td class='muted'>{tags(p['tags'])} {'<br>'.join(news_link(h) for h in p['news'][:2])}</td></tr>" for p in show)
    paper_pos = (f"<p class='muted'>{len(pp)} 个持仓;下面是当日涨跌最大的 {len(show)} 个。</p><table><tr><th>书</th><th>标的</th><th>当日</th><th>超额</th><th>入场以来</th><th>入场日</th><th>标记 / 新闻</th></tr>{prows}</table>"
                 if pp else "<p class='muted'>无持仓</p>")

    # real
    r = rep["real"]
    drows = ""
    for x in r["deals"]:
        det = (f"{x['cp']} {x['strike']:g} 到期 {x['expiry']}({x['dte']} 天,虚值 {x['moneyness_pct']:+.0f}%,RV20 {x['rv20']:.0f}%)" if x["is_option"] and x.get("moneyness_pct") is not None
               else f"当日区间位置 {x['range_pos']:.0f}%" if x.get("range_pos") is not None else "")
        drows += (f"<tr><td>{esc(x['ts'][5:16])}</td><td><b>{esc(x['ticker'])}</b><br><span class='muted'>{esc(x['name'])}</span></td><td>{esc(x['side'])} {x['qty']:g} @ {x['price']:.2f}</td>"
                  f"<td>{pct(x['u_ret'])}<br><span class='muted'>5 日 {pct(x['u_ret5'])}</span></td><td>{pct(x['after_pct'])}</td><td class='muted'>{esc(det)}</td>"
                  f"<td class='muted'>{'<br>'.join(esc(s) for s in x['system'])}</td><td>{tags(x['tags'])}</td></tr>")
    w = r.get("window") or {}
    real_deals = (f"<p class='muted'>上次复盘({esc(w.get('after'))})之后到 {esc(w.get('through'))} 的成交,加上此前没有进过任何复盘的晚到成交({w.get('late', 0)} 笔);"
                  f"剩余天数和标的当日行情按每笔成交自己的日期算。</p>" if w else "") + (f"<table><tr><th>时间</th><th>标的</th><th>成交</th><th>标的当日</th><th>成交后到收盘</th><th>细节</th><th>我们的系统怎么看</th><th>标记</th></tr>{drows}</table>"
                  if r["deals"] else "<p class='muted'>没有新成交</p>")
    rt = "".join(f"<tr><td><b>{esc(x['ticker'])}</b> {esc(x['code'])}</td><td>{esc(x['entry_ts'][:10])} @ {x['entry_px']:.2f}</td><td>{x['exit_px']:.2f}</td>"
                 f"<td>{pct(x['ret_pct'], 1)}</td><td>{x['pnl_usd']:+,.0f}</td><td>{x['hold_days']} 天</td><td>{pct(x['underlying_ret_pct'], 1)}</td><td class='muted'>{esc(x['lesson'])}</td></tr>" for x in r["round_trips"])
    real_rt = f"<h3>本次平仓的完整交易</h3><table><tr><th>合约</th><th>入场</th><th>出场</th><th>收益</th><th>盈亏 $</th><th>持有</th><th>标的同期</th><th>读法</th></tr>{rt}</table>" if rt else ""
    prow = "".join(f"<tr><td><b>{esc(p['ticker'])}</b><br><span class='muted'>{esc(p['name'])}{(' · 到期 ' + str(p['dte']) + ' 天') if p.get('dte') is not None else ''}</span></td>"
                   f"<td>{p['qty']:g} @ {p['cost']:.2f}</td><td>{p['price']:.2f}</td><td>{pct(p['u_ret'])}</td><td>{pct(p['pl_pct'], 1)}<br><span class='muted'>{p['pl']:+,.0f}</span></td>"
                   f"<td>{(f'{p['weight_pct']:.0f}%') if p['weight_pct'] else '—'}</td><td class='muted'>{'<br>'.join(esc(s) for s in p['system'])}</td><td>{tags(p['tags'])}</td></tr>" for p in r["positions"])
    real_pos = (f"<table><tr><th>持仓</th><th>数量 @ 成本</th><th>现价</th><th>标的当日</th><th>浮盈</th><th>占比</th><th>我们的系统怎么看</th><th>标记</th></tr>{prow}</table>"
                if r["positions"] else "<p class='muted'>无持仓</p>")

    fc = flag_classes(rep)

    def flist(title: str, fs: list[dict], counted: bool, muted: bool = False) -> str:
        if not fs:
            return ""
        li = "".join(f"<li>{'<span class=muted>' if muted else ''}{esc(RULE_NAMES.get(f['rule'], f['rule']))}:{esc(f['text'])}"
                     f"{(' · 30 日内第 ' + str(rep['lessons_30d'].get(f['rule'], 1)) + ' 次') if counted else ''}{'</span>' if muted else ''}</li>" for f in fs)
        return f"<h3>{title}({len(fs)})</h3><ul>{li}</ul>"
    frows = (flist("需要处理", fc["action"], True) + flist("实盘行为", fc["real"], True)
             + flist("模拟器已知现象(S27 开盘后成交、S36 限价未成交;偏离监控计数,不计入教训)", fc["sim"], False, True)
             + flist("信息", fc["info"], False, True))
    cl = rep["paper"].get("closed") or []
    clrows = "".join(f"<tr><td>{esc(x['book'])}</td><td><b>{esc(x['ticker'])}</b></td><td>{esc(x['entry_day'])}</td><td>{pct(x['entry_vs_cross_pct'])}</td>"
                     f"<td>{pct(x['exit_vs_cross_pct'])}</td><td>{x['hold_sessions']}</td><td>{pct(x['ret_pct'])}</td><td>{pct(x['spy_pct'])}</td><td>{pct(x['iwm_pct'])}</td>"
                     f"<td>{pct(x['replay_pct'])}</td></tr>" for x in cl)
    paper_closed = (f"<p class='muted'>入场、出场对当天开盘竞价价(买入为正 = 比竞价贵;卖出为负 = 比竞价便宜)。SPY、IWM 和规则重放都按开盘价进出、同一窗口;"
                    f"规则重放用来查实现错误,不评价规则。单笔结果不构成对规则的判断(评估点见 agent/config.yaml)。</p>"
                    f"<div class='tbl'><table><tr><th>书</th><th>标的</th><th>入场日</th><th>入场 对 竞价</th><th>出场 对 竞价</th><th>持有(交易日)</th><th>收益</th><th>SPY 同期</th><th>IWM 同期</th><th>规则重放</th></tr>{clrows}</table></div>"
                    if cl else "<p class='muted'>今日没有平仓</p>")
    ob = r.get("options_30d") or []
    obrows = "".join(f"<tr><td>{esc(x['bucket'])}</td><td>{x['n']}</td><td>{usd(x['premium'])}</td><td>{usd(x['expired_worthless'])}</td><td>{x['realized']:+,.0f}</td>"
                     f"<td>{x['n_open']}{(' · 浮动 ' + format(x['open_mark_pnl'], '+,.0f')) if x['n_open'] else ''}</td></tr>" for x in ob)
    real_opts = (f"<h3>期权仓位按买入时剩余天数(近 30 天开仓,先进先出配对)</h3><p class='muted'>只读汇总;价格为 0 的卖出是券商的到期结算,计为到期作废。不含佣金和费用。</p>"
                 f"<table><tr><th>买入时剩余</th><th>仓位</th><th>权利金</th><th>到期作废</th><th>已实现净盈亏 $</th><th>未平</th></tr>{obrows}</table>" if ob else "")
    lessons = "".join(f"<tr><td>{esc(RULE_NAMES.get(k, k))}</td><td>{v}</td></tr>" for k, v in sorted(rep["lessons_30d"].items(), key=lambda kv: -kv[1]))
    summary = "".join(f"<li>{esc(s)}</li>" for s in rep["summary"])
    return site_theme.head(f"复盘 {d}") + site_theme.nav("review", date=d, when=rep["generated_at"][5:16].replace("T", " ")) + f"""
<h1>今日复盘 · {d}</h1><p class='muted'>收盘后自动生成,只在内网</p>
{cards}
<h2>一句话</h2><ul>{summary}</ul>
<h2>规则检查</h2>{frows or "<p class='muted'>没有触发任何规则</p>"}
<h2>模拟盘:今日订单</h2>{paper_orders}
<h2>模拟盘:今日平仓</h2>{paper_closed}
<h2>Balder 的操作(跟单记分,私有)</h2>{balder_block()}
<h2>模拟盘:竞价口径与错过的交易</h2>{auction_block(rep)}
<h2>模拟盘:持仓异动</h2>{paper_pos}
<h2>实盘:新成交</h2>{real_deals}{real_rt}{real_opts}
<h2>实盘:持仓</h2>{real_pos}
<h2>30 日教训计数</h2>{('<table><tr><th>规则</th><th>次数</th></tr>' + lessons + '</table>') if lessons else "<p class='muted'>30 日内没有触发过规则</p>"}
<p class='muted'>规则来源:S12/S13(价差)、S25(大动后跑输)、S26(期权买方无验证入场)、S31(内部人开盘入场)、S33(否决检验)。实盘部分只是对照,永远不下单。</p>
""" + site_theme.FOOT


# ------------------------------------------------------------------ main ----
def build(day: dt.date) -> dict:
    with PanelStore(read_only=True) as store:
        sess = sessions(store)
        prev = prev_session(sess, day)
        market = {"spy": index_day(store, "SPY", day, prev), "iwm": index_day(store, "IWM", day, prev), "vix": index_day(store, "^VIX", day, prev)}
        con = ledger.connect(read_only=True)
        try:
            ranks = long_ranks(con)
            notes = watch_notes()
            paper = paper_section(con, store, day, prev, market["spy"].get("ret"))
            real = real_section(con, store, day, prev, market["spy"].get("ret"), ranks, notes)
        finally:
            con.close()
    rep = {"date": day.isoformat(), "prev_session": str(prev), "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
           "market": market, "paper": paper, "real": real}
    rep["lessons_30d"] = record_lessons(day, paper["flags"] + real["flags"])
    rep["summary"] = summary_lines(rep)
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    rep = build(day)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"review_{day}.json"), "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1, default=str)
    with open(os.path.join(OUT_DIR, "review_latest.json"), "w") as f:
        fc = flag_classes(rep)
        json.dump({"date": rep["date"], "summary": rep["summary"], "n_flags": len(fc["action"]) + len(fc["real"]), "n_action": len(fc["action"]),
                   "notify": notify_text(rep), "html": f"agent/review_{day}.html"}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT_DIR, f"review_{day}.html"), "w") as f:
        f.write(render_html(rep))
    for s in rep["summary"]:
        print(" -", s)
    if not args.no_notify:
        tn = "/opt/homebrew/bin/terminal-notifier"
        if os.path.exists(tn):
            subprocess.run([tn, "-title", f"复盘 {day}", "-message", notify_text(rep), "-open", f"{SITE}/agent/review_{day}.html",
                            "-group", "review"], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
