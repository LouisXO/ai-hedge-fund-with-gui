"""Event lines: the interface contract and the 13D parser."""
import datetime as dt

import numpy as np
import pandas as pd

from agent.books.data import Market
from agent.events.base import EventLine, LineSpec
from agent.sources.sec_13d import parse_hit


def _market(days=5, tickers=("A", "B", "C")):
    idx = pd.bdate_range("2026-01-05", periods=days)
    px = pd.DataFrame(10.0, index=idx, columns=list(tickers))
    adv = pd.DataFrame(5e6, index=idx, columns=list(tickers))
    adv["C"] = 1e9                                                  # above any ceiling
    listed = pd.DataFrame(True, index=idx, columns=list(tickers))
    return Market(px, px, px, adv, listed, pd.Series(100.0, index=idx), {})


class _Line(EventLine):
    spec = LineSpec("t", "1", 5, 10, 3e6, 1e8, "test")

    def events(self, store, start, end):
        return pd.DataFrame({"date": pd.to_datetime(["2026-01-06", "2026-01-06", "2026-01-06", "2026-01-07"]),
                             "ticker": ["A", "B", "C", "A"], "side": ["L", "L", "L", "S"],
                             "strength": [1.0, 2.0, 9.0, 5.0], "detail": ""})


def test_targets_keep_tradable_longs_in_strength_order():
    line = _Line()
    tg = line.targets(_market(), line.events(None, "", ""), "2026-01-01", "2026-01-31")
    assert tg == {pd.Timestamp("2026-01-06"): ["B", "A"]}          # C above the ADV ceiling, the short dropped


def test_parse_hit_reads_subject_ticker_and_filers():
    hit = {"_id": "0001193125-19-147979:sc13d.htm",
           "_source": {"form": "SC 13D", "file_date": "2019-05-15",
                       "display_names": ["Midstates Petroleum Company, Inc.  (AMPY)  (CIK 0001533924)",
                                         "Amplify Energy Corp  (CIK 0001500000)", "Some Person  (CIK 0001500001)"]}}
    r = parse_hit(hit)
    assert r["accession"] == "0001193125-19-147979" and not r["is_amendment"]
    assert r["subject_cik"] == "0001533924" and r["ticker_display"] == "AMPY"
    assert r["filers"] == "Amplify Energy Corp; Some Person"
    hit["_source"]["form"] = "SCHEDULE 13D/A"
    hit["_source"]["display_names"][0] = "No Ticker Co  (CIK 0000000001)"
    r = parse_hit(hit)
    assert r["is_amendment"] and r["ticker_display"] is None
