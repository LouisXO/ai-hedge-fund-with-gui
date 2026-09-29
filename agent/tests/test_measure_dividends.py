"""S48 measurement: dividends from adjustment-factor steps, regulatory fees, the retried-missed marker."""
import datetime as dt

import pandas as pd
import pytest

from agent import auction_basis, dividends, ledger

D = dt.date
T0 = pd.Timestamp("2026-09-29 13:30")


def _px(ticker, rows, source="alpaca", fetched=T0):
    """rows: [(day, close, adj_close)]"""
    return pd.DataFrame([{"ticker": ticker, "trade_date": pd.Timestamp(d), "close": c, "adj_close": a,
                          "source": source, "fetched_at": fetched} for d, c, a in rows])


def test_dividend_is_read_from_the_factor_step():
    # PSEC-like: 0.035 on a 2.19 close; the factor before the ex-date is (1 - 0.035/2.19) of the one after
    f = 1 - 0.035 / 2.19
    px = _px("PSEC", [(D(2026, 9, 24), 2.20, 2.20 * f), (D(2026, 9, 25), 2.19, 2.19 * f), (D(2026, 9, 28), 2.13, 2.13)])
    ev = dividends.factor_events(px)
    assert list(ev["kind"]) == ["dividend"] and ev["ex_date"].iloc[0] == D(2026, 9, 28)
    assert ev["per_share"].iloc[0] == pytest.approx(0.035, abs=1e-4)


def test_rounding_noise_split_and_basis_break_are_not_dividends():
    # two-decimal adj_close wobble on a $14 stock: inside the tolerance, no event
    noise = _px("BZ", [(D(2026, 9, 17), 14.84, 14.30), (D(2026, 9, 18), 14.80, 14.27)])
    assert dividends.factor_events(noise).empty
    # 25:1 reverse split: the factor jumps up; a 2:1 forward split halves it -> both flagged, never money
    rev = _px("NCT", [(D(2026, 9, 16), 0.352, 8.80), (D(2026, 9, 17), 8.575, 8.575)])
    fwd = _px("XYZ", [(D(2026, 9, 16), 100.0, 50.0), (D(2026, 9, 17), 50.5, 50.5)])
    ev = dividends.factor_events(pd.concat([rev, fwd]))
    assert set(ev["kind"]) == {"split_or_special"} and (ev["per_share"] == 0).all()
    # an older row on its own fetch day's basis (the vendor adjusted since): a break, not an ex-date
    brk = pd.concat([_px("ADAM", [(D(2026, 9, 14), 8.90, 8.90)], fetched=T0 - pd.Timedelta(days=5)),
                     _px("ADAM", [(D(2026, 9, 15), 8.92, 8.62)])])
    ev = dividends.factor_events(brk)
    assert list(ev["kind"]) == ["basis_break"] and ev["per_share"].iloc[0] == 0


def test_entitlement_needs_entry_before_and_holding_through_the_ex_date():
    ev = pd.DataFrame([{"ticker": "A", "ex_date": D(2026, 9, 28), "per_share": 0.5, "kind": "dividend"}])
    lots = pd.DataFrame([
        {"lot_id": "on", "book": "long", "ticker": "A", "qty": 100, "entry_day": D(2026, 9, 22), "exit_day": None},       # held: yes
        {"lot_id": "same", "book": "long", "ticker": "A", "qty": 100, "entry_day": D(2026, 9, 28), "exit_day": None},     # bought on the ex-date: no
        {"lot_id": "sold", "book": "long", "ticker": "A", "qty": 100, "entry_day": D(2026, 9, 22), "exit_day": D(2026, 9, 25)},  # sold before: no
        {"lot_id": "exd", "book": "insider", "ticker": "A", "qty": 10, "entry_day": D(2026, 9, 22), "exit_day": D(2026, 9, 28)},  # sold at the ex-date open: yes
    ])
    r = dividends.entitled(lots, ev)
    assert sorted(r["lot_id"]) == ["exd", "on"]
    assert r.set_index("lot_id")["usd"].to_dict() == {"on": 50.0, "exd": 5.0}
    cum = dividends.cumulative(r, [D(2026, 9, 25), D(2026, 9, 28)], "long")
    assert cum == {D(2026, 9, 25): 0.0, D(2026, 9, 28): 50.0}


