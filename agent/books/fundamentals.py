"""Point-in-time factor inputs from XBRL facts.

Turns panel.xbrl_facts into, for every (ticker, filing date), the trailing
twelve-month flows and the latest balance-sheet instants that were public
as of that filing. A factor on day D then takes each name's most recent row
with filed <= D — never the row for the period that ends before D, which
is the classic look-ahead (a Q4 filed in late February is not known on
January 31).

Quarterly flows: facts with a ~3-month duration are used directly. Items
disclosed year-to-date (the cash flow statement: 3, 6, 9, 12 months) are
differenced inside the fiscal year: Q2 = 6M - Q1, Q3 = 9M - 6M,
Q4 = FY - 9M. A TTM is the sum of four quarters that together cover one
year (330-400 days). Companies with no interim facts (20-F/40-F) take the
latest annual value as the TTM.

Shares: the cover-page count (dei), the balance-sheet count and the
weighted average diluted count are three reports of one number. When they
disagree by more than 1.5x the count is not usable: it is left missing and
the name goes to the review list (review_path), to be settled by hand in
shares_override. Nothing is carried into a filing from a period that ended
more than 400 days before it.

Tag fallbacks: revenue and cost tags changed with ASC 606 (2018); shares
from dei when us-gaap is missing. Equity is the parent's only.

Data corrections of 2026-09-28: docs/AGENT_PLAN.md S47.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from hedge_fund.features.panel import PanelStore

FLOW_TAGS = {
    "ni": ["NetIncomeLoss"],
    "rev": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "gp": ["GrossProfit"],
    "cogs": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "cogs_g": ["CostOfGoodsSold"],          # companies that split goods and services (VZ, JNJ...): summed below
    "cogs_s": ["CostOfServices"],
    "opinc": ["OperatingIncomeLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
}
SHARES_FLOW_TAGS = ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"]
INSTANT_TAGS = {
    "assets": ["Assets"],
    # Was: falling back to StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest, which put
    # XIFR's book value at $10.7B against ~$3B attributable to the parent. A company that reports only that
    # tag has no equity here.
    "equity": ["StockholdersEquity"],
    "debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "shares_dei": ["EntityCommonStockSharesOutstanding"],
    "shares_bs": ["CommonStockSharesOutstanding"],
}
# period lengths in days; 12- and 16-week fiscal quarters (KR, PEP, COST) are 84 and 112
QUARTER, HALF, NINE_MONTHS, YEAR = (75, 125), (160, 205), (245, 290), (350, 380)
TTM_SPAN = (330, 400)            # first quarter's start to last quarter's end
MAX_FACT_AGE_DAYS = 400          # period end to the filing a value is carried into
SHARES_MISMATCH = 1.5            # largest / smallest of the share counts known at one filing
MIN_SHARES = 1000                # 0 / 1 / negative counts are XBRL noise (FOX, HOOD, EL...)
ANNUAL_FORMS = ("20-F", "40-F")
DAY = pd.Timedelta(days=1)
REVIEW_WINDOW_DAYS = 200         # = factors.latest_before: older rows are not scored, so not worth a review
SHARE_SOURCES = ["shares_dei", "shares_bs", "shares_w"]
REVIEW_ONLY = [c for s in SHARE_SOURCES for c in (s, f"{s}_asof")] + ["shares_w_form"]

# TODO(S47, not done): a split between the filing date and the scoring day. factors.factor_scores takes
# mcap = raw close on the day x shares from the latest filing, so from the split date until the next
# 10-Q/10-K the market cap is off by the split ratio (NVDA 10:1 on 2024-06-10: 2.46B shares until the
# 2024-08-28 filing, mcap $300B instead of $2.97T; AMZN, GOOG and CMG reached the top 30 this way).
# The ratio must come from a corporate-actions source; adj/close in the panel is not usable for this
# until the adjusted series is repaired.


def _first_available(df: pd.DataFrame, tags: list[str]) -> pd.DataFrame:
    """Rows from all listed tags, one per period: the earliest tag in preference order that has it.

    Was: the first tag the company ever reported. That froze META's revenue at 2018 (it used
    `Revenues` until then, `RevenueFromContract...` after) and did the same for every company
    that changed tags — found 2026-09-22 when scoring META by hand."""
    sub = df[df["tag"].isin(tags)].copy()
    if sub.empty:
        return sub
    sub["_pref"] = sub["tag"].map({t: i for i, t in enumerate(tags)})
    sub = sub.sort_values(["_pref", "filed"], kind="stable")
    return sub.drop_duplicates(["cik", "period_start", "period_end", "filed"], keep="first").drop(columns="_pref")


def _between(days: pd.Series, *spans: tuple[int, int]) -> pd.Series:
    ok = pd.Series(False, index=days.index)
    for lo, hi in spans:
        ok |= days.between(lo, hi)
    return ok


def _periods(facts: pd.DataFrame, tags: list[str]) -> pd.DataFrame:
    """Facts covering a quarter, half, nine months or a year, as first filed; dates as timestamps."""
    f = _first_available(facts, tags)
    for c in ("period_start", "period_end", "filed"):
        f[c] = pd.to_datetime(f[c]).astype("datetime64[ns]")
    f["days"] = (f["period_end"] - f["period_start"]).dt.days
    f = f[_between(f["days"], QUARTER, HALF, NINE_MONTHS, YEAR)]
    # the value a period is known by: the earliest filing that reported it
    return f.sort_values("filed", kind="stable").drop_duplicates(["cik", "period_start", "period_end"], keep="first")


def _annual(f: pd.DataFrame) -> pd.DataFrame:
    a = f[f["days"].between(*YEAR)]
    return a.sort_values("filed", kind="stable").drop_duplicates(["cik", "period_end"], keep="first")


def _earliest(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (cik, period_end): the version that was known first."""
    return df.sort_values(["filed", "_how"], kind="stable").drop_duplicates(["cik", "period_end"], keep="first")


