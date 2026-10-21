"""S54: the insider all-event shadow — every event the v1 rule takes, its return at four exits, and a truncated SPRT
on the first day (docs/AGENT_PLAN.md "### S54 预注册"; every registered number is in agent/config.yaml `s54`).

A record only: no order, no book, no page headline. evaluate_at stays the primary evaluation of the paper books.

Events
  (ticker, D): the v1 rule names the ticker on the evening of session D, using only the Form 4 rows that evening's
  runs could see. The rule is the live evening's own code (signals_insider.candidates: one filing day qualifies by
  itself, the $50M cap, the price band, ADV $3M-$100M and the listing mask on the filing day, the 2-session window),
  and class shares and pseudo tickers stay out as in execute.entry_candidates. Slots, cash, the larger-first order,
  the retry and the other books' holdings are execution, not the signal, and are ignored. One event per ticker per
  10 sessions: the first. Filings dated before first_event_day are not read.
Visibility: what a run could see, from insider_tx.stored_at (UTC, written at insert, the first one kept)
  The 16:10 and 19:15 PT jobs fetch with --realtime and plan right after the fetch, from the last completed
  session's bar, for the next session's open; orders go out until 09:25 ET. So for the open of session X:
    - a row from a realtime fetch (source 'edgar_realtime') counts if it was stored before X 09:25 ET: the run that
      stored it planned next (10/19's 19:15 run finished its fetch at 04:44 the next morning and still planned
      for that morning's open);
    - any other row (the 06:00 daily index, a late-running daily job, a manual backfill, the bulk data) counts if it
      was stored by 19:15 PT of the last weekday before X: the 19:15 late pass (agent/bin/late_insider.sh, every
      weekday since 2026-09-29) plans the insider book from every row the panel holds, whatever its source, and its
      own fetch skips accessions already known, so such a row keeps its source. Nothing plans after it before X.
  D is the session before X, and entry is the open of X = D+1. A filing first visible on a later evening is an
  event of that evening if it is still inside the window (a late entry, as the live book takes it), and none
  otherwise. Rows stored before the column existed use fetched_at (the parse time, PT). A manual realtime fetch
  is taken as if a planning run followed it.
  An evening is detected once, by the first run after X's bar exists, and never again (agent_insider_shadow_days):
  a later revision (a bulk reload that rewrites source and fetched_at, a bar revision, a late backfill) cannot add,
  move or drop an event.
Returns (%, adjusted prices: open x adj_close / close, so splits and dividends are handled)
  Entry at the open of D+1. E1 = close D+1, E5c = close D+5, E6o = open D+6 (the running rule's exit), E10 = close
  D+10. The test's benchmark is IWM over the identical window (open D+1 to the exit print); SPY and the equal-weight
  return of the same ADV band (Market.tradable on D) over that window are stored and reported, never judged. An exit
  is written once, when its session is SETTLE_SESSIONS old and the bars exist, and never rewritten; it is looked for
  until the entry is REVISIT_SESSIONS old (a run missed for days still catches up) and is missing for good after
  that. An event without the entry open never enters the SPRT; the JSON counts E1s still absent GRACE_SESSIONS
  after their day, as an early note only.
SPRT on the E1 abnormal return (stock - IWM, % per event)
  Event order is append-only: an event gets its sequence number (e1_seq) when its E1 is written, after every E1
  written before, by entry day then ticker inside one run. An E1 that arrives late (a missing bar filled in by a
  later refetch) is appended, never inserted into a path the SPRT has already walked, so the first n events, the
  sd of the first 200 and a frozen decision always name the same events.
  Looks: the boundaries are checked when an entry day's E1s are in (at the last event of each run of equal entry
  days in that order), not between events that became observable together: inside a day the order is only the
  tickers' spelling, and same-day returns move together. The truncation look is at exactly truncate_n events.
  Wald, H0 mean h0 vs H1 mean h1, at each look n: H1 when the mean >= (h0 + h1)/2 + sigma^2/(h1 - h0) ln((1 - beta)/alpha)/n,
  H0 when it is <= (h0 + h1)/2 - sigma^2/(h1 - h0) ln((1 - alpha)/beta)/n; sigma = the prior up to event 199, from
  event 200 on the sample sd of the first 200 (frozen when first computed). The first crossing decides; no crossing by
  the truncation count: H1 if the mean there is >= (h0 + h1)/2, else H0. The decision is frozen when first made
  (agent_insider_shadow_state). It is computed every day; the owner reads it monthly. The t clustered by entry date
  is reported, not judged.
Exit-curve readings (a)(b)(c): read once, on the events the SPRT used at its decision (or those entered by the
  reading date, whichever comes first), when all their exits are in or can no longer come (the entry is older than
  REVISIT_SESSIONS: the same rule that stops the exits being looked for). Until then the JSON and the printed line
  carry counts only: nothing that compares E1, E5c, E6o or E10.

Output: optradar.db agent_insider_shadow, agent_insider_shadow_days, agent_insider_shadow_state;
out/agent/insider_shadow.json; one printed line. Usage: python -m agent.insider_shadow [--db PATH] [--out PATH]
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
from agent.books.short_term import ADV_CEILING, ADV_FLOOR
from agent.evaluate import clustered_mean
from hedge_fund.features.panel import PanelStore

OUT = "/Users/louis/optradar/out/agent/insider_shadow.json"
CONFIG = os.path.join(os.path.dirname(__file__), "config.yaml")
ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
ORDER_CUTOFF_ET = dt.time(9, 25)       # execute.py: orders are sent between 16:05 and 09:25 ET
LATE_RUN_PT = dt.time(19, 15)          # the last planning run before the next open (com.louis.agent.late_insider)
REALTIME = "edgar_realtime"            # agent.sources.sec_daily_form4 --realtime, the fetch the planning jobs run
SETTLE_SESSIONS = 1                    # an exit is written from the run after its session (the 16:10 bars refetch is in)
GRACE_SESSIONS = 3                     # an E1 still absent this long after its day is counted in the JSON (a note only)
REVISIT_SESSIONS = 30                  # exits are looked for while the entry is this recent (E10 + 20 sessions of outages);
                                       # after that a late bar cannot add one, so an old event never moves the SPRT's path
EXITS = ("E1", "E5c", "E6o", "E10")
EXIT_COLS = ("day", "ret_pct", "iwm_pct", "spy_pct", "ew_pct", "at")
SEQ = "e1_seq"                         # the SPRT's append-only event order (module doc)

DDL = [
    """CREATE TABLE IF NOT EXISTS agent_insider_shadow (
        ticker VARCHAR, event_day DATE, entry_day DATE, filing_day DATE, lag_sessions INT, kind VARCHAR,
        n_buyers INT, buy_usd DOUBLE, adv20 DOUBLE, detected_at TIMESTAMP,
        """ + ",\n        ".join(f"{e.lower()}_day DATE, {e.lower()}_ret_pct DOUBLE, {e.lower()}_iwm_pct DOUBLE, "
                                 f"{e.lower()}_spy_pct DOUBLE, {e.lower()}_ew_pct DOUBLE, {e.lower()}_at TIMESTAMP"
                                 for e in EXITS) + """, e1_seq INTEGER,
        PRIMARY KEY (ticker, event_day))""",
    """CREATE TABLE IF NOT EXISTS agent_insider_shadow_days (
        event_day DATE PRIMARY KEY, entry_day DATE, processed_at TIMESTAMP,
        n_candidates INT, n_new INT, n_repeat INT)""",
    """CREATE TABLE IF NOT EXISTS agent_insider_shadow_state (key VARCHAR PRIMARY KEY, value VARCHAR, set_at TIMESTAMP)""",
]
EVENT_COLS = ["ticker", "event_day", "entry_day", "filing_day", "lag_sessions", "kind", "n_buyers", "buy_usd", "adv20",
              "detected_at"]


def sequence(exits: list[dict], have: pd.Series) -> None:
    """Give this run's new E1s the next sequence numbers (entry day, then ticker), after every one already given."""
    nxt = int(pd.to_numeric(have, errors="coerce").max()) + 1 if pd.to_numeric(have, errors="coerce").notna().any() else 1
    for x in sorted((x for x in exits if x["exit"] == "E1"), key=lambda x: (x["entry_day"], x["ticker"])):
        x["seq"], nxt = nxt, nxt + 1


