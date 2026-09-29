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

  3. Regulatory fees (S48). The paper account charges none; a real one pays, on the day of the
     trade: SEC fee 0.00206% of the sell notional, FINRA TAF $0.000195 per share sold (capped at
     $9.79 per trade) and CAT $0.000003 per share on both sides, each fee's daily total rounded
     up to the cent (per book here). equity_auction is net of them (fees_cum).
  4. Dividends (S48, agent/dividends.py). The paper account pays none; the backtest, the rule
     replay and SPY / QQQ are total return. div_cum is the running sum of the dividends the
     book's lots were holder of record for; equity_auction_tr = equity_auction + div_cum. Both
     the price NAV and the total-return NAV are kept; neither touches the book's cash.

Missed trades that the book bought anyway: an unfilled insider entry whose ticker the book filled
within RETRY_DAYS afterwards (the S36b retry) is marked retried_filled and left out of the missed
comparison, so the same name is not counted as both bought and missed.

Writes optradar.db agent_auction_nav (and agent_dividends) and out/agent/auction_basis.json; read by
the review, the dashboard and the paper page. Runs after the close (auction data is 15 minutes delayed).

Usage: python -m agent.auction_basis [--db PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os

import duckdb
import pandas as pd

from agent import dividends, ledger
from agent.books.short_term import HOLD_DAYS
from agent.sources.alpaca_auctions import AUCTIONS_DB, load as load_auctions
from hedge_fund.features.panel import PanelStore

OUT = "/Users/louis/optradar/out/agent/auction_basis.json"
DDL = """CREATE TABLE IF NOT EXISTS agent_auction_nav (as_of DATE, book VARCHAR, equity_sim DOUBLE, adj_cum DOUBLE,
         equity_auction DOUBLE, n_fills INT, PRIMARY KEY (as_of, book))"""
# added 2026-09-29 (S48); rows written before keep NULL until the next run rewrites every row
ADD_COLUMNS = [("fees_cum", "DOUBLE"), ("div_cum", "DOUBLE"), ("equity_auction_tr", "DOUBLE")]
NAV_COLS = ["as_of", "book", "equity_sim", "adj_cum", "equity_auction", "n_fills", "fees_cum", "div_cum", "equity_auction_tr"]

SEC_RATE = 0.0000206            # SEC fee, of the sell notional (Alpaca fee schedule, revised 2026-09-17)
TAF_PER_SHARE = 0.000195        # FINRA trading activity fee, per share sold ...
TAF_CAP = 9.79                  # ... capped per trade
CAT_PER_SHARE = 0.000003        # consolidated audit trail fee, per share bought or sold
RETRY_DAYS = 8                  # execute.insider_targets' look-back for a retry (calendar days)


def ensure_table(con) -> None:
    con.execute(DDL)
    have = {r[0] for r in con.execute("DESCRIBE agent_auction_nav").fetchall()}
    for col, typ in ADD_COLUMNS:
        if col not in have:
            con.execute(f"ALTER TABLE agent_auction_nav ADD COLUMN {col} {typ}")


def ceil_cent(x: float) -> float:
    """Round a fee total UP to the cent (round first, so 0.07 stored as 0.0700000001 stays 0.07)."""
    return math.ceil(round(x * 100, 6)) / 100


def fees_by_day(fills: pd.DataFrame) -> dict[tuple[str, dt.date], float]:
    """{(book, fill day): SEC + TAF + CAT}, each fee's daily total rounded up to the cent.

    fills: book, d, side, filled_qty, filled_avg_px (one row per order that filled).
    """
    out = {}
    for (book, d), g in fills.groupby(["book", "d"]):
        sells = g[g["side"] == "sell"]
        sec = ceil_cent(float((sells["filled_qty"] * sells["filled_avg_px"]).sum()) * SEC_RATE)
        taf = ceil_cent(float(sum(min(q * TAF_PER_SHARE, TAF_CAP) for q in sells["filled_qty"])))
        cat = ceil_cent(float(g["filled_qty"].sum()) * CAT_PER_SHARE)
        out[(book, d)] = sec + taf + cat
    return out


def nav_rows(nav: pd.DataFrame, fills: pd.DataFrame, fees: dict, div: pd.DataFrame) -> list[list]:
    """One agent_auction_nav row per (day, book) of agent_book_nav, in NAV_COLS order."""
    rows = []
    for (d, book), g in nav.groupby(["as_of", "book"]):
        f = fills[(fills["book"] == book) & (fills["d"] <= d)] if len(fills) else fills
        adj = float(f["adj"].sum()) if len(f) else 0.0
        fee = float(sum(v for (b, fd), v in fees.items() if b == book and fd <= d))
        dv = dividends.cumulative(div, [d], book)[d] if len(div) else 0.0
        sim = float(g["equity_usd"].iloc[0])
        auc = sim + adj - fee
        rows.append([d, book, sim, adj, auc, int(len(f)), fee, dv, auc + dv])
    return rows


def mark_retried(missed: pd.DataFrame, bought: pd.DataFrame, days: int = RETRY_DAYS) -> list[bool]:
    """For each missed entry: did the book fill a buy of the same ticker within `days` after its as_of?

    bought: ticker, as_of of the book's filled buy orders.
    """
    out = []
    for t, a in zip(missed["ticker"], missed["as_of"]):
        b = bought[bought["ticker"] == t]["as_of"] if len(bought) else []
        out.append(any(a < x <= a + dt.timedelta(days=days) for x in b))
    return out


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


DIV_COLS = ["lot_id", "book", "ticker", "ex_date", "per_share", "qty", "usd", "kind", "computed_at"]


def soft_dividends(con) -> tuple[pd.DataFrame, str | None]:
    """dividends.update, but a failure there does not stop the auction basis: the price NAV, the missed-trade
    shadow and auction_basis.json are still written (div_cum 0 for the day; postclose also runs agent.dividends
    on its own). Returns (rows, error text or None)."""
    try:
        with PanelStore(read_only=True) as store:
            return dividends.update(con, store), None
    except Exception as e:                        # noqa: BLE001 — reported in the json and on stdout
        msg = f"{type(e).__name__}: {e}"
        print(f"dividends skipped ({msg}); NAV written without them")
        return pd.DataFrame(columns=DIV_COLS), msg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=ledger.OPTRADAR_DB, help="the ledger (a copy, for a test run)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    sess = sessions()
    con = ledger.connect(args.db)
    try:
        ensure_table(con)
        div, div_error = soft_dividends(con)
        fills = con.execute("""SELECT client_order_id, book, ticker, side, filled_qty, filled_avg_px, CAST(filled_at AS DATE) d, reason,
                                      order_type, limit_price
                               FROM agent_orders WHERE dry_run = FALSE AND filled_qty > 0""").df()
        missed = con.execute("""SELECT client_order_id, book, ticker, as_of, qty, reason, status FROM agent_orders
                                WHERE dry_run = FALSE AND side = 'buy' AND book = 'insider' AND (filled_qty IS NULL OR filled_qty = 0)
                                  AND status IN ('expired', 'canceled', 'rejected', 'done_for_day')""").df()
        bought = con.execute("""SELECT ticker, as_of FROM agent_orders
                                WHERE dry_run = FALSE AND side = 'buy' AND book = 'insider' AND filled_qty > 0""").df()
        nav = con.execute("SELECT as_of, book, equity_usd FROM agent_book_nav ORDER BY as_of").df()
        for df_, col in ((fills, "d"), (missed, "as_of"), (nav, "as_of"), (bought, "as_of")):   # DuckDB DATE comes back as Timestamp
            if len(df_):
                df_[col] = pd.to_datetime(df_[col]).dt.date
        missed["retried_filled"] = mark_retried(missed, bought) if len(missed) else []
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
        # A limit order cannot fill in the opening cross when the cross is beyond its limit (S36c: entries are
        # DAY limits at close + 3%). Such a buy filled later in the day, when the price came back; a real account
        # with the same order gets about the limit, not the cross. Re-pricing it at the (higher) cross would book
        # a purchase nobody could have made and understate the book by 9-12%/yr on the insider line (S45 D1).
        # So: no adjustment for these fills, and they are reported apart from the execution gap.
        def beyond(s, c, lim):
            return bool(c and pd.notna(lim) and lim and ((s == "buy" and c > lim) or (s == "sell" and c < lim)))
        fills["gap_fill"] = [beyond(s, c, lim) for s, c, lim in zip(fills["side"], fills["cross"], fills["limit_price"])]
        fills["adj"] = [0.0 if (gf or not c) else ((px - c) if s == "buy" else (c - px)) * q
                        for s, q, px, c, gf in zip(fills["side"], fills["filled_qty"], fills["filled_avg_px"], fills["cross"], fills["gap_fill"])]
        fills["gap_pct"] = [None if (gf or not c) else ((px / c - 1) * 100 * (1 if s == "buy" else -1))
                            for s, px, c, gf in zip(fills["side"], fills["filled_avg_px"], fills["cross"], fills["gap_fill"])]
        fees = fees_by_day(fills) if len(fills) else {}
        rows = nav_rows(nav, fills, fees, div)
        if rows:
            con.executemany(f"INSERT OR REPLACE INTO agent_auction_nav ({', '.join(NAV_COLS)}) VALUES ({', '.join('?' * len(NAV_COLS))})", rows)
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
        status = "retried_filled" if r.retried_filled else "closed" if done else ("open" if ep else "no data")
        miss_rows.append({"ticker": r.ticker, "signal_day": str(r.as_of), "entry_day": str(e) if e else None, "exit_day": str(x) if x else None,
                          "entry_cross": ep, "exit_cross": xp, "status": status, "retried_filled": bool(r.retried_filled),
                          "ret_pct": (xp / ep - 1) * 100 if done else None,
                          "mark_pct": (last[r.ticker] / ep - 1) * 100 if ep and not done and last.get(r.ticker) else None,
                          "spy_pct": (sx / se - 1) * 100 if done and se and sx else None,
                          "reason": r.reason})
    closed = [m for m in miss_rows if m["ret_pct"] is not None and not m["retried_filled"]]   # a retried name is not a missed trade
    by_book = {}
    for b in sorted({r[1] for r in rows}):
        last = [r for r in rows if r[1] == b]
        last = max(last, key=lambda r: r[0])
        by_book[b] = dict(zip(NAV_COLS, last))
        by_book[b]["as_of"] = str(last[0])
        del by_book[b]["book"]
    g = fills["gap_pct"].dropna()
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "books": by_book,
           "fills": {"n": int(len(fills)), "with_cross": int(fills["cross"].notna().sum()) if len(fills) else 0,
                     "n_gap_fills": int(fills["gap_fill"].sum()) if len(fills) else 0,
                     "mean_gap_pct": float(g.mean()) if g.size else None,
                     "median_gap_pct": float(g.median()) if g.size else None},
           "fill_rows": [{"book": b, "ticker": t, "side": s, "day": str(d), "fill": px, "cross": c, "gap_pct": gp, "reason": rs,
                          "limit": (None if pd.isna(lim) else float(lim)), "gap_fill": bool(gf)}
                         for b, t, s, d, px, c, gp, rs, lim, gf in zip(fills["book"], fills["ticker"], fills["side"], fills["d"], fills["filled_avg_px"],
                                                                         fills["cross"], fills["gap_pct"], fills["reason"], fills["limit_price"], fills["gap_fill"])],
           "missed": miss_rows,
           "fees": {"sec_rate": SEC_RATE, "taf_per_share": TAF_PER_SHARE, "taf_cap": TAF_CAP, "cat_per_share": CAT_PER_SHARE,
                    "total": float(sum(fees.values())) if fees else 0.0},
           "dividends": {"n": int((div["kind"] == "dividend").sum()) if len(div) else 0,
                         "usd": float(div.loc[div["kind"] == "dividend", "usd"].sum()) if len(div) else 0.0,
                         "to_check": [{"ticker": t, "ex_date": str(x), "kind": k} for t, x, k in
                                      zip(div["ticker"], div["ex_date"], div["kind"]) if k != "dividend"] if len(div) else [],
                         "error": div_error},
           "missed_summary": {"n": len(miss_rows) - sum(m["retried_filled"] for m in miss_rows), "closed": len(closed),
                              "n_retried_filled": sum(m["retried_filled"] for m in miss_rows),
                              "mean_ret_pct": sum(m["ret_pct"] for m in closed) / len(closed) if closed else None,
                              "mean_abn_pct": (sum(m["ret_pct"] - m["spy_pct"] for m in closed if m["spy_pct"] is not None)
                                               / max(1, sum(1 for m in closed if m["spy_pct"] is not None))) if closed else None}}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    for b, v in by_book.items():
        print(f"{b:8s} sim ${v['equity_sim']:,.0f}  auction ${v['equity_auction']:,.0f}  (adj {v['adj_cum']:+,.0f}, fees {v['fees_cum']:,.2f}, "
              f"{v['n_fills']} fills)  + dividends {v['div_cum']:,.2f} = ${v['equity_auction_tr']:,.0f}")
    print("fills vs cross:", out["fills"])
    print("missed:", out["missed_summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
