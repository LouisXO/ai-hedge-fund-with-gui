"""Balder's views (S52): the direction he gives a named stock without trading it, scored like his trades.

Descriptive only: never a signal, never on the public site. Reads the same drop folder as agent/balder_log.py
(agent/balder_x_save writes it) with its own seen file, so the views of posts already read can be backfilled
without re-extracting trades.

What counts as a view (pre-registered in docs/AGENT_PLAN.md S52):
  - the post says a named US-listed stock, ADR or ETF will rise / is worth buying / a dip is not a worry (bull),
    or will fall / should be sold / avoided (bear);
  - not views: a trade the post states (balder_log records it), index levels and range odds, a recap of a move
    that already happened, a mention with no direction, a sector or a private company with no listed name;
  - index tickers (INDEX_TICKERS) are dropped: the range posts are their own product;
  - the LLM must quote the words that carry the view; a quote not found in the post drops the view;
  - the same ticker and stance within REPEAT_DAYS is one view (the first post).
Scoring: from the next session's open after the post, 5 and 20 sessions, SPY the same way. A bull view is right
when it beats SPY, a bear view when it trails SPY.

Usage: python -m agent.balder_views [--dry-run] [--rebuild]
  --rebuild   back up the table and the seen file, empty the table and re-extract every saved post
  --dry-run   extract and print, write nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re

import pandas as pd

from agent import ledger
from agent.balder_log import DROP, posted_date
from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

SEEN = AGENT_DIR / "balder_views_seen.json"
OUT = "/Users/louis/optradar/out/agent/balder_views.json"
DDL = """CREATE TABLE IF NOT EXISTS balder_views (
    id VARCHAR PRIMARY KEY, posted DATE, ticker VARCHAR, stance VARCHAR, horizon VARCHAR, claim VARCHAR, quote VARCHAR,
    source_file VARCHAR, ret5 DOUBLE, ret20 DOUBLE, spy5 DOUBLE, spy20 DOUBLE, scored_through DATE)"""
SYSTEM = ("你是一个只做信息提取的程序。输入是一段交易员发布的帖子文字(中文或英文)。找出帖子对具体某只美股(含在美上市的 ADR 和 ETF)"
          "表达的方向性观点,输出成 JSON 数组,每项字段:ticker(美股代码,大写,不含 $;帖子用公司中文名或英文名时换成代码)、"
          "stance(bull 或 bear:认为会涨、值得买、回调不用慌算 bull;认为会跌、该卖、该跑、要回避算 bear)、"
          "horizon(short:一个月以内或针对某个事件;long:更长;不清楚填 unknown)、claim(用一句话概括观点,≤40 字)、"
          "quote(帖子里表达这个观点的原文,逐字摘抄,≤40 字)。"
          "这些不是观点,不要输出:帖子写明的交易动作(开仓、平仓、加仓、减仓、月度选股);指数或大盘的点位、区间和概率;"
          "复盘里已经发生的涨跌;没有方向的提及或观察名单;只说行业或板块、没有点名上市公司的判断;未在美国上市的公司。"
          "没有观点就输出空数组 []。只输出 JSON,不解释。")
INDEX_TICKERS = {"SPX", "SPY", "NDX", "QQQ", "DJI", "DIA", "IWM", "RUT", "VIX", "ES", "NQ"}
REPEAT_DAYS = 3
HORIZONS = (5, 20)


def _load_seen(path=SEEN) -> dict:
    try:
        return json.load(open(path))
    except Exception:
        return {}


def _bare(s: str) -> str:
    """Letters, digits and CJK only: a quote matches the post whatever the spacing and punctuation."""
    return re.sub(r"[\W_]+", "", s).lower()


def extract(text: str, transport=None) -> list[dict]:
    if transport is None:
        from integrations.claude_code_llm import ClaudeCodeLLM
        transport = ClaudeCodeLLM(model="sonnet", timeout=90.0)
    r = transport.call(SYSTEM, text)
    out = r["result"] if isinstance(r, dict) else str(r)
    m = re.search(r"\[.*\]", out, re.S)
    rows = json.loads(m.group(0)) if m else []
    body = _bare(text)
    clean = []
    for x in rows:
        if not isinstance(x, dict) or x.get("stance") not in ("bull", "bear"):
            continue
        t = str(x.get("ticker") or "").upper().lstrip("$").strip()
        q = str(x.get("quote") or "")
        if not re.fullmatch(r"[A-Z][A-Z.]{0,5}", t) or t in INDEX_TICKERS or not _bare(q) or _bare(q) not in body:
            continue
        clean.append({"ticker": t, "stance": x["stance"],
                      "horizon": x.get("horizon") if x.get("horizon") in ("short", "long") else "unknown",
                      "claim": str(x.get("claim") or "")[:80], "quote": q[:80]})
    return clean


def collect(drop: str = DROP, seen: dict | None = None, transport=None) -> tuple[list[dict], dict]:
    """(views, seen) for every saved post not in `seen`. No database: the LLM calls run before any lock is taken."""
    seen = dict(seen or {})
    views = []
    if not os.path.isdir(drop):
        return views, seen
    for f in sorted(os.listdir(drop)):
        p = os.path.join(drop, f)
        if not f.lower().endswith((".txt", ".md")) or f in seen:
            continue
        text = open(p, encoding="utf-8", errors="ignore").read().strip()
        if not text:
            seen[f] = "empty"
            continue
        try:
            rows = extract(text, transport)
        except Exception as exc:
            print(f"{f}: extract failed: {exc}")
            continue
        day = posted_date(p, text)
        views += [dict(x, posted=day, source_file=f) for x in rows]
        seen[f] = day.isoformat()
    return views, seen


def record(con, views: list[dict]) -> int:
    """Insert the views in posting order; the same ticker and stance within REPEAT_DAYS of a recorded view is a repeat."""
    n = 0
    for v in sorted(views, key=lambda v: (v["posted"], v["source_file"])):
        prev = con.execute("SELECT 1 FROM balder_views WHERE ticker = ? AND stance = ? AND posted BETWEEN ? AND ?",
                           [v["ticker"], v["stance"], v["posted"] - dt.timedelta(days=REPEAT_DAYS), v["posted"]]).fetchone()
        if prev:
            print(f"  {v['posted']} repeat {v['stance']} {v['ticker']} in {v['source_file']}")
            continue
        vid = hashlib.sha1(f"{v['source_file']}|{v['ticker']}|{v['stance']}".encode()).hexdigest()[:16]
        print(f"  {v['posted']} {v['stance']:4s} {v['ticker']:6s} {v['horizon']:7s} {v['claim']}")
        con.execute("INSERT OR REPLACE INTO balder_views VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)",
                    [vid, v["posted"], v["ticker"], v["stance"], v["horizon"], v["claim"], v["quote"], v["source_file"]])
        n += 1
    return n


def forward(rows, sessions: list, bars: pd.DataFrame, spy_open: dict, spy_close: dict) -> dict:
    """{id: {"ret5": .., "spy5": .., ...}} from the first session after `posted`, entry at its adjusted open.
    rows: (id, posted, ticker); bars: ticker, trade_date, open, close, adj_close."""
    b = bars.assign(adj_open=bars["open"] * bars["adj_close"] / bars["close"])
    g = {t: d.set_index("trade_date") for t, d in b.groupby("ticker")}
    out = {}
    for vid, posted, t in rows:
        after = [s for s in sessions if s > posted]
        if not after or t not in g or after[0] not in g[t].index:
            continue
        d, e = g[t], after[0]
        vals = {}
        for h in HORIZONS:
            if len(after) >= h and after[h - 1] in d.index:
                x = after[h - 1]
                vals[f"ret{h}"] = (float(d.at[x, "adj_close"]) / float(d.at[e, "adj_open"]) - 1) * 100
                if e in spy_open and x in spy_close:
                    vals[f"spy{h}"] = (spy_close[x] / spy_open[e] - 1) * 100
        if vals:
            out[vid] = vals
    return out


def score(con) -> int:
    rows = [(r[0], pd.Timestamp(r[1]).date(), r[2]) for r in con.execute("SELECT id, posted, ticker FROM balder_views").fetchall()]
    if not rows:
        return 0
    with PanelStore(read_only=True) as store:
        sess = [r[0] for r in store.con.execute("SELECT trade_date FROM index_daily WHERE symbol = 'SPY' ORDER BY trade_date").fetchall()]
        spy_close = dict(store.con.execute("SELECT trade_date, adj_close FROM index_daily WHERE symbol = 'SPY'").fetchall())
        spy_open = dict(store.con.execute("SELECT trade_date, open FROM index_daily WHERE symbol = 'SPY'").fetchall())
        tick = sorted({r[2] for r in rows})
        bars = store.con.execute(f"SELECT ticker, trade_date, open, close, adj_close FROM bars WHERE ticker IN ({','.join('?' * len(tick))}) AND trade_date >= ?",
                                 tick + [min(r[1] for r in rows)]).df()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.date
    got = forward(rows, sess, bars, spy_open, spy_close)
    for vid, vals in got.items():
        sets = ", ".join(f"{k} = ?" for k in vals)
        con.execute(f"UPDATE balder_views SET {sets}, scored_through = ? WHERE id = ?", list(vals.values()) + [sess[-1], vid])
    return len(got)


def summary(con) -> dict:
    df = con.execute("SELECT * FROM balder_views ORDER BY posted DESC, ticker").df()
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "n": int(len(df)), "recent": [], "by_stance": {}}
    if df.empty:
        return out
    df["posted"] = df["posted"].astype(str)
    head = df.head(15).astype(object)        # object first: on a float column where(..., None) keeps NaN, which is not JSON
    out["recent"] = head.where(pd.notna(head), None).to_dict("records")
    for s, g in df.groupby("stance"):
        sign = 1 if s == "bull" else -1
        st = {}
        for h in HORIZONS:
            r = g[f"ret{h}"].dropna()
            abn = (r - g.loc[r.index, f"spy{h}"]) * sign
            st[f"h{h}"] = {"n": int(len(r)), "mean_abn_signed": float(abn.mean()) if len(r) else None,
                           "right": float((abn > 0).mean()) if len(r) else None}
        out["by_stance"][s] = st
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    views, seen = collect(seen={} if args.rebuild else _load_seen())
    if args.dry_run:
        for v in views:
            print(f"  {v['posted']} {v['stance']:4s} {v['ticker']:6s} {v['horizon']:7s} {v['claim']} 「{v['quote']}」")
        print(f"balder views (dry run): {len(views)} extracted, nothing written")
        return 0
    con = ledger.connect()                                   # held only for the writes: the LLM calls are done
    try:
        con.execute(DDL)
        if args.rebuild:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            con.execute("SELECT * FROM balder_views").df().to_json(os.path.join(AGENT_DIR, f"balder_views_backup_{stamp}.json"),
                                                                   orient="records", force_ascii=False, date_format="iso")
            con.execute("DELETE FROM balder_views")
        n_new = record(con, views)
        n_scored = score(con)
        out = summary(con)
    finally:
        con.close()
    json.dump(seen, open(SEEN, "w"), ensure_ascii=False)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1, default=str)
    print(f"balder views: {n_new} new, {n_scored} scored, total {out['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
