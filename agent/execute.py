"""Paper execution on Alpaca: the two stock books, run after the close.

What the backtest did (agent/books/engine.py), and what this does with a
real paper account:

  backtest                                  here
  ------------------------------------      ------------------------------------------
  targets chosen with data through day D    scored on D's completed bar, after 16:00 ET
  fills at D+1 open                         OPG orders: limit-on-open buys, market-on-open sells
                                            (DAY orders queued for the open if run 09:28-19:00 ET)
  cost = 0.5 x quoted spread each side      whatever the opening auction gives; measured, not assumed
  long book: enter rank<=30 into free       same, with the book's own lots as the state
    slots, exit when rank>60
  insider book: enter the day's filings,    same; hold_until = fill day + 5 sessions
    exit at the 5th open after entry
  equal $ slots = book equity / N,          same, whole shares, book cash tracked in agent_books
    idle slots in cash

One account, several books. A ticker is in at most one book, so every
Alpaca position maps to exactly one open lot (agent_lots) and any mismatch
is reported and that ticker frozen until it is explained. Every order has
client_order_id = "<book>|<as_of>|<ticker>|<side>", so a rerun cannot
submit twice.

Guards: paper endpoint + PK key + PA account (agent/broker/alpaca.py);
`--submit` is required to send anything, the default prints the order
list; per-order and per-run notional caps; bars must be through the last
session or nothing is planned (a stale signal is worse than no trade).

Usage:
  python -m agent.execute [--submit] [--no-update] [--book long|insider] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from agent import ledger, signals_insider
from agent.books import live as long_live
from agent.books.data import load_market
from agent.books.long_term import TOP_N
from agent.books.short_term import HOLD_DAYS, MAX_POSITIONS as INSIDER_SLOTS
from agent.backfill import backfill_index
from agent.broker import alpaca as broker_mod
from agent.sources.alpaca_bars import update as update_bars
from hedge_fund.features.panel import PanelStore

ET = ZoneInfo("America/New_York")
OUT_DIR = "/Users/louis/optradar/out/agent"

# Allocation of the $100k paper account. The rest ($10k) is reserved for the option book.
BOOKS = {
    "long":    {"alloc_usd": 60_000.0, "max_positions": TOP_N, "hold_days": None,
                "entry_cap_pct": 3.0},        # LOO cap: buy at the open unless it gaps > 3% over D's close
    "insider": {"alloc_usd": 30_000.0, "max_positions": INSIDER_SLOTS, "hold_days": HOLD_DAYS,
                "entry_cap_pct": None},       # cap = one quoted spread (S12/S13: the edge is ~one spread)
}
MAX_ORDERS_PER_RUN = 60
MAX_ORDER_NOTIONAL = 10_000.0
OPEN_STATES = {"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled", "held", "submitted"}
FINAL_STATES = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}


def client_id(book: str, as_of: dt.date, ticker: str, side: str) -> str:
    return f"{book}|{as_of.isoformat()}|{ticker}|{side}"


# ---------------------------------------------------------------- planning (pure) ----
def opg_window(now_et: dt.datetime) -> bool:
    """Alpaca accepts OPG orders only after 19:00 and before 09:28 ET (error 40310000 otherwise).
    Outside that window the same orders go as DAY: submitted after the close they are queued for
    the next open, so a limit buy still fills at the opening print when it is under the cap."""
    t = now_et.time()
    return t >= dt.time(19, 0) or t < dt.time(9, 28)


def plan_book(book: str, cfg: dict, lots: list[dict], ranked: list[str], keep: set[str], as_of: dt.date,
              next_session: dt.date, ref_close: dict[str, float], spread_pct: dict[str, float],
              cash_usd: float, blocked: set[str], scored: bool = True, tif: str = "opg") -> list[dict]:
    """The engine's one-day step, as orders for the next open.

    lots: this book's open lots ({ticker, qty, hold_until}); ranked: entry candidates
    in priority order; keep: names the book still wants (long book: top 2N; insider
    book: irrelevant, exits are by hold_until); blocked: tickers held by another book
    or unreconciled. Returns dicts ready for agent_orders.
    """
    orders: list[dict] = []
    exits: set[str] = set()
    for lot in lots:
        t = lot["ticker"]
        hu = lot.get("hold_until")
        expired = hu is not None and pd.Timestamp(hu).date() <= next_session
        dropped = hu is None and scored and t not in keep
        if (expired or dropped) and t not in exits:
            exits.add(t)
            orders.append({"client_order_id": client_id(book, as_of, t, "sell"), "book": book, "as_of": as_of,
                           "ticker": t, "side": "sell", "qty": int(lot["qty"]), "order_type": "market", "tif": tif,
                           "limit_price": None, "ref_close": ref_close.get(t),
                           "reason": "hold_expired" if expired else "rank_out"})
    equity = cash_usd + sum(float(l["qty"]) * ref_close.get(l["ticker"], float(l.get("entry_px") or 0.0)) for l in lots)
    slot = equity / cfg["max_positions"]
    cash_plan = cash_usd + sum(int(l["qty"]) * ref_close.get(l["ticker"], 0.0) for l in lots if l["ticker"] in exits)
    n_open = len(lots) - len(exits)
    held = {l["ticker"] for l in lots}
    if not scored:
        return orders
    for t in ranked:
        if n_open >= cfg["max_positions"]:
            break
        if t in held or t in blocked or t in exits:
            continue
        px = ref_close.get(t)
        if px is None or not np.isfinite(px) or px <= 0 or cash_plan < slot * 0.5:
            continue
        cap = cfg["entry_cap_pct"] if cfg["entry_cap_pct"] is not None else max(spread_pct.get(t, 0.5), 0.1)
        limit = round(px * (1 + cap / 100), 2 if px >= 1 else 4)
        spend = min(slot, cash_plan, MAX_ORDER_NOTIONAL)
        qty = math.floor(spend / limit)
        if qty < 1:
            continue
        orders.append({"client_order_id": client_id(book, as_of, t, "buy"), "book": book, "as_of": as_of,
                       "ticker": t, "side": "buy", "qty": qty, "order_type": "limit", "tif": tif,
                       "limit_price": limit, "ref_close": px, "reason": "entry"})
        cash_plan -= qty * limit
        n_open += 1
    return orders


# ---------------------------------------------------------------- ledger helpers ------
def ensure_books(con) -> dict[str, dict]:
    rows = {r[0]: {"book": r[0], "alloc_usd": r[1], "cash_usd": r[2], "max_positions": r[3]}
            for r in con.execute("SELECT book, alloc_usd, cash_usd, max_positions FROM agent_books").fetchall()}
    for book, cfg in BOOKS.items():
        if book not in rows:
            con.execute("INSERT INTO agent_books VALUES (?, ?, ?, ?, ?, ?)",
                        [book, cfg["alloc_usd"], cfg["alloc_usd"], cfg["max_positions"], dt.date.today(), pd.Timestamp.now()])
            rows[book] = {"book": book, "alloc_usd": cfg["alloc_usd"], "cash_usd": cfg["alloc_usd"],
                          "max_positions": cfg["max_positions"]}
    return rows


def open_lots(con, book: str | None = None) -> list[dict]:
    q = "SELECT lot_id, book, ticker, qty, entry_day, entry_px, hold_until FROM agent_lots WHERE status = 'open'"
    params = []
    if book:
        q += " AND book = ?"
        params.append(book)
    cols = ["lot_id", "book", "ticker", "qty", "entry_day", "entry_px", "hold_until"]
    return [dict(zip(cols, r)) for r in con.execute(q, params).fetchall()]


def _sessions_after(calendar: list[dt.date], day: dt.date, n: int) -> dt.date:
    later = [d for d in calendar if d > day]
    return later[min(n - 1, len(later) - 1)] if later else day + dt.timedelta(days=n)


def sync_fills(con, broker, calendar: list[dt.date]) -> dict:
    """Pull the state of every order we submitted that is not final; open/close lots on fills."""
    pending = con.execute("""SELECT client_order_id, book, ticker, side, qty FROM agent_orders
                             WHERE dry_run = FALSE AND (status IS NULL OR status NOT IN ('filled','canceled','expired','rejected','done_for_day','replaced'))"""
                          ).fetchall()
    stats = {"checked": len(pending), "filled": 0, "closed": 0, "final_unfilled": 0}
    for coid, book, ticker, side, qty in pending:
        o = broker.order_by_client_id(coid)
        if o is None:
            continue
        status = o.get("status")
        fq = float(o.get("filled_qty") or 0)
        fpx = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
        fat = pd.Timestamp(o["filled_at"]).tz_convert(ET).tz_localize(None) if o.get("filled_at") else None
        con.execute("""UPDATE agent_orders SET status = ?, alpaca_id = ?, filled_qty = ?, filled_avg_px = ?, filled_at = ?
                       WHERE client_order_id = ?""", [status, o.get("id"), fq, fpx, fat, coid])
        if fq <= 0 or fpx is None:
            if status in FINAL_STATES:
                stats["final_unfilled"] += 1
            continue
        fill_day = fat.date()
        if side == "buy":
            hold = BOOKS[book]["hold_days"]
            hold_until = _sessions_after(calendar, fill_day, hold) if hold else None
            con.execute("""INSERT OR REPLACE INTO agent_lots VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL, NULL, 'open', ?, NULL)""",
                        [f"{book}|{ticker}|{fill_day.isoformat()}", book, ticker, fq, fill_day, fpx, hold_until, coid])
            con.execute("UPDATE agent_books SET cash_usd = cash_usd - ?, updated = ? WHERE book = ?",
                        [fq * fpx, pd.Timestamp.now(), book])
            stats["filled"] += 1
        else:
            lot = con.execute("""SELECT lot_id, entry_px, qty FROM agent_lots WHERE book = ? AND ticker = ? AND status = 'open'
                                 ORDER BY entry_day LIMIT 1""", [book, ticker]).fetchone()
            if lot:
                lot_id, entry_px, lqty = lot
                ret = (fpx / entry_px - 1) * 100 if entry_px else None
                if fq >= lqty - 1e-6:
                    con.execute("""UPDATE agent_lots SET status='closed', exit_day=?, exit_px=?, ret_pct=?, exit_order=? WHERE lot_id=?""",
                                [fill_day, fpx, ret, coid, lot_id])
                else:                                            # partial: shrink the lot, keep it open
                    con.execute("UPDATE agent_lots SET qty = qty - ? WHERE lot_id = ?", [fq, lot_id])
                con.execute("UPDATE agent_books SET cash_usd = cash_usd + ?, updated = ? WHERE book = ?",
                            [fq * fpx, pd.Timestamp.now(), book])
                stats["closed"] += 1
    return stats


def fill_model_px(con, store: PanelStore) -> int:
    """The backtest's fill for each executed order = that day's open. The gap is the execution cost."""
    rows = con.execute("""SELECT client_order_id, ticker, CAST(filled_at AS DATE) FROM agent_orders
                          WHERE filled_qty > 0 AND model_px IS NULL AND dry_run = FALSE""").fetchall()
    n = 0
    for coid, ticker, day in rows:
        r = store.con.execute("SELECT open FROM bars WHERE ticker = ? AND trade_date = ?", [ticker, day]).fetchone()
        if r and r[0] is not None:
            con.execute("UPDATE agent_orders SET model_px = ? WHERE client_order_id = ?", [float(r[0]), coid])
            con.execute("""UPDATE agent_lots SET entry_model_px = ? WHERE entry_order = ? AND entry_model_px IS NULL""", [float(r[0]), coid])
            con.execute("""UPDATE agent_lots SET exit_model_px = ? WHERE exit_order = ? AND exit_model_px IS NULL""", [float(r[0]), coid])
            n += 1
    return n


def reconcile(lots: list[dict], positions: dict[str, dict]) -> tuple[set[str], list[str]]:
    """Every position must be one open lot with the same qty. Returns (blocked tickers, messages)."""
    by_t: dict[str, float] = {}
    for l in lots:
        by_t[l["ticker"]] = by_t.get(l["ticker"], 0.0) + float(l["qty"])
    blocked, msgs = set(), []
    for t, q in by_t.items():
        pq = float(positions[t]["qty"]) if t in positions else 0.0
        if abs(pq - q) > 1e-6:
            blocked.add(t)
            msgs.append(f"{t}: lots {q:g} vs alpaca {pq:g}")
    for t in positions:
        if t not in by_t:
            blocked.add(t)
            msgs.append(f"{t}: alpaca position with no lot (manual?)")
    return blocked, msgs


def mark_books(con, as_of: dt.date, close: dict[str, float], account_equity: float | None) -> list[dict]:
    out = []
    for book, row in ensure_books(con).items():
        lots = open_lots(con, book)
        mv = sum(float(l["qty"]) * close.get(l["ticker"], float(l["entry_px"] or 0)) for l in lots)
        eq = row["cash_usd"] + mv
        con.execute("INSERT OR REPLACE INTO agent_book_nav VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [as_of, book, row["cash_usd"], mv, eq, len(lots), account_equity])
        out.append({"book": book, "cash_usd": row["cash_usd"], "market_value_usd": mv, "equity_usd": eq,
                    "n_positions": len(lots), "alloc_usd": row["alloc_usd"]})
    return out


# ---------------------------------------------------------------- targets --------------
def long_targets(store: PanelStore, market, day: pd.Timestamp, con) -> tuple[list[str], set[str], bool]:
    rows = long_live.targets(store, market, day, TOP_N)
    if not rows:
        return [], set(), False
    recorded = con.execute("SELECT count(*) FROM agent_picks WHERE signal_name = ? AND as_of = ?",
                           [long_live.SIGNAL, day.date()]).fetchone()[0]
    if not recorded:                                   # the morning shadow run then finds this bar done
        ledger.write_picks(con, [{**r, "as_of": day.date(), "status": "paper", "ledger_id": None, "run_id": "exec"} for r in rows])
        try:                                           # v2 shadow line recorded alongside (never traded)
            v2 = long_live.targets(store, market, day, TOP_N, v2=True)
            ledger.write_picks(con, [{**r, "as_of": day.date(), "status": "shadow", "ledger_id": None, "run_id": "exec"} for r in v2])
        except Exception as exc:
            print(f"v2 shadow skipped: {exc}")
    ranked = [r["ticker"] for r in rows if r["gate_passed"]]
    keep = {r["ticker"] for r in rows}
    return ranked, keep, True


def insider_targets(store: PanelStore, day: pd.Timestamp, con, window: int = 2) -> list[str]:
    """The last two days' qualifying filings, minus anything this book already ordered in the last 5 sessions.

    Two days, not one: the 06:00 Form 4 job loads EDGAR's index for the previous day, so a
    filing made on D reaches the panel on D+1 and would be missed by a one-day window.
    """
    df = signals_insider.candidates(store, day, window)
    if df.empty:
        return []
    df = df[df["eligible"]]
    recent = {r[0] for r in con.execute("""SELECT DISTINCT ticker FROM agent_orders
                                            WHERE book = 'insider' AND side = 'buy' AND as_of >= ?""",
                                         [(day - pd.Timedelta(days=8)).date()]).fetchall()}
    return [t for t in df.sort_values("buy_usd", ascending=False)["ticker"] if t not in recent]


# ---------------------------------------------------------------- main -----------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--submit", action="store_true", help="send orders; default is a dry run that only prints them")
    ap.add_argument("--no-update", action="store_true", help="skip the after-close bars update")
    ap.add_argument("--book", choices=list(BOOKS), default=None)
    ap.add_argument("--date", default=None, help="bar date to plan from (must be the last session)")
    ap.add_argument("--optradar-db", default=ledger.OPTRADAR_DB)
    args = ap.parse_args()

    broker = broker_mod.from_env()
    acct = broker.account()                                   # raises unless PA… paper account
    now_et = dt.datetime.now(ET)
    calendar = broker.calendar(now_et.date() - dt.timedelta(days=30), now_et.date() + dt.timedelta(days=30))
    past = [d for d in calendar if d < now_et.date() or (d == now_et.date() and now_et.hour >= 16)]
    last_session = past[-1]

    if not args.no_update:
        with PanelStore() as store:
            held = list(broker.positions())
            print("bars:", update_bars(store, 7, held))
            try:
                backfill_index(store)
            except Exception as exc:
                print(f"index update skipped: {exc}")

    con = ledger.connect(args.optradar_db)
    try:
        ledger.ensure_schema(con)
        books = ensure_books(con)
        with PanelStore(read_only=True) as store:
            market = load_market(store, (pd.Timestamp(last_session) - pd.Timedelta(days=420)).date().isoformat())
            day = market.adj.index[-1]
            if args.date:
                day = market.adj.index[market.adj.index <= pd.Timestamp(args.date)][-1]
            stale = day.date() < last_session
            sync = sync_fills(con, broker, calendar)
            n_model = fill_model_px(con, store)
            positions = broker.positions()
            lots_all = open_lots(con)
            blocked, msgs = reconcile(lots_all, positions)
            next_session = _sessions_after(calendar, day.date(), 1)
            tif = "opg" if opg_window(dt.datetime.now(ET)) else "day"
            close_row = market.close.loc[day]
            ref_close = {t: float(v) for t, v in close_row.dropna().items()}
            plans: list[dict] = []
            targets_dbg: dict = {}
            if not stale:
                for book, cfg in BOOKS.items():
                    if args.book and book != args.book:
                        continue
                    lots = [l for l in lots_all if l["book"] == book]
                    others = {l["ticker"] for l in lots_all if l["book"] != book}
                    if book == "long":
                        ranked, keep, scored = long_targets(store, market, day, con)
                    else:
                        ranked, keep, scored = insider_targets(store, day, con), set(), True
                    spreads = {t: market.spread_pct(t, day) for t in ranked}
                    plans += plan_book(book, cfg, lots, ranked, keep, day.date(), next_session, ref_close, spreads,
                                       books[book]["cash_usd"], blocked | others, scored, tif)
                    targets_dbg[book] = {"n_ranked": len(ranked), "n_keep": len(keep), "scored": scored,
                                         "top": ranked[:10]}
            nav = mark_books(con, day.date(), ref_close, float(acct["equity"]))

        # ---- caps, then submit or print
        plans = plans[:MAX_ORDERS_PER_RUN]
        existing = {o["client_order_id"] for o in broker.open_orders()}
        already = {r[0] for r in con.execute("SELECT client_order_id FROM agent_orders WHERE dry_run = FALSE").fetchall()}
        sent, skipped = [], []
        for o in plans:
            if o["client_order_id"] in existing or o["client_order_id"] in already:
                skipped.append(o["client_order_id"])
                continue
            if args.submit:
                try:
                    resp = broker.submit(o["ticker"], o["side"], o["qty"], o["order_type"], o["tif"],
                                         o["client_order_id"], o["limit_price"])
                    o.update({"alpaca_id": resp.get("id"), "status": resp.get("status"), "dry_run": False})
                except broker_mod.BrokerError as exc:
                    o.update({"alpaca_id": None, "status": f"error: {str(exc)[:120]}", "dry_run": False})
            else:
                o.update({"alpaca_id": None, "status": "dry_run", "dry_run": True})
            o["submitted_at"] = pd.Timestamp.now()
            sent.append(o)
        if args.submit:
            ok = [o for o in sent if o["alpaca_id"]]                  # a rejected POST is not an order
            if ok:
                ledger._insert(con, "agent_orders", pd.DataFrame(ok))
    finally:
        con.close()

    summary = {"as_of": str(day.date()), "last_session": str(last_session), "stale_bars": stale, "tif": tif,
               "mode": "submit" if args.submit else "dry_run", "account_equity": float(acct["equity"]),
               "account_cash": float(acct["cash"]), "sync": sync, "model_px_filled": n_model,
               "reconcile": msgs, "targets": targets_dbg, "books": nav,
               "orders": [{k: (str(v) if isinstance(v, (dt.date, pd.Timestamp)) else v) for k, v in o.items()} for o in sent],
               "skipped_duplicates": skipped, "generated_at": dt.datetime.now().isoformat(timespec="seconds")}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"exec_{day.date()}.json"), "w") as f:
        json.dump(summary, f, indent=1, default=str)
    tag = "SUBMITTED" if args.submit else "DRY RUN"
    print(f"[{tag}] bar {day.date()} (last session {last_session}{', STALE — nothing planned' if stale else ''}) tif={tif} "
          f"· account ${float(acct['equity']):,.0f} · fills synced {sync['filled']}+{sync['closed']} · "
          f"reconcile {'ok' if not msgs else msgs}")
    for b in nav:
        print(f"  {b['book']:8s} equity ${b['equity_usd']:,.0f}  cash ${b['cash_usd']:,.0f}  positions {b['n_positions']}")
    for o in sent:
        lp = f"limit {o['limit_price']}" if o["limit_price"] else "MOO"
        print(f"  {o['book']:8s} {o['side']:4s} {o['ticker']:6s} x{o['qty']:<5d} {lp:<14s} ref {o['ref_close']:.2f}  {o['reason']}  [{o['status']}]")
    if skipped:
        print(f"  skipped {len(skipped)} already-submitted ids")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
