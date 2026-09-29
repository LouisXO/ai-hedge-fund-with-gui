"""Paper execution on Alpaca: the two stock books, run after the close.

What the backtest did (agent/books/engine.py), and what this does with a
real paper account:

  backtest                                  here
  ------------------------------------      ------------------------------------------
  targets chosen with data through day D    scored on D's completed bar, after 16:00 ET
  fills at D+1 open                         DAY orders queued overnight, filled in the first minutes
                                            (paper does not simulate the OPG auction; 2026-09-24)
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

Order path (S48): each order is written to agent_orders as soon as its POST
returns; a POST that fails is looked up by client_order_id (found = accepted,
404 = not_sent or rejected); every submit and sync run first claims orders
the broker has under this system's ids that the ledger lacks. Fills are
booked by increment (applied_qty). Buys still in flight hold their slot and
cash; the planned buys must fit in the book's cash plus its sells' proceeds
and in the account's cash (`guard_buys`).

Usage:
  python -m agent.execute [--submit] [--no-update] [--book long|insider] [--date YYYY-MM-DD]
  python -m agent.execute --cancel-open [--confirm]   lists this system's open orders; cancels only with --confirm
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
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

# Allocation of the $100k paper account. The last $10k was the option reserve until 2026-09-24; the
# option book never passed its gate (S26), and S37 found no large-cap selection edge, so it holds SPY
# as the account's large-cap core (bought once, never sold by rule).
BOOKS = {
    "long":    {"alloc_usd": 60_000.0, "max_positions": TOP_N, "hold_days": None,
                "entry_cap_pct": 3.0},        # LOO cap: buy at the open unless it gaps > 3% over D's close
    "insider": {"alloc_usd": 30_000.0, "max_positions": INSIDER_SLOTS, "hold_days": HOLD_DAYS,
                "entry_cap_pct": 3.0},        # DAY limit at D's close + 3% (2026-09-28: a market order paid the ask, INBX 105.33 vs a 98.69 open)
    "core":    {"alloc_usd": 10_000.0, "max_positions": 1, "hold_days": None,
                "entry_cap_pct": 1.0},        # SPY, passive large-cap exposure (S37)
}
CORE_TICKER = "SPY"
MAX_ORDERS_PER_RUN = 60
MAX_ORDER_NOTIONAL = 10_000.0     # hard ceiling of one order, whatever the book's size
SLOT_CAP_MULT = 1.5               # and at most 1.5 x the book's slot (S48; the ceiling alone was 5 slots of the long book)
SELL_HAIRCUT = 0.97               # a planned sell funds buys at ref close x 0.97 in the pre-submit check
MARKET_BUY_PAD = 1.03             # a market buy (or one without a price) is valued at ref close x 1.03
CLAIM_DAYS = 14                   # how far back the broker's orders are searched for ones the ledger lacks
# Ledger statuses of our own besides the broker's: 'not_sent' = the POST failed and the broker has no such
# order (final: the same id is sent again by a rerun, a later evening's order has its own id); 'rejected' with
# no alpaca_id = the broker refused the POST; 'submit_unknown' = the POST failed and so did the lookup (not
# final: the next sync asks again); 'claimed' = found at the broker, missing from the ledger (next sync fills it in).
NOT_SENT, SUBMIT_UNKNOWN, CLAIMED = "not_sent", "submit_unknown", "claimed"
OPEN_STATES = {"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled", "held", "submitted",
               SUBMIT_UNKNOWN, CLAIMED}
FINAL_STATES = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended", NOT_SENT}
FINAL_SQL = ", ".join(f"'{s}'" for s in sorted(FINAL_STATES))     # for "status NOT IN (...)"
COID_RE = re.compile(r"^(?P<book>[a-z]+)\|(?P<as_of>\d{4}-\d{2}-\d{2})\|(?P<ticker>[^|]+)\|(?P<side>buy|sell)$")


def client_id(book: str, as_of: dt.date, ticker: str, side: str) -> str:
    return f"{book}|{as_of.isoformat()}|{ticker}|{side}"


def parse_client_id(coid: str | None) -> dict | None:
    """book|date|ticker|side of one of this system's books, else None (an order placed by hand has its own id)."""
    m = COID_RE.match(coid or "")
    if not m or m["book"] not in BOOKS:
        return None
    return {"book": m["book"], "as_of": dt.date.fromisoformat(m["as_of"]), "ticker": m["ticker"], "side": m["side"]}


def order_cap(slot: float) -> float:
    """The most one buy may cost: 1.5 slots of its book, never more than $10,000."""
    return min(SLOT_CAP_MULT * slot, MAX_ORDER_NOTIONAL)


def buy_px(o: dict, ref_close: dict[str, float] | None = None) -> float | None:
    """The price a buy is budgeted at: its limit, else the reference close + 3% (market / claimed orders)."""
    if o.get("limit_price"):
        return float(o["limit_price"])
    ref = o.get("ref_close") or (ref_close or {}).get(o["ticker"])
    return float(ref) * MARKET_BUY_PAD if ref else None


# ---------------------------------------------------------------- planning (pure) ----
def opg_window(now_et: dt.datetime) -> bool:
    """Alpaca accepts OPG orders only after 19:00 and before 09:28 ET (error 40310000 otherwise).
    Outside that window the same orders go as DAY: submitted after the close they are queued for
    the next open, so a limit buy still fills at the opening print when it is under the cap."""
    t = now_et.time()
    return t >= dt.time(19, 0) or t < dt.time(9, 28)