def _adjacent(left: pd.DataFrame, sq: pd.DataFrame, k: int, after: str | None = None, before: str | None = None) -> pd.DataFrame:
    """Attach as end/start/filed/val<k> the quarter that starts the day after left[after], or ends the day before left[before].

    Quarters are matched by their dates, never by their position in a sorted list: a quarter that
    is filed later must not change which four made up a year that was already known."""
    right = sq[["cik", "period_end", "q_start", "filed", "val"]]
    right = right.rename(columns={"period_end": f"end{k}", "q_start": f"start{k}", "filed": f"filed{k}", "val": f"val{k}"})
    if after is not None:
        return left.assign(**{f"start{k}": left[after] + DAY}).merge(right, on=["cik", f"start{k}"])
    return left.assign(**{f"end{k}": left[before] - DAY}).merge(right, on=["cik", f"end{k}"])


def _single_quarters(f: pd.DataFrame) -> pd.DataFrame:
    """(cik, period_end) -> val of that quarter alone, the filing that made it known, the quarter's first day."""
    cols = ["cik", "period_end", "filed", "val", "q_start", "_how"]
    q = f[f["days"].between(*QUARTER)]
    direct = q.assign(q_start=q["period_start"], _how=0)[cols]
    # Year-to-date facts start on the same day; the later minus the earlier is the quarter between them.
    # Was: only ~3-month facts were kept, and the cash flow statement has one per year (Q1), so
    # rolling(4) added the first quarters of four different years: AAPL's cfo_ttm read $150.3B
    # on every 2023 filing, fiscal 2023 was $110.5B.
    pair = f[f["days"] >= HALF[0]].merge(f[f["days"] <= NINE_MONTHS[1]], on=["cik", "period_start"], suffixes=("", "_prev"))
    pair = pair[(pair["period_end"] - pair["period_end_prev"]).dt.days.between(*QUARTER)]
    diff = pd.DataFrame({"cik": pair["cik"], "period_end": pair["period_end"],
                         "filed": pair[["filed", "filed_prev"]].max(axis=1), "val": pair["val"] - pair["val_prev"],
                         "q_start": pair["period_end_prev"] + DAY,
                         "_how": np.where(pair["days"] >= YEAR[0], 3, 1)})[cols]
    sq = _earliest(pd.concat([direct, diff[diff["_how"] == 1]], ignore_index=True))
    # Q4 = annual - the three quarters that fill the fiscal year up to it (all must already be known), so
    # that the four add up to the year as reported even when a quarter's own fact disagrees with the
    # year-to-date ones (UHAL 2025-12: +$37.0M for the quarter, nine months less six months is -$37.0M).
    # Annual - nine months only when the three are not all there.
    y = _annual(f)[["cik", "period_start", "period_end", "filed", "val"]]
    y = _adjacent(y.assign(_day_before=y["period_start"] - DAY), sq, 1, after="_day_before")
    y = _adjacent(_adjacent(y, sq, 2, after="end1"), sq, 3, after="end2")
    y = y[(y["period_end"] - y["end3"]).dt.days.between(*QUARTER)]
    q4 = pd.DataFrame({"cik": y["cik"], "period_end": y["period_end"],
                       "filed": y[["filed", "filed1", "filed2", "filed3"]].max(axis=1),
                       "val": y["val"] - y["val1"] - y["val2"] - y["val3"], "q_start": y["end3"] + DAY, "_how": 2})
    sq = _earliest(pd.concat([sq, q4, diff[diff["_how"] == 3]], ignore_index=True))
    return sq.sort_values(["cik", "period_end"]).drop(columns="_how").reset_index(drop=True)


