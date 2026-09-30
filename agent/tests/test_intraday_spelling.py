"""A class share is requested in Alpaca's spelling and stored under the listing's (agent/sources/alpaca_intraday.py)."""
import io
import json
import urllib.parse

from agent.sources import alpaca_intraday as ai


def test_class_share_is_requested_with_a_dot_and_stored_with_a_dash(monkeypatch):
    asked = []

    def fake(req, timeout=None):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        asked.append(q["symbols"][0])
        bar = {"t": "2026-09-29T14:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 10, "n": 1}
        return io.BytesIO(json.dumps({"bars": {"BRK.A": [bar]}, "next_page_token": None}).encode())

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake)
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    import datetime as dt
    df = ai.fetch(["BRK-A"], "30Min", dt.date(2026, 9, 29), dt.date(2026, 9, 29), {})
    assert asked == ["BRK.A"] and set(df["ticker"]) == {"BRK-A"}
