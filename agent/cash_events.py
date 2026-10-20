"""Broker cash that is not a fill: the dividends the paper account pays and the regulatory fees it charges.

Why (2026-10-19 diagnosis): the books' cash (agent_books.cash_usd, agent_book_nav) moved only on fills
(agent/execute.py _apply_buy / _apply_sell), while the Alpaca paper account charges FEE activities (sub types
REG and CAT, one per sub type per trading day, since 2026-09-29) and pays DIV activities (since 2026-10-14).
execute.cash_check was off by $122.20 = BZ 68.34 + MRP 55.30 - 1.44 of fees; the fills matched to the cent.
This books those activities into the books, so the books' cash follows the broker's again.

Booked (agent_cash_events, one row per activity and book, PRIMARY KEY (activity_id, book)):
  DIV family (activity_type DIV*): the holder is the non-dry-run lot of that ticker with
      entry_day < cut <= exit_day (or still open), cut = the last session on or before the record date (T+1:
      the ex-date). A purchase settles by the record date only when made before the cut; a sale on the cut or
      later settles after it, so the seller is still the holder of record. With a Saturday record date the cut
      is the Friday: the Friday's seller is paid, its buyer is not. The record date is read from the
      description ("Rec Date: YYYY-MM-DD"), else from the cached corporate action of that ticker payable that
      day (agent_corp_actions, agent/dividends.py), else it is the activity date. A ticker is in one book at a
      time, so normally one book; lots of several books share by the shares held at the cut (dividends.held:
      a partly sold lot's qty is not that). BZ: bought 9/22, record date 9/28, sold 9/30, paid 10/14: the long
      book's. A share count other than the description's "Pos QTY" is noted.
  FEE: shared over the books by that trade day's fills (agent_orders; filled_at is ET, so its date is the trade
      day): REG (SEC fee + TAF, both on sells) by sell notional, CAT by shares filled on both sides, any other
      sub type by notional on both sides. Cents by largest remainder, ties by book name, so the books' shares
      add up to the activity exactly.
Status: only an 'executed' activity is booked (one without a status counts as executed). A 'canceled' one is
not booked, and a booked one the broker has since canceled is listed (canceled, booked True) without changing
its rows: whether the broker took the cash back or posts a reversing activity (booked like any other), the cash
check shows. Any other status is not booked and is reported as unallocated ('status <x>').
Not booked, reported: an activity with no holder lot or no fill that day to share it by ('unallocated'; the cash
check stays off by it until a person books it by hand: its rows in agent_cash_events with basis 'manual', the
same amounts on agent_books.cash_usd), and every activity type other than FILL, JNLC (the initial
funding = the books' allocations), DIV* and FEE seen in the last UNHANDLED_DAYS ('unhandled'). That report reads
every type, unfiltered, over a short window: the broker refuses a whole request with one type name it does not
know (SSO, 2026-10-20), so a list of the other types would break on the first renamed one and miss new ones.

When the cash counts (cash_day): a DIV is credited in the morning (created ~09:38 PT), so that day's 16:10 run
includes it: the first session on or after its date. A FEE posts ~17:07 PT, after the run: the first session
after its trade day. Never before the session of its created_at (ET date, first session on or after it): an
activity the broker posts late or backdates (a dividend paid weeks after its pay date) counts from when the
account had the cash, not from its 'date'. cash_day = min(that, run_day): an event booked by a run counts from
that run's NAV row at the latest. Booking one activity, in one transaction: its rows, agent_books.cash_usd +=
net_usd, and every agent_book_nav row of the book with as_of >= cash_day gets the same amount on cash_usd and
equity_usd: each was written before the money reached the books, the run_day row and any later one included
(execute.mark_books rewrites the run_day row afterwards from agent_books.cash_usd, which already holds it, so
the result is the same; when the run stops before mark_books, or the CLI books after the evening's runs, the
row is still right). account_equity_usd is the broker's and is never touched. After a catch-up the NAV
history's cash follows the broker's; in live running cash_day = run_day and only that day's row is restated.

Idempotent: an activity id already in the table is skipped, and rows are never changed or deleted.
agent/auction_basis.py takes the booked DIV cash out of its basis again (div_cash_cum) and puts the broker's fees
back (fee_cash_cum), so the auction basis is what it was before this module.

Deploying: the first sync changes agent_books.cash_usd and the agent_book_nav history (+$122.20 at the 10/20
catch-up), and agent.dividends ALTERs agent_dividends. The code before 2026-10-20 cannot read that ledger right
(its agent.dividends inserts 9 values into the wider table and fails; its auction basis and evaluate would put
the DIV cash into equity_auction and sim_tr). So: back up optradar.db first; run neither this, agent.dividends
nor agent.auction_basis of an unmerged tree against the real ledger; after the first live run, run
agent.auction_basis once if that run was the 16:10 or 19:15 one (agent_auction_nav then matches before the 08:41
publish). A rollback is restoring that backup, not a git revert.

Usage: python -m agent.cash_events [--db PATH] [--day YYYY-MM-DD]
  --day is the run_day: by default the session after the last agent_book_nav row, so every NAV row already
  written is restated where due and the next mark_books writes the first row that holds the money. Run it after
  the evening's last run: a later run the same evening (the 19:15 late-insider pass) rewrites that day's row with
  the fees posted ~17:07 in its cash, which this run dated to the next session. It is optional: execute.main calls
  sync in every run, so the first run of this code catches up by itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import re

import pandas as pd

from agent import dividends, ledger

START = dt.datetime(2026, 9, 19)        # before the account's first activity (the funding, 2026-09-21)
DIV_TYPES = ("DIV", "DIVCGL", "DIVCGS", "DIVNRA", "DIVROC", "DIVTW", "DIVFT", "DIVTXEX", "DIVFEE")
FEE_TYPES = ("FEE",)
NOT_HERE = ("FILL", "JNLC")             # fills: execute.sync_fills; JNLC: the funding, i.e. the books' allocations
UNHANDLED_DAYS = 31                     # window of the unfiltered read for the 'unhandled' report
REC_RE = re.compile(r"Rec Date:\s*(\d{4}-\d{2}-\d{2})")
QTY_RE = re.compile(r"Pos QTY:\s*([0-9.]+)")

DDL = """CREATE TABLE IF NOT EXISTS agent_cash_events (
    activity_id VARCHAR, book VARCHAR, kind VARCHAR, activity_type VARCHAR, sub_type VARCHAR, ticker VARCHAR,
    activity_date DATE, record_date DATE, net_usd DOUBLE, account_usd DOUBLE, basis VARCHAR, cash_day DATE,
    booked_run_day DATE, booked_at TIMESTAMP, note VARCHAR, PRIMARY KEY (activity_id, book))"""
COLS = ["activity_id", "book", "kind", "activity_type", "sub_type", "ticker", "activity_date", "record_date", "net_usd",
        "account_usd", "basis", "cash_day", "booked_run_day", "booked_at", "note"]


def ensure_table(con) -> None:
    con.execute(DDL)


def kind_of(a: dict) -> str | None:
    t = str(a.get("activity_type") or "")
    return "DIV" if t.startswith("DIV") else "FEE" if t in FEE_TYPES else None


def activity_date(a: dict) -> dt.date:
    """'date' of a non-trade activity (FEE: the trade day; DIV: the pay day), else the transaction time's date."""
    return dt.date.fromisoformat(str(a.get("date") or a.get("transaction_time"))[:10])