def quarterly_flows(facts: pd.DataFrame, name: str, tags: list[str]) -> pd.DataFrame:
    """(cik, period_end, filed) -> quarterly value; quarters not reported alone are derived (see _single_quarters)."""
    f = _periods(facts, tags)
    if f.empty:
        return pd.DataFrame(columns=["cik", "period_end", "filed", name])
    out = _single_quarters(f).rename(columns={"val": name})[["cik", "period_end", "filed", name]]
    if facts["period_end"].dtype == object:             # callers that pass dates get dates back
        for c in ("period_end", "filed"):
            out[c] = out[c].dt.date
    return out


def ttm_flows(facts: pd.DataFrame, name: str, tags: list[str]) -> pd.DataFrame:
    """(cik, period_end, filed) -> trailing twelve months, known when the last piece of it was filed."""
    col = f"{name}_ttm"
    f = _periods(facts, tags)
    if f.empty:
        return pd.DataFrame(columns=["cik", "period_end", "filed", col])
    sq = _single_quarters(f)
    # Four quarters are a year only when each starts the day after the one before ends. Was:
    # rolling(4) over whatever rows there were, so a gap in the filings (or one fact per year)
    # summed quarters of different years.
    t = _adjacent(sq.rename(columns={"q_start": "start0"}), sq, 1, before="start0")
    t = _adjacent(_adjacent(t, sq, 2, before="start1"), sq, 3, before="start2")
    t = t[((t["period_end"] - t["start3"]).dt.days + 1).between(*TTM_SPAN)]
    # a TTM value is known when the last of its four quarters was filed
    rolled = pd.DataFrame({"cik": t["cik"], "period_end": t["period_end"],
                           "filed": t[["filed", "filed1", "filed2", "filed3"]].max(axis=1),
                           col: t["val"] + t["val1"] + t["val2"] + t["val3"], "_how": 0})
    # Annual filers: the year as reported is the TTM. Was: annual / 4 into rolling(4), i.e. the mean
    # of the last four years, available only after four annual reports (PERI $55.3M, 2025 was -$7.9M).
    # An annual filer is a company with no shorter period of this year on file when the annual report came in.
    part = f[f["days"] < YEAR[0]].sort_values("filed", kind="stable")
    part = part.assign(part_end=part.groupby("cik")["period_end"].cummax())[["cik", "filed", "part_end"]]
    a = pd.merge_asof(_annual(f).sort_values("filed", kind="stable"), part, on="filed", by="cik")
    a = a[a["part_end"].isna() | (a["part_end"] <= a["period_start"])].rename(columns={"val": col}).assign(_how=1)
    out = _earliest(pd.concat([rolled, a[["cik", "period_end", "filed", col, "_how"]]], ignore_index=True))
    return out.drop(columns="_how")


