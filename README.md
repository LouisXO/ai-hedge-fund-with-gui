# hedge-fund

A rule-based stock research and paper-trading system. Every signal is pre-registered and tested
before it can trade; two books that passed trade a $100k Alpaca **paper** account; the public
record is at https://hedge-fund.louisleng.com.

No LLM makes a directional call. LLMs are used only for display text (the morning-brief
narrator and headline translation), through a sealed single-turn call.

## Layout

| Path | What it is |
|---|---|
| `agent/` | The system: data loaders (`sources/`), event lines (`events/`), the two books (`books/`), the paper executor (`execute.py`), daily review, dashboard, watchlist, and every pre-registered experiment (`sNN_*.py`) |
| `hedge_fund/` | Shared library: the DuckDB point-in-time panel (`features/panel.py`), factor scores, realized volatility, validation statistics and the Holm family ledger |
| `integrations/` | The sealed Claude Code LLM client and the read-only moomoo data client |
| `earnings/` | The earnings event database and its statistics, used by optradar's volatility pricing |
| `site/` | The public static site: paper portfolio (home page) and the retired master-signal archive |
| `site-data/` | Validation reports, the family ledger, earnings event data, the master-signal archive |
| `docs/AGENT_PLAN.md` | The plan, the execution log (§9, one entry per experiment) and the rolling to-do (§10) |
| `AGENT.md` | Rules, paths, schedules and rituals every working session starts from |

## Setup

```bash
python3.13 -m venv ~/.hedgefund-venv
~/.hedgefund-venv/bin/python -m pip install -r requirements.txt
PYTHONPATH=. ~/.hedgefund-venv/bin/python -m pytest -q agent/tests hedge_fund
```

Secrets live in `~/.hedge-fund/.env`, never in the repository.

## History

This repository started as a fork of [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)
(MIT, see `LICENSE`). Its LLM investor personas were tested here and retired on 2026-09-23: 55%
directional accuracy at five days against 70% for always guessing the majority direction. The
upstream modules were removed the same day; tag `pre-cleanup-2026-09-23` has the last state
that contained them.
