"""Fetch jobs: download with no panel.db connection open, write last, and log every run's outcome.

No network: every HTTP call goes to a fake opener. Temp DuckDB only.
"""
import datetime as dt
import io
import json
import urllib.error

import pandas as pd
import pytest

from agent.sources import sec_daily_form4 as f4
from hedge_fund.features.panel import PanelStore

TODAY = dt.date(2026, 9, 29)                     # a Tuesday
XML = """<ownershipDocument><periodOfReport>2026-09-28</periodOfReport>
 <issuer><issuerCik>0000001234</issuerCik><issuerTradingSymbol>abc</issuerTradingSymbol></issuer>
 <reportingOwner><reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
  <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship></reportingOwner>
 <nonDerivativeTable><nonDerivativeTransaction>
  <transactionDate><value>2026-09-28</value></transactionDate>
  <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
  <transactionAmounts><transactionShares><value>1000</value></transactionShares>
   <transactionPricePerShare><value>12.5</value></transactionPricePerShare>
   <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
 </nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""


class Tracked(PanelStore):
    """PanelStore that counts its open connections, so a fake HTTP call can assert none is open."""
    open = 0

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        Tracked.open += 1

    def close(self):
        Tracked.open -= 1
        super().close()


class FakeSec:
    """EDGAR stand-in. accs: accession numbers the search lists for TODAY; down / missing: URL substrings
    that time out / answer 404."""

    def __init__(self, accs=(), down=(), missing=(), index_days=()):
        self.accs, self.down, self.missing, self.index_days, self.urls = list(accs), down, missing, index_days, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        assert Tracked.open == 0, "panel.db connection held during a download"
        if any(s in url for s in self.down):
            raise urllib.error.URLError("timed out")
        if any(s in url for s in self.missing):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        if "efts.sec.gov" in url:
            hits = [{"_id": f"{a}:form4.xml", "_source": {"form": "4", "ciks": ["0000001234"], "file_date": str(TODAY)}}
                    for a in self.accs]
            body = json.dumps({"hits": {"hits": hits, "total": {"value": len(hits)}}})
        elif "daily-index" in url:
            day = url.rsplit("form.", 1)[1][:8]
            if day not in self.index_days:
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            body = "\n".join(f"4          ABC CORP  1234  {day}  edgar/data/1234/{a}.txt" for a in self.accs)
        elif url.endswith(".xml"):
            body = XML
        else:                                    # the filing's directory listing
            body = f'<a href="{url.replace("https://www.sec.gov", "")}form4.xml">form4.xml</a>'
        return io.BytesIO(body.encode())


@pytest.fixture
def panel(tmp_path, monkeypatch):
    db = tmp_path / "panel.db"
    PanelStore(db).close()
    monkeypatch.setattr(f4, "PanelStore", Tracked)
    Tracked.open = 0
    return db


def runs(db):
    with PanelStore(db, read_only=True) as s:
        return s.con.execute("SELECT source, status, n_requests, n_failed, n_items, n_rows, reason "
                             "FROM fetch_runs ORDER BY started_at").fetchall()


def crawl(db, fake, **kw):
    return f4.crawl(path=db, http=f4.Http("test", opener=fake, pause=0), today=TODAY, **kw)


def test_realtime_writes_rows_and_an_ok_run_with_no_connection_open_during_downloads(panel):
    fake = FakeSec(accs=["0000001234-26-000001", "0000001234-26-000002"])
    st = crawl(panel, fake, days=2, realtime=True)
    assert st["status"] == "ok" and st["filings"] == 2 and st["rows"] == 2
    assert runs(panel) == [("form4_realtime", "ok", 5, 0, 2, 2, None)]
    with PanelStore(panel, read_only=True) as s:
        assert s.con.execute("SELECT ticker, shares, price, source FROM insider_tx").fetchall() == \
            [("ABC", 1000.0, 12.5, "edgar_realtime")] * 2
    st2 = crawl(panel, FakeSec(accs=["0000001234-26-000001"]), days=2, realtime=True)
    assert st2["skipped_known"] == 1 and st2["filings"] == 0 and st2["status"] == "ok"


def test_no_filings_is_ok_and_a_failed_search_is_failed(panel):
    assert crawl(panel, FakeSec(), days=2, realtime=True)["status"] == "ok"
    st = crawl(panel, FakeSec(accs=["0000001234-26-000001"], down=["efts.sec.gov"]), days=2, realtime=True)
    assert st["status"] == "failed" and st["filings"] == 0
    got = runs(panel)
    assert [(r[1], r[4]) for r in got] == [("ok", 0), ("failed", 0)]       # same zero filings, told apart
    assert "efts search" in got[1][6] and "URLError" in got[1][6]


def test_filings_that_cannot_be_fetched_make_the_run_partial_then_failed(panel):
    accs = ["0000001234-26-000001", "0000001234-26-000002", "0000001234-26-000003"]
    st = crawl(panel, FakeSec(accs=accs, down=["000123426000003/"]), days=1, realtime=True)
    assert st["status"] == "partial" and st["rows"] == 2 and st["filings_not_fetched"] == 1
    st = crawl(panel, FakeSec(accs=accs, down=["000123426000003/", "000123426000001/", "000123426000002/"]),
               days=1, realtime=True)
    assert st["status"] == "failed" and "1 of 1 filings not fetched" in st["reason"]


def test_daily_index_not_yet_published_is_not_a_failure_but_a_server_error_is(panel):
    fake = FakeSec(accs=["0000001234-26-000009"], index_days=["20260928"])       # today's index is not out at 06:00
    st = crawl(panel, fake, days=2)
    assert st["status"] == "ok" and st["rows"] == 1
    assert runs(panel)[-1][:2] == ("form4_daily", "ok")
    st = crawl(panel, FakeSec(index_days=["20260928"], down=["form.20260928"]), days=2)
    assert st["status"] == "failed" and "daily index 2026-09-28" in st["reason"]


def test_main_prints_a_start_marker_and_exits_1_on_a_failed_run(panel, monkeypatch, capsys):
    monkeypatch.setattr(f4, "crawl", lambda *a: {"status": "failed", "reason": "x"})
    monkeypatch.setattr("sys.argv", ["sec_daily_form4", "--realtime"])
    assert f4.main() == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("=== form4 realtime ") and out[0].endswith(" ===")


# ---- sec_13d and av_news: same shape (fetch with panel.db closed, write once, one fetch_runs row) ----

from agent.sources import av_news, sec_13d                                # noqa: E402


def hit13d(acc, name="ACME CORP  (ACME)  (CIK 0000005555)"):
    return {"_id": f"{acc}:d.xml", "_source": {"display_names": [name, "Fund LP  (CIK 0000009999)"],
                                               "form": "SCHEDULE 13D", "file_date": "2026-09-28"}}


def test_13d_update_fetches_with_panel_closed_and_logs_a_failed_page(panel, monkeypatch):
    monkeypatch.setattr(sec_13d, "PanelStore", Tracked)
    monkeypatch.setattr(sec_13d, "user_agent", lambda: "test")
    monkeypatch.setattr(sec_13d, "PAUSE", 0)
    down = set()

    def fake_get(params, ua):
        assert Tracked.open == 0, "panel.db connection held during a download"
        if params["forms"] in down:
            return None
        hits = [hit13d("0000009999-26-000001")] if params["forms"] == "SCHEDULE 13D" else []
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}}

    monkeypatch.setattr(sec_13d, "_get_json", fake_get)
    st = sec_13d.load(dt.date(2026, 9, 24), TODAY, quiet=True, path=panel)
    assert st["status"] == "ok" and st["rows"] == 1 and st["initial"] == 1
    down.add("SC 13D")
    st = sec_13d.load(dt.date(2026, 9, 24), TODAY, quiet=True, path=panel)
    assert st["status"] == "failed" and "SC 13D" in st["reason"] and st["rows"] == 1     # what came back is kept
    assert [(r[0], r[1], r[2], r[3]) for r in runs(panel)] == [("sch13d", "ok", 2, 0), ("sch13d", "failed", 2, 1)]
    with PanelStore(panel, read_only=True) as s:
        assert s.con.execute("SELECT ticker, subject_cik FROM sch13d").fetchall() == [("ACME", "0000005555")]


def test_av_news_fetches_with_panel_closed_and_logs_an_error(panel, monkeypatch, tmp_path):
    monkeypatch.setattr(av_news, "PanelStore", Tracked)
    monkeypatch.setattr(av_news, "QUOTA_PATH", tmp_path / "av_quota.json")
    art = {"url": "https://x/1", "time_published": "20260929T100000", "overall_sentiment_score": 0.2,
           "source_domain": "x", "ticker_sentiment": [{"ticker": "AAPL", "relevance_score": "0.5",
                                                       "ticker_sentiment_score": "0.3"}]}

    def ok(frm, to):
        assert Tracked.open == 0, "panel.db connection held during a download"
        return {"feed": [art]}

    st = av_news.crawl(hours=24, max_calls=2, path=panel, fetch_fn=ok)
    assert st["status"] == "ok" and st["rows"] == 1 and st["articles"] == 1

    def throttled(frm, to):
        raise RuntimeError("{'Information': 'rate limit'}")

    st = av_news.crawl(hours=24, max_calls=2, path=panel, fetch_fn=throttled)
    assert st["status"] == "failed" and "rate limit" in st["reason"]
    assert [(r[0], r[1], r[4], r[5]) for r in runs(panel)] == [("av_news", "ok", 1, 1), ("av_news", "failed", 0, 0)]
    with PanelStore(panel, read_only=True) as s:
        assert s.con.execute("SELECT n_articles, n_rows FROM news_fetch_log").fetchall() == [(1, 1)]


# ---- daily_archive: fractionable in the borrow archive ----

import duckdb                                                                # noqa: E402

from agent.sources import daily_archive                                      # noqa: E402

OLD_BORROW = """CREATE TABLE borrow (day DATE, ticker VARCHAR, shortable BOOLEAN, easy_to_borrow BOOLEAN, marginable BOOLEAN,
       PRIMARY KEY (day, ticker))"""


def test_borrow_gains_fractionable_on_an_existing_archive_and_old_rows_stay_null(tmp_path):
    con = duckdb.connect(str(tmp_path / "archive.db"))
    con.execute(OLD_BORROW)
    con.execute("INSERT INTO borrow VALUES ('2026-09-28', 'MU', TRUE, TRUE, TRUE)")
    daily_archive.ensure_schema(con)
    daily_archive.ensure_schema(con)                                        # idempotent
    assert [r[0] for r in con.execute("DESCRIBE borrow").fetchall()] == daily_archive.BORROW_COLS
    assets = [{"symbol": "MU", "tradable": True, "shortable": True, "easy_to_borrow": True, "marginable": True, "fractionable": True},
              {"symbol": "XYZW", "tradable": True, "shortable": False, "easy_to_borrow": False, "marginable": False, "fractionable": False},
              {"symbol": "OLD", "tradable": True, "shortable": True},                                 # flag not in the answer
              {"symbol": "HALT", "tradable": False, "fractionable": True}]
    assert daily_archive.archive_borrow(con, assets) == 3
    got = con.execute("SELECT CAST(day AS VARCHAR), ticker, shortable, fractionable FROM borrow ORDER BY day, ticker").fetchall()
    today = str(dt.date.today())
    assert got == [("2026-09-28", "MU", True, None), (today, "MU", True, True), (today, "OLD", True, None),
                   (today, "XYZW", False, False)]
    con.close()


def test_a_new_archive_has_the_same_column_order():
    con = duckdb.connect(":memory:")
    daily_archive.ensure_schema(con)
    assert [r[0] for r in con.execute("DESCRIBE borrow").fetchall()] == daily_archive.BORROW_COLS