def plan_book(book: str, cfg: dict, lots: list[dict], ranked: list[str], keep: set[str], as_of: dt.date,
              next_session: dt.date, ref_close: dict[str, float], spread_pct: dict[str, float],
              cash_usd: float, blocked: set[str], scored: bool = True, tif: str = "opg",
              frozen: set[str] | None = None, inflight: list[dict] | None = None) -> list[dict]:
    """The engine's one-day step, as orders for the next open.

    lots: this book's open lots ({ticker, qty, hold_until}); ranked: entry candidates
    in priority order; keep: names the book still wants (long book: top 2N; insider
    book: irrelevant, exits are by hold_until); blocked: tickers held by another book
    or unreconciled. Returns dicts ready for agent_orders.

    frozen: tickers whose lots do not match the broker's position (reconcile). No order of
    either side is planned for them: the lot's qty is not what the account holds (split,
    merger, manual trade), so a sell of the lot's qty would be the wrong size. They stay
    frozen until the ledger is corrected by hand (2026-09-28 audit: the sell loop ignored this).

    inflight: this book's buy orders that are not final yet ({ticker, qty (unfilled part), limit_price,
    ref_close}). Each holds a slot unless its ticker is already a lot, and its cost (qty x limit, or ref
    close x 1.03 without a limit) is not available cash: the book's cash only moves when a fill is synced,
    so a second run the same evening would otherwise spend it again on the next candidates (S48).
    """
    frozen = frozen or set()
    inflight = inflight or []
    orders: list[dict] = []
    exits: set[str] = set()
    for lot in lots:
        t = lot["ticker"]
        hu = lot.get("hold_until")
        expired = hu is not None and pd.Timestamp(hu).date() <= next_session
        dropped = hu is None and scored and t not in keep
        if t in frozen:
            continue
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
    flying = {f["ticker"] for f in inflight}
    n_open += len(flying - held)
    for f in inflight:
        px = buy_px(f, ref_close)
        cash_plan -= float(f["qty"]) * px if px else slot       # no price at all: assume it takes a slot's cash
    if not scored:
        return orders
    for t in ranked:
        if n_open >= cfg["max_positions"]:
            break
        if t in held or t in blocked or t in exits or t in flying:
            continue
        px = ref_close.get(t)
        if px is None or not np.isfinite(px) or px <= 0 or cash_plan < slot * 0.5:
            continue
        cap = cfg["entry_cap_pct"] if cfg["entry_cap_pct"] is not None else max(spread_pct.get(t, 0.5), 0.1)
        limit = round(px * (1 + cap / 100), 2 if px >= 1 else 4)
        spend = min(slot, cash_plan, order_cap(slot))
        qty = math.floor(spend / limit)
        if qty < 1:
            continue
        # In the opening auction there is no spread to cross and the backtest bought at the open print,
        # so an OPG entry is market-on-open (2026-09-23: paper left 5 of 7 limit-on-open orders unfilled
        # although the open printed inside the limit). The cap only applies to the DAY fallback.
        # 2026-09-24: on the PAPER account the opening auction is not simulated reliably — every OPG
        # market order expired unfilled (5/5 on 09-24) and 7/9 OPG limits expired on 09-23, while DAY
        # orders queued overnight filled 30/30 in the first minutes (09-22). So main() sends DAY
        # orders; the long book keeps the +3% cap as a limit.
        # 2026-09-28: the insider book bought at market until today. A DAY market order is filled at the
        # ask, and in thin names the ask in the first minutes is a placeholder: INBX filled at 105.33 when
        # the opening cross was 98.69 and no trade all day printed above 104.6; 5 of 7 fills were above
        # the cross. No book buys at market any more: every DAY entry is a limit at the reference close
        # + entry_cap_pct. A name that opens above the cap is not chased; it is retried once (S36b).
        otype = "market" if tif == "opg" else "limit"
        orders.append({"client_order_id": client_id(book, as_of, t, "buy"), "book": book, "as_of": as_of,
                       "ticker": t, "side": "buy", "qty": qty, "order_type": otype, "tif": tif,
                       "limit_price": None if otype == "market" else limit, "ref_close": px, "reason": "entry",
                       "slot_usd": slot})                   # not a ledger column; the pre-submit cap reads it
        cash_plan -= qty * limit
        n_open += 1
    return orders


# ---------------------------------------------------------------- bars guards (pure) --
COVERAGE_ADV = 3e6          # liquid enough to trade every session: a missing bar is a data gap, not a quiet day
COVERAGE_MIN = 0.98


def bars_coverage(close: pd.DataFrame, adv20: pd.DataFrame, day: pd.Timestamp) -> float | None:
    """Of the names with ADV >= $3M and a bar on the session before `day`, the share with a bar on `day`.

    The date check compares the newest bar with the last session, so it passes when a single symbol has
    today's bar: three batches of 100 symbols went without data for a week that way (audit 2026-09-28).
    Below COVERAGE_MIN nothing is planned. None when there is no earlier session to compare with.
    """
    i = close.index.get_loc(day)
    if i == 0:
        return None
    prev = close.index[i - 1]
    base = close.loc[prev].notna() & (adv20.loc[prev] >= COVERAGE_ADV)
    return float(close.loc[day][base].notna().mean()) if base.any() else None


def held_without_bar(lots: list[dict], adj: pd.DataFrame, day: pd.Timestamp) -> list[str]:
    """Held tickers with no adjusted close on the scoring day. Such a name is not tradable that day
    (agent/books/data.py), so it gets no rank and would leave the keep zone: the book would sell a
    position because a request failed. It is kept until a bar says where it ranks."""
    row = adj.loc[day]
    return sorted({l["ticker"] for l in lots if l["ticker"] not in row.index or pd.isna(row[l["ticker"]])})


def bars_guard_reason(coverage: float | None) -> str | None:
    """The skipped_reason for a coverage under COVERAGE_MIN, else None. Floored to 0.01%, so 97.996% does
    not read as 98.00%."""
    if coverage is None or coverage >= COVERAGE_MIN:
        return None
    return f"bars coverage {math.floor(coverage * 10000) / 100:.2f}%"


def long_keep(keep: set[str], no_bar: list[str]) -> set[str]:
    """The long book's keep zone plus the held names without a bar today: no bar = no rank, not a rank_out."""
    return set(keep) | set(no_bar)


