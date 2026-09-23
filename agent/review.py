"""Daily post-close review: what happened today in the paper books and the real account,
one line per trade, and the rule checks that turn today's mistakes into tomorrow's vetoes.

Runs at the end of agent/bin/execute.sh (after fills are synced, bars updated, books marked
and the moomoo snapshot taken). Writes out/agent/review_<date>.json / .html, review_latest.json,
appends every flagged rule to out/agent/lessons.jsonl (the cumulative "same mistake again"
counter), and sends a clickable Mac notification. Private site only: the real account is in here.

No LLM. Every sentence is a template over numbers we computed; the rules are our own
findings (S12/S13 spreads, S25 big moves, S26 option cheapness, S31 entry timing, S33 vetoes).

Usage: python -m agent.review [--date YYYY-MM-DD] [--no-notify]
"""
from __future__ import annotations

import argparse
import datetime as dt
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
from agent.sources.alpaca_news import NEWS_DB
from agent.sources.retail_heat import RETAIL_DB
from agent.watch import WATCHLIST
from hedge_fund.features.panel import PanelStore

OUT_DIR = "/Users/louis/optradar/out/agent"
SITE = "https://optradar.tail5b470b.ts.net"
LESSONS = os.path.join(OUT_DIR, "lessons.jsonl")
OPT_CODE = re.compile(r"^US\.([A-Z.]+?)(\d{6})([CP])(\d+)$")
BIG_SIGMA, BIG_ABS, SD_WIN, VETO_WIN = 3.0, 5.0, 60, 5

sys.path.insert(0, "/Users/louis/optradar/bin")
import site_theme  # noqa: E402


# ------------------------------------------------------------------ helpers ----
def esc(x) -> str:
    return html.escape("" if x is None else str(x))


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


def headlines(symbols: list[str], day: dt.date) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not symbols:
        return out
    try:
        n = duckdb.connect(str(NEWS_DB), read_only=True)
        rows = n.execute(f"""SELECT s.symbol, i.headline FROM news_symbols s JOIN news_items i USING (id)
                             WHERE s.symbol IN ({','.join('?' * len(symbols))}) AND i.n_symbols <= 3
                               AND i.created_at >= ? AND i.created_at < ? ORDER BY i.created_at DESC""",
                       list(symbols) + [pd.Timestamp(day) - pd.Timedelta(hours=8), pd.Timestamp(day) + pd.Timedelta(hours=24)]).fetchall()
        n.close()
        for s, h in rows:
            out.setdefault(s, [])
            if len(out[s]) < 3:
                out[s].append(h)
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
    lots = con.execute("SELECT book, ticker, qty, entry_day, entry_px, entry_model_px, hold_until FROM agent_lots WHERE status = 'open'").fetchall()
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
        if b == "insider" and side == "buy" and otype == "limit":
            row["tags"].append("内部人入场用了限价(S31:应开盘市价)")
        out["orders"].append(row)

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

    # rule checks
    unfilled = [o for o in out["orders"] if "未成交" in o["tags"]]
    if unfilled:
        out["flags"].append({"rule": "paper_unfilled", "text": f"{len(unfilled)} 张单未成交:{', '.join(o['ticker'] for o in unfilled)}"})
    gaps = [o["gap_pct"] for o in out["orders"] if o["gap_pct"] is not None]
    if gaps and np.mean(np.abs(gaps)) >= 0.5:
        out["flags"].append({"rule": "paper_exec_gap", "text": f"平均执行偏差 {np.mean(gaps):+.2f}%/边({len(gaps)} 笔),超过 0.5%"})
    lim_ins = [o for o in out["orders"] if any(t.startswith("内部人入场用了限价") for t in o["tags"])]
    if lim_ins:
        out["flags"].append({"rule": "paper_insider_limit", "text": f"内部人书 {len(lim_ins)} 张限价入场单(规则:市价开盘单)"})
    big = [p for p in out["positions"] if p["day_ret"] is not None and abs(p["day_ret"]) >= 5]
    if big:
        out["flags"].append({"rule": "paper_big_move", "text": "持仓大动:" + ", ".join(f"{p['ticker']} {p['day_ret']:+.1f}%" for p in big[:6]), "info": True})
    return out


