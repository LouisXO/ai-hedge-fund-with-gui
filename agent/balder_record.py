"""Balder's public track record (balder-ai.com/record) -> a private trade log, scored against QQQ and SPY.

The page publishes both of his books with entry and exit prices, losers included:
  algo table   Symbol, Strategy, Opened, Closed, Entry, Exit, Return          (closed short-term trades)
  long table   Symbol, In, Out, Entry, Last / exit, Return, Held              (an open position shows OPEN
                                                                               in place of In / Out)
robots.txt allows everything; this fetches the page once a day after the close (execute.sh, 16:10 PT).
Nothing here reads X: what his X subscription adds (rationale, timing) reaches us only through the owner
forwarding posts to agent/balder_log.py. Private: the rows live in optradar.db `balder_record`, the summary
in out/agent/balder_record.json for the private site. Never on the public site, never a signal.

A position is keyed by book, symbol and entry price, so an open long position and the same position once
closed are one row. An open long position does not show its entry date: `opened` is then the first day
this job saw it (opened_seen = TRUE) until the closed row gives the real date. Dates on the page are MM-DD;
the year is the one that puts the date on or before the fetch day.

Usage: python -m agent.balder_record [--html FILE] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmlmod
import json
import math
import os
import re
import urllib.request

import pandas as pd

from agent import ledger
from hedge_fund.features.panel import PanelStore

URL = "https://balder-ai.com/record"
OUT = "/Users/louis/optradar/out/agent/balder_record.json"
UA = "Mozilla/5.0 (Macintosh) personal-research"
ALGO_HEADS = ["Symbol", "Strategy", "Opened", "Closed", "Entry", "Exit", "Return"]
LONG_HEADS = ["Symbol", "In", "Out", "Entry", "Last / exit", "Return", "Held"]
DDL = """CREATE TABLE IF NOT EXISTS balder_record (
    key VARCHAR PRIMARY KEY, book VARCHAR, symbol VARCHAR, strategy VARCHAR, opened DATE, opened_seen BOOLEAN,
    closed DATE, entry DOUBLE, exit DOUBLE, ret_pct DOUBLE, status VARCHAR, first_seen DATE, last_seen DATE)"""
COLS = ["key", "book", "symbol", "strategy", "opened", "opened_seen", "closed", "entry", "exit", "ret_pct", "status",
        "first_seen", "last_seen"]


def fetch(url: str = URL) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def tables(page: str) -> list[tuple[list[str], list[list[str]]]]:
    """Every <table> as (header cells, body rows of cell text)."""
    out = []
    for tb in re.findall(r"<table>(.*?)</table>", page, re.S):
        clean = lambda s: re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", s))).strip()
        heads = [clean(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", tb, re.S)]
        rows = [[clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)] for tr in re.findall(r"<tr>(.*?)</tr>", tb, re.S)]
        out.append((heads, [r for r in rows if r]))
    return out


def to_date(md: str, ref: dt.date) -> dt.date:
    """'09-28' -> the date on or before `ref` with that month and day."""
    m, d = (int(x) for x in md.split("-"))
    year = ref.year if (m, d) <= (ref.month, ref.day) else ref.year - 1
    return dt.date(year, m, d)


def num(s: str) -> float:
    return float(s.replace(",", "").replace("%", "").replace("+", "").replace("−", "-"))


def parse(page: str, today: dt.date) -> pd.DataFrame:
    """Rows of both books (COLS order, first_seen / last_seen = today). Raises if a table is missing, so a page
    redesign fails loudly instead of recording nothing."""
    rows, found = [], set()
    for heads, body in tables(page):
        if heads == ALGO_HEADS:
            found.add("algo")
            for sym, strat, o, c, e, x, r in (b for b in body if len(b) == 7):
                entry = num(e)
                rows.append([f"algo|{sym}|{to_date(o, today)}|{entry:.4f}", "algo", sym, strat, to_date(o, today), False,
                             to_date(c, today), entry, num(x), num(r), "closed", today, today])
        elif heads == LONG_HEADS:
            found.add("long")
            for b in body:
                if len(b) == 6 and b[1] == "OPEN":                       # Symbol, OPEN, Entry, Last, Return, Held
                    sym, _, e, last, r, _held = b
                    entry = num(e)
                    rows.append([f"long|{sym}|{entry:.4f}", "long", sym, "", today, True, None, entry, num(last), num(r),
                                 "open", today, today])
                elif len(b) == 7:
                    sym, i, o, e, x, r, _held = b
                    entry = num(e)
                    rows.append([f"long|{sym}|{entry:.4f}", "long", sym, "", to_date(i, today), False, to_date(o, today),
                                 entry, num(x), num(r), "closed", today, today])
    missing = {"algo", "long"} - found
    if missing:
        raise RuntimeError(f"record page: table(s) not found: {sorted(missing)}")
    return pd.DataFrame(rows, columns=COLS)


def upsert(con, new: pd.DataFrame) -> dict:
    """Merge today's rows: a new key is inserted (first_seen = today); a known key keeps first_seen and, while it
    has no real entry date, its first-seen `opened`; status, exit, return and last_seen are refreshed."""
    con.execute(DDL)
    old = con.execute("SELECT key, opened, opened_seen, first_seen FROM balder_record").df().set_index("key")
    stats = {"new_open": [], "new_closed": [], "now_closed": []}
    for r in new.itertuples(index=False):
        row = r._asdict()
        if row["key"] in old.index:
            prev = old.loc[row["key"]]
            row["first_seen"] = prev["first_seen"]
            if row["opened_seen"]:                                   # still open: keep the day we first saw it
                row["opened"] = prev["opened"]
            was_open = con.execute("SELECT status FROM balder_record WHERE key = ?", [row["key"]]).fetchone()[0] == "open"
            if was_open and row["status"] == "closed":
                stats["now_closed"].append(f"{row['book']} {row['symbol']} {row['ret_pct']:+.1f}%")
        else:
            (stats["new_open"] if row["status"] == "open" else stats["new_closed"]).append(f"{row['book']} {row['symbol']}")
        con.execute(f"INSERT OR REPLACE INTO balder_record ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                    [row[c] for c in COLS])
    return stats


def benchmarks() -> pd.DataFrame:
    with PanelStore(read_only=True) as store:
        q = store.con.execute("SELECT trade_date, symbol, adj_close FROM index_daily WHERE symbol IN ('QQQ', 'SPY')").df()
    q = q.pivot(index="trade_date", columns="symbol", values="adj_close")
    q.index = pd.to_datetime(q.index)
    return q.sort_index()


def score(df: pd.DataFrame, bench: pd.DataFrame) -> pd.DataFrame:
    """QQQ and SPY over each trade's window, close to close (an open position: first seen to the last close)."""
    out = df.copy()
    last = bench.index[-1]
    for s in ("QQQ", "SPY"):
        a = [bench[s].asof(pd.Timestamp(o)) for o in out["opened"]]
        b = [bench[s].asof(pd.Timestamp(c)) if c is not None and not pd.isna(c) else bench[s].loc[last] for c in out["closed"]]
        out[s.lower()] = [(y / x - 1) * 100 if x and y else math.nan for x, y in zip(a, b)]
    out["ex_qqq"] = out["ret_pct"] - out["qqq"]
    return out


