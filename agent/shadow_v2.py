"""The v2 bundle's shadow books (S44): six lines replayed on the recorded daily lists.

Definitions, start date and the adoption rule are fixed in agent/books/long_v2_bundle.py.
Nothing here trades. Every run rebuilds the whole record from the lists in agent_picks
(v1's `composite_long` top 60 and the two cluster lists), so the record is stateless: there
is no shadow ledger that could drift from the rules.

  record   writes the cluster lists for a bar (called by agent.execute next to the v1 list;
           `python -m agent.shadow_v2` also records the latest bar if it is missing)
  replay   engine.simulate on the recorded lists from 2026-09-25, $60,000, fills at the open,
           zero cost (the auction basis the evaluation uses), idle cash stays cash
Output: out/agent/shadow_v2.json (NAV per line, difference to the control, holdings, what
each rule blocked).

Usage: python -m agent.shadow_v2
"""
from __future__ import annotations

import datetime as dt
import json
import os

import duckdb
import numpy as np
import pandas as pd

from agent import ledger
from agent.books import long_v2_bundle as v2
from agent.books.data import Market, fundamentals, load_market
from agent.books.industry import industry_by_ticker
from agent.books.live import SIGNAL
from agent.s44_v2_bundle import run_lines
from hedge_fund.features.panel import PanelStore
from hedge_fund.validation.stats import newey_west_t

DB = "/Users/louis/optradar/optradar.db"
OUT = "/Users/louis/optradar/out/agent/shadow_v2.json"
START = "2026-09-29"          # restarted with the S47 data correction (was 2026-09-25); first fills at the 2026-09-30 open
CAPITAL = 60_000.0
NAMES = {"v1c": "v1 对照", "floor2": "+ $2 下限", "jump5": "+ 5 日大动不进", "cap20": "+ 行业上限 20%", "clusters": "两簇", "bundle": "v2 规则包"}


def record(con, store: PanelStore, market: Market, day: pd.Timestamp) -> int:
    """The two cluster lists for `day` into agent_picks (status shadow). Idempotent."""
    done = con.execute("SELECT count(*) FROM agent_picks WHERE signal_name IN (?, ?) AND as_of = ?", [v2.SIGNAL_M, v2.SIGNAL_V, day.date()]).fetchone()[0]
    if done:
        return 0
    fs = v2.scored(market, fundamentals(store), day)
    rows = []
    for c, sig in (("M", v2.SIGNAL_M), ("V", v2.SIGNAL_V)):
        for rank, t in enumerate(v2.cluster_lists(fs)[c], 1):
            r = fs.loc[t]
            rows.append({"as_of": day.date(), "ticker": t, "signal_name": sig, "side": "L", "rank": rank, "value": float(r["composite"]),
                         "instrument": "stock", "limit_ref": float(market.close.at[day, t]), "spread_pct": market.spread_pct(t, day),
                         "gate_passed": rank <= v2.HALF_N, "gate_reason": f"v{r['value']:+.2f} m{r['momentum']:+.2f}", "status": "shadow",
                         "ledger_id": None, "run_id": "shadow_v2", "expected_net_pct": None, "iv": None, "rv20": None, "rv60": None, "breakeven_pct": None})
    return ledger.write_picks(con, rows) if rows else 0


def recorded_lists(con) -> tuple[dict, dict]:
    df = con.execute("""SELECT as_of, signal_name, ticker, rank FROM agent_picks
                        WHERE signal_name IN (?, ?, ?) AND as_of >= ? ORDER BY as_of, signal_name, rank""",
                     [SIGNAL, v2.SIGNAL_M, v2.SIGNAL_V, START]).df()
    lists, cl = {}, {}
    for (d, sig), g in df.groupby(["as_of", "signal_name"]):
        d = pd.Timestamp(d)
        if sig == SIGNAL:
            lists[d] = g["ticker"].tolist()
        else:
            cl.setdefault(d, {"M": [], "V": []})["M" if sig == v2.SIGNAL_M else "V"] = g["ticker"].tolist()
    days = sorted(set(lists) & set(cl))                       # a day counts only if every list was recorded
    return {d: lists[d] for d in days}, {d: cl[d] for d in days}


def replay(market: Market, lists: dict, cl: dict, groups: pd.Series) -> dict:
    end = market.adj.index[-1]
    res = run_lines(market, lists, cl, groups, START, str(end.date()), CAPITAL, exec_frac=0.0, cash_in_spy=False, fixed_cost_pct=0.0)
    nav = pd.DataFrame({k: r.nav for k, r in res.items()})
    ret = nav.pct_change().dropna()
    spy = market.spy.reindex(nav.index).ffill()
    lines = {}
    for k in v2.LINES:
        diff = (ret[k] - ret["v1c"]).to_numpy() if len(ret) else np.array([])
        lines[k] = {"name": NAMES[k], "nav": [round(float(x), 2) for x in nav[k]], "since_pct": float(nav[k].iloc[-1] / CAPITAL - 1) * 100,
                    "vs_control_pct": float(nav[k].iloc[-1] / nav["v1c"].iloc[-1] - 1) * 100,
                    "diff_t_nw": None if k == "v1c" or len(diff) < 20 else float(newey_west_t(diff, lag=5)),
                    "n_trades_closed": len(res[k].trades), "exposure": float(res[k].exposure.iloc[-1])}
    # what the entry rules block on the latest list (the names v1 would enter and v2 would not)
    last = max(lists)
    top = lists[last][:30]
    px = market.close.loc[last]
    jump = v2.veto_frame(market, False, True).loc[last]
    g = groups.to_dict()
    blocked = {"floor2": [t for t in top if t in px.index and pd.notna(px[t]) and px[t] < v2.FLOOR_USD],
               "jump5": [t for t in top if t in jump.index and bool(jump[t])]}
    counts = pd.Series([g.get(t, "Other") for t in top]).value_counts()
    return {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "start": START, "capital": CAPITAL, "days": [str(d.date()) for d in nav.index],
            "spy": [round(float(x / spy.iloc[0] * 100), 3) for x in spy], "lines": lines, "n_lists": len(lists), "last_list": str(last.date()),
            "blocked_on_last_list": blocked, "top30_by_industry": {k: int(v) for k, v in counts.items()},
            "top30_share_M": float(np.mean([t in set(cl[last]["M"]) for t in top])),
            "rule": "bundle 减 v1c 累计 > 0 且日差 NW t ≥ 1,并且回测 alpha2 +0.5%/年(S44);评估点之前不下结论"}


def main() -> int:
    with PanelStore(read_only=True) as store:
        market = load_market(store, START)
        groups = industry_by_ticker(store)
        day = market.adj.index[-1]
        con = duckdb.connect(DB)
        try:
            n = record(con, store, market, day)
            lists, cl = recorded_lists(con)
        finally:
            con.close()
    if n:
        print(f"recorded {n} cluster picks for {day.date()}")
    if not lists:
        print("shadow v2: no recorded lists yet")
        return 0
    out = replay(market, lists, cl, groups)
    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"shadow v2: {len(out['days'])} days, {out['n_lists']} lists; " + ", ".join(f"{k} {v['since_pct']:+.2f}%" for k, v in out["lines"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
