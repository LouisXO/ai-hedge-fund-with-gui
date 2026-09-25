"""Auction-basis view of the paper books, and a shadow record of the trades the simulator never filled.

Why (S27 / S27b): the paper simulator fills after the open at the ask, about +0.6% per side worse
than the opening auction; a real account's market-on-open order at our sizes fills at the auction
price (median 0.4–0.5% of the cross). So the paper record understates what the rules would earn
by roughly 4–5% a year. Two corrections, both from the free Alpaca auctions data:

  1. Auction-basis NAV. Every filled paper order is re-priced at that day's opening cross:
     adjustment = (fill − cross) x qty for buys, (cross − fill) x qty for sells, positive when the
     simulator did worse. equity_auction = simulator equity + cumulative adjustment to that day.
     Both are kept; the evaluation point reads the auction basis, the simulator basis is the
     conservative floor.
  2. Missed trades. Insider entries the broker left unfilled (expired / canceled with 0 filled),
     entered at the next session's opening cross and exited at the cross five sessions later
     (the book's rule), with SPY over the same days. Recorded separately, never mixed into NAV.

Writes optradar.db agent_auction_nav and out/agent/auction_basis.json; read by the review, the
dashboard and the paper page. Runs after the close (auction data is 15 minutes delayed).

Usage: python -m agent.auction_basis
"""
from __future__ import annotations

import datetime as dt
import json
import os

import duckdb
import pandas as pd

from agent import ledger
from agent.books.short_term import HOLD_DAYS
from agent.sources.alpaca_auctions import AUCTIONS_DB, load as load_auctions
from hedge_fund.features.panel import PanelStore

OUT = "/Users/louis/optradar/out/agent/auction_basis.json"
DDL = """CREATE TABLE IF NOT EXISTS agent_auction_nav (as_of DATE, book VARCHAR, equity_sim DOUBLE, adj_cum DOUBLE,
         equity_auction DOUBLE, n_fills INT, PRIMARY KEY (as_of, book))"""


def sessions() -> list[dt.date]:
    with PanelStore(read_only=True) as store:
        return [r[0] for r in store.con.execute("SELECT trade_date FROM index_daily WHERE symbol = 'SPY' ORDER BY trade_date").fetchall()]


def next_session(sess: list[dt.date], d: dt.date, k: int = 1) -> dt.date | None:
    after = [s for s in sess if s > d]
    return after[k - 1] if len(after) >= k else None


def cross_prices(pairs: set[tuple[str, dt.date]]) -> dict[tuple[str, dt.date], float]:
    """Opening-cross price per (ticker, day); loads anything missing from Alpaca first."""
    if not pairs:
        return {}
    want = pd.DataFrame(sorted(pairs), columns=["ticker", "day"])
    try:
        con = duckdb.connect(str(AUCTIONS_DB), read_only=True)
        have = con.execute("SELECT ticker, day, open_px FROM auctions").df()
        con.close()
    except Exception:
        have = pd.DataFrame(columns=["ticker", "day", "open_px"])
    have["day"] = pd.to_datetime(have["day"]).dt.date
    known = set(zip(have["ticker"], have["day"]))
    missing = [p for p in pairs if p not in known and p[1] < dt.date.today() + dt.timedelta(days=1)]
    if missing:
        tick = sorted({t for t, _ in missing})
        load_auctions(tick, min(d for _, d in missing), max(d for _, d in missing))
        con = duckdb.connect(str(AUCTIONS_DB), read_only=True)
        have = con.execute("SELECT ticker, day, open_px FROM auctions").df()
        con.close()
        have["day"] = pd.to_datetime(have["day"]).dt.date
    return {(t, d): float(p) for t, d, p in zip(have["ticker"], have["day"], have["open_px"]) if pd.notna(p)}