def bars_update_note(stats: dict | None) -> str | None:
    """The log line of a bars update that failed in full or in part, None when it did not (or did not run).
    It says 'failed': whether to plan is left to the guards, but a failed update must be seen in the log."""
    if not stats:
        return None
    if stats.get("error"):
        return f"bars update failed: {stats['error']}"
    if stats.get("failed_symbols"):
        return (f"bars update failed for {len(stats['failed_symbols'])} symbols in {stats['n_batches_failed']} "
                f"batches (NO DATA): {stats['failed_symbols'][:20]}")
    return None


def entry_candidates(ranked: list[str]) -> list[str]:
    """No book enters a class share ('BRK-A', 'LGF.B') for now: the panel and the broker spell it
    differently (S47 addendum, 2026-09-28). A held one is unaffected: its exits do not go through here."""
    return [t for t in ranked if "-" not in t and "." not in t]


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


def _et_naive(stamp) -> pd.Timestamp:
    t = pd.Timestamp(stamp)
    return (t.tz_localize("UTC") if t.tzinfo is None else t).tz_convert(ET).tz_localize(None)


def sync_fills(con, broker, calendar: list[dt.date]) -> dict:
    """Pull the state of every order we submitted that is not final; open/close lots on fills.

    Alpaca reports an order's cumulative filled_qty and average price. Only the increment over what the ledger
    already applied (applied_qty, applied_notional) moves the lot and the book's cash, so an order seen while
    partially filled and again when filled is booked once (2026-09-28 audit: it was booked in full each time).
    The order's row, its lot and the book's cash change in one transaction. A lot sold in parts, by one order
    or several, closes at the weighted price of all the parts. An order the broker does not know and never
    confirmed (the POST failed, see submit_and_record) becomes not_sent.
    """
    pending = con.execute(f"""SELECT client_order_id, book, ticker, side, alpaca_id,
                                     coalesce(applied_qty, 0), coalesce(applied_notional, 0) FROM agent_orders
                              WHERE dry_run = FALSE AND (status IS NULL OR status NOT IN ({FINAL_SQL}))""").fetchall()
    stats = {"checked": len(pending), "filled": 0, "closed": 0, "final_unfilled": 0, "partial": 0, "not_sent": 0,
             "sell_without_lot": 0}
    for coid, book, ticker, side, alpaca_id, aq, an in pending:
        o = broker.order_by_client_id(coid)
        if o is None:
            if not alpaca_id:                    # never confirmed by the broker and it has no such order
                con.execute("UPDATE agent_orders SET status = ? WHERE client_order_id = ?", [NOT_SENT, coid])
                stats["not_sent"] += 1
            continue
        status = o.get("status")
        fq = float(o.get("filled_qty") or 0)
        fpx = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
        stamp = o.get("filled_at") or o.get("updated_at")      # a partial fill may carry no filled_at yet
        fat = _et_naive(stamp) if stamp else (pd.Timestamp.now(ET).tz_localize(None) if fq > 0 else None)
        dq = fq - aq
        dn = fq * fpx - an if fpx is not None else 0.0
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("""UPDATE agent_orders SET status = ?, alpaca_id = ?, filled_qty = ?, filled_avg_px = ?, filled_at = ?,
                                  applied_qty = ?, applied_notional = ? WHERE client_order_id = ?""",
                        [status, o.get("id"), fq, fpx, fat if fq > 0 else None,
                         fq if dq > 1e-9 and fpx is not None else aq, fq * fpx if dq > 1e-9 and fpx is not None else an, coid])
            if dq > 1e-9 and fpx is not None:
                if status not in FINAL_STATES:
                    stats["partial"] += 1
                if side == "buy":
                    _apply_buy(con, coid, book, ticker, fq, fpx, dq, dn, fat, calendar)
                    stats["filled"] += 1
                elif _apply_sell(con, coid, book, ticker, dq, dn, fat, stats):
                    stats["closed"] += 1
            elif fq <= 0 and status in FINAL_STATES:
                stats["final_unfilled"] += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return stats


def _apply_buy(con, coid, book, ticker, fq, fpx, dq, dn, fat, calendar) -> None:
    """The lot of a buy order holds its cumulative fill at the order's average price."""
    lot = con.execute("SELECT lot_id FROM agent_lots WHERE entry_order = ?", [coid]).fetchone()
    if lot:
        con.execute("UPDATE agent_lots SET qty = qty + ?, entry_px = ? WHERE lot_id = ?", [dq, fpx, lot[0]])
    else:
        fill_day = fat.date()
        hold = BOOKS[book]["hold_days"]
        hold_until = _sessions_after(calendar, fill_day, hold) if hold else None
        # by column name (the table grows by ALTER); the entry classification comes from the order row
        con.execute("""INSERT OR REPLACE INTO agent_lots (lot_id, book, ticker, qty, entry_day, entry_px, hold_until, status,
                                                          entry_order, trigger_filing_day, lag_sessions, entry_kind)
                       SELECT ?, ?, ?, ?, ?, ?, ?, 'open', client_order_id, trigger_filing_day, lag_sessions, entry_kind
                       FROM agent_orders WHERE client_order_id = ?""",
                    [f"{book}|{ticker}|{fill_day.isoformat()}", book, ticker, dq, fill_day, fpx, hold_until, coid])
    con.execute("UPDATE agent_books SET cash_usd = cash_usd - ?, updated = ? WHERE book = ?", [dn, pd.Timestamp.now(), book])