def latest_instants(facts: pd.DataFrame, name: str, tags: list[str]) -> pd.DataFrame:
    f = _first_available(facts[facts["is_instant"]], tags)
    if f.empty:
        return pd.DataFrame(columns=["cik", "period_end", "filed", name])
    f = f.sort_values("filed", kind="stable").drop_duplicates(["cik", "period_end"], keep="first")
    return f[["cik", "period_end", "filed", "val"]].rename(columns={"val": name})


def weighted_shares(facts: pd.DataFrame) -> pd.DataFrame:
    """(cik, period_end, filed) -> weighted average diluted shares as reported, and the form that reported it.

    Was: through quarterly_flows, which is for flows. A 10-K's count became annual minus three
    quarters (META -5.22B, F -8.04B, MA -1.86B shares, so no market cap from February to May)
    and a 20-F's count was divided by 4 (MOMO, TAL, BEKE, XPEV ... at exactly 0.25x)."""
    f = _periods(facts, SHARES_FLOW_TAGS)
    f = f[f["val"] > MIN_SHARES]
    if f.empty:
        return pd.DataFrame(columns=["cik", "period_end", "filed", "shares_w", "shares_w_form"])
    # per period end: the earliest filing, and in it the shortest period (a quarter's average is nearer
    # to the count today than the year's)
    f = f.sort_values(["filed", "days"], kind="stable").drop_duplicates(["cik", "period_end"], keep="first")
    return f[["cik", "period_end", "filed", "val", "form"]].rename(columns={"val": "shares_w", "form": "shares_w_form"})


def _carry(grid: pd.DataFrame, item: pd.DataFrame, col: str, extra: tuple = ()) -> pd.DataFrame:
    """`col` as known at each filing of `grid` (sorted by filed): its latest period filed on or before.

    Was: ffill without a limit, so an item a company stopped reporting kept its last value for
    ever — UHAL's 2022 share count, BZ's 2019 one, EDRY's 2019-22 net income were all scored in
    2026. A value whose period ended more than MAX_FACT_AGE_DAYS before the filing is dropped;
    `<col>_asof` keeps the period end either way, so the review list can say how old it was."""
    asof = f"{col}_asof"
    if item.empty:
        return pd.DataFrame({col: np.nan, asof: pd.NaT, **{e: None for e in extra}}, index=grid.index)
    s = item.dropna(subset=[col]).sort_values(["cik", "filed", "period_end"], kind="stable")
    s = s[s["period_end"] >= s.groupby("cik")["period_end"].cummax()]      # a late filing for an older period replaces nothing
    s = s.drop_duplicates(["cik", "filed"], keep="last").rename(columns={"period_end": asof})
    got = pd.merge_asof(grid[["cik", "filed"]], s[["cik", "filed", col, asof, *extra]].sort_values("filed", kind="stable"),
                        on="filed", by="cik")
    old = (got["filed"] - got[asof]).dt.days > MAX_FACT_AGE_DAYS
    got.loc[old, [col, *extra]] = np.nan
    return got.drop(columns=["cik", "filed"])


def _usable_shares(out: pd.DataFrame) -> pd.DataFrame:
    """shares, shares_asof, shares_flag from the three counts known at each filing."""
    src = out[SHARE_SOURCES]
    # dual-class companies (META, V, MA, BRK.B, F, ...) report shares outstanding per class, which
    # companyfacts leaves out; the weighted average diluted count is undimensioned and stands in
    dei_newer = out["shares_bs"].isna() | (out["shares_dei"].notna() & (out["shares_dei_asof"] >= out["shares_bs_asof"]))
    instant = out["shares_dei"].where(dei_newer, out["shares_bs"])
    instant_asof = out["shares_dei_asof"].where(dei_newer, out["shares_bs_asof"]).where(instant.notna())
    shares = instant.fillna(out["shares_w"])
    shares_asof = instant_asof.fillna(out["shares_w_asof"].where(out["shares_w"].notna()))
    flag = pd.Series("", index=out.index)
    had_one = out[[f"{s}_asof" for s in SHARE_SOURCES]].notna().any(axis=1)
    flag[shares.isna() & had_one] = "stale"
    # price is per ADS, the count is ordinary shares, and the ratio between them is not in XBRL
    flag[instant.isna() & out["shares_w"].notna() & out["shares_w_form"].isin(ANNUAL_FORMS)] = "ads"
    # HG: cover page 65.9M, balance sheet 98.6M; MCHB: 18.9M from before the merger, 222M weighted
    flag[src.max(axis=1) > SHARES_MISMATCH * src.min(axis=1)] = "mismatch"
    unusable = flag.isin(["ads", "mismatch"])
    return out.assign(shares=shares.mask(unusable), shares_asof=shares_asof.mask(unusable), shares_flag=flag)


