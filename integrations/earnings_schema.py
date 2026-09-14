"""EarningsSnapshot — 财报预测专用 schema(P5)。

与 aihf 的 FundamentalsSnapshot 互补:那个描述"公司好不好"(价值视角),
这个描述"这只票财报时怎么动"(事件+期权视角)。

设计原则(源自 Balder 的公开方法论):没有结构性的输入就不会有结构性的输出。
所以统计量一律由代码算好再喂给模型 —— LLM 不擅长在长表格上做算术,
让它算等于引入噪声。模型只负责在给定事实上做判断。

严格 point-in-time:事件列表按 as_of 截断,IV 分位只用该期之前的数据。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), 2) if vals else None


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
            "d0_avg": _avg(d0), "d0_up_rate": _pct_pos(d0),
            "d5_avg": _avg(d5), "d5_up_rate": _pct_pos(d5),
            "d0_abs_avg": _avg([abs(v) for v in d0 if v is not None]),
            "beat_d0_avg": _avg(beat_d0), "miss_d0_avg": _avg(miss_d0),
            "iv_crush_avg": _avg([e.get("iv_crush") for e in ev]),
            "iv_pre_avg": _avg([e.get("iv_pre") for e in ev]),
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
                 "| 当日% | +1日% | +3日% | +5日% | IV crush")
        for e in self.events:
            def f(v, d=1, suf=""):
                return f"{v:.{d}f}{suf}" if isinstance(v, (int, float)) else "-"
            L.append(
                f"  {e.get('period','-')} | {e.get('filed','-')} | {f(e.get('iv_pre'))} "
                f"| {f(e.get('iv_hv_ratio'), 2)} | {f(e.get('iv_pctile_pit'), 0)} "
                f"| {e.get('eps_actual')}/{e.get('eps_est')} | {e.get('eps_surprise') or '-'} "
                f"| {e.get('rev_surprise') or '-'} | {f(e.get('move_d0'))} | {f(e.get('move_d1'))} "
                f"| {f(e.get('move_d3'))} | {f(e.get('move_d5'))} | {f(e.get('iv_crush'))}")
        L += ["", "程序计算的统计:",
              f"  EPS 超预期比例 {s['beat_rate']}%  |  财报当日上涨比例 {s['d0_up_rate']}%"
              f"  |  +5日上涨比例 {s['d5_up_rate']}%",
              f"  当日平均涨跌 {s['d0_avg']}%(绝对值均值 {s['d0_abs_avg']}%)"
              f"  |  +5日平均 {s['d5_avg']}%",
              f"  超预期时当日均值 {s['beat_d0_avg']}%  |  不及预期时当日均值 {s['miss_d0_avg']}%",
              f"  财报前 IV 均值 {s['iv_pre_avg']}  |  平均 IV crush {s['iv_crush_avg']}"]
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