def _apply_sell(con, coid, book, ticker, dq, dn, fat, stats) -> bool:
    """Shrink the book's lot by the sold increment; close it at the weighted price of all its sold parts."""
    lot = con.execute("""SELECT lot_id, entry_px, qty, coalesce(sold_qty, 0), coalesce(sold_notional, 0) FROM agent_lots
                         WHERE book = ? AND ticker = ? AND status = 'open' ORDER BY entry_day LIMIT 1""", [book, ticker]).fetchone()
    if not lot:
        stats["sell_without_lot"] += 1
        return False
    lot_id, entry_px, lqty, sq, sn = lot
    sq, sn = sq + dq, sn + dn
    if lqty - dq <= 1e-6:
        exit_px = sn / sq
        ret = (exit_px / entry_px - 1) * 100 if entry_px else None
        # a closed lot shows the whole position: qty = all shares sold, exit_px = their weighted price
        con.execute("""UPDATE agent_lots SET status = 'closed', qty = ?, sold_qty = ?, sold_notional = ?, exit_day = ?,
                              exit_px = ?, ret_pct = ?, exit_order = ? WHERE lot_id = ?""",
                    [sq, sq, sn, fat.date(), exit_px, ret, coid, lot_id])
    else:                                                # partial: the lot keeps what the account still holds
        con.execute("UPDATE agent_lots SET qty = qty - ?, sold_qty = ?, sold_notional = ? WHERE lot_id = ?",
                    [dq, sq, sn, lot_id])
    con.execute("UPDATE agent_books SET cash_usd = cash_usd + ?, updated = ? WHERE book = ?", [dn, pd.Timestamp.now(), book])
    return True


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
            hint = split_hint(q, pq)
            msgs.append(f"{t}: lots {q:g} vs alpaca {pq:g}" + (f" ({hint})" if hint else ""))
    for t in positions:
        if t not in by_t:
            blocked.add(t)
            msgs.append(f"{t}: alpaca position with no lot (manual?)")
    return blocked, msgs


def split_hint(lot_qty: float, broker_qty: float) -> str | None:
    """'possible split 3:1' / 'possible reverse split 1:10' when the broker holds a whole multiple (2..20) or a
    whole fraction (1/20..1/2) of the ledger's shares. Only reported: the lot stays frozen until the owner
    confirms the corporate action and corrects qty and entry_px by hand (S48; no automatic change)."""
    if lot_qty <= 0 or broker_qty <= 0:
        return None
    for r, kind in ((broker_qty / lot_qty, "split {n}:1"), (lot_qty / broker_qty, "reverse split 1:{n}")):
        n = round(r)
        if 2 <= n <= 20 and abs(r - n) < 1e-6:
            return "possible " + kind.format(n=n)
    return None


def split_hints(lots: list[dict], positions: dict[str, dict]) -> list[dict]:
    """The reconcile mismatches that look like a split or a reverse split, for the JSON."""
    by_t: dict[str, float] = {}
    for l in lots:
        by_t[l["ticker"]] = by_t.get(l["ticker"], 0.0) + float(l["qty"])
    out = []
    for t, q in sorted(by_t.items()):
        pq = float(positions[t]["qty"]) if t in positions else 0.0
        hint = split_hint(q, pq) if abs(pq - q) > 1e-6 else None
        if hint:
            out.append({"ticker": t, "lots_qty": q, "broker_qty": pq, "hint": hint})
    return out


def cash_check(con, broker_cash: float | None, tol: float = 1.0) -> dict:
    """The three books' cash must add up to the account's cash within $1, checked only when no order is in
    flight (a working order's fill has not reached the books yet). ok = None when it was not checked.
    Written to the JSON as cash_check; agent.health reads it."""
    books_cash = float(con.execute("SELECT coalesce(sum(cash_usd), 0) FROM agent_books").fetchone()[0])
    n_open = int(con.execute(f"""SELECT count(*) FROM agent_orders WHERE dry_run = FALSE
                                 AND (status IS NULL OR status NOT IN ({FINAL_SQL}))""").fetchone()[0])
    out = {"books_cash": round(books_cash, 2), "broker_cash": None if broker_cash is None else round(broker_cash, 2),
           "diff": None, "orders_in_flight": n_open, "ok": None}
    if broker_cash is None or n_open:
        return out
    out["diff"] = round(broker_cash - books_cash, 2)
    out["ok"] = abs(out["diff"]) <= tol
    return out


def inflight_buys(con, book: str | None = None) -> list[dict]:
    """Buy orders not final yet, the unfilled part only (the filled part is a lot already and out of the cash)."""
    q = f"""SELECT book, ticker, qty - coalesce(applied_qty, 0), limit_price, ref_close FROM agent_orders
            WHERE dry_run = FALSE AND side = 'buy' AND (status IS NULL OR status NOT IN ({FINAL_SQL}))"""
    rows = con.execute(q + (" AND book = ?" if book else ""), [book] if book else []).fetchall()
    return [{"book": b, "ticker": t, "qty": float(q_ or 0), "limit_price": lp, "ref_close": rc}
            for b, t, q_, lp, rc in rows if (q_ or 0) > 0]


def _notional(o: dict, ref_close: dict[str, float] | None = None) -> float:
    px = buy_px(o, ref_close)
    return float(o["qty"]) * px if px else 0.0


