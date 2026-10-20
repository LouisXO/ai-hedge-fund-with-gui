"""Dividends the paper lots were entitled to by ex-date: the total-return yardstick (S48; source changed 2026-10-20).

Why: the backtests, the rule replay (agent/drift.py) and the SPY / QQQ comparisons are total return (adj_close).
Since 2026-10-14 the Alpaca paper account pays DIV activities, and agent/cash_events.py books them into the
books' cash on the day the broker pays them, to the lot that held the shares on the record date. This table is
the other view: per lot, the cash dividends a holder of the same shares was entitled to, counted on the
ex-date as adj_close counts them. agent/auction_basis.py keeps their running sum (div_cum) next to the price NAV,
and takes the DIV cash booked into the books out of its basis again (div_cash_cum), so nothing counts twice. The
books' cash and the cash check follow the broker (cash_events), never this table.

Source: Alpaca's market-data corporate actions (/v1/corporate-actions), cached in agent_corp_actions. Every run
asks for the lots' tickers from the earliest entry - LOOKBACK_DAYS to today + FORWARD_DAYS and upserts by id;
rows are never deleted, so a failed fetch keeps what the last one knew (the error is reported, never raised).
The endpoint's start / end select by process (= pay) date, not ex-date (asked 10/20..10/25 it returns PSEC ex
9/28 pay 10/21; LWAY's ex 6/05 came with its pay date 10/19): the window reaches a year ahead, so a passed
ex-date whose pay date is months away is known on its ex-date and does not appear later, rewriting history.
Until 2026-10-20 the ex-dates were read from steps of adj_close / close in the panel (factor_events). The
nightly re-fetch of the last 7 days leaves neighbouring rows fetched about a day apart, so a real ex-date step
was taken for a basis break ($0) as soon as the row before it left the window, and the table, recomputed every
night, changed its history (BZ $68.34 on 10/01, $0 from 10/05). factor_events stays as a cross-check only: a
step on a lot's ticker with no cached corporate action within CROSS_CHECK_DAYS weekdays is listed under
to_check, never counted.

Rules:
  - entitlement: bought before the ex-date and still held at the close before it, entry_day < ex_date <= exit_day
    (or still open: a lot sold at the ex-date's open was the holder), and ex_date <= the last session (the last
    agent_book_nav day): an announced ex-date counts once it has happened.
  - shares: those the lot held at the close before the ex-date (held): bought minus its sells filled before the
    ex-date, sells going to the book's oldest lot of the ticker as execute._apply_sell books them. agent_lots.qty
    is not that after a partial sale (an open lot's is what is left, a closed lot's all it sold), and a nightly
    rebuild on it would change a past ex-date's amount with every later sale.
  - amount: per_share = the rate, usd = round(shares x rate, 2), gross before withholding, on the ex-date (the cash
    comes on pay_date). Special dividends and returns of capital are cash too: kind 'dividend'. Two cash dividends
    of one ticker on one ex-date (a regular and a special, GOLD 9/16) make one row, since the table's key is
    (lot_id, ex_date): rates and amounts added, ca_id lists both.
  - non-cash actions (splits, reverse splits, stock dividends, spin-offs, mergers, name changes ...) on a held lot:
    kind 'split_or_special' and $0, for a person to check (execute.reconcile freezes the ticker when its share
    count moved). One that falls on the ex-date of a cash dividend of the same lot is listed in to_check only.
  - due bills (ex-date after the pay date: ARI, VISN): the ex-date rule still holds here; the broker pays the
    record-date holder, which is the rule cash_events follows.

Usage: python -m agent.dividends     (also run by agent.auction_basis before it builds the NAV)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.parse

import numpy as np
import pandas as pd

from agent import ledger
from hedge_fund.features.panel import PanelStore

FACTOR_TOL = 0.0005          # as agent/sources/alpaca_bars.FACTOR_TOL
SPLIT_MIN = 1.25             # as alpaca_bars.SPLIT_MIN: a factor ratio this far from 1 is a split, not a dividend
SAME_FETCH = pd.Timedelta(hours=1)
INDEX_SYMBOLS = ("SPY", "QQQ", "IWM")
LOOKBACK_DAYS = 10           # before the earliest entry: corporate actions asked for, price history loaded for the cross-check
FORWARD_DAYS = 365           # by pay date (Alpaca's start / end are the process date): actions counted once their ex-date has passed
CROSS_CHECK_DAYS = 3         # weekdays between a factor step and a cached action that still explain each other
CA_URL = "https://data.alpaca.markets/v1/corporate-actions"
CA_BATCH = 50                # symbols per request (the URL carries them)
CASH = "cash_dividends"
SOURCE = "alpaca_ca"

DDL = """CREATE TABLE IF NOT EXISTS agent_dividends (
    lot_id VARCHAR, book VARCHAR, ticker VARCHAR, ex_date DATE, per_share DOUBLE, qty DOUBLE, usd DOUBLE,
    kind VARCHAR, computed_at TIMESTAMP, PRIMARY KEY (lot_id, ex_date))"""
# added 2026-10-20 with the corporate-actions source; record_date matches the broker's DIV (cash_events) to the row
ADD_COLUMNS = [("pay_date", "DATE"), ("special", "BOOLEAN"), ("source", "VARCHAR"), ("ca_id", "VARCHAR"), ("record_date", "DATE")]
DIV_COLS = ["lot_id", "book", "ticker", "ex_date", "per_share", "qty", "usd", "kind", "computed_at"] + [c for c, _ in ADD_COLUMNS]
# "foreign" is a reserved word in DuckDB: quoted wherever it is named
CA_DDL = """CREATE TABLE IF NOT EXISTS agent_corp_actions (
    id VARCHAR PRIMARY KEY, ca_type VARCHAR, symbol VARCHAR, ex_date DATE, record_date DATE, payable_date DATE,
    rate DOUBLE, special BOOLEAN, "foreign" BOOLEAN, raw VARCHAR, fetched_at TIMESTAMP)"""
CA_COLS = ["id", "ca_type", "symbol", "ex_date", "record_date", "payable_date", "rate", "special", '"foreign"', "raw", "fetched_at"]
# the held side of an action that names two symbols (spin-off, merger, name change, unit split)
SYMBOL_KEYS = ("symbol", "source_symbol", "acquiree_symbol", "old_symbol", "target_symbol")


def ensure_tables(con) -> None:
    con.execute(DDL)
    con.execute(CA_DDL)
    have = {r[0] for r in con.execute("DESCRIBE agent_dividends").fetchall()}
    for col, typ in ADD_COLUMNS:
        if col not in have:
            con.execute(f"ALTER TABLE agent_dividends ADD COLUMN {col} {typ}")


# ---------------------------------------------------------------- corporate actions ----
def _day(v) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def fetch_alpaca(symbols: list[str], start: dt.date, end: dt.date) -> list[tuple[str, dict]]:
    """[(type, action)] of every corporate action of `symbols` in [start, end] (all types, every page). Raises on any
    answer other than 200: the caller keeps the cache."""
    from agent.sources.price_probe import _get, keys_from_env
    key, secret = keys_from_env("alpaca")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    out: list[tuple[str, dict]] = []
    for i in range(0, len(symbols), CA_BATCH):
        token = None
        while True:
            p = {"symbols": ",".join(symbols[i:i + CA_BATCH]), "start": start.isoformat(), "end": end.isoformat(), "limit": 1000}
            if token:
                p["page_token"] = token
            code, body = _get(f"{CA_URL}?{urllib.parse.urlencode(p)}", headers)
            if code != 200:
                raise RuntimeError(f"corporate actions HTTP {code}: {body[:160]}")
            d = json.loads(body)
            for ca_type, items in (d.get("corporate_actions") or {}).items():
                out.extend((ca_type, x) for x in items or [])
            token = d.get("next_page_token")
            if not token:
                break
    return out


def upsert_actions(con, actions: list[tuple[str, dict]], fetched_at: dt.datetime | None = None) -> int:
    """Write fetched actions by id (a corrected one replaces its row; a row is never deleted)."""
    now = fetched_at or dt.datetime.now()
    rows = []
    for ca_type, x in actions:
        if not x.get("id"):
            continue
        sym = next((x[k] for k in SYMBOL_KEYS if x.get(k)), None)
        ex = _day(x.get("ex_date") or x.get("effective_date") or x.get("process_date"))
        rows.append([x["id"], ca_type, sym, ex, _day(x.get("record_date")), _day(x.get("payable_date")),
                     float(x["rate"]) if ca_type == CASH and x.get("rate") is not None else None,
                     bool(x.get("special")), bool(x.get("foreign")), json.dumps(x, sort_keys=True), now])
    if rows:
        con.executemany(f"INSERT OR REPLACE INTO agent_corp_actions ({', '.join(CA_COLS)}) VALUES ({', '.join('?' * len(CA_COLS))})", rows)
    return len(rows)


def paper_lots(con) -> pd.DataFrame:
    """Every lot whose entry order was not a dry run: lot_id, book, ticker, qty, entry_day, exit_day, bought_qty (the
    shares bought: an open lot's qty is what is left after its partial sales, a closed lot's is all it sold)."""
    return con.execute("""SELECT l.lot_id, l.book, l.ticker, l.qty, l.entry_day, l.exit_day,
                                 CASE WHEN l.exit_day IS NULL THEN l.qty + coalesce(l.sold_qty, 0) ELSE l.qty END AS bought_qty
                          FROM agent_lots l LEFT JOIN agent_orders o ON o.client_order_id = l.entry_order
                          WHERE coalesce(o.dry_run, FALSE) = FALSE""").df()


def sold_by_lot(con, lots: pd.DataFrame) -> dict[str, list[tuple[dt.date, float]]]:
    """{lot_id: [(day, shares sold)]} from the filled non-dry-run sells (agent_orders): each goes to the book's oldest
    lot of the ticker bought by then with shares left, as execute._apply_sell books it."""
    sells = con.execute("""SELECT book, ticker, CAST(filled_at AS DATE), filled_qty FROM agent_orders
                           WHERE dry_run = FALSE AND side = 'sell' AND filled_qty > 0 ORDER BY filled_at""").fetchall()
    left: dict[tuple[str, str], list[list]] = {}
    for r in lots.sort_values(["entry_day", "lot_id"]).itertuples() if len(lots) else []:
        left.setdefault((r.book, r.ticker), []).append([r.lot_id, pd.Timestamp(r.entry_day).date(), float(r.bought_qty)])
    out: dict[str, list[tuple[dt.date, float]]] = {}
    for book, ticker, day, q in sells:
        x = next((x for x in left.get((book, ticker), []) if x[1] <= pd.Timestamp(day).date() and x[2] > 1e-6), None)
        if x:
            out.setdefault(x[0], []).append((pd.Timestamp(day).date(), float(q)))
            x[2] -= float(q)
    return out


def held(bought: float, sold: list[tuple[dt.date, float]] | None, day: dt.date) -> float:
    """Shares a lot held at the close before `day`: bought minus what it sold on earlier days (a sale on `day` itself
    is at or after its open, so the lot still held them)."""
    return max(0.0, float(bought) - sum(q for d, q in (sold or []) if d < day))


def refresh(con, fetch=None, today: dt.date | None = None) -> tuple[int, str | None]:
    """Fetch the lots' tickers' corporate actions into agent_corp_actions. Never raises: (rows written, error or None);
    on an error the cache keeps what it had."""
    try:
        lots = paper_lots(con)
        if lots.empty:
            return 0, None
        today = today or dt.date.today()
        start = pd.to_datetime(lots["entry_day"]).min().date() - dt.timedelta(days=LOOKBACK_DAYS)
        acts = (fetch or fetch_alpaca)(sorted(set(lots["ticker"])), start, today + dt.timedelta(days=FORWARD_DAYS))
        return upsert_actions(con, acts), None
    except (Exception, SystemExit) as e:            # noqa: BLE001 — keys_from_env exits when the key is missing
        return 0, f"{type(e).__name__}: {str(e)[:200]}"


def load_actions(con, tickers: list[str]) -> pd.DataFrame:
    cols = ["id", "ca_type", "symbol", "ex_date", "record_date", "payable_date", "rate", "special"]
    if not tickers:
        return pd.DataFrame(columns=cols)
    df = con.execute(f"""SELECT {', '.join(cols)} FROM agent_corp_actions WHERE symbol IN ({','.join('?' * len(tickers))})""",
                     list(tickers)).df()
    for c in ("ex_date", "record_date", "payable_date"):
        df[c] = [None if pd.isna(x) else pd.Timestamp(x).date() for x in df[c]]
    return df


def action_events(actions: pd.DataFrame, last_session: dt.date) -> pd.DataFrame:
    """Cached actions with an ex-date on or before the last session, as events for entitled(): cash dividends carry
    their rate as per_share and kind 'dividend', every other type $0 and kind 'split_or_special'."""
    cols = ["ticker", "ex_date", "per_share", "kind", "pay_date", "record_date", "special", "source", "ca_id", "ca_type"]
    a = actions[[x is not None and x <= last_session for x in actions["ex_date"]]] if len(actions) else actions
    if not len(a):
        return pd.DataFrame(columns=cols)
    cash = (a["ca_type"] == CASH).to_numpy()
    return pd.DataFrame({"ticker": a["symbol"].to_numpy(), "ex_date": a["ex_date"].to_numpy(),
                         "per_share": np.where(cash, pd.to_numeric(a["rate"]).fillna(0.0).to_numpy(), 0.0),
                         "kind": np.where(cash, "dividend", "split_or_special"), "pay_date": a["payable_date"].to_numpy(),
                         "record_date": a["record_date"].to_numpy(), "special": a["special"].fillna(False).astype(bool).to_numpy(),
                         "source": SOURCE, "ca_id": a["id"].to_numpy(), "ca_type": a["ca_type"].to_numpy()})


# ---------------------------------------------------------------- factor cross-check --
def _tick(px: np.ndarray) -> np.ndarray:
    """The increment the vendor rounded a price to: $0.01 with at most two decimals, $0.001 with three, else $0.0001."""
    p = np.asarray(px, dtype=float)
    return np.where(np.abs(p - p.round(2)) < 1e-9, 0.01, np.where(np.abs(p - p.round(3)) < 1e-9, 0.001, 0.0001))


def factor_events(px: pd.DataFrame) -> pd.DataFrame:
    """Ex-date steps of adj_close / close per ticker (the cross-check; the corporate actions are the source).

    px: ticker, trade_date, close, adj_close, source, fetched_at (one row per ticker and day).
    Returns ticker, ex_date, per_share, kind ('dividend' | 'split_or_special' | 'basis_break'): a step between rows
    not fetched together (another source, or more than an hour apart) is a basis break; a factor ratio beyond
    1 / SPLIT_MIN .. 1 a split or special; a step inside FACTOR_TOL plus half a price tick on each row is rounding.
    """
    cols = ["ticker", "ex_date", "per_share", "kind"]
    if px.empty:
        return pd.DataFrame(columns=cols)
    df = px[(px["close"] > 0) & (px["adj_close"] > 0)].sort_values(["ticker", "trade_date"]).copy()
    df["f"] = df["adj_close"] / df["close"]
    g = df.groupby("ticker")
    df["prev_f"], df["prev_close"], df["prev_adj"] = g["f"].shift(), g["close"].shift(), g["adj_close"].shift()
    df["prev_source"], df["prev_fetched"] = g["source"].shift(), g["fetched_at"].shift()
    df = df.dropna(subset=["prev_f"])
    ratio = df["prev_f"] / df["f"]
    tol = FACTOR_TOL + _tick(df["adj_close"]) / 2 / df["adj_close"] + _tick(df["prev_adj"]) / 2 / df["prev_adj"]
    step = df[(ratio - 1).abs() > tol].copy()
    if step.empty:
        return pd.DataFrame(columns=cols)
    r = step["prev_f"] / step["f"]
    fetched = pd.to_datetime(step["fetched_at"])
    prev_fetched = pd.to_datetime(step["prev_fetched"])
    together = (step["source"] == step["prev_source"]) & ((fetched - prev_fetched).abs() <= SAME_FETCH)
    split = (r < 1 / SPLIT_MIN) | (r > 1)            # a dividend only ever lowers the earlier factor
    step["kind"] = np.where(~together, "basis_break", np.where(split, "split_or_special", "dividend"))
    step["per_share"] = np.where(step["kind"] == "dividend", (step["prev_close"] * (1 - r)).round(4), 0.0)
    step["ex_date"] = pd.to_datetime(step["trade_date"]).dt.date
    return step[cols].reset_index(drop=True)


def unexplained_steps(steps: pd.DataFrame, actions: pd.DataFrame, days: int = CROSS_CHECK_DAYS) -> pd.DataFrame:
    """The factor steps with no cached corporate action of the same ticker within `days` weekdays of their date."""
    if not len(steps):
        return steps
    by_t: dict[str, list[dt.date]] = {}
    for t, x in (zip(actions["symbol"], actions["ex_date"]) if len(actions) else []):
        if x is not None:
            by_t.setdefault(t, []).append(x)
    keep = [not any(int(np.busday_count(min(d, x), max(d, x))) <= days for x in by_t.get(t, []))
            for t, d in zip(steps["ticker"], steps["ex_date"])]
    return steps[keep].reset_index(drop=True)


def load_prices(store: PanelStore, tickers: list[str], since: dt.date) -> pd.DataFrame:
    """close / adj_close rows of `tickers` from `since`: index_daily for the ETFs it carries, bars for the rest."""
    idx = [t for t in tickers if t in INDEX_SYMBOLS]
    rest = [t for t in tickers if t not in INDEX_SYMBOLS]
    frames = []
    if rest:
        q = f"""SELECT ticker, trade_date, close, adj_close, source, fetched_at FROM bars
                WHERE ticker IN ({','.join('?' * len(rest))}) AND trade_date >= ?"""
        frames.append(store.con.execute(q, rest + [since]).df())
    if idx:
        q = f"""SELECT symbol AS ticker, trade_date, close, adj_close, source, fetched_at FROM index_daily
                WHERE symbol IN ({','.join('?' * len(idx))}) AND trade_date >= ?"""
        frames.append(store.con.execute(q, idx + [since]).df())
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["ticker", "trade_date", "close", "adj_close", "source", "fetched_at"])


# ---------------------------------------------------------------- entitlement --------
def entitled(lots: pd.DataFrame, events: pd.DataFrame, computed_at: dt.datetime | None = None,
             sold: dict[str, list[tuple[dt.date, float]]] | None = None) -> pd.DataFrame:
    """One row per (lot, event) the lot was holder of record for: entry_day < ex_date <= exit_day (or still open).

    lots: lot_id, book, ticker, qty, entry_day, exit_day (NaT / None while open). events: ticker, ex_date, per_share,
    kind, and optionally pay_date, special, source, ca_id, record_date, ca_type (carried through). With sold
    (sold_by_lot) and lots.bought_qty, qty is the shares held at the close before the ex-date (held), else lots.qty.
    """
    extra = [c for c in ("pay_date", "special", "source", "ca_id", "record_date", "ca_type") if c in events.columns]
    cols = ["lot_id", "book", "ticker", "ex_date", "per_share", "qty", "usd", "kind", "computed_at"] + extra
    if lots.empty or events.empty:
        return pd.DataFrame(columns=cols)
    m = lots.merge(events, on="ticker")
    entry = pd.to_datetime(m["entry_day"]).dt.date
    exit_ = [None if pd.isna(x) else pd.Timestamp(x).date() for x in m["exit_day"]]
    keep = [e < x and (out is None or x <= out) for e, x, out in zip(entry, m["ex_date"], exit_)]
    m = m[keep].copy()
    if sold is not None and "bought_qty" in m.columns:
        m["qty"] = [held(b, sold.get(i), x) for i, b, x in zip(m["lot_id"], m["bought_qty"], m["ex_date"])]
    m["usd"] = (m["qty"].astype(float) * m["per_share"].astype(float)).round(2)
    m["computed_at"] = computed_at or dt.datetime.now()
    return m[cols].reset_index(drop=True)


def one_per_key(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per (lot_id, ex_date), the table's key: cash dividends of one day added up (rates, amounts, ids); a
    non-cash action on the day of a cash dividend gives way to it (it stays in to_check)."""
    if not len(rows):
        return rows.drop(columns=["ca_type"], errors="ignore")
    out = []
    for _, g in rows.groupby(["lot_id", "ex_date"], sort=True):
        cash = g[g["kind"] == "dividend"]
        g = cash if len(cash) else g.iloc[:1]
        r = g.iloc[0].to_dict()
        if len(g) > 1:
            pays = [x for x in g["pay_date"] if x is not None and not pd.isna(x)]
            r.update(per_share=float(g["per_share"].sum()), usd=round(float(g["usd"].sum()), 2),
                     special=bool(g["special"].any()), ca_id=",".join(sorted(map(str, g["ca_id"]))), pay_date=min(pays) if pays else None)
        out.append(r)
    return pd.DataFrame(out).drop(columns=["ca_type"], errors="ignore").reset_index(drop=True)