def main() -> int:
    sess = sessions()
    con = ledger.connect()
    try:
        con.execute(DDL)
        fills = con.execute("""SELECT client_order_id, book, ticker, side, filled_qty, filled_avg_px, CAST(filled_at AS DATE) d, reason
                               FROM agent_orders WHERE dry_run = FALSE AND filled_qty > 0""").df()
        missed = con.execute("""SELECT client_order_id, book, ticker, as_of, qty, reason, status FROM agent_orders
                                WHERE dry_run = FALSE AND side = 'buy' AND book = 'insider' AND (filled_qty IS NULL OR filled_qty = 0)
                                  AND status IN ('expired', 'canceled', 'rejected', 'done_for_day')""").df()
        nav = con.execute("SELECT as_of, book, equity_usd FROM agent_book_nav ORDER BY as_of").df()
        for df_, col in ((fills, "d"), (missed, "as_of"), (nav, "as_of")):          # DuckDB DATE comes back as Timestamp
            if len(df_):
                df_[col] = pd.to_datetime(df_[col]).dt.date
        pairs = {(t, d) for t, d in zip(fills["ticker"], fills["d"])}
        plan = []
        for r in missed.itertuples(index=False):
            e = next_session(sess, r.as_of, 1)
            x = next_session(sess, r.as_of, 1 + HOLD_DAYS)
            plan.append((r, e, x))
            if e:
                pairs.add((r.ticker, e))
                pairs.add(("SPY", e))
            if x and x <= sess[-1]:
                pairs.add((r.ticker, x))
                pairs.add(("SPY", x))
        cross = cross_prices(pairs)

        fills["cross"] = [cross.get((t, d)) for t, d in zip(fills["ticker"], fills["d"])]
        fills["adj"] = [((px - c) if s == "buy" else (c - px)) * q if c else 0.0
                        for s, q, px, c in zip(fills["side"], fills["filled_qty"], fills["filled_avg_px"], fills["cross"])]
        fills["gap_pct"] = [((px / c - 1) * 100 * (1 if s == "buy" else -1)) if c else None
                            for s, px, c in zip(fills["side"], fills["filled_avg_px"], fills["cross"])]
        rows = []
        for (d, book), g in nav.groupby(["as_of", "book"]):
            f = fills[(fills["book"] == book) & (fills["d"] <= d)]
            adj = float(f["adj"].sum())
            rows.append([d, book, float(g["equity_usd"].iloc[0]), adj, float(g["equity_usd"].iloc[0]) + adj, int(len(f))])
        if rows:
            con.executemany("INSERT OR REPLACE INTO agent_auction_nav VALUES (?, ?, ?, ?, ?, ?)", rows)
    finally:
        con.close()

    with PanelStore(read_only=True) as store:                  # latest close, to mark open shadow trades
        last = dict(store.con.execute("""SELECT ticker, last(close ORDER BY trade_date) FROM bars
                                         WHERE trade_date >= current_date - 10 GROUP BY 1""").fetchall())
    miss_rows = []
    for r, e, x in plan:
        ep, xp = cross.get((r.ticker, e)) if e else None, cross.get((r.ticker, x)) if x else None
        se, sx = cross.get(("SPY", e)) if e else None, cross.get(("SPY", x)) if x else None
        done = bool(ep and xp)
        miss_rows.append({"ticker": r.ticker, "signal_day": str(r.as_of), "entry_day": str(e) if e else None, "exit_day": str(x) if x else None,
                          "entry_cross": ep, "exit_cross": xp, "status": "closed" if done else ("open" if ep else "no data"),
                          "ret_pct": (xp / ep - 1) * 100 if done else None,
                          "mark_pct": (last[r.ticker] / ep - 1) * 100 if ep and not done and last.get(r.ticker) else None,
                          "spy_pct": (sx / se - 1) * 100 if done and se and sx else None,
                          "reason": r.reason})
    closed = [m for m in miss_rows if m["ret_pct"] is not None]
    by_book = {}
    for b in sorted({r[1] for r in rows}):
        last = [r for r in rows if r[1] == b]
        last = max(last, key=lambda r: r[0])
        by_book[b] = {"as_of": str(last[0]), "equity_sim": last[2], "adj_cum": last[3], "equity_auction": last[4], "n_fills": last[5]}
    g = fills["gap_pct"].dropna()
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "books": by_book,
           "fills": {"n": int(len(fills)), "with_cross": int(g.size), "mean_gap_pct": float(g.mean()) if g.size else None,
                     "median_gap_pct": float(g.median()) if g.size else None},
           "fill_rows": [{"book": b, "ticker": t, "side": s, "day": str(d), "fill": px, "cross": c, "gap_pct": gp, "reason": rs}
                         for b, t, s, d, px, c, gp, rs in zip(fills["book"], fills["ticker"], fills["side"], fills["d"], fills["filled_avg_px"],
                                                                fills["cross"], fills["gap_pct"], fills["reason"])],
           "missed": miss_rows,
           "missed_summary": {"n": len(miss_rows), "closed": len(closed),
                              "mean_ret_pct": sum(m["ret_pct"] for m in closed) / len(closed) if closed else None,
                              "mean_abn_pct": (sum(m["ret_pct"] - m["spy_pct"] for m in closed if m["spy_pct"] is not None)
                                               / max(1, sum(1 for m in closed if m["spy_pct"] is not None))) if closed else None}}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    for b, v in by_book.items():
        print(f"{b:8s} sim ${v['equity_sim']:,.0f}  auction ${v['equity_auction']:,.0f}  (adj {v['adj_cum']:+,.0f}, {v['n_fills']} fills)")
    print("fills vs cross:", out["fills"])
    print("missed:", out["missed_summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
