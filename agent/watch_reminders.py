"""Sync the watchlist's price levels to moomoo price reminders, so the phone app pushes them.

For every name in agent/watchlist.yaml:
  below: [..]  -> PRICE_DOWN reminders at each level
  above: [..]  -> PRICE_UP reminders at each level
  plus a +/-5% daily move reminder (CHANGE_RATE_UP / CHANGE_RATE_DOWN, 5)
Frequency ONCE_A_DAY, regular session plus pre-market and after-hours.

Only reminders whose note starts with "agent" are ever added, modified or deleted; reminders the
user set by hand are never touched. Levels removed from the watchlist are deleted from moomoo on the
next sync. This uses the quote context's reminder API only — no trading context is opened, nothing
here can place an order.

Runs after agent.watch in the post-close job, and by hand after editing the watchlist.

Usage: python -m agent.watch_reminders [--dry-run]
"""
from __future__ import annotations

import argparse
import time

import moomoo as mm
import yaml

from agent.watch import WATCHLIST

TAG = "agent"
MOVE_PCT = 5.0


def desired() -> dict[tuple[str, str, float], str]:
    """(code, reminder_type, value) -> note."""
    wl = yaml.safe_load(open(WATCHLIST))["watch"]
    out = {}
    for t, cfg in wl.items():
        code = f"US.{t}"
        for lvl in cfg.get("below", []) or []:
            out[(code, "PRICE_DOWN", float(lvl))] = f"{TAG} 跌破 {lvl}"
        for lvl in cfg.get("above", []) or []:
            out[(code, "PRICE_UP", float(lvl))] = f"{TAG} 突破 {lvl}"
        out[(code, "CHANGE_RATE_UP", MOVE_PCT)] = f"{TAG} 当日 +{MOVE_PCT:g}%"
        out[(code, "CHANGE_RATE_DOWN", MOVE_PCT)] = f"{TAG} 当日 -{MOVE_PCT:g}%"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    want = desired()
    q = mm.OpenQuoteContext(host="127.0.0.1", port=11111)
    added = deleted = kept = 0
    try:
        ret, cur = q.get_price_reminder(market=mm.Market.US)
        if ret != 0:
            raise RuntimeError(f"get_price_reminder: {cur}")
        ours = cur[cur["note"].fillna("").str.startswith(TAG)]
        have = {}
        for r in ours.itertuples(index=False):
            have[(r.code, str(r.reminder_type), float(r.value))] = r.key
        for k, key in have.items():
            if k not in want:
                if not args.dry_run:
                    r = q.set_price_reminder(k[0], mm.SetPriceReminderOp.DEL, key=key)
                    if r[0] != 0:
                        print(f"delete failed {k}: {r[1]}")
                        continue
                    time.sleep(0.4)
                print(f"- {k[0]} {k[1]} {k[2]:g}")
                deleted += 1
            else:
                kept += 1
        for k, note in want.items():
            if k in have:
                continue
            if not args.dry_run:
                r = q.set_price_reminder(k[0], mm.SetPriceReminderOp.ADD, reminder_type=getattr(mm.PriceReminderType, k[1]),
                                         reminder_freq=mm.PriceReminderFreq.ONCE_A_DAY, value=k[2], note=note)
                if r[0] != 0:
                    print(f"add failed {k}: {r[1]}")
                    continue
                time.sleep(0.4)
            print(f"+ {k[0]} {k[1]} {k[2]:g}  ({note})")
            added += 1
    finally:
        q.close()
    print(f"reminders: +{added} -{deleted} kept {kept}" + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