def guard_buys(plans: list[dict], book_cash: dict[str, float], inflight: list[dict], broker_cash: float | None,
               slots: dict[str, float], ref_close: dict[str, float] | None = None) -> tuple[list[dict], list[str]]:
    """The last check before anything is sent (S48). Buys only; a sell is never cut.

    1. one buy costs at most order_cap(its book's slot): 1.5 slots, never over $10,000;
    2. a book's buys cost at most its cash, less its buys in flight, plus its planned sells at ref close x 0.97;
    3. all buys together cost at most the broker's cash, less all buys in flight, plus all sells x 0.97.
    Over a limit, shares come off the lowest-priority buy first (the book's last; across books the plan's last),
    down to dropping it. With the books' cash right this never binds: plan_book sizes within it. It is there for
    a ledger that is wrong. Returns (the orders, one note per cut).
    """
    notes: list[str] = []
    out = [dict(o) for o in plans]
    for o in out:
        if o["side"] != "buy" or o["book"] not in slots:
            continue
        px, cap = buy_px(o, ref_close), order_cap(slots[o["book"]])
        if px and o["qty"] * px > cap + 1e-6:
            q = math.floor(cap / px)
            notes.append(f"{o['book']} {o['ticker']}: ${o['qty'] * px:,.0f} over the ${cap:,.0f} order cap, qty {o['qty']} -> {q}")
            o["qty"] = q

    def sells(rows):
        return sum(o["qty"] * (o.get("ref_close") or 0.0) * SELL_HAIRCUT for o in rows if o["side"] == "sell")

    def trim(rows, avail, label):
        buys = [o for o in rows if o["side"] == "buy" and o["qty"] > 0]
        over = sum(_notional(o, ref_close) for o in buys) - avail
        for o in reversed(buys):
            if over <= 1e-6:
                break
            px = buy_px(o, ref_close) or 0.0
            cut = min(o["qty"], math.ceil(over / px)) if px else o["qty"]
            notes.append(f"{label}: buys over the cash by ${over:,.0f}, {o['book']} {o['ticker']} qty {o['qty']} -> {o['qty'] - cut}")
            o["qty"] -= cut
            over -= cut * px

    for book in dict.fromkeys(o["book"] for o in out):
        rows = [o for o in out if o["book"] == book]
        fly = sum(_notional(f, ref_close) for f in inflight if f["book"] == book)
        trim(rows, book_cash.get(book, 0.0) - fly + sells(rows), f"book {book}")
    if broker_cash is not None:
        fly = sum(_notional(f, ref_close) for f in inflight)
        trim(out, broker_cash - fly + sells(out), "account")
    return [o for o in out if o["side"] == "sell" or o["qty"] >= 1], notes


def cap_orders(plans: list[dict], n: int = MAX_ORDERS_PER_RUN) -> list[dict]:
    """At most n orders a run; the cut falls on buys only, every sell is kept."""
    sells = [o for o in plans if o["side"] == "sell"]
    buys = [o for o in plans if o["side"] != "sell"][:max(0, n - len(sells))]
    keep = {id(o) for o in sells + buys}
    return [o for o in plans if id(o) in keep]


def claim_orphans(con, broker_orders: list[dict]) -> list[dict]:
    """Orders the broker has under this system's ids (book|date|ticker|side) that the ledger does not (or holds as
    not_sent): a run that died between a POST and its ledger write left them. Each is written with status
    'claimed' and applied_qty 0, so the sync that follows books its fills. Returns the claimed rows."""
    known = {r[0]: r[1] for r in con.execute("SELECT client_order_id, status FROM agent_orders WHERE dry_run = FALSE").fetchall()}
    rows = []
    for o in broker_orders:
        coid = o.get("client_order_id")
        p = parse_client_id(coid)
        if p is None or (coid in known and known[coid] != NOT_SENT):
            continue
        sub = o.get("submitted_at") or o.get("created_at")
        rows.append({"client_order_id": coid, **p, "qty": int(float(o.get("qty") or 0)), "order_type": o.get("type"),
                     "tif": o.get("time_in_force"), "limit_price": float(o["limit_price"]) if o.get("limit_price") else None,
                     "ref_close": None, "reason": "claimed", "alpaca_id": o.get("id"), "status": CLAIMED,
                     "submitted_at": _et_naive(sub) if sub else None, "dry_run": False,
                     "applied_qty": 0.0, "applied_notional": 0.0})
    if rows:
        ledger._insert(con, "agent_orders", pd.DataFrame(rows))
    return rows


def submit_and_record(con, broker, o: dict) -> dict:
    """POST one order and write its ledger row at once, whatever happened (S48; the run used to write all rows
    after the loop, so one exception lost every order already accepted).

    A POST that raises is looked up by client_order_id: found = the broker took it (status 'accepted'); not found =
    'rejected' when the broker answered 4xx, else 'not_sent'; the lookup failing too = 'submit_unknown', which holds
    its slot and cash until the next sync asks again. Returns o, updated as written.
    """
    o.update({"dry_run": False, "submitted_at": pd.Timestamp.now(), "applied_qty": 0.0, "applied_notional": 0.0})
    err = None
    try:
        resp = broker.submit(o["ticker"], o["side"], o["qty"], o["order_type"], o["tif"], o["client_order_id"], o["limit_price"])
        if resp and resp.get("id"):
            o.update({"alpaca_id": resp["id"], "status": resp.get("status") or "accepted"})
        else:
            err = broker_mod.BrokerError(f"POST answered without an order id: {str(resp)[:120]}")
    except broker_mod.BrokerError as exc:
        err = exc
    if err is not None:
        o["error"] = str(err)[:200]
        try:
            found = broker.order_by_client_id(o["client_order_id"])
            if found:
                o.update({"alpaca_id": found.get("id"), "status": "accepted"})
            else:
                rejected = err.status is not None and 400 <= err.status < 500
                o.update({"alpaca_id": None, "status": "rejected" if rejected else NOT_SENT})
        except broker_mod.BrokerError as exc:
            o.update({"alpaca_id": None, "status": SUBMIT_UNKNOWN, "error": f"{o['error']}; lookup: {str(exc)[:120]}"})
    ledger._insert(con, "agent_orders", pd.DataFrame([o]))
    return o


def send_orders(con, broker, plans: list[dict], submit: bool) -> tuple[list[dict], list[str]]:
    """Send (or, without submit, only mark as dry run) each order in turn, each written as it returns. After an
    order with no answer at all (submit_unknown) the rest are not attempted. Returns (sent, ids not attempted)."""
    sent: list[dict] = []
    for i, o in enumerate(plans):
        if not submit:
            o.update({"alpaca_id": None, "status": "dry_run", "dry_run": True, "submitted_at": pd.Timestamp.now()})
            sent.append(o)
            continue
        sent.append(submit_and_record(con, broker, o))
        if o["status"] == SUBMIT_UNKNOWN:
            return sent, [p["client_order_id"] for p in plans[i + 1:]]
    return sent, []


