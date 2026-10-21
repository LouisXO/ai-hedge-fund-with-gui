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

  3. Regulatory fees (S48). A real account pays, on the day of the trade: SEC fee 0.00206% of the
     sell notional, FINRA TAF $0.000195 per share sold (capped at $9.79 per trade) and CAT
     $0.000003 per share on both sides, each fee's daily total rounded up to the cent (per book
     here). equity_auction is net of this MODEL (fees_cum), so the basis stays comparable with the
     backtest over time. Since 2026-09-29 the paper account charges its own FEE activities too, and
     agent/cash_events.py books them into the books' cash (in equity_sim): fee_cash_cum (a positive
     number) puts them back, so they are not paid twice.
  4. Dividends (S48, agent/dividends.py). The backtest, the rule replay and SPY / QQQ are total
     return. div_cum is the running sum of the dividends the book's lots were entitled to by
     ex-date; equity_auction_tr = equity_auction + div_cum. Since 2026-10-14 the paper account pays
     DIV activities, which cash_events books into the books' cash on the day they count
     (equity_sim includes them): div_cash_cum takes them out again, so equity_auction stays the
     price NAV and a dividend counts once, on its ex-date.
     equity_auction = equity_sim - div_cash_cum + fee_cash_cum + adj_cum - fees_cum. A catch-up that
     restates agent_book_nav (cash_events) leaves every equity_auction as it was. The json also
     compares the broker's fees with the model's and the dividends received with those entitled,
     listing the entitled ones past their pay date that the broker has not paid.
     Unknown dividends are never $0 (2026-10-21, S54 investigation; before, a failed dividends.update
     rewrote every row with div_cum 0, so equity_auction_tr fell to the price NAV for the whole history):
     when the update fails, the rows it last stored (agent_dividends, rebuilt in one transaction) count
     for the days before the day they were computed; a later day keeps the div_cum a run with known
     dividends stored for it, else div_cum and equity_auction_tr are NULL. The json says which
     (dividends.status, tr_unknown_days); agent.drift, agent.evaluate and agent.health read NULL as unknown.

Missed trades that the book bought anyway: an unfilled insider entry whose ticker the book filled
within RETRY_DAYS afterwards (the S36b retry) is marked retried_filled and left out of the missed
comparison, so the same name is not counted as both bought and missed.

Writes optradar.db agent_auction_nav (and agent_dividends, agent_corp_actions) and out/agent/auction_basis.json; read by
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

from agent import cash_events, dividends, ledger
from agent.books.short_term import HOLD_DAYS
from agent.sources.alpaca_auctions import AUCTIONS_DB, load as load_auctions
from hedge_fund.features.panel import PanelStore

OUT = "/Users/louis/optradar/out/agent/auction_basis.json"
DDL = """CREATE TABLE IF NOT EXISTS agent_auction_nav (as_of DATE, book VARCHAR, equity_sim DOUBLE, adj_cum DOUBLE,
         equity_auction DOUBLE, n_fills INT, PRIMARY KEY (as_of, book))"""
# added 2026-09-29 (S48); rows written before keep NULL until the next run rewrites every row
ADD_COLUMNS = [("fees_cum", "DOUBLE"), ("div_cum", "DOUBLE"), ("equity_auction_tr", "DOUBLE")]
# added 2026-10-20 (agent/cash_events.py): the broker's DIV and FEE cash inside equity_sim, by the day it counts
ADD_COLUMNS += [("div_cash_cum", "DOUBLE"), ("fee_cash_cum", "DOUBLE")]
NAV_COLS = ["as_of", "book", "equity_sim", "adj_cum", "equity_auction", "n_fills", "fees_cum", "div_cum", "equity_auction_tr",
            "div_cash_cum", "fee_cash_cum"]

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