def build(store: PanelStore) -> pd.DataFrame:
    """One row per (cik, filed): TTM flows + latest instants known at that filing."""
    # ordered, and every sort below is stable: two facts that tie are always resolved the same way
    facts = store.con.execute("SELECT cik, tag, period_start, period_end, is_instant, val, form, filed "
                              "FROM xbrl_facts WHERE unit IN ('USD','shares') "
                              "ORDER BY cik, tag, period_start, period_end, filed, accn").df()
    for c in ("period_start", "period_end", "filed"):
        facts[c] = pd.to_datetime(facts[c]).astype("datetime64[ns]")
    # a period that ends after its own filing is a typing error (a 2033 cover date in a 2023 10-Q); carried
    # as "the latest" it would block every real value after it
    facts = facts[facts["period_end"] <= facts["filed"]]
    # cik -> ticker is point-in-time: 388 tickers have belonged to more than one company (SPACs,
    # renames, recycled symbols), so a filing is mapped to the ticker its CIK carried in that quarter
    # (issuer_seen), falling back to the CIK's latest ticker. Found by the 2026-09-22 audit.
    tick_pit = store.con.execute("""SELECT CAST(cik AS INT) AS cik, quarter, ticker, n_filings FROM issuer_seen
                                    WHERE ticker IN (SELECT DISTINCT ticker FROM bars)
                                    ORDER BY ticker, quarter, cik""").df()
    # Form 4 filers mistype symbols; per (ticker, quarter) the CIK with the most filings owns the symbol
    winner = (tick_pit.sort_values("n_filings", ascending=False, kind="stable")
              .drop_duplicates(["ticker", "quarter"])[["ticker", "quarter", "cik"]])
    winner = winner.rename(columns={"cik": "winner_cik"})
    tick_pit = tick_pit[["cik", "quarter", "ticker"]]
    tick = (tick_pit.sort_values("quarter", kind="stable").drop_duplicates("cik", keep="last")[["cik", "ticker"]]
            .rename(columns={"ticker": "ticker_latest"}))
    items = {f"{name}_ttm": ttm_flows(facts, name, tags) for name, tags in FLOW_TAGS.items()}
    parts = items.pop("cogs_g_ttm").merge(items.pop("cogs_s_ttm"), on=["cik", "period_end", "filed"], how="outer")
    parts["parts"] = parts[["cogs_g_ttm", "cogs_s_ttm"]].sum(axis=1, min_count=1)
    cogs = items["cogs_ttm"].merge(parts[["cik", "period_end", "filed", "parts"]], on=["cik", "period_end", "filed"], how="outer")
    items["cogs_ttm"] = cogs.assign(cogs_ttm=cogs["cogs_ttm"].fillna(cogs["parts"])).drop(columns="parts")
    instants = facts[facts["is_instant"]]
    for name, tags in INSTANT_TAGS.items():
        items[name] = latest_instants(instants, name, tags)
    for name in ("shares_dei", "shares_bs"):
        items[name] = items[name][items[name][name] > MIN_SHARES]
    items["shares_w"] = weighted_shares(facts)
    grid = pd.concat([df[["cik", "period_end", "filed"]] for df in items.values() if not df.empty], ignore_index=True)
    grid = grid.groupby(["cik", "filed"], as_index=False)["period_end"].max()
    grid = grid.sort_values(["filed", "cik"]).reset_index(drop=True)
    grid["cik"] = grid["cik"].astype("int64")
    cols = [grid[["cik", "period_end", "filed"]]]
    for col, df in items.items():
        df = df.astype({"cik": "int64"}) if not df.empty else df
        cols.append(_carry(grid, df, col, ("shares_w_form",) if col == "shares_w" else ()))
    out = pd.concat(cols, axis=1)
    out = out.drop(columns=[c for c in out.columns if c.endswith("_asof") and c not in REVIEW_ONLY])
    out = _usable_shares(out)
    out["quarter"] = out["filed"].dt.year.astype(str) + "q" + out["filed"].dt.quarter.astype(str)
    out = out.merge(tick_pit, on=["cik", "quarter"], how="left").merge(tick, on="cik", how="inner")
    out["ticker"] = out["ticker"].fillna(out["ticker_latest"])
    # A quarter with no Form 4 under the symbol keeps the owner of the last quarter that had one.
    # Was: no owner at all, so every CIK ever seen under the symbol kept it. issuer_seen ends a quarter
    # behind the newest filings, and GSBD's latest row came from a CIK seen under it once, in 2016
    # (14.9M shares instead of 112.6M); 10 liquid names on 2026-09-25.
    for df in (out, winner):
        df["qn"] = df["quarter"].str[:4].astype(int) * 4 + df["quarter"].str[-1].astype(int)
    out = pd.merge_asof(out.sort_values("qn", kind="stable"), winner.sort_values("qn", kind="stable")[["ticker", "qn", "winner_cik"]],
                        on="qn", by="ticker")
    out = out[out["winner_cik"].isna() | (out["winner_cik"] == out["cik"])]      # a losing CIK does not get the symbol
    out = out.drop(columns=["quarter", "qn", "ticker_latest", "winner_cik"])
    # if two CIKs still land on the same ticker on the same filing date, keep the larger balance sheet
    out = out.sort_values(["ticker", "filed", "assets", "cik"], kind="stable").drop_duplicates(["ticker", "filed"], keep="last")
    return out


