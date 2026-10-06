"""Save Balder posts read from X in the owner's own browser session (Claude in Chrome, owner-initiated, read only).

stdin: JSON list of {"id": status id, "time": "...", "text": "...", "tab": "subs" | "posts"}, optionally with
  "media":   what the post's images say, read from the photo page (chart levels, the "THE READ" box)
  "replies": Balder's own replies under the post, as shown on the post page
  "enrich":  true for a post saved before: the full text / images / replies are added to the private
             feed log only, never as a second file in the drop folder (balder_log would read it as a
             second post and count its trades twice)
  "todo":    "fulltext" / "image" / "fulltext,image" when the timeline shows a "Show more" or pictures still
             to be read: kept in the state file until an enrichment of the post arrives
A post not saved before is written to the balder_log drop folder (one .txt per post, picked up at 13:25)
with its media and replies appended, and to the private feed log. Prints what it saved.
Private: nothing here goes to the public site. The owner asked on 2026-10-06 for full texts and images,
and for reading down the timeline to where the last read ended, over several runs if there is a backlog.

State (~/.hedge-fund/agent/balder_x_state.json): per tab `read_through`, the newest post time up to which
everything older has been read without a gap (a run that scrolls down to a post at or before it may move it
to the newest post it read); `todo`, posts whose full text or images are still to be read.

Usage: echo '[...]' | python -m agent.balder_x_save
       python -m agent.balder_x_save --state                 # read_through, recent ids, todo (for the reader)
       python -m agent.balder_x_save --read-through subs=2026-10-06T18:29:20Z
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

DROP = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Balder")
FEED = "/Users/louis/optradar/out/agent/balder_x_feed.jsonl"
STATE = os.path.expanduser("~/.hedge-fund/agent/balder_x_state.json")
RECENT_IDS = 120


def load_state(path: str = STATE) -> dict:
    try:
        s = json.load(open(path))
    except Exception:
        s = {}
    return {"read_through": s.get("read_through", {}), "todo": s.get("todo", [])}


def write_state(s: dict, path: str = STATE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def update_todo(s: dict, recs: list[dict], posts: list[dict]) -> dict:
    """Add posts flagged `todo`; drop a post from the list once an enrichment of it has been saved."""
    done = {r["id"] for r in recs if r.get("enrich")}
    todo = [t for t in s["todo"] if t["id"] not in done]
    have = {t["id"] for t in todo}
    for p in posts:
        pid = str(p.get("id") or "")
        if p.get("todo") and not p.get("enrich") and pid not in have and pid not in done:
            todo.append({"id": pid, "why": p["todo"], "time": p.get("time", ""), "tab": p.get("tab", "")})
            have.add(pid)
    return {**s, "todo": todo}


def recent_ids(feed: str = FEED, n: int = RECENT_IDS) -> list[str]:
    if not os.path.exists(feed):
        return []
    ids = [json.loads(line)["id"] for line in open(feed) if line.strip()]
    return list(dict.fromkeys(reversed(ids)))[:n]


def body(p: dict) -> str:
    text = (p.get("text") or "").strip()
    if (p.get("media") or "").strip():
        text += f" [图] {p['media'].strip()}"
    if (p.get("replies") or "").strip():
        text += f" [他的回复] {p['replies'].strip()}"
    return text


def save(posts: list[dict], drop: str = DROP, feed: str = FEED, today: dt.date | None = None) -> list[dict]:
    """The records written (new posts and enrichments), in input order."""
    os.makedirs(drop, exist_ok=True)
    os.makedirs(os.path.dirname(feed), exist_ok=True)
    seen = set()
    if os.path.exists(feed):
        seen = {json.loads(line)["id"] for line in open(feed) if line.strip() and not json.loads(line).get("enrich")}
    today = (today or dt.date.today()).isoformat()
    out = []
    for p in posts:
        pid = str(p.get("id") or "").strip()
        text = body(p)
        if not pid or not text:
            continue
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        if pid in seen:
            if not p.get("enrich"):
                continue
            rec = {"id": pid, "enrich": True, "saved_at": stamp, "time": p.get("time", ""), "tab": p.get("tab", ""), "text": text}
        else:
            with open(os.path.join(drop, f"{today}-x-{pid}.txt"), "w") as f:
                f.write(f"{today} Balder X {p.get('tab', '')} 帖子({p.get('time', '')}):{text}\n")
            rec = {"id": pid, "saved_at": stamp, **{k: v for k, v in p.items() if k not in ("id", "enrich")}, "text": text}
            seen.add(pid)
        with open(feed, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.append(rec)
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    s = load_state()
    if args and args[0] == "--state":
        print(json.dumps({**s, "recent_ids": recent_ids()}, ensure_ascii=False))
        return 0
    if args and args[0] == "--read-through":
        for kv in args[1:]:
            tab, when = kv.split("=", 1)
            s["read_through"][tab] = when
        write_state(s)
        print(json.dumps(s["read_through"]))
        return 0
    posts = json.load(sys.stdin)
    recs = save(posts)
    write_state(update_todo(s, recs, posts))
    for r in recs:
        print(f"{'ENRICH' if r.get('enrich') else 'NEW'} [{r.get('tab')}] {r.get('time')}: {r['text'][:200]}")
    print(f"{sum(not r.get('enrich') for r in recs)} new, {sum(bool(r.get('enrich')) for r in recs)} enriched, of {len(posts)}; "
          f"todo {len(load_state()['todo'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