def parse_record_date(desc: str | None) -> dt.date | None:
    m = REC_RE.search(desc or "")
    return dt.date.fromisoformat(m.group(1)) if m else None


def parse_pos_qty(desc: str | None) -> float | None:
    m = QTY_RE.search(desc or "")
    return float(m.group(1)) if m else None


def split_cents(amount: float, weights: dict[str, float]) -> dict[str, float] | None:
    """amount shared by weight in whole cents, largest remainder first (ties by book name), so the shares add up to
    the amount exactly; the sign is the amount's. None when no weight is positive (nothing to share it by)."""
    w = {b: float(v) for b, v in weights.items() if v and v > 0}
    if not w:
        return None
    cents = round(amount * 100)
    sign, total, tw = (-1 if cents < 0 else 1), abs(cents), sum(w.values())
    exact = {b: total * v / tw for b, v in w.items()}
    out = {b: math.floor(x + 1e-9) for b, x in exact.items()}
    for b in sorted(w, key=lambda b: (-(exact[b] - out[b]), b))[:total - sum(out.values())]:
        out[b] += 1
    return {b: sign * c / 100 for b, c in sorted(out.items())}


def _session_from(day: dt.date, sessions: list[dt.date]) -> dt.date:
    """The first session on or after `day`; weekdays stand in for sessions the list lacks."""
    s = sorted(sessions)
    later = [x for x in s if x >= day]
    if later and s[0] <= day:
        return later[0]
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def created_day(a: dict) -> dt.date | None:
    """The ET date the broker posted the activity on (created_at, UTC); None without one."""
    c = a.get("created_at")
    try:
        t = pd.Timestamp(c)
    except (TypeError, ValueError):
        return None
    if pd.isna(t):
        return None
    return (t.tz_localize("UTC") if t.tzinfo is None else t).tz_convert("America/New_York").date()