def cancel_open(broker, confirm: bool) -> int:
    """List this system's open orders (client_order_id book|date|ticker|side) and cancel them only with confirm.
    Orders placed by hand are neither listed nor touched. The next sync records the canceled status."""
    mine = [o for o in broker.open_orders() if parse_client_id(o.get("client_order_id"))]
    print(f"{len(mine)} open order(s) of this system" + ("" if confirm else " — listing only, add --confirm to cancel them"))
    for o in mine:
        print(f"  {o['client_order_id']:<36s} {o.get('side', ''):4s} {o.get('qty', '')!s:>6s} {o.get('type', '')} "
              f"{o.get('limit_price') or ''} [{o.get('status')}]")
    if not confirm:
        return 0
    failed = 0
    for o in mine:
        try:
            broker.cancel(o["id"])
            print(f"  canceled {o['client_order_id']}")
        except broker_mod.BrokerError as exc:
            failed += 1
            print(f"  cancel FAILED {o['client_order_id']}: {exc}")
    return 1 if failed else 0


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
        try:                                           # S37 large-cap quality + low-vol, recorded as a shadow line (never traded)
            lc = long_live.largecap_qlv_targets(store, market, day, TOP_N)
            ledger.write_picks(con, [{**r, "as_of": day.date(), "status": "shadow", "ledger_id": None, "run_id": "exec"} for r in lc])
        except Exception as exc:
            print(f"large-cap shadow skipped: {exc}")
        try:                                           # S44 v2 bundle: the two cluster lists, replayed by agent.shadow_v2 (never traded)
            from agent import shadow_v2
            shadow_v2.record(con, store, market, day)
        except Exception as exc:
            print(f"v2 cluster lists skipped: {exc}")
    ranked = [r["ticker"] for r in rows if r["gate_passed"]]
    keep = {r["ticker"] for r in rows}
    return ranked, keep, True


def blocking_orders(rows: list[tuple]) -> tuple[set[str], set[str]]:
    """From recent insider buy orders [(ticker, status, filled_qty)], which tickers to skip and which to retry.

    Skip a ticker if any of its orders filled (we own it or owned it) or is still working.
    An order that ended unfilled (expired / canceled / rejected) does not block: the name is
    retried once — but a ticker with two unfilled orders is skipped, so a bad name is not chased.
    (2026-09-24: the old rule skipped anything ever ordered, so 12 simulator no-fills were never retried.)
    """
    filled_or_open, unfilled = set(), {}
    for t, status, fq in rows:
        if (fq or 0) > 0 or status not in FINAL_STATES:
            filled_or_open.add(t)
        else:
            unfilled[t] = unfilled.get(t, 0) + 1
    skip = filled_or_open | {t for t, n in unfilled.items() if n >= 2}
    retry = {t for t in unfilled if t not in skip}
    return skip, retry


def insider_targets(store: PanelStore, day: pd.Timestamp, con, sessions: list[dt.date],
                    window: int = 2) -> tuple[list[str], set[str], dict[str, dict]]:
    """The qualifying filings of the last two SESSIONS, minus names this book bought or is buying in the last 8 days.

    A name qualifies as in the backtest: on one filing day by itself (signals_insider.candidates).
    Two sessions, not one: a filing EDGAR accepts after the 16:10 PT run is loaded by the 06:00
    Form 4 job, so a filing made on D can reach the panel on D+1 and would be missed by a one-day
    window; counted in sessions, Monday's window still holds Friday. The same filing therefore
    shows up two evenings in a row; `blocking_orders` keeps that from buying twice while still
    retrying an entry the broker left unfilled — over three sessions, so a late entry is retried too.
    Returns (tickers, retries, {ticker: trigger_filing_day, lag_sessions, entry_kind}); the third
    is recorded on the order for the evaluation and decides nothing here.
    """
    # An order the broker never had (not_sent, or a POST it refused: rejected without an alpaca_id) is not a
    # miss: before S48 such orders were not written at all, and counting them would use up the one retry.
    rows = con.execute("""SELECT ticker, status, filled_qty FROM agent_orders
                          WHERE book = 'insider' AND side = 'buy' AND dry_run = FALSE AND as_of >= ?
                            AND NOT (coalesce(status, '') IN (?, 'rejected') AND alpaca_id IS NULL)""",
                       [(day - pd.Timedelta(days=8)).date(), NOT_SENT]).fetchall()
    skip, retry = blocking_orders(rows)
    # the one retry (S36b) holds for a late entry too: its filing day is a session further back (owner, S47 addendum d)
    df = signals_insider.candidates(store, day, window, sessions, longer={t: window + 1 for t in retry})
    if df.empty:
        return [], set(), {}
    df = df[df["eligible"] & ~df["ticker"].isin(skip)].sort_values("buy_usd", ascending=False)
    names = list(df["ticker"])
    retries = {t for t in names if t in retry}
    info = {r.ticker: {"trigger_filing_day": r.trigger_filing_day, "lag_sessions": int(r.lag_sessions),
                       "entry_kind": signals_insider.entry_kind(int(r.lag_sessions), r.ticker in retries)}
            for r in df.itertuples()}
    return names, retries, info


def tag_insider_entries(orders: list[dict], retries: set[str], info: dict[str, dict]) -> list[dict]:
    """Mark the insider book's buys: a retry keeps its own reason (S36b), every entry its classification."""
    for o in orders:
        if o["side"] != "buy":
            continue
        if o["ticker"] in retries:
            o["reason"] = "entry_retry"
        o.update(info.get(o["ticker"], {}))
    return orders