# ------------------------------------------------------------------ real ----
def real_section(con, store, day: dt.date, prev: dt.date | None, spy_ret: float | None, ranks: dict, notes: dict) -> dict:
    out: dict = {"nav": {}, "deals": [], "round_trips": [], "positions": [], "flags": []}
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
    today = dl[dl["day"] == day]
    pos = con.execute("""SELECT code, name, qty, cost_price, price, market_val, pl_val, is_option, underlying FROM acct_positions
                         WHERE date = ? ORDER BY market_val DESC""", [day]).fetchall()
    tickers = sorted({parse_code(c)["ticker"] for c in list(today["code"]) + [p[0] for p in pos]})
    hist = bars(store, tickers, day - dt.timedelta(days=120), day)
    spy = store.index_series("SPY", "adj_close")
    spy.index = spy.index.date
    ctxs = {t: day_context(hist, spy, t, day) for t in tickers}
    news = headlines(tickers, day)
    heat = retail(tickers, day)
    ins = insiders_30d(store, tickers, day)

    # FIFO round trips: a SELL today matched against earlier BUYs of the same code
    for _, d in today.iterrows():
        p = parse_code(d["code"])
        t = p["ticker"]
        c = ctxs.get(t, {})
        row = {"ts": str(d["ts"]), "code": d["code"], "name": d["name"], "ticker": t, "side": d["side"], "qty": float(d["qty"]), "price": float(d["price"]),
               "is_option": bool(p["is_option"]), "u_open": c.get("open"), "u_close": c.get("close"), "u_ret": c.get("ret"), "u_ret5": c.get("ret5"),
               "range_pos": range_pos(float(d["price"]), c) if not p["is_option"] else None,
               "after_pct": None, "system": system_view(t, c, ranks, ins, heat, notes), "news": news.get(t, []), "tags": []}
        if not p["is_option"] and c.get("close"):
            row["after_pct"] = (c["close"] / row["price"] - 1) * 100 * (1 if d["side"] == "BUY" else -1)
        if p["is_option"]:
            dte = (p["expiry"] - day).days
            spot = c.get("close")
            row.update({"expiry": str(p["expiry"]), "cp": p["cp"], "strike": p["strike"], "dte": dte,
                        "moneyness_pct": ((p["strike"] / spot - 1) * 100 * (1 if p["cp"] == "C" else -1)) if spot else None, "rv20": c.get("rv20")})
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
        if d["side"] == "SELL":
            buys = dl[(dl["code"] == d["code"]) & (dl["side"] == "BUY") & (dl["ts"] < d["ts"])]
            if not buys.empty:
                qty_left, cost, first = float(d["qty"]), 0.0, None
                for _, b in buys.iterrows():
                    take = min(qty_left, float(b["qty"]))
                    cost += take * float(b["price"])
                    first = first or b["ts"]
                    qty_left -= take
                    if qty_left <= 0:
                        break
                q = float(d["qty"]) - max(qty_left, 0)
                if q > 0:
                    avg = cost / q
                    mult = 100 if p["is_option"] else 1
                    ret = (float(d["price"]) / avg - 1) * 100 if avg else None
                    hold = (pd.Timestamp(d["ts"]) - pd.Timestamp(first)).days
                    # the underlying over the same hold, for the leverage check
                    g = hist[hist["ticker"] == t].set_index("trade_date")["adj_close"]
                    u0 = g[g.index <= pd.Timestamp(first).date()]
                    u_ret = (float(g.loc[day]) / float(u0.iloc[-1]) - 1) * 100 if not u0.empty and day in g.index else None
                    out["round_trips"].append({"code": d["code"], "ticker": t, "is_option": bool(p["is_option"]), "qty": q, "entry_ts": str(first),
                                               "entry_px": avg, "exit_px": float(d["price"]), "ret_pct": ret, "pnl_usd": (float(d["price"]) - avg) * q * mult,
                                               "hold_days": hold, "underlying_ret_pct": u_ret,
                                               "lesson": (f"标的同期 {u_ret:+.1f}%,期权 {ret:+.0f}%:杠杆 {ret / u_ret:.1f}x" if p["is_option"] and u_ret and ret is not None and abs(u_ret) > 0.5 else "")})

    for code, name, qty, cost, px, mv, pl, is_opt, und in pos:
        p = parse_code(code)
        t = p["ticker"]
        c = ctxs.get(t, {})
        total = out["nav"].get("total") or 0
        row = {"code": code, "name": name, "ticker": t, "qty": qty, "cost": cost, "price": px, "market_val": mv, "pl": pl,
               "pl_pct": (px / cost - 1) * 100 if cost and cost > 0 else None, "is_option": bool(is_opt), "u_ret": c.get("ret"),
               "weight_pct": (mv / total * 100) if total else None, "system": system_view(t, c, ranks, ins, heat, notes), "news": news.get(t, []), "tags": []}
        if p["is_option"]:
            row["dte"] = (p["expiry"] - day).days
            if row["dte"] <= 7:
                row["tags"].append(f"到期 {row['dte']} 天")
        if row["weight_pct"] and row["weight_pct"] >= 40:
            row["tags"].append(f"占净值 {row['weight_pct']:.0f}%")
        out["positions"].append(row)

    for code, g in today[today["side"] == "BUY"].groupby("code"):
        if len(g) >= 2:
            out["flags"].append({"rule": "real_add_same_day", "text": f"{parse_code(code)['ticker']} 同一合约当日买入 {len(g)} 次({', '.join(f'{p:.2f}' for p in g['price'])})"})
    n_deals = len(out["deals"])
    if n_deals >= 5:
        out["flags"].append({"rule": "real_overtrading", "text": f"当日 {n_deals} 笔成交"})
    for d in out["deals"]:
        for tg in d["tags"]:
            key = ("real_buy_after_jump" if tg.startswith("S25") else "real_0dte" if tg.startswith("当日到期") else "real_short_dte" if "只剩" in tg else "real_otm_lottery" if "虚值" in tg
                   else "real_buy_high" if tg.startswith("买在") else "real_sell_low" if tg.startswith("卖在") else "real_vs_insiders" if "净卖出" in tg else None)
            if key:
                out["flags"].append({"rule": key, "text": f"{d['ticker']} {d['side']}:{tg}"})
    conc = [p for p in out["positions"] if p["weight_pct"] and p["weight_pct"] >= 40]
    if conc:
        out["flags"].append({"rule": "real_concentration", "text": "单一持仓占比 ≥ 40%:" + ", ".join(f"{p['ticker']} {p['weight_pct']:.0f}%" for p in conc), "info": True})
    return out


