"""安慰剂检验:同一个「30 日 IV × √(4/252)」代理,在非事件窗口是什么样?

对每个财报事件用 moomoo price_move(−5..+5 日)构造两个 4 日窗口:
  pre  : IV(−5) vs |close(−1)/close(−5) − 1|   —— 事件前,IV 已含事件溢价,正常实际波动
  post : IV(+1) vs |close(+5)/close(+1) − 1|   —— 事件后,IV 已 crush,实际含漂移
再和事件库的事件窗口(iv_pre(−1) vs 对齐后的 4 日反应)并列。按 filing 日簇 bootstrap。
它回答的是「事件窗口的 −1.5pp 相对非事件窗口是否异常」,不回答「事件跨式贵不贵」——
后者只能靠真实期权价(av_calibrate.py)。零 LLM。"""
import datetime as dt, json, math, os, sys, time
import numpy as np, duckdb
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from integrations.moomoo_client import MoomooDataClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "site-data", "events.db")
OUT = os.path.join(ROOT, "site-data", "earnings", "placebo.json")
K = math.sqrt(4 / 252) * 100


def windows(rows):
    by = {r.get("day_offset"): r for r in rows}
    out = {}
    for name, i0, i1 in (("pre", -5, -1), ("post", 1, 5)):
        a, b = by.get(i0), by.get(i1)
        if not a or not b or not a.get("option_iv") or not a.get("close_price") or not b.get("close_price"):
            continue
        iv = float(a["option_iv"])
        if not 5 <= iv <= 300:
            continue
        implied = iv / 100 * K
        realised = abs(float(b["close_price"]) / float(a["close_price"]) - 1) * 100
        out[name] = dict(iv=iv, implied=implied, realised=realised, edge=implied - realised,
                         day=str(by.get(0, {}).get("pub_trading_day_str") or by.get(0, {}).get("trading_day_str") or ""))
    return out


def cluster_ci(vals, groups, n_boot=4000, seed=7):
    rng = np.random.default_rng(seed)
    _, gi = np.unique(groups, return_inverse=True)
    G = gi.max() + 1
    s = np.bincount(gi, weights=vals, minlength=G); c = np.bincount(gi, minlength=G).astype(float)
    draws = rng.integers(0, G, size=(n_boot, G))
    m = s[draws].sum(1) / c[draws].sum(1)
    return np.percentile(m, [2.5, 97.5]).tolist()


def main():
    con = duckdb.connect(DB, read_only=True)
    tickers = [r[0] for r in con.execute("SELECT ticker FROM universe ORDER BY ticker").fetchall()]
    con.close()
    client = MoomooDataClient()
    recs = {"pre": [], "post": []}
    t0 = time.time(); fails = 0
    for i, t in enumerate(tickers, 1):
        try:
            pm = client._price_move(t)
        except Exception as e:
            fails += 1; continue
        for period, rows in pm.items():
            for name, w in windows(rows).items():
                recs[name].append(dict(ticker=t, period=period, **w))
        if i % 50 == 0:
            print(f"  [{i}/{len(tickers)}] pre {len(recs['pre'])} post {len(recs['post'])} fails {fails} ({time.time()-t0:.0f}s)", flush=True)
    client.close()
    res = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "tickers": len(tickers), "fails": fails}
    print(f"\n=== 安慰剂:同一代理在非事件窗口(n_tickers={len(tickers)}) ===")
    print(f"{'窗口':<6}{'n':>6}{'隐含均值':>9}{'实际均值':>9}{'edge均值':>9}{'簇CI':>18}{'edge中位':>9}{'实际>隐含':>9}{'实际/隐含':>9}")
    for name in ("pre", "post"):
        R = recs[name]
        if not R:
            continue
        e = np.array([r["edge"] for r in R]); imp = np.array([r["implied"] for r in R]); rl = np.array([r["realised"] for r in R])
        g = np.array([r["day"] or r["period"] for r in R])
        ci = cluster_ci(e, g)
        rich = float((rl > imp).mean() * 100)
        print(f"{name:<6}{len(R):>6}{imp.mean():>9.2f}{rl.mean():>9.2f}{e.mean():>+9.2f}  [{ci[0]:+.2f},{ci[1]:+.2f}]{np.median(e):>+9.2f}{rich:>8.1f}%{rl.mean()/imp.mean():>9.2f}")
        res[name] = dict(n=len(R), implied_mean=float(imp.mean()), realised_mean=float(rl.mean()), edge_mean=float(e.mean()),
                         edge_ci=ci, edge_median=float(np.median(e)), rich_pct=rich, ratio=float(rl.mean() / imp.mean()))
    try:
        es = json.load(open(os.path.join(ROOT, "site-data", "earnings", "event_stats.json")))
        ov = es["overall"]
        print(f"{'event':<6}{es['n']:>6}{'':>9}{'':>9}{ov['mean']:>+9.2f}  [{ov['ci'][0]:+.2f},{ov['ci'][1]:+.2f}]{ov['median']:>+9.2f}{ov['rich_pct']:>8.1f}%")
        res["event"] = dict(n=es["n"], edge_mean=ov["mean"], edge_ci=ov["ci"], edge_median=ov["median"], rich_pct=ov["rich_pct"])
    except Exception:
        pass
    print("\n公平定价下(对数正态)实际>隐含(1σ)应 ≈32%,实际/隐含 ≈0.80。"
          "pre 窗口 IV 含事件溢价 → edge 偏正;post 窗口含漂移 → edge 偏负;两者夹住「正常日」。")
    with open(OUT, "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