def test_fees_sell_side_sec_and_taf_cap_cat_both_sides_rounded_up_per_day():
    fills = pd.DataFrame([
        {"book": "long", "d": D(2026, 9, 30), "side": "sell", "filled_qty": 100_000, "filled_avg_px": 10.0},   # TAF 19.5 -> capped 9.79
        {"book": "long", "d": D(2026, 9, 30), "side": "sell", "filled_qty": 10, "filled_avg_px": 10.0},
        {"book": "long", "d": D(2026, 9, 30), "side": "buy", "filled_qty": 1000, "filled_avg_px": 5.0},
        {"book": "insider", "d": D(2026, 9, 30), "side": "buy", "filled_qty": 1, "filled_avg_px": 5.0},
    ])
    f = auction_basis.fees_by_day(fills)
    sec = auction_basis.ceil_cent(1_000_100 * 0.0000206)                 # 20.60206 -> 20.61
    taf = auction_basis.ceil_cent(9.79 + 10 * 0.000195)                  # 9.79195 -> 9.80
    cat = auction_basis.ceil_cent(101_010 * 0.000003)                    # 0.30303 -> 0.31
    assert (sec, taf, cat) == (20.61, 9.80, 0.31)
    assert f[("long", D(2026, 9, 30))] == pytest.approx(sec + taf + cat)
    assert f[("insider", D(2026, 9, 30))] == pytest.approx(0.01)          # a tiny buy still pays a cent of CAT
    assert auction_basis.ceil_cent(0.07) == 0.07 and auction_basis.ceil_cent(0.0) == 0.0


def test_nav_rows_keep_price_and_total_return_and_net_fees():
    nav = pd.DataFrame([{"as_of": D(2026, 9, 29), "book": "long", "equity_usd": 1000.0},
                        {"as_of": D(2026, 9, 30), "book": "long", "equity_usd": 1010.0}])
    fills = pd.DataFrame([{"book": "long", "d": D(2026, 9, 30), "adj": 5.0}])
    div = pd.DataFrame([{"book": "long", "kind": "dividend", "ex_date": D(2026, 9, 30), "usd": 2.0}])
    rows = auction_basis.nav_rows(nav, fills, {("long", D(2026, 9, 30)): 0.03}, div)
    r = dict(zip(auction_basis.NAV_COLS, rows[-1]))
    assert r["equity_auction"] == pytest.approx(1010 + 5 - 0.03) and r["div_cum"] == 2.0
    assert r["equity_auction_tr"] == pytest.approx(r["equity_auction"] + 2.0)
    assert dict(zip(auction_basis.NAV_COLS, rows[0]))["div_cum"] == 0.0


def test_missed_entry_retried_and_filled_within_8_days_is_marked():
    missed = pd.DataFrame([{"ticker": "INBX", "as_of": D(2026, 9, 23)}, {"ticker": "GLP", "as_of": D(2026, 9, 23)},
                           {"ticker": "OLD", "as_of": D(2026, 9, 1)}])
    bought = pd.DataFrame([{"ticker": "INBX", "as_of": D(2026, 9, 25)}, {"ticker": "OLD", "as_of": D(2026, 9, 20)}])
    assert auction_basis.mark_retried(missed, bought) == [True, False, False]


def test_auction_nav_table_gains_the_new_columns(tmp_path):
    con = ledger.connect(str(tmp_path / "t.db"))
    con.execute(auction_basis.DDL)                                       # a ledger from before S48
    con.execute("INSERT INTO agent_auction_nav VALUES ('2026-09-25', 'long', 1.0, 0.0, 1.0, 0)")
    auction_basis.ensure_table(con)
    auction_basis.ensure_table(con)                                      # idempotent
    cols = [r[0] for r in con.execute("DESCRIBE agent_auction_nav").fetchall()]
    assert cols == auction_basis.NAV_COLS
    con.close()


def test_a_failed_rewrite_keeps_the_previous_dividend_rows(tmp_path, monkeypatch):
    con = ledger.connect(str(tmp_path / "t.db"))
    con.execute(dividends.DDL)
    con.execute("INSERT INTO agent_dividends VALUES ('L1', 'long', 'X', '2026-09-01', 0.1, 10, 1.0, 'dividend', '2026-09-28 13:30')")
    dup = pd.DataFrame([{"lot_id": "L2", "book": "long", "ticker": "Y", "ex_date": D(2026, 9, 2), "per_share": 0.2, "qty": 5.0,
                         "usd": 1.0, "kind": "dividend", "computed_at": T0}] * 2)          # same key twice: the insert fails
    monkeypatch.setattr(dividends, "compute", lambda con, store: (dup, None))
    with pytest.raises(Exception):
        dividends.update(con, None)
    assert con.execute("SELECT lot_id FROM agent_dividends").fetchall() == [("L1",)]
    con.close()


def test_a_dividend_failure_does_not_stop_the_auction_basis(monkeypatch):
    class Store:
        def __init__(self, read_only=True):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def boom(con, store):
        raise RuntimeError("panel locked")
    monkeypatch.setattr(auction_basis, "PanelStore", Store)
    monkeypatch.setattr(auction_basis.dividends, "update", boom)
    div, err = auction_basis.soft_dividends(None)
    assert err == "RuntimeError: panel locked"
    assert div.empty and list(div.columns) == auction_basis.DIV_COLS
    rows = auction_basis.nav_rows(pd.DataFrame([{"as_of": D(2026, 9, 25), "book": "long", "equity_usd": 100.0}]),
                                  pd.DataFrame(columns=["book", "d", "adj"]), {}, div)
    assert rows and rows[0][auction_basis.NAV_COLS.index("div_cum")] == 0.0
