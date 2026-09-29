"""S48 drift monitor: per-book fill rate and execution gap, the insider book's orders vs its rule; capital from the ledger."""
import datetime as dt

import pandas as pd
import pytest

from agent import drift, ledger, shadow_v2

D = dt.date


def test_fill_rate_is_split_by_book():
    orders = pd.DataFrame([{"as_of": D(2026, 9, 30), "book": "long", "filled": True, "status": "filled"},
                           {"as_of": D(2026, 9, 30), "book": "insider", "filled": False, "status": "expired"},
                           {"as_of": D(2026, 9, 30), "book": "insider", "filled": True, "status": "filled"},
                           {"as_of": D(2026, 9, 30), "book": "insider", "filled": False, "status": "accepted"}])   # still working: not counted
    c = {x["book"]: x for x in drift.fill_checks(orders)}
    assert set(c) == {"long", "insider"}
    assert c["long"]["level"] == "ok" and c["insider"]["rate"] == 0.5 and c["insider"]["level"] == "warn"
    assert "内部人" in c["insider"]["name"]


def test_execution_gap_per_book_keeps_the_sign_auction_basis_gave():
    rows = [{"book": "long", "side": "sell", "day": "2026-09-30", "gap_pct": 0.4},      # sold 0.4% below the cross: worse
            {"book": "long", "side": "buy", "day": "2026-09-30", "gap_pct": 0.2},
            {"book": "insider", "side": "buy", "day": "2026-09-30", "gap_pct": None}]   # a gap fill: no cross comparison
    c = drift.gap_checks(rows)
    assert [x["book"] for x in c] == ["long"]
    assert c[0]["mean_pct"] == pytest.approx(0.3)                                     # not (0.2 - 0.4) / 2


def test_insider_orders_vs_rule():
    ok = drift.insider_vs_rule({"A", "B"}, ["A", "B", "C"], 2, "2026-09-30")
    assert ok["level"] == "ok"                                                         # C: no free slot
    miss = drift.insider_vs_rule({"A"}, ["A", "B", "C"], 5, "2026-09-30")
    assert miss["level"] == "warn" and miss["missing"] == ["B", "C"]
    bad = drift.insider_vs_rule({"A", "Z"}, ["A"], 5, "2026-09-30")
    assert bad["level"] == "bad" and bad["extra"] == ["Z"]


def test_shadow_capital_comes_from_the_ledger(tmp_path):
    con = ledger.connect(str(tmp_path / "t.db"))
    ledger.ensure_schema(con)
    with pytest.raises(RuntimeError):
        shadow_v2.long_capital(con)
    con.execute("INSERT INTO agent_books VALUES ('long', 45000, 45000, 30, '2026-09-21', now())")
    assert shadow_v2.long_capital(con) == 45000.0
    con.close()


def test_the_ledger_is_closed_before_the_replay_starts(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    con = ledger.connect(path)
    ledger.ensure_schema(con)
    con.execute("INSERT INTO agent_books VALUES ('long', 60000, 60000, 30, '2026-09-21', now())")
    con.execute("CREATE TABLE agent_auction_nav (as_of DATE, book VARCHAR, equity_sim DOUBLE, adj_cum DOUBLE, equity_auction DOUBLE, n_fills INT)")
    con.close()
    opened = []

    class Conn:
        def __init__(self, p, read_only=True):
            import duckdb
            self.c, self.closed = duckdb.connect(p, read_only=read_only), False
            opened.append(self)

        def execute(self, *a):
            return self.c.execute(*a)

        def close(self):
            self.closed = True
            self.c.close()

    class Stop(Exception):
        pass

    class Store:
        def __init__(self, read_only=True):
            pass

        def __enter__(self):
            assert opened and all(c.closed for c in opened)       # the replay and the list recomputation hold no ledger lock
            raise Stop

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(drift, "duckdb", type("M", (), {"connect": staticmethod(lambda p, read_only=True: Conn(p, read_only))}))
    monkeypatch.setattr(drift, "PanelStore", Store)
    with pytest.raises(Stop):
        drift.main(["--db", path, "--out", str(tmp_path / "d.json"), "--auction", str(tmp_path / "a.json")])