def cash_day(kind: str, day: dt.date, sessions: list[dt.date], run_day: dt.date, created: dt.date | None = None) -> dt.date:
    """The first session whose 16:10 run sees the money: on or after the day for a DIV (credited in the morning), after
    it for a FEE (posted in the evening), and not before the session the broker posted it on (created: a late or
    backdated activity); never later than run_day."""
    rule = _session_from(day if kind == "DIV" else day + dt.timedelta(days=1), sessions)
    if created is not None:
        rule = max(rule, _session_from(created, sessions))
    return min(rule, run_day)


def record_cut(record_date: dt.date, sessions: list[dt.date]) -> dt.date:
    """The T+1 ex-date of a record date: the last session on or before it (the record date itself when it is one).
    Weekdays stand in for sessions when the list does not cover the record date."""
    s = sorted(sessions)
    if s and s[0] <= record_date <= s[-1]:
        return max(x for x in s if x <= record_date)
    d = record_date
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def _has_table(con, name: str) -> bool:
    return bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0])


def record_date_of(con, a: dict) -> tuple[dt.date, str]:
    """(record date, where it came from): the description, the cached corporate action payable that day, the activity date."""
    rd = parse_record_date(a.get("description"))
    if rd:
        return rd, "description"
    day = activity_date(a)
    if _has_table(con, "agent_corp_actions"):
        r = con.execute("""SELECT min(record_date) FROM agent_corp_actions WHERE ca_type = 'cash_dividends'
                           AND symbol = ? AND payable_date = ?""", [a.get("symbol"), day]).fetchone()
        if r and r[0] is not None:
            return pd.Timestamp(r[0]).date(), "corporate action"
    return day, "activity date"


def div_holders(con, ticker: str, cut: dt.date) -> dict[str, float]:
    """{book: shares held at the close before the cut} of the non-dry-run lots of `ticker` with entry_day < cut <= exit_day
    (or open); cut = record_cut(record date)."""
    lots = dividends.paper_lots(con)
    lots = lots[lots["ticker"] == ticker]
    sold = dividends.sold_by_lot(con, lots)
    out: dict[str, float] = {}
    for r in lots.itertuples():
        entry, exit_ = pd.Timestamp(r.entry_day).date(), None if pd.isna(r.exit_day) else pd.Timestamp(r.exit_day).date()
        if entry < cut and (exit_ is None or cut <= exit_):
            out[r.book] = out.get(r.book, 0.0) + dividends.held(r.bought_qty, sold.get(r.lot_id), cut)
    return out


FEE_BASIS = {"REG": "sell_notional", "CAT": "filled_shares"}