def nav_rows(nav: pd.DataFrame, fills: pd.DataFrame, fees: dict, div: pd.DataFrame, cash: pd.DataFrame | None = None,
             known_before: dt.date | None = None, stored: dict | None = None) -> list[list]:
    """One agent_auction_nav row per (day, book) of agent_book_nav, in NAV_COLS order.

    cash: the booked broker DIV / FEE rows (cash_events.load). equity_sim holds them from their cash_day on, so the
    auction basis takes the DIV cash out (div_cum counts the dividend on its ex-date) and the broker's fees back in
    (fees_cum is the model): equity_auction = equity_sim - div_cash_cum + fee_cash_cum + adj_cum - fees_cum.
    known_before (soft_dividends): `div` counts the dividends of the days before it only (None: every day); a later
    day takes its stored div_cum ({(day, book): div_cum} of the rows a run with known dividends wrote), else div_cum
    and equity_auction_tr are None: unknown, never $0."""
    rows = []
    for (d, book), g in nav.groupby(["as_of", "book"]):
        f = fills[(fills["book"] == book) & (fills["d"] <= d)] if len(fills) else fills
        adj = float(f["adj"].sum()) if len(f) else 0.0
        fee = float(sum(v for (b, fd), v in fees.items() if b == book and fd <= d))
        if known_before is None or d < known_before:
            dv = dividends.cumulative(div, [d], book)[d] if len(div) else 0.0
        else:
            dv = (stored or {}).get((d, book))
        div_cash = cash_events.cumulative(cash, d, book, "DIV")
        fee_cash = 0.0 - cash_events.cumulative(cash, d, book, "FEE")      # 0.0 - x: no -0.0 in the table
        sim = float(g["equity_usd"].iloc[0])
        auc = sim - div_cash + fee_cash + adj - fee
        rows.append([d, book, sim, adj, auc, int(len(f)), fee, dv, None if dv is None else auc + dv, div_cash, fee_cash])
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


DIV_COLS = dividends.DIV_COLS


def soft_dividends(con) -> tuple[pd.DataFrame, dict]:
    """dividends.update, but a failure there does not stop the auction basis (the price NAV, the missed-trade shadow
    and auction_basis.json are still written), and dividends it could not establish are never counted as $0.

    Returns (rows, info); info["status"] is
      ok        the update ran
      cache     it ran, but the corporate-actions fetch failed and the cached actions were used (the cache is kept)
      fallback  it failed (stored_dividends): the rows the last successful update stored, computed at info["computed_at"]
      unknown   it failed and no stored rows can be used
    info["error"] is the text for the json (None when ok); info["known_before"] is nav_rows' argument: None, every day
    from these rows."""
    try:
        with PanelStore(read_only=True) as store:
            rows = dividends.update(con, store)
    except Exception as e:                        # noqa: BLE001 — reported in the json and on stdout
        return stored_dividends(con, f"{type(e).__name__}: {e}")
    fe = rows.attrs.get("fetch_error")
    if fe:
        print(f"corporate actions not fetched ({fe}); dividends from the cached ones")
    return rows, {"status": "cache" if fe else "ok", "error": f"corporate actions fetch failed, cache kept: {fe}" if fe else None,
                  "computed_at": None, "known_before": None}


def stored_dividends(con, msg: str) -> tuple[pd.DataFrame, dict]:
    """soft_dividends once dividends.update has failed with `msg`: agent_dividends as the last successful update left it
    (it rebuilds the table in one transaction, so a failure never leaves it half written). Those rows count every
    ex-date up to the last NAV session before the day they were computed, so they are used for the days before that day
    (known_before); a later day keeps the div_cum a run with known dividends stored for it, else it is unknown. Rows
    that cannot be read, or an empty table while paper lots exist (no date to trust it to), leave every day to its
    stored div_cum (status unknown); with no paper lot nothing is owed and $0 is known."""
    empty = pd.DataFrame(columns=DIV_COLS)
    why = None
    try:
        rows = con.execute("SELECT * FROM agent_dividends").df().reindex(columns=DIV_COLS)
        at = pd.Timestamp(rows["computed_at"].min()) if len(rows) else None
    except Exception as e:                        # noqa: BLE001 — named in the error text
        rows, at, why = empty, None, f"{type(e).__name__}: {str(e)[:120]}"
    if at is not None and not pd.isna(at):
        err = (f"dividends update failed ({msg}); fell back to the rows stored at {at:%Y-%m-%d %H:%M} (ex-dates through the "
               f"session before {at.date()}; a later day keeps its stored div_cum or is left unknown)")
        print(err)
        return rows, {"status": "fallback", "error": err, "computed_at": at.isoformat(timespec="seconds"), "known_before": at.date()}
    try:
        no_lots = why is None and dividends.paper_lots(con).empty
    except Exception:                             # noqa: BLE001 — lots unknown: they may be owed something
        no_lots = False
    if no_lots:
        err = f"dividends update failed ({msg}); no paper lot, so no dividend is owed"
        print(err)
        return empty, {"status": "fallback", "error": err, "computed_at": None, "known_before": None}
    err = (f"dividends update failed ({msg}); no stored rows to fall back on ({why or 'agent_dividends is empty'}): "
           "every day keeps its stored div_cum, a day without one is left unknown (NULL), never $0")
    print(err)
    return empty, {"status": "unknown", "error": err, "computed_at": None, "known_before": dt.date.min}