def review_path(store: PanelStore) -> Path:
    """The review list: names whose share count could not be used. It lives next to panel.db."""
    return store.path.parent / "shares_review.csv"


def share_review(store: PanelStore, df: pd.DataFrame) -> pd.DataFrame:
    """Latest row of every name that would be scored today and has a flagged share count."""
    latest = df.sort_values("filed", kind="stable").drop_duplicates("ticker", keep="last")
    latest = latest[(latest["shares_flag"] != "") & (latest["filed"] >= df["filed"].max() - pd.Timedelta(days=REVIEW_WINDOW_DAYS))]
    try:
        ov = store.con.execute("SELECT ticker, shares FROM shares_override").df().set_index("ticker")["shares"]
    except Exception:
        ov = pd.Series(dtype=float)
    out = latest[["ticker", "cik", "filed", "shares_flag", *REVIEW_ONLY]].assign(override=latest["ticker"].map(ov).to_numpy())
    return out.sort_values("ticker").reset_index(drop=True)


def factor_inputs(store: PanelStore) -> pd.DataFrame:
    """Cached build; stores panel.fundamentals_pit and writes the share review list."""
    df = build(store)
    share_review(store, df).to_csv(review_path(store), index=False)
    df = df.drop(columns=REVIEW_ONLY)
    df["assets_1y"] = np.nan
    df = df.sort_values(["ticker", "filed"])
    # asset growth: assets vs the value known ~one year earlier
    out = []
    for t, g in df.groupby("ticker"):
        g = g.copy()
        past = g.set_index("filed")["assets"]
        g["assets_1y"] = [past[:f - pd.Timedelta(days=330)].iloc[-1] if len(past[:f - pd.Timedelta(days=330)]) else np.nan
                          for f in g["filed"]]
        out.append(g)
    df = pd.concat(out, ignore_index=True)
    store.con.execute("DROP TABLE IF EXISTS fundamentals_pit")
    store.con.register("_f", df)
    store.con.execute("CREATE TABLE fundamentals_pit AS SELECT * FROM _f")
    store.con.unregister("_f")
    return df