def config(path: str = CONFIG) -> dict:
    """The s54 block of agent/config.yaml: the one source of the registered numbers."""
    import yaml
    return yaml.safe_load(open(path))["s54"]


def ensure_tables(con) -> None:
    for stmt in DDL:
        con.execute(stmt)


# ------------------------------------------------------------------ visibility ----
def last_weekday_before(day: dt.date) -> dt.date:
    d = day - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def cutoffs(entry_day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """(realtime rows: stored before this, other rows: stored at or before this) for the open of entry_day."""
    rt = dt.datetime.combine(entry_day, ORDER_CUTOFF_ET, tzinfo=ET)
    other = dt.datetime.combine(last_weekday_before(entry_day), LATE_RUN_PT, tzinfo=PT)
    return rt, other


def visible_filter(entry_day: dt.date, first_day: dt.date, has_stored_at: bool = True) -> tuple[str, list]:
    """SQL condition on insider_tx keeping the rows the runs before the open of entry_day could see (module doc)."""
    t = "timezone('America/Los_Angeles', fetched_at)"
    if has_stored_at:
        t = f"coalesce(stored_at, {t})"
    rt, other = cutoffs(entry_day)
    return (f"filing_date >= ? AND {t} < ? AND (source = '{REALTIME}' OR {t} <= ?)", [first_day, rt, other])


def has_stored_at(store: PanelStore) -> bool:
    return bool(store.con.execute("SELECT count(*) FROM information_schema.columns "
                                  "WHERE table_name = 'insider_tx' AND column_name = 'stored_at'").fetchone()[0])


# ------------------------------------------------------------------ events ----
def sessions(store: PanelStore) -> list[dt.date]:
    """The exchange days the panel has (SPY's), as auction_basis does: only past sessions matter here."""
    return [pd.Timestamp(r[0]).date() for r in
            store.con.execute("SELECT trade_date FROM index_daily WHERE symbol = 'SPY' ORDER BY trade_date").fetchall()]


def evening_candidates(store: PanelStore, day: dt.date, entry_day: dt.date, sess: list[dt.date], cfg: dict,
                       stored_col: bool = True) -> pd.DataFrame:
    """The v1 rule's eligible names on the evening of `day`, with only the rows visible before entry_day's open."""
    from agent.execute import entry_candidates          # class shares and pseudo tickers: not entered (S47)
    sh = cfg["insider_shadow"]
    df = signals_insider.candidates(store, pd.Timestamp(day), int(sh["window_sessions"]), sess,
                                    tx_filter=visible_filter(entry_day, _date(sh["first_event_day"]), stored_col))
    if df.empty:
        return df
    df = df[df["eligible"]]
    df = df[df["ticker"].isin(entry_candidates(list(df["ticker"])))]
    return df.sort_values("ticker").reset_index(drop=True)


def new_events(cands: pd.DataFrame, day: dt.date, entry_day: dt.date, last_event: dict[str, dt.date],
               pos: dict[dt.date, int], dedupe: int, now: dt.datetime) -> tuple[list[dict], int]:
    """Candidates of one evening that are events: a ticker's event within `dedupe` sessions of its last one is a repeat.

    last_event: {ticker: event_day of its latest event} (updated in place); pos: session -> index."""
    out, repeat = [], 0
    for r in cands.itertuples():
        prev = last_event.get(r.ticker)
        if prev is not None and pos[day] - pos[prev] < dedupe:
            repeat += 1
            continue
        last_event[r.ticker] = day
        out.append({"ticker": r.ticker, "event_day": day, "entry_day": entry_day,
                    "filing_day": pd.Timestamp(r.trigger_filing_day).date(), "lag_sessions": int(r.lag_sessions),
                    "kind": str(r.kind), "n_buyers": int(r.n_buyers), "buy_usd": float(r.buy_usd),
                    "adv20": float(r.adv20), "detected_at": now})
    return out, repeat


# ------------------------------------------------------------------ returns ----
def index_prices(store: PanelStore, symbol: str) -> tuple[pd.Series, pd.Series]:
    """(adjusted open, adjusted close) of an index_daily symbol, by day."""
    df = store.con.execute("SELECT trade_date, open, close, adj_close FROM index_daily WHERE symbol = ? ORDER BY 1",
                           [symbol]).df()
    idx = pd.to_datetime(df["trade_date"])
    k = df["adj_close"] / df["close"]
    return pd.Series((df["open"] * k).to_numpy(), index=idx), pd.Series(df["adj_close"].to_numpy(), index=idx)


def exit_day(entry_day: dt.date, sess: list[dt.date], pos: dict[dt.date, int], offset: int) -> dt.date | None:
    """The session `offset` after the event day (entry_day is offset 1); None while it is in the future."""
    i = pos[entry_day] - 1 + offset
    return sess[i] if i < len(sess) else None


def window_return(o: pd.Series | pd.DataFrame, c: pd.Series | pd.DataFrame, entry: pd.Timestamp, ex: pd.Timestamp,
                  field: str):
    """% return from the adjusted open of `entry` to the adjusted `field` ('open' / 'close') of `ex`."""
    end = (o if field == "open" else c).loc[ex]
    return (end / o.loc[entry] - 1) * 100


def exit_rows(events: pd.DataFrame, market, iwm: tuple, spy: tuple, sess: list[dt.date], cfg: dict,
              now: dt.datetime) -> list[dict]:
    """The exits of stored events that can be written now: {ticker, event_day, exit, day, ret_pct, iwm_pct, ...}.

    Written once its session is SETTLE_SESSIONS old and the stock, IWM and SPY have the bars; never rewritten."""
    pos = {d: i for i, d in enumerate(sess)}
    last = len(sess) - 1
    band = market.tradable(ADV_FLOOR, ADV_CEILING)
    ew_cache: dict = {}
    out = []
    for r in events.itertuples():
        if r.entry_day not in pos:
            continue
        x = pd.Timestamp(r.entry_day)
        for name, (offset, field) in cfg["insider_shadow"]["exits"].items():
            if pd.notna(getattr(r, f"{name.lower()}_ret_pct")):
                continue
            d = exit_day(r.entry_day, sess, pos, int(offset))
            if d is None or last - pos[d] < SETTLE_SESSIONS:
                continue
            e = pd.Timestamp(d)
            try:
                ret = window_return(market.adj_open[r.ticker], market.adj[r.ticker], x, e, field)
                rows = [window_return(iwm[0], iwm[1], x, e, field), window_return(spy[0], spy[1], x, e, field)]
            except KeyError:                                 # no column or no bar row yet
                continue
            if any(pd.isna(v) for v in [ret, *rows]):
                continue
            key = (r.event_day, name)
            if key not in ew_cache:
                day = pd.Timestamp(r.event_day)
                names = band.columns[band.loc[day]] if day in band.index else []
                w = window_return(market.adj_open[names], market.adj[names], x, e, field) if len(names) else pd.Series(dtype=float)
                ew_cache[key] = float(w.mean()) if w.notna().any() else None
            out.append({"ticker": r.ticker, "event_day": r.event_day, "entry_day": r.entry_day, "exit": name, "day": d,
                        "ret_pct": float(ret),
                        "iwm_pct": float(rows[0]), "spy_pct": float(rows[1]), "ew_pct": ew_cache[key], "at": now})
    return out


# ------------------------------------------------------------------ SPRT ----
def boundaries(n: int, sigma: float, sp: dict) -> tuple[float, float]:
    """(lower, upper) boundary of the running mean after n events, % per event."""
    h0, h1, a, b = float(sp["h0_pct"]), float(sp["h1_pct"]), float(sp["alpha"]), float(sp["beta"])
    mid, k = (h0 + h1) / 2, sigma * sigma / (h1 - h0)
    return mid - k * math.log((1 - a) / b) / n, mid + k * math.log((1 - b) / a) / n


def looks_at(entry_days: list) -> list[int]:
    """The looks for events in SPRT order: n at the last event of each run of equal entry days (module doc)."""
    return [i + 1 for i in range(len(entry_days)) if i + 1 == len(entry_days) or entry_days[i + 1] != entry_days[i]]


def sprt(x: np.ndarray, sp: dict, frozen: dict | None = None, looks: list[int] | None = None) -> dict:
    """The truncated SPRT over x (E1 abnormal, %, in event order). frozen: the state already recorded
    ({'sigma200': .., 'decision': {...}}), which wins over a recomputation. looks: the n at which the boundaries are
    checked (looks_at); None = after every event. The truncation look is at exactly truncate_n events."""
    frozen = frozen or {}
    x = np.asarray(x, dtype=float)
    n_re, trunc, prior = int(sp["sigma_reestimate_n"]), int(sp["truncate_n"]), float(sp["sigma_prior_pct"])
    mid = (float(sp["h0_pct"]) + float(sp["h1_pct"])) / 2
    s200 = frozen.get("sigma200")
    if s200 is None and len(x) >= n_re:
        s200 = float(np.std(x[:n_re], ddof=1))
    sig = lambda n: prior if n < n_re else s200
    decision = frozen.get("decision")
    if decision is None:                                     # Wald checks at every look; the first crossing decides
        csum = np.cumsum(x[:trunc])
        at = range(1, len(csum) + 1) if looks is None else \
            sorted({n for n in looks if 1 <= n < min(trunc, len(x) + 1)} | ({trunc} if len(x) >= trunc else set()))
        for n in at:
            m = csum[n - 1] / n
            lo, up = boundaries(n, sig(n), sp)
            if m >= up or m <= lo:
                decision = {"decision": "H1" if m >= up else "H0", "n": n, "mean_pct": float(m), "lower_pct": lo,
                            "upper_pct": up, "sigma_pct": sig(n), "truncated": False}
                break
        if decision is None and len(x) >= trunc:
            m = float(csum[trunc - 1] / trunc)
            lo, up = boundaries(trunc, sig(trunc), sp)
            decision = {"decision": "H1" if m >= mid else "H0", "n": trunc, "mean_pct": m, "lower_pct": lo,
                        "upper_pct": up, "sigma_pct": sig(trunc), "truncated": True}
    n = decision["n"] if decision else min(len(x), trunc)    # stopped at the decision: later events do not move it
    cur = {"n": int(n), "mean_pct": float(x[:n].mean()) if n else None, "n_after_stop": int(len(x) - n) if decision else 0}
    if n:
        cur["lower_pct"], cur["upper_pct"] = boundaries(n, sig(n), sp)
        cur["sigma_pct"] = sig(n)
    cur["sigma_source"] = "prior" if n < n_re else f"sd of the first {n_re}"
    return {"current": cur, "sigma200": s200, "decision": decision}


# ------------------------------------------------------------------ readings ----
def reading_batch(ev: pd.DataFrame, decision: dict | None, reading_date: dt.date, truncate_n: int) -> tuple[pd.DataFrame, dt.date | None]:
    """The events the exit-curve readings use and the day they are due: the SPRT's events at its decision, or the
    ones entered by reading_date, whichever comes first. ev: the SPRT's events in order (E1 present)."""
    if decision is not None:
        d = ev.iloc[decision["n"] - 1]["entry_day"]
        if d <= reading_date:
            return ev.iloc[:decision["n"]], d
    return ev[ev["entry_day"] <= reading_date].iloc[:truncate_n], reading_date


def readings(batch: pd.DataFrame, rd: dict) -> dict:
    """(a) |E5c - E6o|, (b) E5c - E1, (c) E10 - E5c: paired, on the abnormal returns, t clustered by entry day."""
    a = {e: batch[f"{e.lower()}_ret_pct"] - batch[f"{e.lower()}_iwm_pct"] for e in EXITS}
    ok = pd.concat(a, axis=1).notna().all(axis=1)
    g = list(batch.loc[ok, "entry_day"])
    out = {"n_events": int(ok.sum())}
    for key, (x, y) in {"a_E5c_minus_E6o": ("E5c", "E6o"), "b_E5c_minus_E1": ("E5c", "E1"),
                        "c_E10_minus_E5c": ("E10", "E5c")}.items():
        out[key] = clustered_mean((a[x] - a[y])[ok].to_numpy(), g)
    ma, mb, mc, tc = out["a_E5c_minus_E6o"]["mean"], out["b_E5c_minus_E1"]["mean"], out["c_E10_minus_E5c"]["mean"], out["c_E10_minus_E5c"]["t"]
    out["a_v3_exit_at_day5_close"] = ma is not None and abs(ma) <= float(rd["a_abs_max_pp"])
    out["b_v3_considers_same_day"] = mb is not None and mb <= float(rd["b_max_pp"])
    out["c_v3_considers_longer_hold"] = mc is not None and tc is not None and mc >= float(rd["c_min_pp"]) and tc >= float(rd["c_min_t"])
    return out


# ------------------------------------------------------------------ run ----
def _date(v) -> dt.date:
    return pd.Timestamp(str(v)).date()


def load_state(con) -> tuple[pd.DataFrame, set[dt.date], dict]:
    """(stored events in event order, processed evenings, frozen state) from a ledger connection (read-only is fine)."""
    have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if "agent_insider_shadow" not in have:
        cols = EVENT_COLS + [f"{e.lower()}_{c}" for e in EXITS for c in EXIT_COLS] + [SEQ]
        return pd.DataFrame(columns=cols), set(), {}
    ev = con.execute("SELECT * FROM agent_insider_shadow ORDER BY entry_day, ticker").df()
    for c in [c for c in ev.columns if c.endswith("_day")]:
        ev[c] = pd.to_datetime(ev[c]).dt.date
    days = {pd.Timestamp(r[0]).date() for r in con.execute("SELECT event_day FROM agent_insider_shadow_days").fetchall()}
    state = {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM agent_insider_shadow_state").fetchall()}
    return ev, days, state


def detect(store: PanelStore, sess: list[dt.date], done: set[dt.date], events: pd.DataFrame, cfg: dict,
           now: dt.datetime) -> tuple[list[dict], list[dict]]:
    """Events of every evening not processed yet whose entry session has a bar: (new events, processed-day rows)."""
    sh = cfg["insider_shadow"]
    first = _date(sh["first_event_day"])
    pos = {d: i for i, d in enumerate(sess)}
    last_event: dict[str, dt.date] = {}
    for r in events.sort_values("event_day").itertuples():
        last_event[r.ticker] = r.event_day
    stored_col = has_stored_at(store)
    new, days = [], []
    for d, x in zip(sess, sess[1:]):
        if d < first or d in done:
            continue
        cands = evening_candidates(store, d, x, sess, cfg, stored_col)
        rows, repeat = new_events(cands, d, x, last_event, pos, int(sh["dedupe_sessions"]), now)
        new += rows
        days.append({"event_day": d, "entry_day": x, "processed_at": now, "n_candidates": int(len(cands)),
                     "n_new": len(rows), "n_repeat": repeat})
    return new, days


def write(con, new: list[dict], days: list[dict], exits: list[dict], state: dict) -> None:
    """One transaction: new events and days (never replaced), exits only where still empty, state keys only once."""
    ensure_tables(con)
    con.execute("BEGIN TRANSACTION")
    try:
        for e in new:
            con.execute(f"INSERT INTO agent_insider_shadow ({', '.join(EVENT_COLS)}) VALUES ({', '.join('?' * len(EVENT_COLS))}) "
                        "ON CONFLICT DO NOTHING", [e[c] for c in EVENT_COLS])
        for d in days:
            con.execute("INSERT INTO agent_insider_shadow_days VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        [d["event_day"], d["entry_day"], d["processed_at"], d["n_candidates"], d["n_new"], d["n_repeat"]])
        for x in exits:
            p = x["exit"].lower()
            seq = ", e1_seq = ?" if p == "e1" else ""
            con.execute(f"""UPDATE agent_insider_shadow SET {p}_day = ?, {p}_ret_pct = ?, {p}_iwm_pct = ?, {p}_spy_pct = ?,
                            {p}_ew_pct = ?, {p}_at = ?{seq} WHERE ticker = ? AND event_day = ? AND {p}_ret_pct IS NULL""",
                        [x["day"], x["ret_pct"], x["iwm_pct"], x["spy_pct"], x["ew_pct"], x["at"],
                         *([x["seq"]] if seq else []), x["ticker"], x["event_day"]])
        for k, v in state.items():
            con.execute("INSERT INTO agent_insider_shadow_state VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                        [k, json.dumps(v, default=str), dt.datetime.now()])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def summarize(ev: pd.DataFrame, sess: list[dt.date], state: dict, cfg: dict, today: dt.date) -> dict:
    """The JSON: counts, the SPRT, the reported benchmarks of E1, and the readings only once they are due and complete."""
    sh, sp = cfg["insider_shadow"], cfg["sprt"]
    pos = {d: i for i, d in enumerate(sess)}
    last = len(sess) - 1
    def gone(day) -> bool:                                   # a day this many sessions back without its bar: missing
        return day in pos and last - pos[day] >= GRACE_SESSIONS
    counts = {e: int(ev[f"{e.lower()}_ret_pct"].notna().sum()) for e in EXITS} if len(ev) else {e: 0 for e in EXITS}
    ev = ev.copy()
    if SEQ not in ev:
        ev[SEQ] = np.nan
    e1 = ev[ev["e1_ret_pct"].notna() & ev["e1_iwm_pct"].notna()]
    e1 = e1.assign(_seq=pd.to_numeric(e1[SEQ], errors="coerce")).sort_values(["_seq", "entry_day", "ticker"], na_position="last")
    e1 = e1.drop(columns="_seq").reset_index(drop=True)
    no_e1 = ev[ev["e1_ret_pct"].isna() & ev["entry_day"].map(gone)] if len(ev) else ev
    x = (e1["e1_ret_pct"] - e1["e1_iwm_pct"]).to_numpy(dtype=float)
    res = sprt(x, sp, state, looks_at(list(e1["entry_day"])))
    dec = res["decision"]
    if dec is not None and "entry_day" not in dec and dec["n"] <= len(e1):   # a frozen decision keeps its own day
        lastev = e1.iloc[dec["n"] - 1]                       # and names its last event: the order is append-only
        dec = {**dec, "entry_day": str(lastev["entry_day"]), "last_event": [lastev["ticker"], str(lastev["event_day"])]}
    clus = clustered_mean(x, list(e1["entry_day"])) if len(x) else clustered_mean(x, [])
    rep = {}
    for k in ("spy", "ew"):
        y = e1["e1_ret_pct"] - e1[f"e1_{k}_pct"]
        rep[f"E1_minus_{'SPY' if k == 'spy' else 'band_ew'}"] = clustered_mean(y.dropna().to_numpy(), list(e1.loc[y.notna(), "entry_day"]))
    out = {"n_events": int(len(ev)), "n_with_E1": int(len(e1)), "n_without_entry_or_E1": int(len(no_e1)),
           "exit_counts": counts,
           "sprt": {**res["current"], "decision": dec["decision"] if dec else "none",
                    "boundary_crossed": bool(dec and not dec["truncated"]), "decided": dec,
                    "sigma200_pct": res["sigma200"], "clustered_by_date": clus,
                    "params": {k: sp[k] for k in sp}},
           "reported_not_judged": rep,
           "recent_events": [{"ticker": r.ticker, "event_day": str(r.event_day), "entry_day": str(r.entry_day),
                              "filing_day": str(r.filing_day), "lag_sessions": int(r.lag_sessions), "kind": r.kind,
                              "buy_usd": round(float(r.buy_usd)),
                              "E1_abn_pct": None if pd.isna(r.e1_ret_pct) or pd.isna(r.e1_iwm_pct) else round(float(r.e1_ret_pct - r.e1_iwm_pct), 3)}
                             for r in ev.sort_values(["event_day", "ticker"]).tail(30).itertuples()]}
    # readings: computed and shown only from the due day, once every exit of the batch is in or missing
    rdate = _date(sh["reading_date"])
    batch, due = reading_batch(e1, dec if dec else None, rdate, int(sp["truncate_n"]))
    rr = {"due": str(due) if due else None}
    if state.get("readings"):
        rr.update(status="read", **state["readings"])
    elif today >= due:                                        # an exit is missing for good once it is no longer looked for
        revisited = set(sess[-REVISIT_SESSIONS:])
        complete = [all(pd.notna(r[f"{e.lower()}_ret_pct"]) for e in EXITS) or r["entry_day"] not in revisited
                    for _, r in batch.iterrows()]
        if all(complete):
            rr.update(status="read", **readings(batch, cfg["exit_readings"]), read_on=str(today))
        else:
            rr.update(status="due, waiting for exits", n_batch=int(len(batch)), n_complete=int(sum(complete)))
    else:
        rr.update(status=f"hidden until {due}", counts=counts)
    out["exit_readings"] = rr
    out["_state"] = {"sigma200": res["sigma200"], "decision": dec, "readings": {k: v for k, v in rr.items() if k != "status"}
                     if rr.get("status") == "read" and not state.get("readings") else None}
    return out


def line(rep: dict) -> str:
    s = rep["sprt"]
    cur = (f"mean {s['mean_pct']:+.3f}% at n {s['n']}, boundaries {s['lower_pct']:+.3f}% / {s['upper_pct']:+.3f}%"
           if s.get("n") else "no E1 yet")
    dec = s["decision"] if s["decision"] == "none" else f"{s['decision']} at n {s['decided']['n']} ({s['decided'].get('entry_day')})"
    return (f"insider shadow: {rep['n_events']} events ({rep['n_with_E1']} with E1) · SPRT {dec}: {cur} · "
            f"exit readings {rep['exit_readings']['status']}")


def run(db: str, out: str, today: dt.date | None = None, cfg: dict | None = None,
        panel: str | None = None) -> dict:
    cfg = cfg or config()
    today = today or dt.date.today()
    now = dt.datetime.now()
    con = ledger.connect(db, read_only=True)
    try:
        events, done, state = load_state(con)
    finally:
        con.close()                                           # no ledger lock held while the panel is read
    from agent.books.data import load_market
    with (PanelStore(panel, read_only=True) if panel else PanelStore(read_only=True)) as store:
        sess = sessions(store)
        new, days = detect(store, sess, done, events, cfg, now)
        allev = (pd.concat([events, pd.DataFrame(new)], ignore_index=True) if len(events) else
                 pd.DataFrame(new).reindex(columns=events.columns)) if new else events
        exits = []
        recent = set(sess[-REVISIT_SESSIONS:])
        open_ = allev[allev[[f"{e.lower()}_ret_pct" for e in EXITS]].isna().any(axis=1)
                      & allev["entry_day"].isin(recent)] if len(allev) else allev
        if len(open_):                                       # only events that can still get an exit: the market is loaded
            start = (min(open_["event_day"]) - dt.timedelta(days=10)).isoformat()   # from the oldest of them
            market = load_market(store, start, lookback_days=60)
            bench = cfg["insider_shadow"]["benchmark"]          # IWM (the iwm_* columns); SPY is reported beside it
            exits = exit_rows(open_, market, index_prices(store, bench), index_prices(store, "SPY"), sess, cfg, now)
    if SEQ not in allev:
        allev[SEQ] = np.nan
    sequence(exits, allev[SEQ])                               # append-only: after every E1 already written
    for c in [f"{e.lower()}_{k}" for e in EXITS for k in ("day", "at")]:
        allev[c] = allev[c].astype(object)                    # dates and stamps, empty columns read back as float
    for x in exits:                                           # the summary reads what is about to be written
        m = (allev["ticker"] == x["ticker"]) & (allev["event_day"] == x["event_day"])
        for c in EXIT_COLS + (("seq",) if "seq" in x else ()):
            allev.loc[m, SEQ if c == "seq" else f"{x['exit'].lower()}_{c}"] = x[c]
    rep = summarize(allev, sess, state, cfg, today)
    freeze = {k: v for k, v in rep.pop("_state").items() if v is not None and k not in state}
    con = ledger.connect(db)
    try:
        write(con, new, days, exits, freeze)
    finally:
        con.close()
    rep = {"generated_at": now.isoformat(timespec="seconds"), "registered": str(cfg["registered"]),
           "first_event_day": str(cfg["insider_shadow"]["first_event_day"]),
           "evenings_processed": len(done) + len(days), "last_evening": str(max([*done, *[d["event_day"] for d in days]]))
           if (done or days) else None, "new_today": len(new), **rep, "error": None}
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1, default=str)
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=ledger.OPTRADAR_DB)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--panel", default=None, help="a panel copy (read-only)")
    args = ap.parse_args(argv)
    try:
        rep = run(args.db, args.out, panel=args.panel)
    except Exception as exc:                                   # keep the last good report, say the run failed
        try:
            prev = json.load(open(args.out))
        except Exception:
            prev = {}
        prev.update(error=f"{type(exc).__name__}: {str(exc)[:300]}", error_at=dt.datetime.now().isoformat(timespec="seconds"))
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(prev, f, ensure_ascii=False, indent=1, default=str)
        print(f"insider shadow failed: {prev['error']}")          # not 'FAILED': a record, not a book-feeding step (health)
        return 1
    print(line(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
