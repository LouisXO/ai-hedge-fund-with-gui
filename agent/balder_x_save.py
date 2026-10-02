"""Save Balder posts read from X in the owner's own browser session (Claude in Chrome, owner-initiated, read only).

stdin: JSON list of {"id": status id, "time": "...", "text": "...", "tab": "subs" | "posts"}. A post not saved before is
written to the balder_log drop folder (one .txt per post, picked up at 13:25) and appended to a private feed log.
Prints the new posts only. Private: nothing here goes to the public site.

Usage: echo '[...]' | python -m agent.balder_x_save
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

DROP = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Balder")
FEED = "/Users/louis/optradar/out/agent/balder_x_feed.jsonl"


def main() -> int:
    posts = json.load(sys.stdin)
    os.makedirs(DROP, exist_ok=True)
    seen = set()
    if os.path.exists(FEED):
        seen = {json.loads(l)["id"] for l in open(FEED) if l.strip()}
    new = []
    for p in posts:
        pid = str(p.get("id") or "").strip()
        if not pid or pid in seen or not (p.get("text") or "").strip():
            continue
        today = dt.date.today().isoformat()
        with open(os.path.join(DROP, f"{today}-x-{pid}.txt"), "w") as f:
            f.write(f"{today} Balder X {p.get('tab', '')} 帖子({p.get('time', '')}):{p['text'].strip()}\n")
        rec = {"id": pid, "saved_at": dt.datetime.now().isoformat(timespec="seconds"), **p}
        with open(FEED, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        seen.add(pid)
        new.append(rec)
    for r in new:
        print(f"NEW [{r.get('tab')}] {r.get('time')}: {r['text'][:200]}")
    print(f"{len(new)} new of {len(posts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
