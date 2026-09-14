"""EarningsSnapshot — 财报预测专用 schema(P5)。

与 aihf 的 FundamentalsSnapshot 互补:那个描述"公司好不好"(价值视角),
这个描述"这只票财报时怎么动"(事件+期权视角)。

设计原则(源自 Balder 的公开方法论):没有结构性的输入就不会有结构性的输出。
所以统计量一律由代码算好再喂给模型 —— LLM 不擅长在长表格上做算术,
让它算等于引入噪声。模型只负责在给定事实上做判断。

严格 point-in-time:事件列表按 as_of 截断,IV 分位只用该期之前的数据。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), 2) if vals else None


def _med(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def _pct_pos(vals):
    vals = [v for v in vals if v is not None]
    return round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 0) if vals else None


@dataclass
class EarningsSnapshot:
    ticker: str
    as_of: str
    spot: float | None = None
    iv_now: float | None = None
    hv_now: float | None = None
    next_earnings: str | None = None
    days_to_earnings: int | None = None
    events: list = field(default_factory=list)   # newest first, already filtered

    # ---- derived stats (computed here, never by the LLM) ----
    @staticmethod
    def implied_5d(iv: float | None) -> float | None:
        """IV (annualised %) -> implied move over 5 trading days, in %."""
        return round(iv / 100 * math.sqrt(5 / 252) * 100, 1) if iv else None

    @property
    def stats(self) -> dict:
        ev = self.events
        beats = [e for e in ev if e.get("eps_surprise") == "BEAT"]
        d0 = [e.get("move_d0") for e in ev]
        d5 = [e.get("move_d5") for e in ev]
        beat_d0 = [e.get("move_d0") for e in beats]
        miss_d0 = [e.get("move_d0") for e in ev if e.get("eps_surprise") == "MISS"]
        return {
            "n": len(ev),
            "beat_rate": round(100.0 * len(beats) / len(ev), 0) if ev else None,
            "d0_avg": _avg(d0), "d0_med": _med(d0), "d0_up_rate": _pct_pos(d0),
            "d5_avg": _avg(d5), "d5_med": _med(d5), "d5_up_rate": _pct_pos(d5),
            "d5_max": round(max([v for v in d5 if v is not None], default=0), 1) or None,
            "d5_min": round(min([v for v in d5 if v is not None], default=0), 1) or None,
            "d0_abs_avg": _avg([abs(v) for v in d0 if v is not None]),
            "beat_d0_avg": _avg(beat_d0), "miss_d0_avg": _avg(miss_d0),
            "iv_crush_avg": _avg([e.get("iv_crush") for e in ev]),
            "iv_pre_avg": _avg([e.get("iv_pre") for e in ev]),
            **self.vol_stats,
        }

    @property
    def vol_stats(self) -> dict:
        """Realised vs implied around each print — the seller/buyer question.

        `rich_rate` is the share of prints where the actual 5-day move EXCEEDED
        what the pre-earnings IV implied. Below 50% means options were, more
        often than not, priced above what the stock delivered (seller's edge).
        """
        pairs = []
        for e in self.events:
            imp = self.implied_5d(e.get("iv_pre"))
            act = e.get("move_d5")
            if imp and act is not None:
                pairs.append((imp, abs(act)))
        if not pairs:
            return {"vol_n": 0, "implied_avg": None, "realised_avg": None,
                    "rich_rate": None, "rv_iv_ratio": None}
        imp_avg = statistics.mean(p[0] for p in pairs)
        act_avg = statistics.mean(p[1] for p in pairs)
        # Medians as well: a single blown-out IV print (RGTI once had IV 432
        # against a 94-151 baseline) drags the mean ratio far from typical,
        # and the mean alone would overstate the seller's edge.
        imp_med = statistics.median(p[0] for p in pairs)
        act_med = statistics.median(p[1] for p in pairs)
        return {
            "vol_n": len(pairs),
            "implied_avg": round(imp_avg, 1),
            "realised_avg": round(act_avg, 1),
            "implied_med": round(imp_med, 1),
            "realised_med": round(act_med, 1),
            "rich_rate": round(100.0 * sum(1 for i, a in pairs if a > i) / len(pairs), 0),
            "rv_iv_ratio": round(act_avg / imp_avg, 2) if imp_avg else None,
            "rv_iv_ratio_med": round(act_med / imp_med, 2) if imp_med else None,
        }

    def render(self) -> str:
        s = self.stats
        L = [f"标的 {self.ticker} — 财报事件档案",
             "所有数据均为该时点已公开信息;统计量由程序计算,非估计值。", ""]
        L.append("当前状态:")
        L.append(f"  股价 {self.spot}  |  当前 IV {self.iv_now}  HV {self.hv_now}"
                 f"  |  IV/HV {round(self.iv_now / self.hv_now, 2) if (self.iv_now and self.hv_now) else '-'}")
        if self.next_earnings:
            L.append(f"  下次财报 {self.next_earnings}"
                     + (f"({self.days_to_earnings} 天后)" if self.days_to_earnings is not None else ""))
        L.append("")
        L.append(f"历史财报反应({s['n']} 次,最近在前):")
        L.append("  期间 | 发布日 | IV(前1日) | IV/HV | IV历史分位 | EPS实际/预期 | EPS意外 | 营收意外 "
                 "| 当日% | +1日% | +3日% | +5日% | IV crush | 隐含5日 vs 实际5日")
        for e in self.events:
            def f(v, d=1, suf=""):
                return f"{v:.{d}f}{suf}" if isinstance(v, (int, float)) else "-"
            L.append(
                f"  {e.get('period','-')} | {e.get('filed','-')} | {f(e.get('iv_pre'))} "
                f"| {f(e.get('iv_hv_ratio'), 2)} | {f(e.get('iv_pctile_pit'), 0)} "
                f"| {e.get('eps_actual')}/{e.get('eps_est')} | {e.get('eps_surprise') or '-'} "
                f"| {e.get('rev_surprise') or '-'} | {f(e.get('move_d0'))} | {f(e.get('move_d1'))} "
                f"| {f(e.get('move_d3'))} | {f(e.get('move_d5'))} | {f(e.get('iv_crush'))} "
                f"| 隐含{f(self.implied_5d(e.get('iv_pre')))} vs 实际"
                f"{f(abs(e['move_d5']) if e.get('move_d5') is not None else None)}")
        L += ["", "程序计算的统计:",
              f"  EPS 超预期比例 {s['beat_rate']}%  |  财报当日上涨比例 {s['d0_up_rate']}%"
              f"  |  +5日上涨比例 {s['d5_up_rate']}%",
              f"  当日:中位数 {s['d0_med']}%  均值 {s['d0_avg']}%  (绝对值均值 {s['d0_abs_avg']}%)",
              f"  +5日:中位数 {s['d5_med']}%  均值 {s['d5_avg']}%  "
              f"(区间 {s['d5_min']}% ~ {s['d5_max']}%)",
              "  ⚠️ 均值与中位数差异大时,说明分布被少数极端值主导 —— "
              "以中位数和上涨比例判断典型情形,均值只说明尾部有多厚。",
              f"  超预期时当日均值 {s['beat_d0_avg']}%  |  不及预期时当日均值 {s['miss_d0_avg']}%",
              f"  财报前 IV 均值 {s['iv_pre_avg']}  |  平均 IV crush {s['iv_crush_avg']}"]
        if s.get("vol_n"):
            L += ["", "波动率定价(核心:期权贵还是便宜):",
                  f"  样本 {s['vol_n']} 次  |  财报前 IV 隐含的 5 日波动均值 {s['implied_avg']}%"
                  f"  |  实际 5 日波动均值 {s['realised_avg']}%",
                  f"  中位数口径:隐含 {s['implied_med']}%  实际 {s['realised_med']}%",
                  f"  实际波动超过隐含的比例 {s['rich_rate']}%  |  "
                  f"实际/隐含 = {s['rv_iv_ratio']}(均值) / {s['rv_iv_ratio_med']}(中位数)",
                  "  两个比值差距大 ⇒ 有离群的 IV 定价,以中位数为准。",
                  "  超出比例 <50% ⇒ 期权定价高于兑现,卖方占优;>50% ⇒ 买方占优。",
                  "  注意:比值与超出比例可能背离 —— 那说明分布负偏(多数小赚、少数大亏)。"]
        return "\n".join(L)
        return "\n".join(L)


def build_earnings_snapshot(ticker: str, as_of: str, client, limit: int = 12
                            ) -> EarningsSnapshot:
    """Assemble the schema from moomoo, truncated at *as_of*."""
    from integrations.moomoo_client import MoomooError, _norm, _num

    events = client.earnings_events(ticker, as_of=as_of, limit=limit)
    try:
        spot = _num(client._snapshot(_norm(ticker)).get("last_price"))
    except MoomooError:
        spot = None
    # current IV/HV = the most recent observed pre-earnings sample we have
    iv_now = events[0].get("iv_pre") if events else None
    hv_now = events[0].get("hv_pre") if events else None
    return EarningsSnapshot(ticker=ticker, as_of=as_of, spot=spot,
                            iv_now=iv_now, hv_now=hv_now, events=events)