# ---------------------------------------------------------------- main -----------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--submit", action="store_true", help="send orders; default is a dry run that only prints them")
    ap.add_argument("--no-update", action="store_true", help="skip the after-close bars update")
    ap.add_argument("--book", choices=list(BOOKS), default=None)
    ap.add_argument("--date", default=None, help="bar date to plan from (must be the last session)")
    ap.add_argument("--optradar-db", default=ledger.OPTRADAR_DB)
    ap.add_argument("--out-dir", default=OUT_DIR, help="where exec_<date>.json goes (the selftest points this at a temp dir)")
    ap.add_argument("--sync-only", action="store_true", help="after-close bookkeeping only: bars, fills, reconcile, mark; plan nothing")
    ap.add_argument("--cancel-open", action="store_true",
                    help="list this system's open orders (book|date|ticker|side ids) and exit; cancels them only with --confirm")
    ap.add_argument("--confirm", action="store_true", help="with --cancel-open: really cancel")
    args = ap.parse_args()

    broker = broker_mod.from_env()
    acct = broker.account()                                   # raises unless PA… paper account
    if args.cancel_open:                                      # stop tool: touches no ledger, plans nothing
        return cancel_open(broker, args.confirm)
    now_et = dt.datetime.now(ET)
    calendar = broker.calendar(now_et.date() - dt.timedelta(days=30), now_et.date() + dt.timedelta(days=30))
    past = [d for d in calendar if d < now_et.date() or (d == now_et.date() and now_et.hour >= 16)]
    last_session = past[-1]

    bars_stats = None
    if not args.no_update:
        with PanelStore() as store:
            held = list(broker.positions())
            try:
                bars_stats = update_bars(store, 7, held)
            except Exception as exc:                          # the guards below decide on what the panel then holds
                bars_stats = {"error": str(exc)[:200], "failed_symbols": [], "n_batches_failed": 0}
            print("bars:", {k: (len(v) if isinstance(v, list) else v) for k, v in bars_stats.items()})
            bars_note = bars_update_note(bars_stats)
            if bars_note:                                     # a request that failed is named, never dropped in silence
                print(bars_note)
            try:
                backfill_index(store)
            except Exception as exc:
                print(f"index update skipped: {exc}")

    con = ledger.connect(args.optradar_db)
    claimed, claim_error, not_attempted, guard_notes = [], None, [], []
    try:
        ledger.ensure_schema(con)
        ensure_books(con)
        # Orders the broker has under our ids that the ledger lacks (a run that died after a POST): written
        # before the sync, so their fills are booked like any other. Submit and sync runs only; a dry run
        # writes no order. A failed lookup stops a submit run from sending: the ledger may be incomplete.
        if args.submit or args.sync_only:
            try:
                claimed = claim_orphans(con, broker.orders_since(now_et - dt.timedelta(days=CLAIM_DAYS)))
            except broker_mod.BrokerError as exc:
                claim_error = str(exc)[:200]
            for r in claimed:
                print(f"  claimed an order the ledger lacked: {r['client_order_id']} ({r['alpaca_id']})")
            if claim_error:
                print(f"  orphan check failed: {claim_error}")
        with PanelStore(read_only=True) as store:
            market = load_market(store, (pd.Timestamp(last_session) - pd.Timedelta(days=420)).date().isoformat())
            day = market.adj.index[-1]
            if args.date:
                day = market.adj.index[market.adj.index <= pd.Timestamp(args.date)][-1]
            # Plan only from the last completed session's bar. Older = the update failed; newer = today's
            # unfinished bar was loaded (a job replayed by launchd after a sleep runs in market hours).
            stale = day.date() != last_session
            market_hours = now_et.date() in calendar and dt.time(9, 25) <= now_et.time() < dt.time(16, 5)
            skipped_reason = ("bar date != last session" if stale else
                              "market hours: orders are only planned between 16:05 and 09:25 ET" if (market_hours and args.submit) else None)
            # ---- bars guards (S47 item 4): the date check above passes as soon as ONE symbol has today's bar
            coverage = bars_coverage(market.close, market.adv20, day)
            skipped_reason = skipped_reason or bars_guard_reason(coverage)
            if claim_error and args.submit:
                skipped_reason = skipped_reason or "orphan check failed: the broker's order list could not be read"
            sync = sync_fills(con, broker, calendar)
            books = ensure_books(con)                         # after the sync: its fills moved the cash
            cash_chk = cash_check(con, float(acct["cash"]) if acct.get("cash") is not None else None)
            flying = inflight_buys(con)
            n_model = fill_model_px(con, store)
            positions = broker.positions()
            lots_all = open_lots(con)
            no_bar = held_without_bar([l for l in lots_all if l["book"] == "long"], market.adj, day)   # bars guard, see plan below
            blocked, msgs = reconcile(lots_all, positions)
            splits = split_hints(lots_all, positions)
            next_session = _sessions_after(calendar, day.date(), 1)
            tif = "opg" if (os.environ.get("AGENT_TIF") == "opg" and opg_window(dt.datetime.now(ET))) else "day"   # DAY on paper; see plan_book
            close_row = market.close.loc[day]
            ref_close = {t: float(v) for t, v in close_row.dropna().items()}
            spy_close = store.con.execute("SELECT close FROM index_daily WHERE symbol = ? AND trade_date = ?", [CORE_TICKER, day.date()]).fetchone()
            if spy_close and spy_close[0]:                  # SPY lives in index_daily, not in the stock panel
                ref_close[CORE_TICKER] = float(spy_close[0])
            plans: list[dict] = []
            targets_dbg: dict = {}
            if skipped_reason is None and not args.sync_only:
                for book, cfg in BOOKS.items():
                    if args.book and book != args.book:
                        continue
                    lots = [l for l in lots_all if l["book"] == book]
                    others = {l["ticker"] for l in lots_all if l["book"] != book}
                    if book == "long":
                        ranked, keep, scored = long_targets(store, market, day, con)
                        retries = set()
                        keep = long_keep(keep, no_bar)    # no bar today = no rank today: a data gap is not a rank_out
                    elif book == "core":
                        ranked, keep, scored, retries = [CORE_TICKER], {CORE_TICKER}, True, set()
                    else:
                        ranked, retries, entry_info = insider_targets(store, day, con, calendar)
                        keep, scored = set(), True
                    ranked = entry_candidates(ranked)     # class shares: not entered for now (S47 addendum)
                    spreads = {t: market.spread_pct(t, day) for t in ranked if t in market.close.columns}
                    book_plans = plan_book(book, cfg, lots, ranked, keep, day.date(), next_session, ref_close, spreads,
                                           books[book]["cash_usd"], blocked | others, scored, tif, frozen=blocked,
                                           inflight=[f for f in flying if f["book"] == book])
                    if book == "insider":                 # retries and late entries: tagged, evaluated separately
                        tag_insider_entries(book_plans, retries, entry_info)
                    plans += book_plans
                    targets_dbg[book] = {"n_ranked": len(ranked), "n_keep": len(keep), "scored": scored,
                                         "top": ranked[:10]}
            nav = mark_books(con, day.date(), ref_close, float(acct["equity"]))

        # ---- caps, then submit or print
        existing = {o["client_order_id"] for o in broker.open_orders()}
        # an order the broker never had (not_sent, or a refused POST) is sent again by a rerun under the same id,
        # as before S48 when neither was written
        already = {r[0] for r in con.execute("""SELECT client_order_id FROM agent_orders WHERE dry_run = FALSE
                                                AND NOT (coalesce(status, '') IN (?, 'rejected') AND alpaca_id IS NULL)""",
                                             [NOT_SENT]).fetchall()}
        skipped = [o["client_order_id"] for o in plans if o["client_order_id"] in existing | already]
        plans = [o for o in plans if o["client_order_id"] not in existing | already]
        slots = {b["book"]: b["equity_usd"] / BOOKS[b["book"]]["max_positions"] for b in nav if b["book"] in BOOKS}
        plans, guard_notes = guard_buys(cap_orders(plans), {b: r["cash_usd"] for b, r in books.items()}, flying,
                                        float(acct["cash"]) if acct.get("cash") is not None else None, slots, ref_close)
        for n in guard_notes:
            print(f"  pre-submit guard: {n}")
        sent, not_attempted = send_orders(con, broker, plans, args.submit)
        if not_attempted:
            print(f"  broker unreachable, {len(not_attempted)} order(s) not attempted")
    finally:
        con.close()

    summary = {"as_of": str(day.date()), "last_session": str(last_session), "stale_bars": stale, "tif": tif,
               "skipped_reason": None if args.sync_only else skipped_reason,
               "mode": "sync" if args.sync_only else "submit" if args.submit else "dry_run", "account_equity": float(acct["equity"]),
               "account_cash": float(acct["cash"]), "sync": sync, "model_px_filled": n_model,
               "reconcile": msgs, "possible_splits": splits, "cash_check": cash_chk, "targets": targets_dbg, "books": nav,
               "claimed_orders": [r["client_order_id"] for r in claimed], "claim_error": claim_error,
               "guard_notes": guard_notes, "not_attempted": not_attempted,
               "orders": [{k: (str(v) if isinstance(v, (dt.date, pd.Timestamp)) else v) for k, v in o.items()} for o in sent],
               "skipped_duplicates": skipped, "generated_at": dt.datetime.now().isoformat(timespec="seconds")}
    summary.update({"bars_coverage_pct": None if coverage is None else round(coverage * 100, 2),
                    "bars_missing_for_held": no_bar, "bars_update": bars_stats,
                    "bars_update_failed": None if bars_stats is None else bars_update_note(bars_stats) is not None})
    if no_bar:
        print(f"  held without a bar on {day.date()} (kept, not ranked): {no_bar}")
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, f"{'sync' if args.sync_only else 'exec'}_{day.date()}.json"), "w") as f:
        json.dump(summary, f, indent=1, default=str)
    tag = "SYNC" if args.sync_only else "SUBMITTED" if args.submit else "DRY RUN"
    note = f", NOTHING PLANNED: {skipped_reason}" if (skipped_reason and not args.sync_only) else ""
    print(f"[{tag}] bar {day.date()} (last session {last_session}{note}) tif={tif} "
          f"· account ${float(acct['equity']):,.0f} · fills synced {sync['filled']}+{sync['closed']} · "
          f"reconcile {'ok' if not msgs else msgs} · cash check "
          f"{'not run (orders in flight)' if cash_chk['ok'] is None else 'ok' if cash_chk['ok'] else 'OFF by $' + format(cash_chk['diff'], ',.2f')}")
    for b in nav:
        print(f"  {b['book']:8s} equity ${b['equity_usd']:,.0f}  cash ${b['cash_usd']:,.0f}  positions {b['n_positions']}")
    for o in sent:
        lp = f"limit {o['limit_price']}" if o["limit_price"] else ("MOO" if o["tif"] == "opg" else "MKT day")
        print(f"  {o['book']:8s} {o['side']:4s} {o['ticker']:6s} x{o['qty']:<5d} {lp:<14s} ref {o['ref_close'] or 0:.2f}  {o['reason']}  "
              f"[{o['status']}]" + (f"  {o['error']}" if o.get("error") else ""))
    if skipped:
        print(f"  skipped {len(skipped)} already-submitted ids")
    if skipped_reason and not args.sync_only:
        return 3                                      # non-zero: the wrapper logs "execute failed", health reads it
    incomplete = not_attempted or [o for o in sent if o.get("status") in (NOT_SENT, SUBMIT_UNKNOWN)]
    return 4 if incomplete else 0                     # an order the broker may not have: the evening needs a rerun


if __name__ == "__main__":
    raise SystemExit(main())
