"""Balder posts from X: one drop file per new post, images and replies in the same text, an enrichment as a supplement."""
import datetime as dt
import json
import re

from agent import balder_x_save as bx

DAY = dt.date(2026, 10, 6)


def _feed(path):
    return [json.loads(line) for line in open(path) if line.strip()]


def test_a_new_post_carries_its_chart_and_replies_and_is_saved_once(tmp_path):
    drop, feed = tmp_path / "drop", tmp_path / "feed.jsonl"
    post = {"id": "1", "time": "2026-10-02T14:16Z", "tab": "subs", "text": "10月发财股票 $ASTS 初步60 目标72",
            "media": "ASTS 阻力 66.12, 20日均线 60.84, 支撑 55.80", "replies": "可以"}
    recs = bx.save([post], str(drop), str(feed), DAY)
    assert len(recs) == 1 and "[图] ASTS 阻力 66.12" in recs[0]["text"] and "[他的回复] 可以" in recs[0]["text"]
    assert [p.name for p in drop.iterdir()] == ["2026-10-06-x-1.txt"]
    assert bx.save([post], str(drop), str(feed), DAY) == []                        # seen: nothing written again
    assert len(list(drop.iterdir())) == 1 and len(_feed(feed)) == 1


def test_an_enrichment_is_a_supplement_dated_by_the_post_not_a_second_post(tmp_path):
    drop, feed = tmp_path / "drop", tmp_path / "feed.jsonl"
    bx.save([{"id": "2", "time": "2026-10-02T14:16:00Z", "text": "平仓获利 NU ... NVO", "tab": "subs"}], str(drop), str(feed), DAY)
    recs = bx.save([{"id": "2", "text": "全文 NVO 37.16→37.54", "tab": "subs", "enrich": True}], str(drop), str(feed), DAY)
    assert recs[0]["enrich"] and recs[0]["time"] == "2026-10-02T14:16:00Z"        # no time given: the first save's
    names = sorted((p.name for p in drop.iterdir()), key=len)                    # the post, then its supplement
    assert names[0] == "2026-10-06-x-2.txt" and re.fullmatch(r"2026-10-06-x-2-full-\d{6}\.txt", names[1])
    sup = (drop / names[1]).read_text()
    assert "帖子(2026-10-02T14:16:00Z)(补全)" in sup and "NVO 37.16" in sup
    lines = _feed(feed)
    assert len(lines) == 2 and lines[1]["enrich"] and lines[1]["supplement"] == names[1]
    assert bx.save([{"id": "3", "text": "new", "enrich": True}], str(drop), str(feed), DAY)[0].get("enrich") is None   # unseen: a post


def test_backfill_writes_one_supplement_per_old_enrichment_and_only_once(tmp_path):
    drop, feed = tmp_path / "drop", tmp_path / "feed.jsonl"
    drop.mkdir()
    feed.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"id": "7", "time": "2026-10-01T20:00:00Z", "tab": "subs", "text": "平仓 …"},
        {"id": "7", "enrich": True, "time": "2026-10-01T20:00:00Z", "tab": "subs", "text": "全文 ABC 10.00→10.50"},
        {"id": "8", "enrich": True, "time": "2026-10-02T13:00:00Z", "tab": "subs", "text": "   "}]) + "\n")
    names = bx.backfill_supplements(str(drop), str(feed), DAY)
    assert len(names) == 1 and names[0].startswith("2026-10-06-x-7-full-")
    assert "帖子(2026-10-01T20:00:00Z)(补全)" in (drop / names[0]).read_text()
    assert bx.backfill_supplements(str(drop), str(feed), DAY) == []


def test_drop_todo_removes_only_the_named_posts():
    s = {"read_through": {}, "todo": [{"id": "1", "why": "image", "time": "", "tab": "posts"},
                                      {"id": "2", "why": "fulltext", "time": "", "tab": "subs"}]}
    s, gone = bx.drop_todo(s, ["1", "9"])
    assert [t["id"] for t in gone] == ["1"] and [t["id"] for t in s["todo"]] == ["2"]


def test_todo_is_kept_until_the_enrichment_arrives():
    s = {"read_through": {}, "todo": []}
    posts = [{"id": "4", "text": "图", "todo": "image", "tab": "subs"}, {"id": "5", "text": "x", "tab": "posts"}]
    s = bx.update_todo(s, [{"id": "4"}, {"id": "5"}], posts)
    assert [t["id"] for t in s["todo"]] == ["4"]
    s = bx.update_todo(s, [], posts)                                                # flagged again: not duplicated
    assert len(s["todo"]) == 1
    s = bx.update_todo(s, [{"id": "4", "enrich": True}], [{"id": "4", "text": "图上的价位", "enrich": True}])
    assert s["todo"] == []