def fee_weights(con, day: dt.date, sub_type: str | None) -> tuple[dict[str, float], str]:
    """({book: weight}, basis) from the trade day's fills: REG by sell notional, CAT by shares both sides, else notional."""
    basis = FEE_BASIS.get(sub_type or "", "notional")
    rows = con.execute("""SELECT book, side, sum(filled_qty), sum(filled_qty * filled_avg_px) FROM agent_orders
                          WHERE dry_run = FALSE AND filled_qty > 0 AND CAST(filled_at AS DATE) = ? GROUP BY 1, 2""", [day]).fetchall()
    w: dict[str, float] = {}
    for book, side, qty, notional in rows:
        v = float(qty or 0) if basis == "filled_shares" else float(notional or 0) if (basis == "notional" or side == "sell") else 0.0
        w[book] = w.get(book, 0.0) + v
    return w, basis


def allocate(con, a: dict, sessions: list[dt.date], run_day: dt.date) -> tuple[list[dict], str | None, str | None]:
    """(rows to book, why it cannot be booked or None, a note or None) for one DIV-family or FEE activity."""
    kind, day, amount = kind_of(a), activity_date(a), float(a.get("net_amount") or 0)
    note, rd, ticker = None, None, a.get("symbol")
    if kind == "DIV":
        if not ticker:
            return [], "no symbol on the activity", None
        rd, src = record_date_of(con, a)
        cut = record_cut(rd, sessions)
        held = div_holders(con, ticker, cut)
        shares = split_cents(amount, held)
        if shares is None:
            return [], f"no lot of {ticker} held over the record date {rd} ({src}" + (f", ex-date {cut}" if cut != rd else "") + ")", None
        basis = "record_date_holder" + ("_by_qty" if len(held) > 1 else "")
        pq = parse_pos_qty(a.get("description"))
        notes = [] if src == "description" else [f"record date from the {src}"]
        if pq is not None and abs(pq - sum(held.values())) > 1e-6:
            notes.append(f"broker Pos QTY {pq:g}, lots {sum(held.values()):g}")
        note = "; ".join(notes) or None
    else:
        w, basis = fee_weights(con, day, a.get("activity_sub_type"))
        shares = split_cents(amount, w)
        if shares is None:
            return [], f"no fill on {day} to share it by ({basis})", None
    shares = {b: v for b, v in shares.items() if v} or shares     # a book whose share rounds to 0 gets no row
    cd = cash_day(kind, day, sessions, run_day, created_day(a))
    now = pd.Timestamp.now()
    rows = [{"activity_id": a["id"], "book": b, "kind": kind, "activity_type": a.get("activity_type"),
             "sub_type": a.get("activity_sub_type"), "ticker": ticker, "activity_date": day, "record_date": rd,
             "net_usd": usd, "account_usd": amount, "basis": basis, "cash_day": cd, "booked_run_day": run_day,
             "booked_at": now, "note": note} for b, usd in shares.items()]
    return rows, None, note


