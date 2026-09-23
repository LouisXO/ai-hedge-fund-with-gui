"""Narrator contract: numbers-only, fabricated numbers fall back, cache hits."""
import json

from agent import narrator


class FakeTransport:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def resolved_model(self):
        return "claude-sonnet-5"

    def call(self, system, user):
        self.calls += 1
        return {"result": self.reply}


D = {"as_of": "2026-09-22", "gate_passed": 229, "gate_of": 503, "n_option_picks": 30, "n_insider_picks": 9,
     "paper": {"long": -0.56, "insider": 0.0}, "vix": 14.8}


def test_valid_reply_passes_and_is_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(narrator, "CACHE_DIR", tmp_path)
    t = FakeTransport(json.dumps({"headline": "便宜门 229/503", "commentary": "今天期权候选 30 个,内部人候选 9 个。模拟盘长线 -0.56%。",
                                  "caveat": "记录期仍短。"}, ensure_ascii=False))
    r1 = narrator.narrate(D, t)
    assert r1["source"].startswith("llm:") and "229" in r1["headline"]
    r2 = narrator.narrate(D, t)
    assert r2 == r1 and t.calls == 1                                        # second call served from cache


def test_fabricated_number_falls_back_to_template(tmp_path, monkeypatch):
    monkeypatch.setattr(narrator, "CACHE_DIR", tmp_path)
    t = FakeTransport(json.dumps({"headline": "命中率 71%", "commentary": "候选 30 个。", "caveat": "短。"}, ensure_ascii=False))
    r = narrator.narrate(D, t)
    assert r["source"] == "template:fabricated_number:71" and "229/503" in r["commentary"]


def test_garbage_and_missing_keys_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(narrator, "CACHE_DIR", tmp_path)
    assert narrator.narrate(D, FakeTransport("not json"), use_cache=False)["source"] == "template:shape"
    assert narrator.narrate(D, FakeTransport('{"headline": "x"}'), use_cache=False)["source"] == "template:shape"