def stats(df: pd.DataFrame) -> dict:
    n = int(len(df))
    if not n:
        return {"n": 0}
    ex = df["ex_qqq"].dropna()
    se = ex.std(ddof=1) / math.sqrt(len(ex)) if len(ex) > 1 else math.nan
    return {"n": n, "mean_ret": round(float(df["ret_pct"].mean()), 2), "mean_qqq": round(float(df["qqq"].mean()), 2),
            "mean_ex_qqq": round(float(ex.mean()), 2), "t": round(float(ex.mean() / se), 2) if se and se == se else None,
            "beat_qqq": round(float((ex > 0).mean()), 2), "sum_ret": round(float(df["ret_pct"].sum()), 1)}


def summary(con, today: dt.date, bench: pd.DataFrame) -> dict:
    df = con.execute("SELECT * FROM balder_record").df()
    if df.empty:
        return {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "n": 0}
    for c in ("opened", "closed", "first_seen", "last_seen"):
        df[c] = pd.to_datetime(df[c]).dt.date
    sc = score(df, bench)
    closed = sc[sc["status"] == "closed"]
    week = closed[pd.to_datetime(closed["closed"]) >= pd.Timestamp(today - dt.timedelta(days=7))]
    by_strategy = {s: stats(g) for s, g in closed[closed["book"] == "algo"].groupby("strategy")}
    trade = lambda r: {k: (str(v) if isinstance(v, dt.date) else (None if isinstance(v, float) and math.isnan(v) else v))
                       for k, v in r.items() if k in ("book", "symbol", "strategy", "opened", "closed", "entry", "exit",
                                                         "ret_pct", "qqq", "ex_qqq", "opened_seen")}
    return {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "source": URL, "n": int(len(df)),
            "algo": stats(closed[closed["book"] == "algo"]), "long": stats(closed[closed["book"] == "long"]),
            "by_strategy": by_strategy, "week": {"algo": stats(week[week["book"] == "algo"]), "long": stats(week[week["book"] == "long"])},
            "week_trades": [trade(r) for r in week.sort_values("closed").to_dict("records")],
            "open": [trade(r) for r in sc[sc["status"] == "open"].sort_values("ret_pct", ascending=False).to_dict("records")]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=None, help="parse a saved page instead of fetching")
    ap.add_argument("--dry-run", action="store_true", help="parse and print; write nothing")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    today = dt.date.today()
    page = open(args.html, encoding="utf-8", errors="replace").read() if args.html else fetch()
    rows = parse(page, today)
    if args.dry_run:
        print(rows.groupby(["book", "status"]).size().to_string())
        return 0
    con = ledger.connect()
    try:
        st = upsert(con, rows)
        out = summary(con, today, benchmarks())
    finally:
        con.close()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    a, l = out.get("algo", {}), out.get("long", {})
    print(f"balder record: algo {a.get('n', 0)} closed ({a.get('mean_ret', 0):+.2f}%/trade, vs QQQ {a.get('mean_ex_qqq', 0):+.2f}%, "
          f"t {a.get('t')}) · long {l.get('n', 0)} closed ({l.get('mean_ret', 0):+.2f}%/trade, vs QQQ {l.get('mean_ex_qqq', 0):+.2f}%, "
          f"t {l.get('t')}) · open {len(out.get('open', []))}")
    for k, v in st.items():
        if v:
            print(f"  {k}: {', '.join(v[:12])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