def stored_div_cum(con) -> dict[tuple[dt.date, str], float]:
    """{(day, book): div_cum} of the agent_auction_nav rows that have one (a run with known dividends wrote it); {} when
    unreadable, so a day the dividends do not cover is then unknown."""
    try:
        got = con.execute("SELECT as_of, book, div_cum FROM agent_auction_nav WHERE div_cum IS NOT NULL").fetchall()
    except Exception:                             # noqa: BLE001
        return {}
    return {(pd.Timestamp(d).date(), b): float(v) for d, b, v in got}


def broker_vs_model(div: pd.DataFrame, cash: pd.DataFrame, fees: dict, today: dt.date) -> tuple[dict, dict]:
    """(fees, dividends) parts of the json: the broker's fees next to the model's (also from the broker's first fee
    day, the comparable window), the DIV cash received next to the dividends entitled, and the entitled ones past
    their pay date that the broker has not paid (cash_events.unpaid, no grace)."""
    fee_ev = cash[cash["kind"] == "FEE"] if len(cash) else cash
    div_ev = cash[cash["kind"] == "DIV"] if len(cash) else cash
    first = min(fee_ev["activity_date"]) if len(fee_ev) else None
    books = sorted({b for b, _ in fees} | set(fee_ev["book"] if len(fee_ev) else []))
    f = {"broker_total": round(0.0 - float(fee_ev["net_usd"].sum()), 2) if len(fee_ev) else 0.0,
         "broker_by_book": {b: round(0.0 - float(fee_ev.loc[fee_ev["book"] == b, "net_usd"].sum()), 2) for b in books} if len(fee_ev) else {},
         "model_by_book": {b: round(sum(v for (bb, _), v in fees.items() if bb == b), 2) for b in books},
         "broker_first_day": str(first) if first else None,
         "model_since_broker_first_day": round(sum(v for (_, d), v in fees.items() if first and d >= first), 2) if first else None}
    cash_div = div[div["kind"] == "dividend"] if len(div) else div
    ent = [{"ticker": r.ticker, "book": r.book, "lot_id": r.lot_id, "ex_date": pd.Timestamp(r.ex_date).date(),
            "record_date": None if pd.isna(getattr(r, "record_date", None)) else pd.Timestamp(r.record_date).date(),
            "pay_date": None if pd.isna(getattr(r, "pay_date", None)) else pd.Timestamp(r.pay_date).date(), "usd": float(r.usd)}
           for r in cash_div.itertuples()] if len(cash_div) else []
    paid = [{"ticker": t, "record_date": rd, "activity_date": ad}
            for t, rd, ad in zip(div_ev["ticker"], div_ev["record_date"], div_ev["activity_date"])] if len(div_ev) else []
    unpaid = cash_events.unpaid(ent, paid, today, grace_days=0)
    dv = {"received_usd": round(float(div_ev["net_usd"].sum()), 2) if len(div_ev) else 0.0,
          "received_by_book": {b: round(float(g["net_usd"].sum()), 2) for b, g in div_ev.groupby("book")} if len(div_ev) else {},
          "entitled_by_book": {b: round(float(g["usd"].sum()), 2) for b, g in cash_div.groupby("book")} if len(cash_div) else {},
          "unpaid_past_pay_date": [{k: (str(v) if isinstance(v, dt.date) else v) for k, v in u.items()} for u in unpaid],
          "unpaid_usd": round(sum(u["usd"] for u in unpaid), 2)}
    return f, dv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=ledger.OPTRADAR_DB, help="the ledger (a copy, for a test run)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    sess = sessions()
    con = ledger.connect(args.db)
    try:
        ensure_table(con)
        div, dinfo = soft_dividends(con)
        cash = cash_events.load(con)
        fills = con.execute("""SELECT client_order_id, book, ticker, side, filled_qty, filled_avg_px, CAST(filled_at AS DATE) d, reason,
                                      order_type, limit_price
                               FROM agent_orders WHERE dry_run = FALSE AND filled_qty > 0""").df()
        missed = con.execute("""SELECT client_order_id, book, ticker, as_of, qty, reason, status FROM agent_orders
                                WHERE dry_run = FALSE AND side = 'buy' AND book = 'insider' AND (filled_qty IS NULL OR filled_qty = 0)
                                  AND status IN ('expired', 'canceled', 'rejected', 'done_for_day')
                                  AND alpaca_id IS NOT NULL""").df()          # a POST the broker refused (S48) never met a price
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
        stored = stored_div_cum(con) if dinfo["known_before"] is not None else None    # read before the rows are replaced
        rows = nav_rows(nav, fills, fees, div, cash, dinfo["known_before"], stored)
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
    fee_cmp, div_cmp = broker_vs_model(div, cash, fees, dt.date.today())
    tr_at = NAV_COLS.index("equity_auction_tr")
    tr_unknown = {b: [str(r[0]) for r in rows if r[1] == b and r[tr_at] is None] for b in sorted({r[1] for r in rows})}
    tr_unknown = {b: v for b, v in tr_unknown.items() if v}                   # the days left NULL, per book
    known = dinfo["status"] != "unknown"
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
                    "total": float(sum(fees.values())) if fees else 0.0, **fee_cmp},
           "dividends": {"n": (int((div["kind"] == "dividend").sum()) if len(div) else 0) if known else None,
                         "usd": (float(div.loc[div["kind"] == "dividend", "usd"].sum()) if len(div) else 0.0) if known else None,
                         "to_check": div.attrs.get("to_check") or [], "error": dinfo["error"], "status": dinfo["status"],
                         "computed_at": dinfo["computed_at"], "tr_unknown_days": tr_unknown,
                         "fetch_error": div.attrs.get("fetch_error"), "factor_error": div.attrs.get("factor_error"), **div_cmp},
           "missed_summary": {"n": len(miss_rows) - sum(m["retried_filled"] for m in miss_rows), "closed": len(closed),
                              "n_retried_filled": sum(m["retried_filled"] for m in miss_rows),
                              "mean_ret_pct": sum(m["ret_pct"] for m in closed) / len(closed) if closed else None,
                              "mean_abn_pct": (sum(m["ret_pct"] - m["spy_pct"] for m in closed if m["spy_pct"] is not None)
                                               / max(1, sum(1 for m in closed if m["spy_pct"] is not None))) if closed else None}}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    for b, v in by_book.items():
        tr = (f"  + dividends {v['div_cum']:,.2f} = ${v['equity_auction_tr']:,.0f}" if v["div_cum"] is not None
              else "  + dividends unknown: with-dividends NAV not written")
        print(f"{b:8s} sim ${v['equity_sim']:,.0f} (broker DIV {v['div_cash_cum']:,.2f}, FEE {v['fee_cash_cum']:,.2f})  auction "
              f"${v['equity_auction']:,.0f}  (adj {v['adj_cum']:+,.0f}, fees {v['fees_cum']:,.2f}, {v['n_fills']} fills){tr}")
    if tr_unknown:
        print("with-dividends NAV left unknown (NULL):", tr_unknown)
    print("fills vs cross:", out["fills"])
    print("missed:", out["missed_summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