# ------------------------------------------------------------------ lessons ----
def record_lessons(day: dt.date, flags: list[dict]) -> dict[str, int]:
    """Append today's non-informational flags; return the 30-day count per rule (today included)."""
    rows = []
    if os.path.exists(LESSONS):
        with open(LESSONS) as f:
            rows = [json.loads(l) for l in f if l.strip()]
    rows = [r for r in rows if r.get("date") != day.isoformat()]                      # rerun-safe
    rows += [{"date": day.isoformat(), "rule": f["rule"], "text": f["text"]} for f in flags if not f.get("info")]
    with open(LESSONS, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    cutoff = (day - dt.timedelta(days=30)).isoformat()
    counts: dict[str, int] = {}
    for r in rows:
        if r["date"] >= cutoff:
            counts[r["rule"]] = counts.get(r["rule"], 0) + 1
    return counts


RULE_NAMES = {"paper_unfilled": "模拟盘未成交", "paper_exec_gap": "执行偏差 > 0.5%", "paper_insider_limit": "内部人限价入场", "paper_big_move": "持仓大动",
              "real_overtrading": "实盘当日 ≥ 5 笔", "real_buy_after_jump": "实盘大动后追买(S25)", "real_short_dte": "实盘买临期期权", "real_0dte": "实盘买当日到期期权",
              "real_otm_lottery": "实盘买深虚值", "real_buy_high": "实盘买在日高附近", "real_sell_low": "实盘卖在日低附近",
              "real_vs_insiders": "实盘逆内部人卖出买入", "real_concentration": "实盘单一持仓 ≥ 40%", "real_add_same_day": "实盘同一合约当日加仓"}


# ------------------------------------------------------------------ render ----
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
    if r["deals"]:
        L.append(f"实盘成交 {len(r['deals'])} 笔" + (f",平仓 {len(r['round_trips'])} 笔:" + "; ".join(f"{x['ticker']} {x['ret_pct']:+.0f}%" for x in r["round_trips"]) if r["round_trips"] else ""))
    else:
        L.append("实盘今日无成交")
    flags = [f for f in rep["paper"]["flags"] + rep["real"]["flags"] if not f.get("info")]
    L.append(f"规则检查:{len(flags)} 条需要注意" if flags else "规则检查:无违规")
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
                    f"<td>{esc(p['entry_day'])}</td><td class='muted'>{tags(p['tags'])} {'<br>'.join(esc(h[:70]) for h in p['news'][:2])}</td></tr>" for p in show)
    paper_pos = (f"<p class='muted'>{len(pp)} 个持仓;下面是当日涨跌最大的 {len(show)} 个。</p><table><tr><th>书</th><th>标的</th><th>当日</th><th>超额</th><th>入场以来</th><th>入场日</th><th>标记 / 新闻</th></tr>{prows}</table>"
                 if pp else "<p class='muted'>无持仓</p>")

    # real
    r = rep["real"]
    drows = ""
    for x in r["deals"]:
        det = (f"{x['cp']} {x['strike']:g} 到期 {x['expiry']}({x['dte']} 天,虚值 {x['moneyness_pct']:+.0f}%,RV20 {x['rv20']:.0f}%)" if x["is_option"] and x.get("moneyness_pct") is not None
               else f"当日区间位置 {x['range_pos']:.0f}%" if x.get("range_pos") is not None else "")
        drows += (f"<tr><td>{esc(x['ts'][11:16])}</td><td><b>{esc(x['ticker'])}</b><br><span class='muted'>{esc(x['name'])}</span></td><td>{esc(x['side'])} {x['qty']:g} @ {x['price']:.2f}</td>"
                  f"<td>{pct(x['u_ret'])}<br><span class='muted'>5 日 {pct(x['u_ret5'])}</span></td><td>{pct(x['after_pct'])}</td><td class='muted'>{esc(det)}</td>"
                  f"<td class='muted'>{'<br>'.join(esc(s) for s in x['system'])}</td><td>{tags(x['tags'])}</td></tr>")
    real_deals = (f"<table><tr><th>时间</th><th>标的</th><th>成交</th><th>标的当日</th><th>成交后到收盘</th><th>细节</th><th>我们的系统怎么看</th><th>标记</th></tr>{drows}</table>"
                  if r["deals"] else "<p class='muted'>今日无成交</p>")
    rt = "".join(f"<tr><td><b>{esc(x['ticker'])}</b> {esc(x['code'])}</td><td>{esc(x['entry_ts'][:10])} @ {x['entry_px']:.2f}</td><td>{x['exit_px']:.2f}</td>"
                 f"<td>{pct(x['ret_pct'], 1)}</td><td>{x['pnl_usd']:+,.0f}</td><td>{x['hold_days']} 天</td><td>{pct(x['underlying_ret_pct'], 1)}</td><td class='muted'>{esc(x['lesson'])}</td></tr>" for x in r["round_trips"])
    real_rt = f"<h3>今日平仓的完整交易</h3><table><tr><th>合约</th><th>入场</th><th>出场</th><th>收益</th><th>盈亏 $</th><th>持有</th><th>标的同期</th><th>读法</th></tr>{rt}</table>" if rt else ""
    prow = "".join(f"<tr><td><b>{esc(p['ticker'])}</b><br><span class='muted'>{esc(p['name'])}{(' · 到期 ' + str(p['dte']) + ' 天') if p.get('dte') is not None else ''}</span></td>"
                   f"<td>{p['qty']:g} @ {p['cost']:.2f}</td><td>{p['price']:.2f}</td><td>{pct(p['u_ret'])}</td><td>{pct(p['pl_pct'], 1)}<br><span class='muted'>{p['pl']:+,.0f}</span></td>"
                   f"<td>{(f'{p['weight_pct']:.0f}%') if p['weight_pct'] else '—'}</td><td class='muted'>{'<br>'.join(esc(s) for s in p['system'])}</td><td>{tags(p['tags'])}</td></tr>" for p in r["positions"])
    real_pos = (f"<table><tr><th>持仓</th><th>数量 @ 成本</th><th>现价</th><th>标的当日</th><th>浮盈</th><th>占比</th><th>我们的系统怎么看</th><th>标记</th></tr>{prow}</table>"
                if r["positions"] else "<p class='muted'>无持仓</p>")

    flags = rep["paper"]["flags"] + r["flags"]
    frows = "".join(f"<li>{'<span class=muted>' if f.get('info') else ''}{esc(RULE_NAMES.get(f['rule'], f['rule']))}:{esc(f['text'])}"
                    f"{(' · 30 日内第 ' + str(rep['lessons_30d'].get(f['rule'], 1)) + ' 次') if not f.get('info') else ''}{'</span>' if f.get('info') else ''}</li>" for f in flags)
    lessons = "".join(f"<tr><td>{esc(RULE_NAMES.get(k, k))}</td><td>{v}</td></tr>" for k, v in sorted(rep["lessons_30d"].items(), key=lambda kv: -kv[1]))
    summary = "".join(f"<li>{esc(s)}</li>" for s in rep["summary"])
    return site_theme.head(f"复盘 {d}") + site_theme.nav("review", date=d, when=rep["generated_at"][5:16].replace("T", " ")) + f"""
<h1>今日复盘 · {d}</h1><p class='muted'>收盘后自动生成,只在内网</p>
{cards}
<h2>一句话</h2><ul>{summary}</ul>
<h2>规则检查</h2>{('<ul>' + frows + '</ul>') if frows else "<p class='muted'>没有触发任何规则</p>"}
<h2>模拟盘:今日订单</h2>{paper_orders}
<h2>模拟盘:持仓异动</h2>{paper_pos}
<h2>实盘:今日成交</h2>{real_deals}{real_rt}
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
        json.dump({"date": rep["date"], "summary": rep["summary"], "n_flags": len([x for x in rep["paper"]["flags"] + rep["real"]["flags"] if not x.get("info")]),
                   "html": f"agent/review_{day}.html"}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT_DIR, f"review_{day}.html"), "w") as f:
        f.write(render_html(rep))
    for s in rep["summary"]:
        print(" -", s)
    if not args.no_notify:
        tn = "/opt/homebrew/bin/terminal-notifier"
        if os.path.exists(tn):
            subprocess.run([tn, "-title", f"复盘 {day}", "-message", " · ".join(rep["summary"][1:4])[:180], "-open", f"{SITE}/agent/review_{day}.html",
                            "-group", "review"], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
