"""Balder (X @Balder13946731) trade log: what he posts, scored with our own yardstick.

Nothing here reads X. The user forwards the posts he is entitled to see (subscriber content, for
personal use only — never on the public site): share a post as text into the drop folder
~/Library/Mobile Documents/com~apple~CloudDocs/Balder/ (one file per post, any name, .txt / .md),
or paste it into a file there by hand. This job:

  1. reads every new file (state in ~/.hedge-fund/agent/balder_seen.json),
  2. extracts trades with the sealed single-turn LLM (agent/translate.py's transport; no tools,
     numbers validated: every price in the output must appear in the post text),
  3. records them in optradar.db `balder_trades` (open / close / add / trim, strategy, price),
  4. scores every open from the next session: 1 / 5 / 20-session return vs SPY, from our panel,
  5. writes out/agent/balder_latest.json for the review page (private site only).

Usage: python -m agent.balder_log [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re

import duckdb
import pandas as pd

from agent import ledger
from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

DROP = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Balder")
SEEN = AGENT_DIR / "balder_seen.json"
OUT = "/Users/louis/optradar/out/agent/balder_latest.json"
DDL = """CREATE TABLE IF NOT EXISTS balder_trades (
    id VARCHAR PRIMARY KEY, posted DATE, book VARCHAR, strategy VARCHAR, ticker VARCHAR, action VARCHAR, side VARCHAR,
    price DOUBLE, qty DOUBLE, note VARCHAR, source_file VARCHAR, ret1 DOUBLE, ret5 DOUBLE, ret20 DOUBLE, spy1 DOUBLE, spy5 DOUBLE, spy20 DOUBLE,
    scored_through DATE)"""
SYSTEM = ("你是一个只做信息提取的程序。输入是一段交易员发布的帖子文字(中文或英文),里面可能有一笔或多笔股票/期权交易。"
          "把每笔交易输出成 JSON 数组,每项字段:ticker(美股代码,大写,不含 $)、action(open/close/add/trim 之一)、side(long/short)、"
          "book(short 或 long:短线算法用 short,长线趋势仓位用 long)、strategy(原文的策略名,如 超跌反弹/动量捕捉/趋势追随,没有就空字符串)、"
          "price(成交价数字,没有就 null)、qty(数量或仓位比例,没有就 null)、note(一句话摘要,≤40 字)。"
          "没有交易就输出空数组 []。只输出 JSON,不解释。价格只能用原文里出现过的数字。")


def _load_seen() -> dict:
    try:
        return json.load(open(SEEN))
    except Exception:
        return {}


def numbers_in(text: str) -> set[str]:
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in re.findall(r"\d+(?:\.\d+)?", text)}


def extract(text: str, transport=None) -> list[dict]:
    if transport is None:
        from integrations.claude_code_llm import ClaudeCodeLLM
        transport = ClaudeCodeLLM(model="sonnet", timeout=90.0)
    r = transport.call(SYSTEM, text)
    out = r["result"] if isinstance(r, dict) else str(r)
    m = re.search(r"\[.*\]", out, re.S)
    rows = json.loads(m.group(0)) if m else []
    allowed = numbers_in(text)
    clean = []
    for x in rows:
        if not isinstance(x, dict) or not x.get("ticker") or x.get("action") not in ("open", "close", "add", "trim"):
            continue
        p = x.get("price")
        if p is not None:
            ps = str(float(p)).rstrip("0").rstrip(".")
            if ps not in allowed:                             # a price the post never mentioned: drop it, keep the trade
                p = None
        clean.append({"ticker": str(x["ticker"]).upper().lstrip("$"), "action": x["action"], "side": x.get("side") or "long",
                      "book": x.get("book") if x.get("book") in ("short", "long") else "short", "strategy": (x.get("strategy") or "")[:40],
                      "price": p, "qty": x.get("qty"), "note": (x.get("note") or "")[:80]})
    return clean


def posted_date(path: str, text: str) -> dt.date:
    m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return dt.date.fromtimestamp(os.path.getmtime(path))


def ingest(con, dry_run: bool) -> int:
    if not os.path.isdir(DROP):
        os.makedirs(DROP, exist_ok=True)
        return 0
    seen = _load_seen()
    n = 0
    for f in sorted(os.listdir(DROP)):
        p = os.path.join(DROP, f)
        if not f.lower().endswith((".txt", ".md")) or f in seen:
            continue
        text = open(p, encoding="utf-8", errors="ignore").read().strip()
        if not text:
            seen[f] = "empty"
            continue
        try:
            rows = extract(text)
        except Exception as exc:
            print(f"{f}: extract failed: {exc}")
            continue
        day = posted_date(p, text)
        for x in rows:
            rid = hashlib.sha1(f"{f}|{x['ticker']}|{x['action']}|{x['price']}".encode()).hexdigest()[:16]
            print(f"  {day} {x['book']:5s} {x['action']:5s} {x['ticker']:6s} {x['price']} {x['strategy']} — {x['note']}")
            if not dry_run:
                con.execute("INSERT OR REPLACE INTO balder_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
                            [rid, day, x["book"], x["strategy"], x["ticker"], x["action"], x["side"], x["price"], x["qty"], x["note"], f])
            n += 1
        seen[f] = day.isoformat()
    if not dry_run:
        json.dump(seen, open(SEEN, "w"), ensure_ascii=False)
    return n


def score(con) -> int:
    """Forward returns from the next session's open after the post, for every open; SPY the same way."""
    rows = con.execute("SELECT id, posted, ticker FROM balder_trades WHERE action IN ('open', 'add')").fetchall()
    if not rows:
        return 0
    with PanelStore(read_only=True) as store:
        sess = [r[0] for r in store.con.execute("SELECT trade_date FROM index_daily WHERE symbol = 'SPY' ORDER BY trade_date").fetchall()]
        spy = dict(store.con.execute("SELECT trade_date, adj_close FROM index_daily WHERE symbol = 'SPY'").fetchall())
        spy_open = dict(store.con.execute("SELECT trade_date, open FROM index_daily WHERE symbol = 'SPY'").fetchall())
        tick = sorted({r[2] for r in rows})
        bars = store.con.execute(f"SELECT ticker, trade_date, open, adj_close, close FROM bars WHERE ticker IN ({','.join('?' * len(tick))}) AND trade_date >= ?",
                                 tick + [min(r[1] for r in rows) - dt.timedelta(days=3)]).df()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.date
    bars["adj_open"] = bars["open"] * bars["adj_close"] / bars["close"]
    g = {t: d.set_index("trade_date") for t, d in bars.groupby("ticker")}
    n = 0
    for rid, posted, t in rows:
        posted = pd.Timestamp(posted).date()
        after = [s for s in sess if s > posted]
        if not after or t not in g:
            continue
        e = after[0]
        d = g[t]
        if e not in d.index:
            continue
        e_px = float(d.at[e, "adj_open"])
        vals = {}
        for h, col in ((1, "ret1"), (5, "ret5"), (20, "ret20")):
            if len(after) > h - 1 and after[h - 1] in d.index and after[h - 1] <= sess[-1]:
                x = after[h - 1]
                vals[col] = (float(d.at[x, "adj_close"]) / e_px - 1) * 100
                if e in spy_open and x in spy:
                    vals[f"spy{h}"] = (spy[x] / spy_open[e] - 1) * 100
        if vals:
            sets = ", ".join(f"{k} = ?" for k in vals)
            con.execute(f"UPDATE balder_trades SET {sets}, scored_through = ? WHERE id = ?", list(vals.values()) + [sess[-1], rid])
            n += 1
    return n