def book(con, rows: list[dict]) -> int:
    """One activity's rows, the books' cash and the NAV rows from its cash_day on (all written before the money reached
    the books), in one transaction. Returns the number of agent_book_nav rows restated."""
    n = 0
    con.execute("BEGIN TRANSACTION")
    try:
        con.executemany(f"INSERT INTO agent_cash_events ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                        [[r[c] for c in COLS] for r in rows])
        for r in rows:
            con.execute("UPDATE agent_books SET cash_usd = cash_usd + ?, updated = ? WHERE book = ?",
                        [r["net_usd"], pd.Timestamp.now(), r["book"]])
            where = "book = ? AND as_of >= ?"
            args = [r["book"], r["cash_day"]]
            n += int(con.execute(f"SELECT count(*) FROM agent_book_nav WHERE {where}", args).fetchone()[0])
            con.execute(f"UPDATE agent_book_nav SET cash_usd = cash_usd + ?, equity_usd = equity_usd + ? WHERE {where}",
                        [r["net_usd"], r["net_usd"], *args])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n


def _brief(a: dict) -> dict:
    return {"id": a.get("id"), "type": a.get("activity_type"), "sub_type": a.get("activity_sub_type"), "ticker": a.get("symbol"),
            "date": str(activity_date(a)), "net_usd": float(a.get("net_amount") or 0)}


def sync(con, broker, sessions: list[dt.date], run_day: dt.date) -> dict:
    """Book every DIV-family and FEE activity since START that the table lacks; report what could not be booked and
    the activity types this module does not handle. A failed read of the activities raises (the caller reports it
    and goes on); a failed read for the 'unhandled' report is only recorded in it."""
    ensure_table(con)
    acts = broker.activities(list(DIV_TYPES + FEE_TYPES), START)
    known = {r[0] for r in con.execute("SELECT DISTINCT activity_id FROM agent_cash_events").fetchall()}
    out = {"run_day": str(run_day), "n_fetched": len(acts), "n_already": 0, "booked": [], "unallocated": [], "unhandled": [],
           "canceled": [], "notes": [], "unhandled_error": None, "booked_usd": {}}
    for a in sorted(acts, key=lambda a: (activity_date(a), str(a.get("id")))):
        if kind_of(a) is None:
            continue
        status = str(a.get("status") or "executed").lower()
        if a.get("id") in known:
            out["n_already"] += 1
            if status == "canceled":                # booked, since canceled: listed, its rows stay
                out["canceled"].append(dict(_brief(a), kind=kind_of(a), booked=True))
            continue
        if status == "canceled":
            out["canceled"].append(dict(_brief(a), kind=kind_of(a), booked=False))
            continue
        if status != "executed":
            out["unallocated"].append(dict(_brief(a), kind=kind_of(a), why=f"status {status}"))
            continue
        rows, why, note = allocate(con, a, sessions, run_day)
        if why:
            out["unallocated"].append(dict(_brief(a), kind=kind_of(a), why=why))
            continue
        restated = book(con, rows)
        known.add(a["id"])
        for r in rows:
            out["booked_usd"][r["book"]] = round(out["booked_usd"].get(r["book"], 0.0) + r["net_usd"], 2)
        out["booked"].append(dict(_brief(a), kind=rows[0]["kind"], books={r["book"]: r["net_usd"] for r in rows},
                                  cash_day=str(rows[0]["cash_day"]), restated_rows=restated))
        if note:
            out["notes"].append(f"{a.get('symbol') or a.get('activity_type')} {activity_date(a)}: {note}")
    try:
        since = dt.datetime.combine(run_day - dt.timedelta(days=UNHANDLED_DAYS), dt.time())
        for a in broker.activities(None, max(START, since)):
            t = str(a.get("activity_type") or "")
            if t not in NOT_HERE and kind_of(a) is None:
                out["unhandled"].append(dict(_brief(a), description=str(a.get("description") or "")[:120]))
    except Exception as exc:                       # noqa: BLE001 — the booking above stands; the report says it is incomplete
        out["unhandled_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return out


def summary_line(r: dict | None) -> str:
    """One line for the run's log: booked / unallocated / unhandled, or the error that stopped the sync. No 'failed' in
    it: agent.health reads the job's log for such words, and agent.health check_cash_events reports these itself."""
    if not r:
        return "cash events not run"
    if r.get("error"):
        return f"cash events not booked: {r['error']}"
    usd = round(sum(sum(b["books"].values()) for b in r.get("booked", [])), 2)
    back = sum(1 for c in r.get("canceled", []) if c.get("booked"))
    return (f"cash events booked {len(r.get('booked', []))} (net {'-' if usd < 0 else ''}${abs(usd):,.2f}), "
            f"unallocated {len(r.get('unallocated', []))}, "
            f"unhandled {len(r.get('unhandled', []))}" + (" (other types not read)" if r.get("unhandled_error") else "")
            + (f", canceled after booking {back}" if back else ""))


def load(con) -> pd.DataFrame:
    """book, kind, net_usd, cash_day of every booked row; empty (same columns) on a ledger without the table."""
    cols = ["book", "kind", "ticker", "net_usd", "cash_day", "record_date", "activity_date"]
    if not _has_table(con, "agent_cash_events"):
        return pd.DataFrame(columns=cols)
    df = con.execute(f"SELECT {', '.join(cols)} FROM agent_cash_events").df()
    for c in ("cash_day", "record_date", "activity_date"):
        df[c] = [None if pd.isna(x) else pd.Timestamp(x).date() for x in df[c]]
    return df


def cumulative(ev: pd.DataFrame, day: dt.date, book: str, kind: str) -> float:
    """Sum of the book's booked net_usd of `kind` with cash_day <= day (signed as the broker signs it)."""
    if ev is None or not len(ev):
        return 0.0
    m = (ev["book"] == book) & (ev["kind"] == kind) & ev["cash_day"].map(lambda d: d is not None and d <= day)
    return float(ev.loc[m, "net_usd"].sum())


def unpaid(entitled: list[dict], paid: list[dict], today: dt.date, grace_days: int = 5) -> list[dict]:
    """Entitled cash dividends ({ticker, book, ex_date, record_date, pay_date, usd}) whose pay date is more than grace_days
    past with no broker DIV of that ticker for them ({ticker, record_date, activity_date}): same record date, or, when
    either side lacks one, paid on the pay date or later. The paper broker does not pay every dividend (STX 10/07,
    KBDC 10/16): this is a note, not an error."""
    out = []
    for e in entitled:
        pay = e.get("pay_date")
        if pay is None or (today - pay).days <= grace_days:
            continue
        mine = [p for p in paid if p.get("ticker") == e.get("ticker")]
        hit = any((p.get("record_date") == e.get("record_date")) if (p.get("record_date") and e.get("record_date"))
                  else (p.get("activity_date") is not None and p["activity_date"] >= pay) for p in mine)
        if not hit:
            out.append(e)
    return out


def default_run_day(con, sessions: list[dt.date]) -> dt.date:
    """The session after the last agent_book_nav row (today on a ledger without one): the next row mark_books writes."""
    last = con.execute("SELECT max(as_of) FROM agent_book_nav").fetchone()[0]
    if last is None:
        return dt.date.today()
    last = pd.Timestamp(last).date()
    later = [s for s in sorted(sessions) if s > last]
    return later[0] if later else last + dt.timedelta(days=1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=ledger.OPTRADAR_DB, help="the ledger (a copy, for a test run)")
    ap.add_argument("--day", default=None, help="run_day (default: the session after the last agent_book_nav row)")
    args = ap.parse_args(argv)
    from agent.broker import alpaca
    broker = alpaca.from_env()
    broker.account()                                          # raises unless PA… paper account
    sessions = broker.calendar(START.date(), dt.date.today() + dt.timedelta(days=30))
    con = ledger.connect(args.db)
    try:
        ledger.ensure_schema(con)
        run_day = dt.date.fromisoformat(args.day) if args.day else default_run_day(con, sessions)
        r = sync(con, broker, sessions, run_day)
    finally:
        con.close()
    print(f"run_day {run_day}: fetched {r['n_fetched']}, already booked {r['n_already']}")
    for b in r["booked"]:
        print(f"  booked     {b['kind']:3s} {b['sub_type'] or '':5s} {b['ticker'] or '':6s} {b['date']} ${b['net_usd']:>9,.2f} -> "
              + ", ".join(f"{k} {v:+.2f}" for k, v in b["books"].items()) + f"  cash_day {b['cash_day']}, {b['restated_rows']} NAV rows restated")
    for u in r["unallocated"]:
        print(f"  UNALLOCATED {u['kind']} {u['sub_type'] or ''} {u['ticker'] or ''} {u['date']} ${u['net_usd']:,.2f}: {u['why']}")
    for u in r["unhandled"]:
        print(f"  unhandled  {u['type']} {u['date']} ${u['net_usd']:,.2f} {u['description']}")
    for c in r["canceled"]:
        print(f"  canceled   {c['kind']} {c['sub_type'] or ''} {c['ticker'] or ''} {c['date']} ${c['net_usd']:,.2f}"
              + ("  (booked earlier; its rows stay)" if c["booked"] else "  (not booked)"))
    for n in r["notes"]:
        print(f"  note: {n}")
    if r["unhandled_error"]:
        print(f"  other activity types not read: {r['unhandled_error']}")
    print(summary_line(r))
    return 1 if r["unallocated"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
