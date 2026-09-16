"""财报事件库的统计检验(零 LLM)。

替换早期的 t 检验:
  * 分组 vs 其余:标签置换检验(20,000 次),给经验 p 值;
  * 多重比较:同一批置换里取 max-|T|(Westfall–Young)得族错误率 p,另给 Holm;
  * 同日事件相关:按 filing_date 整簇重抽样(cluster bootstrap)给 95% CI;
  * 交易成本:历史价差没有,按跨式权利金的 0/5/10% 做敏感性。
结论只认「校正后 p<0.05 且簇 CI 不跨 0」的分组。

重要口径:implied = iv_pre × sqrt(4/252),iv_pre 是财报前的 30 日 ATM IV(见
moomoo_client.earnings_events),不是事件周跨式的真实价格。30 日 IV 把事件方差摊在
30 天里,所以这个 implied 系统性低估事件隐含波动 → edge 的绝对值不能当作跨式 P&L,
「买方 +1.5pp」不是买方优势;只有组间比较有意义,而且偏差本身随 IV 水平变化。
要拿到真实事件隐含波动,需要历史期权链(Alpha Vantage HISTORICAL_OPTIONS)校准。

用法: python masters/event_stats.py [--perms 20000] [--boots 4000] [--json PATH]
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, os, sys
import numpy as np
import duckdb
from scipy import stats

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "site-data", "events.db")
COST_LEVELS = (0.0, 0.05, 0.10)   # 往返价差 = 跨式权利金的比例(假设,非历史实测)


def load() -> dict[str, np.ndarray]:
    con = duckdb.connect(DB, read_only=True)
    rows = con.execute("""
        SELECT e.ticker, e.filing_date, e.filing_window, e.iv_pre, e.hv_pre, e.move_d4,
               u.market_cap
        FROM earnings_events e LEFT JOIN universe u
          ON replace(u.ticker, 'US.', '') = replace(e.ticker, 'US.', '')
        WHERE e.iv_pre IS NOT NULL AND e.move_d4 IS NOT NULL AND e.iv_pre BETWEEN 5 AND 300
    """).fetchall()
    con.close()
    d = {k: np.array(v) for k, v in zip(
        ["ticker", "date", "window", "iv", "hv", "d4", "mcap"], zip(*rows))}
    d["iv"], d["hv"], d["d4"] = d["iv"].astype(float), d["hv"].astype(float), d["d4"].astype(float)
    d["mcap"] = np.array([float(x) if x is not None else np.nan for x in d["mcap"]])
    d["implied"] = d["iv"] / 100 * math.sqrt(4 / 252) * 100     # 4 个交易日
    d["realised"] = np.abs(d["d4"])
    d["edge"] = d["implied"] - d["realised"]                      # 卖方视角, pp of spot
    d["rich"] = (d["realised"] > d["implied"]).astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        d["iv_hv"] = np.where(d["hv"] > 0, d["iv"] / d["hv"], np.nan)
    d["year"] = np.array([x.year for x in d["date"]])
    return d


def gates(d) -> list[tuple[str, np.ndarray]]:
    iv, r, mc, w, y = d["iv"], d["iv_hv"], d["mcap"], d["window"], d["year"]
    g = [
        ("IV/HV < 0.9", r < 0.9), ("IV/HV 0.9–1.1", (r >= 0.9) & (r < 1.1)),
        ("IV/HV 1.1–1.4", (r >= 1.1) & (r < 1.4)), ("IV/HV > 1.4", r >= 1.4),
        ("IV < 40", iv < 40), ("IV 40–60", (iv >= 40) & (iv < 60)),
        ("IV 60–90", (iv >= 60) & (iv < 90)), ("IV 90–120", (iv >= 90) & (iv < 120)),
        ("IV > 120", iv >= 120),
        ("市值 < 5B", mc < 5e9), ("市值 5–30B", (mc >= 5e9) & (mc < 3e10)), ("市值 > 30B", mc >= 3e10),
        ("盘前发布", w == "PRE_MARKET"), ("盘后发布", w == "AFTER_MARKET"),
        ("2024 年", y == 2024), ("2025 年", y == 2025), ("2026 年", y == 2026),
    ]
    out = []
    for n, m in g:
        m = np.nan_to_num(m, nan=False).astype(bool)
        if 30 <= m.sum() <= len(m) - 30:   # 空组/全组会让 max-|T| 失效
            out.append((n, m))
    return out


def perm_gate_tests(y: np.ndarray, masks: list[np.ndarray], n_perm: int, rng) -> dict:
    """标签置换。同一批置换同时算所有分组 → max-|T| 族错误率。返回每组 obs/p/p_fwer。"""
    n = len(y)
    M = np.stack(masks).astype(float)                 # K×n
    n_in = M.sum(1); n_out = n - n_in
    tot = y.sum()
    def diffs(Y):                                     # Y: n×B → K×B (组内均值 − 组外均值)
        s_in = M @ Y
        return s_in / n_in[:, None] - (tot - s_in) / n_out[:, None]
    obs = diffs(y[:, None])[:, 0]
    B, out = 1000, []
    for _ in range(n_perm // B):
        Y = np.stack([rng.permutation(y) for _ in range(B)], axis=1)
        out.append(diffs(Y))
    P = np.concatenate(out, axis=1)                   # K×n_perm
    sd = P.std(1, ddof=1)
    p_raw = ((np.abs(P) >= np.abs(obs)[:, None]).sum(1) + 1) / (P.shape[1] + 1)
    T_obs = np.abs(obs) / sd
    T_max = (np.abs(P) / sd[:, None]).max(0)          # 每次置换的 max-|T|
    p_fwer = ((T_max[None, :] >= T_obs[:, None]).sum(1) + 1) / (len(T_max) + 1)
    # Holm
    order = np.argsort(p_raw); K = len(p_raw); p_holm = np.empty(K); running = 0
    for rank, i in enumerate(order):
        running = max(running, (K - rank) * p_raw[i]); p_holm[i] = min(1.0, running)
    return dict(obs=obs, p_raw=p_raw, p_fwer=p_fwer, p_holm=p_holm, n_in=n_in.astype(int))


def cluster_boot(y: np.ndarray, groups: np.ndarray, masks: list[np.ndarray] | None,
                 n_boot: int, rng) -> dict:
    """按 filing_date 整簇重抽样。返回 overall 均值 CI,及各分组 (组内−组外) 的 CI。"""
    _, gidx = np.unique(groups, return_inverse=True)
    G = gidx.max() + 1
    cnt = np.bincount(gidx, minlength=G).astype(float)
    s_all = np.bincount(gidx, weights=y, minlength=G)
    draws = rng.integers(0, G, size=(n_boot, G))
    def stat(sums, counts):
        return (sums[draws].sum(1)) / (counts[draws].sum(1))
    overall = stat(s_all, cnt)
    res = dict(overall_ci=np.percentile(overall, [2.5, 97.5]), gates=[])
    for m in masks or []:
        s_in = np.bincount(gidx, weights=y * m, minlength=G); c_in = np.bincount(gidx, weights=m.astype(float), minlength=G)
        with np.errstate(invalid="ignore", divide="ignore"):
            diff = stat(s_in, c_in) - stat(s_all - s_in, cnt - c_in)
        res["gates"].append(np.nanpercentile(diff, [2.5, 97.5]))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--perms", type=int, default=20000)
    ap.add_argument("--boots", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", default=os.path.join(ROOT, "site-data", "earnings", "event_stats.json"))
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    d = load()
    y, n = d["edge"], len(d["edge"])
    n_dates = len(np.unique(d["date"]))
    print(f"=== 事件库置换检验  n={n} 事件 / {n_dates} 个财报日 / {len(np.unique(d['ticker']))} 只 ===\n")

    # ---- 全样本 ----
    cb = cluster_boot(y, d["date"], None, a.boots, rng)
    rich = d["rich"].mean() * 100
    bt = stats.binomtest(int(d["rich"].sum()), n, 0.5)
    cb_rich = cluster_boot(d["rich"], d["date"], None, a.boots, rng)["overall_ci"] * 100
    print("全样本(卖方视角:隐含 − 实际,pp of spot):")
    print(f"  均值 {y.mean():+.2f}pp   中位 {np.median(y):+.2f}pp   "
          f"簇 bootstrap 95% CI [{cb['overall_ci'][0]:+.2f}, {cb['overall_ci'][1]:+.2f}]")
    print(f"  实际>隐含 {rich:.1f}%   二项 p={bt.pvalue:.3f}   簇 CI [{cb_rich[0]:.1f}, {cb_rich[1]:.1f}]")
    print("  → 按此 proxy,均值为负且 CI 不跨 0,胜率 ≈50%;但 proxy 低估事件隐含(见文件头),绝对值不可当 P&L。\n")

    # ---- 成本敏感性 ----
    print("交易成本敏感性(往返价差 = 跨式权利金的比例,假设值;买方 = −卖方 − 成本):")
    print(f"  {'成本':>6} {'卖方均值':>10} {'簇CI':>18} {'买方均值':>10} {'簇CI':>18}")
    cost_rows = []
    for c in COST_LEVELS:
        cost = d["implied"] * c
        s, b = y - cost, -y - cost
        cs = cluster_boot(s, d["date"], None, a.boots, rng)["overall_ci"]
        cbb = cluster_boot(b, d["date"], None, a.boots, rng)["overall_ci"]
        print(f"  {c*100:>5.0f}% {s.mean():>+10.2f} [{cs[0]:+.2f}, {cs[1]:+.2f}]   {b.mean():>+10.2f} [{cbb[0]:+.2f}, {cbb[1]:+.2f}]")
        cost_rows.append(dict(cost=c, seller=s.mean(), seller_ci=cs.tolist(), buyer=b.mean(), buyer_ci=cbb.tolist()))
    print("  → 注意:这里的 implied 是 30 日 IV 的换算,低估事件隐含波动,所以「买方为正」不是买方优势,\n"
          "    只说明真实事件隐含 > 30日IV换算;成本每 5% 权利金 ≈ 0.37pp of spot,与组间差同量级。\n")

    # ---- 分组 ----
    G = gates(d)
    names, masks = [g[0] for g in G], [g[1] for g in G]
    pt = perm_gate_tests(y, masks, a.perms, rng)
    cbg = cluster_boot(y, d["date"], masks, a.boots, rng)
    print(f"分组检验(组内均值 − 组外均值;置换 {a.perms} 次;族错误率 = max-|T|;簇 CI 按财报日):")
    print(f"  {'分组':<14}{'n':>6}{'组内均值':>9}{'差':>8}{'p':>8}{'p_FWER':>8}{'p_Holm':>8}{'簇CI':>20}  判定")
    grows = []
    for i, nm in enumerate(names):
        m = masks[i]; mean_in = y[m].mean() if m.any() else float("nan")
        lo, hi = cbg["gates"][i]
        sig = pt["p_fwer"][i] < 0.05 and (lo > 0 or hi < 0)
        verdict = "✅ 校正后仍显著" if sig else ("△ 仅未校正显著" if pt["p_raw"][i] < 0.05 else "—")
        print(f"  {nm:<14}{pt['n_in'][i]:>6}{mean_in:>+9.2f}{pt['obs'][i]:>+8.2f}{pt['p_raw'][i]:>8.4f}"
              f"{pt['p_fwer'][i]:>8.4f}{pt['p_holm'][i]:>8.4f}  [{lo:+.2f}, {hi:+.2f}]  {verdict}")
        grows.append(dict(gate=nm, n=int(pt["n_in"][i]), mean_in=float(mean_in), diff=float(pt["obs"][i]),
                          rich_pct=float(d["rich"][m].mean() * 100) if m.any() else None,
                          p=float(pt["p_raw"][i]), p_fwer=float(pt["p_fwer"][i]), p_holm=float(pt["p_holm"][i]),
                          cluster_ci=[float(lo), float(hi)], significant=bool(sig)))
    # 组内均值本身 ≠ 0 的簇 CI(给早报用:该组卖方期望)
    for i, m in enumerate(masks):
        ci = cluster_boot(y[m], d["date"][m], None, a.boots // 2, rng)["overall_ci"] if m.sum() > 30 else [None, None]
        grows[i]["mean_in_ci"] = [float(x) if x is not None else None for x in ci]
    n_sig = sum(r["significant"] for r in grows)
    print(f"\n  校正后显著的分组: {n_sig}/{len(grows)}。"
          + (" 注意:显著的是「比其他组更差/更好」,组内期望本身请看 mean_in_ci。" if n_sig else " 分组不提供可用的择时/择股信息。"))

    out = dict(generated_at=dt.datetime.now().isoformat(timespec="seconds"), n=n, n_dates=int(n_dates),
               perms=a.perms, boots=a.boots,
               overall=dict(mean=float(y.mean()), median=float(np.median(y)), ci=cb["overall_ci"].tolist(),
                            rich_pct=float(rich), rich_p=float(bt.pvalue), rich_ci=cb_rich.tolist()),
               cost=cost_rows, gates=grows)
    os.makedirs(os.path.dirname(a.json), exist_ok=True)
    with open(a.json, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n→ {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