def summary(con) -> dict:
    df = con.execute("SELECT * FROM balder_trades ORDER BY posted DESC, ticker").df()
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "n": int(len(df)), "recent": [], "by_book": {}}
    if df.empty:
        return out
    df["posted"] = df["posted"].astype(str)
    out["recent"] = df.head(15).where(pd.notna(df.head(15)), None).to_dict("records")
    for b, g in df[df["action"].isin(["open", "add"])].groupby("book"):
        s = {}
        for h in (1, 5, 20):
            r = g[f"ret{h}"].dropna()
            sp = g.loc[r.index, f"spy{h}"]
            s[f"h{h}"] = {"n": int(len(r)), "mean": float(r.mean()) if len(r) else None, "hit": float((r > 0).mean()) if len(r) else None,
                          "mean_abn": float((r - sp).mean()) if len(r) else None}
        out["by_book"][b] = s
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    con = ledger.connect(read_only=args.dry_run) if args.dry_run else ledger.connect()
    try:
        if not args.dry_run:
            con.execute(DDL)
        n_new = ingest(con, args.dry_run)
        n_scored = score(con) if not args.dry_run else 0
        out = summary(con) if not args.dry_run else {}
    finally:
        con.close()
    if not args.dry_run:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1, default=str)
    print(f"balder: {n_new} new trades, {n_scored} scored, total {out.get('n', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
