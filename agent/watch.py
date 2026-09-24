"""Watchlist alerts: names the user follows by hand (agent/watchlist.yaml), checked after the close.

Per name: new SEC filings (submissions API), open-market insider buys/sells and 13D filings
from the panel, headlines tagged with the symbol (news.db, last 24h), a close-to-close move
>= 5%, and price levels crossed. Writes out/agent/watch_<date>.json, prints one line per
alert, and sends a Mac notification when anything fired. Read by brief.py (⑨) and the
private site's front page. State (last accession seen per name) in ~/.hedge-fund/agent/
watch_state.json so a filing is announced once.

Usage: python -m agent.watch [--no-notify]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time
import urllib.request

import duckdb
import pandas as pd
import yaml

from agent.sources.alpaca_news import NEWS_DB
from agent.sources.sec_form4 import user_agent
from hedge_fund.features.panel import PanelStore
from hedge_fund.paths import AGENT_DIR

WATCHLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.yaml")
STATE = AGENT_DIR / "watch_state.json"
OUT_DIR = "/Users/louis/optradar/out/agent"
MOVE_PCT = 5.0


def load_watchlist() -> dict:
    with open(WATCHLIST) as f:
        return yaml.safe_load(f)["watch"]


def sec_filings(cik: str, ua: str, since_acc: str | None, forms: list[str], days: int = 5) -> tuple[list[dict], str | None]:
    req = urllib.request.Request(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except Exception:
        return [], since_acc
    rec = d["filings"]["recent"]
    out, newest = [], rec["accessionNumber"][0] if rec["accessionNumber"] else since_acc
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    for form, date, acc, doc in zip(rec["form"], rec["filingDate"], rec["accessionNumber"], rec["primaryDocument"]):
        if acc == since_acc:
            break
        if date < cutoff:
            break
        if form in forms:
            out.append({"form": form, "date": date, "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"})
    return out, newest


def check(name: str, cfg: dict, store: PanelStore, state: dict, ua: str) -> dict:
    q = store.con.execute
    alerts: list[str] = []
    yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    # price
    px = q("SELECT trade_date, close FROM bars WHERE ticker = ? ORDER BY trade_date DESC LIMIT 2", [name]).fetchall()
    last = prev = None
    if len(px) == 2:
        (d1, last), (_, prev) = px
        ret = (last / prev - 1) * 100
        if abs(ret) >= MOVE_PCT:
            alerts.append(f"价格 {ret:+.1f}% → {last:.2f}")
        for lvl in cfg.get("below", []):
            if prev >= lvl > last:
                alerts.append(f"跌破 {lvl} → {last:.2f}")
        for lvl in cfg.get("above", []):
            if prev <= lvl < last:
                alerts.append(f"突破 {lvl} → {last:.2f}")
    # filings
    st = state.setdefault(name, {})
    filings, newest = sec_filings(cfg["cik"], ua, st.get("last_acc"), cfg.get("forms", []))
    st["last_acc"] = newest
    today = dt.date.today().isoformat()
    if st.get("today") != today:                         # filings announced today stay on today's page across reruns
        st["today"], st["today_filings"] = today, []
    known = {f["url"] for f in st["today_filings"]}
    st["today_filings"] += [f for f in filings if f["url"] not in known]
    filings = st["today_filings"]
    for f in filings:
        alerts.append(f"申报 {f['form']} {f['date']} {f['url']}")
    # insiders / 13D
    for r in q("""SELECT filing_date, trans_code, owner_name, officer_title, shares, price, value_usd FROM insider_tx
                  WHERE ticker = ? AND filing_date >= ? AND trans_code IN ('P','S') ORDER BY filing_date DESC LIMIT 6""", [name, yday]).fetchall():
        alerts.append(f"内部人{'买入' if r[1] == 'P' else '卖出'} {r[0]} {r[2]}({r[3] or '—'}) {r[4]:,.0f}股 @ {r[5]:.2f} = ${r[6]:,.0f}")
    for r in q("SELECT filed, form, filers FROM sch13d WHERE ticker = ? AND filed >= ?", [name, yday]).fetchall():
        alerts.append(f"13D {r[0]} {r[1]} {r[2]}")
    # news
    headlines = []
    try:
        n = duckdb.connect(str(NEWS_DB), read_only=True)
        headlines = [{"en": h, "url": u, "at": str(t)[:16]} for h, u, t in n.execute("""SELECT i.headline, i.url, i.created_at FROM news_symbols s JOIN news_items i USING (id)
                                                WHERE s.symbol = ? AND i.n_symbols <= 3 AND i.created_at >= now() - INTERVAL 1 DAY
                                                ORDER BY i.created_at DESC LIMIT 5""", [name]).fetchall()]
        n.close()
    except Exception:
        pass
    return {"ticker": name, "note": cfg.get("note", ""), "last": last, "prev": prev, "alerts": alerts, "headlines": headlines}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()
    wl = load_watchlist()
    state = json.load(open(STATE)) if STATE.exists() else {}
    ua = user_agent()
    with PanelStore(read_only=True) as store:
        results = [check(t, cfg, store, state, ua) for t, cfg in wl.items()]
    try:                                                  # retail attention (ApeWisdom + Stocktwits), stored daily in retail.db
        from agent.sources.retail_heat import snapshot
        heat = snapshot(list(wl))
        for r in results:
            h = heat.get(r["ticker"], {})
            a, st = h.get("apewisdom"), h.get("stocktwits")
            r["retail"] = (f"Reddit 提及 {a['mentions']}/24h(排名 {a['rank']},前日 {a['mentions_24h_ago']})" if a else "Reddit 榜外") + \
                          (f" · Stocktwits {st['watchers']:,} 关注,近 30 条 {st['bullish']} 多 / {st['bearish']} 空" if st else "")
            if a and a["mentions_24h_ago"] and a["mentions"] >= 3 * a["mentions_24h_ago"] and a["mentions"] >= 30:
                r["alerts"].append(f"散户热度 {a['mentions_24h_ago']} → {a['mentions']} 提及/24h")
    except Exception as exc:
        print(f"retail heat skipped: {exc}")
    try:                                                  # display only: analyst consensus + unusual options (moomoo, read-only)
        import moomoo as mm
        q = mm.OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            for r in results:
                code = f"US.{r['ticker']}"
                c = q.get_research_analyst_consensus(code)
                if c[0] == 0 and isinstance(c[1], dict) and c[1].get("total"):
                    d = c[1]
                    up = (d["average"] / r["last"] - 1) * 100 if r.get("last") else None
                    r["consensus"] = (f"{d['total']} 家分析师,平均目标 {d['average']:.2f}" + (f"({up:+.0f}%)" if up is not None else "")
                                      + f",区间 {d['lowest']:.2f}–{d['highest']:.2f};买入 {d['buy']:.0f}% / 持有 {d['hold']:.0f}% / 卖出 {d['sell']:.0f}%")
                u = q.get_derivative_unusual(code)
                if u[0] == 0 and isinstance(u[1], dict) and u[1].get("content"):
                    lines = [l.strip() for l in u[1]["content"].splitlines() if l.strip() and "：" not in l[:8] and not l.strip().startswith("[")]
                    r["options_unusual"] = lines[:3]
                time.sleep(0.6)
        finally:
            q.close()
    except Exception as exc:
        print(f"consensus / unusual options skipped: {exc}")
    try:                                                  # display-only Chinese headlines (agent/translate.py, cached)
        from agent.translate import translate
        zh = translate([h["en"] for r in results for h in r["headlines"]])
        for r in results:
            for h in r["headlines"]:
                h["zh"] = zh.get(h["en"], h["en"])
    except Exception as exc:
        print(f"translation skipped: {exc}")
    os.makedirs(AGENT_DIR, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f)
    os.makedirs(OUT_DIR, exist_ok=True)
    payload = {"date": dt.date.today().isoformat(), "generated_at": dt.datetime.now().isoformat(timespec="seconds"), "names": results}
    with open(os.path.join(OUT_DIR, f"watch_{dt.date.today()}.json"), "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=float)
    with open(os.path.join(OUT_DIR, "watch_latest.json"), "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=float)
    fired = [(r["ticker"], a) for r in results for a in r["alerts"]]
    for r in results:
        print(f"{r['ticker']:6s} {r['last']} | {len(r['alerts'])} alerts | {len(r['headlines'])} headlines")
        for a in r["alerts"]:
            print("   -", a)
    if fired and not args.no_notify:
        import re as _re
        msg = "; ".join(f"{t}: {_re.sub(r' https?://\S+', '', a)[:60]}" for t, a in fired[:4])
        tn = "/opt/homebrew/bin/terminal-notifier"                    # absolute: launchd's PATH lacks /opt/homebrew/bin
        if os.path.exists(tn):
            subprocess.run([tn, "-title", "关注名单", "-message", msg, "-open", "https://optradar.tail5b470b.ts.net/",
                            "-group", "watch"], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