def last_nav_day(con) -> dt.date:
    """The last session the books are marked to (agent_book_nav); today on a ledger without NAV rows."""
    try:
        d = con.execute("SELECT max(as_of) FROM agent_book_nav").fetchone()[0]
    except Exception:                               # noqa: BLE001 — a ledger without the table
        d = None
    return pd.Timestamp(d).date() if d is not None else dt.date.today()


def compute(con, store: PanelStore | None, last_session: dt.date | None = None) -> tuple[pd.DataFrame, dict]:
    """(rows for agent_dividends, {'to_check': [...], 'factor_error': str | None}) from the cached corporate actions.

    to_check: the non-cash actions on held lots, and the factor steps on held lots that no cached action explains;
    neither is counted. The factor cross-check needs the panel; without it (store None, or a failure) it is skipped."""
    info: dict = {"to_check": [], "factor_error": None}
    lots = paper_lots(con)
    if lots.empty:
        return entitled(lots, pd.DataFrame()), info
    tickers = sorted(set(lots["ticker"]))
    acts = load_actions(con, tickers)
    rows = entitled(lots, action_events(acts, last_session or last_nav_day(con)), sold=sold_by_lot(con, lots))
    for r in (rows[rows["kind"] != "dividend"].itertuples() if len(rows) else []):
        info["to_check"].append({"ticker": r.ticker, "ex_date": str(r.ex_date), "kind": r.kind, "source": SOURCE,
                                 "ca_type": r.ca_type, "lot_id": r.lot_id})
    try:
        if store is not None:
            since = pd.to_datetime(lots["entry_day"]).min().date() - dt.timedelta(days=LOOKBACK_DAYS)
            steps = unexplained_steps(factor_events(load_prices(store, tickers, since)), acts)
            for r in entitled(lots, steps).itertuples():
                info["to_check"].append({"ticker": r.ticker, "ex_date": str(r.ex_date), "kind": r.kind, "source": "factor",
                                         "ca_type": None, "lot_id": r.lot_id, "per_share": float(r.per_share)})
    except Exception as e:                          # noqa: BLE001 — a cross-check only; the corporate actions are the source
        info["factor_error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return one_per_key(rows), info


def update(con, store: PanelStore | None, fetch=None) -> pd.DataFrame:
    """Refresh the corporate-actions cache, then rebuild agent_dividends from it (delete + insert in one transaction: a
    failure keeps the previous rows). Returns the rows; rows.attrs carries 'fetch_error', 'to_check', 'factor_error'."""
    ensure_tables(con)
    _, fetch_error = refresh(con, fetch)
    rows, info = compute(con, store)
    info = info or {}
    cols = [r[0] for r in con.execute("DESCRIBE agent_dividends").fetchall() if r[0] in rows.columns]
    con.execute("BEGIN TRANSACTION")               # delete + insert together: a failure keeps the previous rows
    try:
        con.execute("DELETE FROM agent_dividends")
        if len(rows):
            con.register("_div_in", rows[cols])
            con.execute(f"INSERT INTO agent_dividends ({', '.join(cols)}) SELECT {', '.join(cols)} FROM _div_in")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        if len(rows):
            try:
                con.unregister("_div_in")
            except Exception:
                pass
    rows.attrs.update(fetch_error=fetch_error, to_check=info.get("to_check") or [], factor_error=info.get("factor_error"))
    return rows


def cumulative(rows: pd.DataFrame, days: list[dt.date], book: str) -> dict[dt.date, float]:
    """Dividends of `book` with ex_date <= day, for each day (only kind 'dividend' carries money)."""
    r = rows[(rows["book"] == book) & (rows["kind"] == "dividend")] if len(rows) else rows
    ex = [pd.Timestamp(x).date() for x in r["ex_date"]] if len(r) else []
    usd = list(r["usd"]) if len(r) else []
    return {d: float(sum(u for x, u in zip(ex, usd) if x <= d)) for d in days}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=ledger.OPTRADAR_DB, help="the ledger (a copy, for a test run)")
    args = ap.parse_args(argv)
    con = ledger.connect(args.db)
    try:
        with PanelStore(read_only=True) as store:
            rows = update(con, store)
    finally:
        con.close()
    paid = rows[rows["kind"] == "dividend"] if len(rows) else rows
    for b, g in (paid.groupby("book") if len(paid) else []):
        print(f"{b:8s} dividends ${g['usd'].sum():,.2f} over {len(g)} lot ex-dates: "
              + ", ".join(f"{t} {x} {p:.4f}" for t, x, p in zip(g["ticker"], g["ex_date"], g["per_share"])))
    chk = rows.attrs.get("to_check") or []
    if chk:
        print("to check (not counted):", ", ".join(f"{c['ticker']} {c['ex_date']} {c['kind']} ({c['source']})" for c in chk))
    if not len(rows):
        print("dividends: no lot was holder of record on an ex-date")
    for k in ("fetch_error", "factor_error"):
        if rows.attrs.get(k):
            print(f"{k.replace('_', ' ')}: {rows.attrs[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
