"""S41 — the insider line inside the day: when does day-one accrue, and can an early filing be
bought the same afternoon? Pre-registered 2026-09-25, before any run.

S31 found half of the insider line's 5-session edge on day one (close/open +0.27%, t 4.7), which is
why entries are market-on-open. Two questions only intraday data can answer:

  Q1  Path of day one (D+1, the entry session): mean cumulative return from the 09:30 open to each
      30-minute mark, all entries with bars. Reading: descriptive; it tells whether an opening order
      captures the drift (front-loaded) or a later fill would do (spread through the day).
  Q2  Same-day entry for early filings: for events whose Form 4 was ACCEPTED by EDGAR before
      15:00 ET on the filing day D (acceptanceDateTime from the SEC submissions API), enter at the
      open of the first 30-minute bar that starts after acceptance on D, instead of the next
      session's open. The exit is unchanged (the open 5 sessions after D+1), so the difference is
      exactly the extra leg: D intraday entry → D+1 open. Reading (mirrors S31's bar): adopt
      same-day entry if that extra leg averages >= +0.15% with NW t >= 2, clustered by day, and
      the sign holds in both halves of the window. Late filings (after 15:00) keep the rule.

Window: 2025-09-01 → 2026-08-31 (the 30-minute bars loaded first; acceptance times fetched per
issuer from data.sec.gov). Not a book-level variant; no Holm entry. An adoption would need an
intraday execution job (today the executor runs once at 16:10 PT).

Usage: python -m agent.s41_intraday_entry
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.request

import duckdb
import numpy as np
import pandas as pd

from agent.books.data import load_market
from agent.events import LINES
from agent.sources.alpaca_intraday import INTRADAY_DB
from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import newey_west_t

OUT_DIR = "/Users/louis/hedge-fund/site-data/validation"
START, END = "2025-09-01", "2026-08-31"
CUTOFF = dt.time(15, 0)
ET = "America/New_York"


def acceptance_times(ciks: list[str], ua: str) -> dict[tuple[str, str], pd.Timestamp]:
    """(cik, accession) -> acceptance time in ET, for Form 4 filings of each issuer."""
    out = {}
    for i, cik in enumerate(ciks):
        try:
            req = urllib.request.Request(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
        except Exception:
            continue
        rec = d["filings"]["recent"]
        for f, acc, at in zip(rec["form"], rec["accessionNumber"], rec["acceptanceDateTime"]):
            if f in ("4", "4/A") and at:
                out[(cik, acc)] = pd.Timestamp(at).tz_convert(ET)
        time.sleep(0.12)
        if (i + 1) % 100 == 0:
            print(f"  acceptance times: {i + 1}/{len(ciks)} issuers", flush=True)
    return out


def main() -> int:
    t0 = time.time()
    with PanelStore(read_only=True) as store:
        market = load_market(store, "2025-08-01")
        ins = LINES["insider_buy"]
        ev = ins.events(store, START, END)
        tg = ins.targets(market, ev, START, END)
        acc = store.con.execute("""SELECT ticker, filing_date, issuer_cik, accession FROM insider_tx
                                   WHERE trans_code = 'P' AND filing_date BETWEEN ? AND ?""", [START, END]).df()
    idx = market.adj.index
    sess = [d.date() for d in idx]
    con = duckdb.connect(str(INTRADAY_DB), read_only=True)
    b = con.execute("SELECT ticker, ts, o, c FROM bars30 WHERE ts >= ? AND ts < ?", [START, "2026-09-15"]).df()
    con.close()
    b["day"] = b["ts"].dt.date
    b["hm"] = b["ts"].dt.strftime("%H:%M")
    key = b.set_index(["ticker", "day", "hm"])
    spy = key.loc["SPY"] if "SPY" in b["ticker"].values else None

    # ---- Q1: day-one path
    marks = ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00", "15:30", "close"]
    rows, entries = [], []
    for d, names in tg.items():
        p = idx.searchsorted(d)
        e = p + 1 if p < len(idx) and idx[p] == d else p
        if e >= len(idx):
            continue
        ed = idx[e].date()
        for t in names:
            entries.append((d.date(), ed, t))
            try:
                g = key.loc[(t, ed)]
            except KeyError:
                continue
            if "09:30" not in g.index:
                continue
            o = float(g.at["09:30", "o"])
            r = {"day": ed, "ticker": t}
            for hm in marks[:-1]:
                if hm in g.index:
                    r[hm] = float(g.at[hm, "o"]) / o - 1
            r["close"] = float(g["c"].iloc[-1]) / o - 1
            if spy is not None and ed in spy.index.get_level_values(0):
                s = spy.loc[ed]
                so = float(s.at["09:30", "o"])
                for hm in marks[:-1]:
                    if hm in r and hm in s.index:
                        r[hm] -= float(s.at[hm, "o"]) / so - 1
                r["close"] -= float(s["c"].iloc[-1]) / so - 1
            rows.append(r)
    path = pd.DataFrame(rows)
    q1 = {}
    if len(path):
        for hm in marks:
            if hm in path:
                x = path.groupby("day")[hm].mean().dropna()
                q1[hm] = {"mean_pct": float(x.mean() * 100), "t_nw": newey_west_t(x.to_numpy(), lag=1), "n": int(path[hm].notna().sum())}
    print(f"Q1: {len(path)} entries with bars of {len(entries)} [{time.time() - t0:.0f}s]", flush=True)

    # ---- Q2: same-day entry for early filings
    ua = user_agent()
    ciks = sorted({str(c) for c in acc["issuer_cik"].dropna().unique()})
    at = acceptance_times(ciks, ua)
    acc["accepted"] = [at.get((str(c), a)) for c, a in zip(acc["issuer_cik"], acc["accession"])]
    first = acc.dropna(subset=["accepted"]).sort_values("accepted").drop_duplicates(["ticker", "filing_date"])
    first_at = {(t, pd.Timestamp(d).date()): a for t, d, a in zip(first["ticker"], first["filing_date"], first["accepted"])}
    legs = []
    for d, ed, t in entries:
        a = first_at.get((t, d))
        if a is None or a.date() != d or a.time() >= CUTOFF or a.time() < dt.time(9, 30):
            continue
        try:
            g = key.loc[(t, d)]
            g1 = key.loc[(t, ed)]
        except KeyError:
            continue
        # first 30-min bar starting after acceptance
        later = [hm for hm in g.index if hm > a.strftime("%H:%M")]
        if not later or "09:30" not in g1.index:
            continue
        entry_px = float(g.at[later[0], "o"])
        next_open = float(g1.at["09:30", "o"])
        leg = next_open / entry_px - 1
        if spy is not None:
            try:
                s0, s1 = spy.loc[d], spy.loc[ed]
                leg -= float(s1.at["09:30", "o"]) / float(s0.at[later[0], "o"]) - 1
            except KeyError:
                pass
        legs.append({"day": d, "ticker": t, "accepted": a.strftime("%H:%M"), "entry_bar": later[0], "extra_leg_pct": leg * 100})
    lg = pd.DataFrame(legs)
    q2 = {"n_events": int(len(lg)), "n_early_filings": int(sum(1 for (t, d), a in first_at.items() if a.date() == d and dt.time(9, 30) <= a.time() < CUTOFF))}
    if len(lg) >= 30:
        x = lg.groupby("day")["extra_leg_pct"].mean()
        h = len(x) // 2
        q2.update({"n_days": int(len(x)), "mean_pct": float(x.mean()), "t_nw": newey_west_t(x.to_numpy(), lag=1),
                   "first_half": float(x.iloc[:h].mean()), "second_half": float(x.iloc[h:].mean()), "hit": float((lg["extra_leg_pct"] > 0).mean())})
        q2["adopt"] = bool(q2["mean_pct"] >= 0.15 and q2["t_nw"] >= 2 and q2["first_half"] > 0 and q2["second_half"] > 0)
    print(f"Q2: {q2} [{time.time() - t0:.0f}s]", flush=True)

    stamp = dt.date.today().isoformat()
    base = os.path.join(OUT_DIR, f"s41_intraday_entry_{stamp}")
    with open(base + ".json", "w") as f:
        json.dump({"start": START, "end": END, "q1_path": q1, "q2_same_day": q2, "n_entries": len(entries)}, f, indent=1, default=str)
    L = [f"# S41 — insider line inside the day ({stamp})", "", f"{START} → {END}, {len(entries)} entries, {len(path)} with 30-minute bars. Abnormal vs SPY.", "",
         "## Q1 · day-one path: mean cumulative abnormal return from the 09:30 open (clustered by day)", "", "| mark | mean % | NW t | n |", "|---|---|---|---|"]
    L += [f"| {hm} | {v['mean_pct']:+.2f} | {v['t_nw']:.2f} | {v['n']} |" for hm, v in q1.items()]
    L += ["", "## Q2 · same-day entry for filings accepted before 15:00 ET: extra leg (D intraday entry → D+1 open), abnormal", ""]
    if "mean_pct" in q2:
        L += [f"{q2['n_events']} events on {q2['n_days']} days (of {q2['n_early_filings']} early filings): mean {q2['mean_pct']:+.2f}%, NW t {q2['t_nw']:.2f}, "
              f"halves {q2['first_half']:+.2f} / {q2['second_half']:+.2f}, hit {q2['hit']:.0%}. Pre-registered bar: >= +0.15%, t >= 2, both halves > 0 → "
              f"**{'ADOPT' if q2['adopt'] else 'not adopted'}**"]
    else:
        L += [f"too few events ({q2['n_events']}); {q2['n_early_filings']} early filings in the window"]
    text = "\n".join(L) + "\n"
    open(base + ".md", "w").write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
