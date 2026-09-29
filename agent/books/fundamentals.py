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
year (330-400 days); where that chain is broken, the latest annual value
plus this year's year-to-date less last year's; an annual report is a TTM
on its own (20-F/40-F filers, a company's first 10-K).

Shares: the cover-page count (dei), the balance-sheet count and the
weighted average diluted count are three reports of one number. Only
sources of the same period are compared (period ends within 100 days of
the newest). When two agree within 1.5x their value is used; when none
agree the count is not usable: it is left missing and the name goes to the
review list (review_path), to be settled by hand in shares_override.
Nothing is carried into a filing from a period that ended more than 400
days before it.

Tag fallbacks: revenue and cost tags changed with ASC 606 (2018), chosen per
period; net income is NetIncomeLoss while it is fresh, else the next tag's
TTM; shares from dei when us-gaap is missing. Equity is the parent's:
StockholdersEquity, else the total less noncontrolling interest.

Data corrections of 2026-09-28: docs/AGENT_PLAN.md S47.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from hedge_fund.features.panel import PanelStore

FLOW_TAGS = {
    # alternatives, not periods to mix: see BY_TAG
    "ni": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
    # Chosen per period, so a tag change (ASC 606, 2018) does not wait for the new tag to have a year of its own.
    # TODO(S47 note 8): the year and its quarters can still come from different tags, and Q4 = one tag's year
    # less another's quarters (PLXS rev_ttm 30.7B, 20.7B, 10.8B on its 2025 filings, ~40B real). Building the
    # TTM within each tag, then choosing, fixes it but delays the 2018 switch by up to three quarters.
    "rev": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "gp": ["GrossProfit"],
    "cogs": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "cogs_g": ["CostOfGoodsSold"],          # companies that split goods and services (VZ, JNJ...): summed below
    "cogs_s": ["CostOfServices"],
    "opinc": ["OperatingIncomeLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
}
SHARES_FLOW_TAGS = ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"]
# Items whose TTM is built within each tag, then taken from the first tag in the list that has a fresh one
# (S47 补充 2). A stale preferred tag does not block a fresh one: EDRY stopped tagging NetIncomeLoss in 2022.
BY_TAG = {"ni"}
INSTANT_TAGS = {
    "assets": ["Assets"],
    # the parent's equity; when it is not reported, the total less noncontrolling interest (_parent_equity).
    # Was: the total as it stood, which put XIFR's book value at $10.7B against ~$3B attributable to the parent.
    "equity": ["StockholdersEquity"],
    "equity_total": ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "nci": ["MinorityInterest"],
    "nci_redeemable": ["RedeemableNoncontrollingInterestEquityCarryingAmount"],
    "debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "shares_dei": ["EntityCommonStockSharesOutstanding"],
    "shares_bs": ["CommonStockSharesOutstanding"],
}
# period lengths in days; 12- and 16-week fiscal quarters (KR, PEP, COST) are 84 and 112
QUARTER, HALF, NINE_MONTHS, YEAR = (75, 125), (160, 205), (245, 290), (350, 380)
TTM_SPAN = (330, 400)            # first quarter's start to last quarter's end
MAX_FACT_AGE_DAYS = 400          # period end to the filing a value is carried into
SAME_PERIOD_DAYS = 100           # facts whose period ends are this close describe the same balance sheet
SHARES_MISMATCH = 1.5            # largest / smallest of two share counts of the same period
PRIOR_YTD_DAYS = 8               # last year's year-to-date may be up to 8 days longer or shorter (52/53 weeks)
CARRY_QUARTERS = 4               # a symbol's owner is carried into quarters with no Form 4 for at most this many
MAX_COVERAGE_DROP = 0.10         # factor_inputs refuses a table with this much less shares / equity coverage
MIN_SHARES = 1000                # 0 / 1 / negative counts are XBRL noise (FOX, HOOD, EL...)
ANNUAL_FORMS = ("20-F", "40-F")
DAY = pd.Timedelta(days=1)
REVIEW_WINDOW_DAYS = 200         # = factors.latest_before: older rows are not scored, so not worth a review
SHARE_SOURCES = ["shares_dei", "shares_bs", "shares_w"]
REVIEW_ONLY = [c for s in SHARE_SOURCES for c in (s, f"{s}_asof")] + ["shares_w_form"]
HELPERS = ["equity_total", "nci", "nci_redeemable"]         # inputs of equity, not columns of the table

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
    that changed tags — found 2026-09-22 when scoring META by hand.

    A zero does not win over another tag's non-zero value for the same period: FLS tags Revenues = 0
    next to $4.7B of RevenueFromContract..., and its rev_ttm read 0 or negative through 2025-26."""
    sub = df[df["tag"].isin(tags)].copy()
    if sub.empty:
        return sub
    sub["_pref"] = sub["tag"].map({t: i for i, t in enumerate(tags)})
    sub["_zero"] = sub["val"] == 0
    sub = sub.sort_values(["_zero", "_pref", "filed"], kind="stable")
    return sub.drop_duplicates(["cik", "period_start", "period_end", "filed"], keep="first").drop(columns=["_pref", "_zero"])


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
    # A 10-K fact with a quarter-long period and the whole year's value is the year mislabelled, not a
    # quarter (TSCO 2025: $1.096B for 2025-09-28..12-27 = the year, so ni_ttm read $1.88B against $1.01B;
    # 97 companies since 2012). The fourth quarter is then the year less its three quarters. Only a year
    # already filed counts: AMSC's fiscal 2021 appeared as a year only in the next 10-K, a year later.
    year = _annual(f)[["cik", "period_end", "val", "filed"]].rename(columns={"val": "_year", "filed": "_year_filed"})
    q = q.merge(year, on=["cik", "period_end"], how="left")
    q = q[~(q["form"].fillna("").str.startswith("10-K") & (q["val"] == q["_year"]) & (q["_year_filed"] <= q["filed"]))]
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
    # The year as reported is the TTM at its own end: 20-F/40-F filers, and a company's first 10-K, whose
    # quarters of the year before are not on file. Was: annual / 4 into rolling(4), i.e. the mean of the
    # last four years, available only after four annual reports (PERI $55.3M, 2025 was -$7.9M).
    a = _annual(f)
    year = pd.DataFrame({"cik": a["cik"], "period_end": a["period_end"], "filed": a["filed"], col: a["val"], "_how": 1})
    # A broken chain (a new 10-Q filer, a changed fiscal calendar, a quarter never tagged): the latest year,
    # plus this year to date, less last year to the same point. Was: the last TTM the chain gave, one to three
    # quarters behind (VSNT ni_ttm $930M = fiscal 2025 on its 2026 10-Qs; $758M by this rule).
    a = a.rename(columns={"period_start": "a_start", "period_end": "a_end", "filed": "a_filed", "val": "a_val"})
    ytd = f[f["days"] < YEAR[0]][["cik", "period_start", "period_end", "filed", "val", "days"]]
    cur = a[["cik", "a_start", "a_end", "a_filed", "a_val"]].assign(period_start=a["a_end"] + DAY).merge(ytd, on=["cik", "period_start"])
    prior = ytd.rename(columns={"period_start": "a_start", "period_end": "p_end", "filed": "p_filed", "val": "p_val", "days": "p_days"})
    cp = cur.merge(prior, on=["cik", "a_start"])
    cp = cp[((cp["days"] - cp["p_days"]).abs() <= PRIOR_YTD_DAYS) & (cp["period_end"] - cp["p_end"]).dt.days.between(*YEAR)]
    bridged = pd.DataFrame({"cik": cp["cik"], "period_end": cp["period_end"],
                            "filed": cp[["a_filed", "filed", "p_filed"]].max(axis=1),
                            col: cp["a_val"] + cp["val"] - cp["p_val"], "_how": 2})
    # per period end the version known first; the chain when both come with the same filing
    out = _earliest(pd.concat([rolled, year, bridged], ignore_index=True))
    return out.drop(columns="_how").sort_values(["cik", "period_end"], kind="stable").reset_index(drop=True)


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
    """shares, shares_asof, shares_flag from the three counts known at each filing.

    Only counts of the same period are compared: those whose period ends within SAME_PERIOD_DAYS of the
    newest one. Was: the latest of each, up to 400 days apart, so after a split the old 10-K count
    disagreed with the new cover page (NVDA had no share count from 2024-08-28 to 2025-02-26).
    A count that agrees with another within SHARES_MISMATCH is usable; an instant count (cover page,
    else balance sheet, the newer of the two) is preferred to the weighted average. Flags:
      mismatch     two or three counts of the period and no two agree: missing, to the review list
      ads          only a weighted count, from a 20-F/40-F: missing, to the review list
      stale        every count is older than MAX_FACT_AGE_DAYS: missing, to the review list
      unconfirmed  a single count of the period, and an older one disagrees with it (MCHB, FUBO): used,
                   but listed for review
    Known limitation (S47 补充 7): a 20-F/40-F filer with an instant count keeps it. That is ordinary
    shares against a price per ADS, the ratio taken as 1, so its market cap can be overstated (TSM x5,
    BABA x8); cheapness is understated rather than invented. The ratio is not in XBRL."""
    vals = out[SHARE_SOURCES]
    asof = pd.DataFrame({s: out[f"{s}_asof"] for s in SHARE_SOURCES}).where(vals.notna())
    newest = asof.max(axis=1)
    cur = vals.where(asof.ge(newest - pd.Timedelta(days=SAME_PERIOD_DAYS), axis=0))
    d, b, w = (cur[s] for s in SHARE_SOURCES)

    def agree(x, y):
        return np.maximum(x, y) <= SHARES_MISMATCH * np.minimum(x, y)          # False when either is missing

    n = cur.notna().sum(axis=1)
    alone = n == 1
    # how many of the other counts each one agrees with; 1.5x is not transitive, so the best supported wins
    # (HG: 65.9M agrees with 98.6M, 98.6M with 101.1M, 65.9M not with 101.1M -> 98.6M)
    sup = pd.DataFrame({s: sum(agree(cur[s], cur[o]).astype(int) for o in SHARE_SOURCES if o != s) for s in SHARE_SOURCES})
    ok = cur.notna() & (alone.to_numpy()[:, None] | ((sup >= 1) & sup.eq(sup.max(axis=1), axis=0)))
    d_ok, b_ok, w_ok = (ok[s] for s in SHARE_SOURCES)
    # dual-class companies (META, V, MA, BRK.B, F, ...) report shares outstanding per class, which
    # companyfacts leaves out; the weighted average diluted count is undimensioned and stands in
    use_d = d_ok & (~b_ok | (out["shares_dei_asof"] >= out["shares_bs_asof"]))
    use_b = b_ok & ~use_d
    use_w = w_ok & ~use_d & ~use_b
    shares = pd.Series(np.nan, index=out.index)
    shares_asof = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns]")
    for use, s in ((use_d, "shares_dei"), (use_b, "shares_bs"), (use_w, "shares_w")):
        shares[use] = out.loc[use, s]
        shares_asof[use] = out.loc[use, f"{s}_asof"]
    flag = pd.Series("", index=out.index)
    older = vals.notna() & cur.isna()                  # of an earlier period: not compared, not a mismatch
    contradicted = pd.concat([older[s] & ~agree(vals[s], shares) for s in SHARE_SOURCES], axis=1).any(axis=1)
    flag[alone & shares.notna() & contradicted] = "unconfirmed"
    had_one = out[[f"{s}_asof" for s in SHARE_SOURCES]].notna().any(axis=1)
    flag[(n == 0) & had_one] = "stale"
    # price is per ADS, the count is ordinary shares, and the ratio between them is not in XBRL
    flag[use_w & out["shares_w_form"].isin(ANNUAL_FORMS)] = "ads"
    # GMRS: 22.1M on the balance sheet against 62.7M weighted, the same quarter, nothing else to decide by
    flag[(n >= 2) & shares.isna()] = "mismatch"
    unusable = flag.isin(["ads", "mismatch"])
    return out.assign(shares=shares.mask(unusable), shares_asof=shares_asof.mask(unusable), shares_flag=flag)


def _parent_equity(out: pd.DataFrame) -> pd.Series:
    """StockholdersEquity when there is a fresh one, else the total less noncontrolling interest (S47 补充 1).

    Noncontrolling interest counts when its period ends within SAME_PERIOD_DAYS of the total's; a tag not
    reported counts as 0. PG, CAT and XIFR tag only the total; UNH's StockholdersEquity ends in 2015.
    Only MinorityInterest is subtracted: redeemable NCI sits outside the total."""
    def nci(col):
        near = (out[f"{col}_asof"] - out["equity_total_asof"]).dt.days.abs() <= SAME_PERIOD_DAYS
        return out[col].where(near).fillna(0.0)
    # Redeemable NCI is mezzanine equity (ASC 480-10-S99), not part of the total: subtracting it would count it twice
    # (ADTN 129M total - 359M redeemable < 0). It is still loaded and kept, only not subtracted (2026-09-29).
    return out["equity"].fillna(out["equity_total"] - nci("nci"))


def _symbol_owners(tp: pd.DataFrame) -> pd.DataFrame:
    """(ticker, qn) -> owner_cik: the CIK with the most Form 4 filings under the symbol that quarter.

    Form 4 filers mistype symbols. On a tie the previous owner (of a quarter at most CARRY_QUARTERS
    before) keeps the symbol, else the smaller CIK. Was: always the smaller CIK, so BH went to a
    stray CIK for six quarters on one filing each (2022q2)."""
    top = tp[tp["n_filings"] == tp.groupby(["ticker", "qn"])["n_filings"].transform("max")]
    top = top.sort_values(["ticker", "qn", "cik"], kind="stable")
    tied = top.duplicated(["ticker", "qn"], keep=False)
    owners = top[~tied][["ticker", "qn", "cik"]]
    resolved = []
    for t, g in top[top["ticker"].isin(set(top.loc[tied, "ticker"]))].groupby("ticker", sort=False):
        prev_q, prev_c = None, None
        for q, gq in g.groupby("qn", sort=True):
            ciks = gq["cik"].tolist()
            c = ciks[0]
            if len(ciks) > 1:
                if prev_c in ciks and q - prev_q <= CARRY_QUARTERS:
                    c = prev_c
                resolved.append((t, q, c))
            prev_q, prev_c = q, c
    owners = pd.concat([owners, pd.DataFrame(resolved, columns=["ticker", "qn", "cik"])], ignore_index=True)
    return owners.rename(columns={"cik": "owner_cik"}).astype({"qn": "int64", "owner_cik": "int64"})


def _qn(quarter: pd.Series) -> pd.Series:
    return quarter.str[:4].astype(int) * 4 + quarter.str[-1].astype(int)


def build(store: PanelStore) -> pd.DataFrame:
    """One row per (cik, filed): TTM flows + latest instants known at that filing."""
    # ordered, and every sort below is stable: two facts that tie are always resolved the same way.
    # A period that ends after its own filing is a typing error (a 2033 cover date in a 2023 10-Q); carried
    # as "the latest" it would block every real value after it. Dates outside 1990..filed also cannot be
    # held as datetime64[ns] (year 3024 or 0202 would stop the whole build).
    facts = store.con.execute("""SELECT cik, tag, period_start, period_end, is_instant, val, form, filed
                                 FROM xbrl_facts WHERE unit IN ('USD','shares')
                                   AND period_start >= DATE '1990-01-01' AND period_end BETWEEN DATE '1990-01-01' AND filed
                                 ORDER BY cik, tag, period_start, period_end, filed, accn""").df()
    for c in ("period_start", "period_end", "filed"):
        facts[c] = pd.to_datetime(facts[c]).astype("datetime64[ns]")
    # cik -> ticker is point-in-time: 388 tickers have belonged to more than one company (SPACs,
    # renames, recycled symbols), so a filing is mapped to the ticker its CIK carried in that quarter
    # (issuer_seen), falling back to the CIK's latest ticker. Found by the 2026-09-22 audit.
    tick_pit = store.con.execute("""SELECT CAST(cik AS INT) AS cik, quarter, ticker, n_filings FROM issuer_seen
                                    WHERE ticker IN (SELECT DISTINCT ticker FROM bars)
                                    ORDER BY ticker, quarter, cik""").df()
    tick_pit = tick_pit.astype({"cik": "int64"}).assign(qn=lambda d: _qn(d["quarter"]))
    owners = _symbol_owners(tick_pit)
    # the CIK's latest symbol: the one with the most filings in its latest quarter, the shorter on a tie.
    # Was: the last alphabetically, so SLNH's 2026q3 filing went to its preferred stock SLNHP.
    tick = (tick_pit.assign(_len=tick_pit["ticker"].str.len())
            .sort_values(["cik", "qn", "n_filings", "_len", "ticker"], ascending=[True, True, True, False, False], kind="stable")
            .drop_duplicates("cik", keep="last")[["cik", "ticker"]].rename(columns={"ticker": "ticker_latest"}))
    items = {}
    for name, tags in FLOW_TAGS.items():
        if name in BY_TAG:
            for i, tag in enumerate(tags):
                items[f"{name}_ttm{i}"] = ttm_flows(facts, name, [tag]).rename(columns={f"{name}_ttm": f"{name}_ttm{i}"})
        else:
            items[f"{name}_ttm"] = ttm_flows(facts, name, tags)
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
    for name in BY_TAG:                          # the first tag with a fresh TTM (_carry has dropped stale ones)
        alts = [f"{name}_ttm{i}" for i in range(len(FLOW_TAGS[name]))]
        out[f"{name}_ttm"] = out[alts].bfill(axis=1).iloc[:, 0]
        out = out.drop(columns=alts + [f"{a}_asof" for a in alts])
    out["equity"] = _parent_equity(out)
    out = _usable_shares(out)
    out = out.drop(columns=HELPERS + [c for c in out.columns if c.endswith("_asof") and c not in REVIEW_ONLY + ["shares_asof"]])
    out["qn"] = _qn(out["filed"].dt.year.astype(str) + "q" + out["filed"].dt.quarter.astype(str))
    out = out.merge(tick_pit[["cik", "qn", "ticker"]], on=["cik", "qn"], how="left").merge(tick, on="cik", how="inner")
    out["ticker"] = out["ticker"].fillna(out["ticker_latest"])
    # A quarter with no Form 4 under the symbol keeps the owner of the last quarter that had one, for at
    # most CARRY_QUARTERS. Was: no owner at all, so every CIK ever seen under the symbol kept it: issuer_seen
    # ends a quarter behind the newest filings, and GSBD's latest row came from a CIK seen under it once, in
    # 2016 (14.9M shares instead of 112.6M). Without a limit, a symbol unused for years stayed with its old
    # owner and the company using it now lost its rows (NIO 2019-2025: the symbol was CIK 878242's in 2015).
    out = pd.merge_asof(out.sort_values("qn", kind="stable"), owners.sort_values("qn", kind="stable"),
                        on="qn", by="ticker", tolerance=CARRY_QUARTERS)
    out = out[out["owner_cik"].isna() | (out["owner_cik"] == out["cik"])]      # a losing CIK does not get the symbol
    out = out.drop(columns=["qn", "ticker_latest", "owner_cik"])
    # if two CIKs still land on the same ticker on the same filing date, keep the larger balance sheet
    out = out.sort_values(["ticker", "filed", "assets", "cik"], kind="stable").drop_duplicates(["ticker", "filed"], keep="last")
    return out.reset_index(drop=True)


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


def coverage(df: pd.DataFrame) -> dict:
    """Names with a share count / equity on their latest row, among those filed in the last REVIEW_WINDOW_DAYS."""
    latest = df.sort_values("filed", kind="stable").drop_duplicates("ticker", keep="last")
    latest = latest[pd.to_datetime(latest["filed"]) >= pd.to_datetime(latest["filed"]).max() - pd.Timedelta(days=REVIEW_WINDOW_DAYS)]
    return {"shares": int(latest["shares"].notna().sum()), "equity": int(latest["equity"].notna().sum())}


def factor_inputs(store: PanelStore, force: bool = False) -> pd.DataFrame:
    """Cached build; replaces panel.fundamentals_pit in one statement, then writes the share review list.

    Refuses (RuntimeError, the old table stays) when the names with shares or with equity fall by more
    than MAX_COVERAGE_DROP against the table it would replace: the weekly job rebuilds unattended after
    a network refresh, and a partial refresh would otherwise unscore names and sell them the next day.
    force=True replaces anyway (a change of definition, such as the S47 rebuild)."""
    df = build(store)
    review = share_review(store, df)
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
    new = coverage(df)
    try:
        old = coverage(store.con.execute("SELECT ticker, filed, shares, equity FROM fundamentals_pit").df())
    except duckdb.CatalogException:
        old = None
    print(f"fundamentals_pit: names filed in the last {REVIEW_WINDOW_DAYS} days with shares / equity: was {old}, now {new}")
    fell = [k for k in new if old and new[k] < (1 - MAX_COVERAGE_DROP) * old[k]]
    if fell and not force:
        raise RuntimeError(f"fundamentals_pit not replaced: {', '.join(fell)} coverage fell more than "
                           f"{MAX_COVERAGE_DROP:.0%} (was {old}, now {new}); check the XBRL refresh, or pass force=True")
    store.con.register("_f", df)
    try:
        # one statement: the old table stays if it fails. Was: DROP, then CREATE, each committed on its own.
        store.con.execute("CREATE OR REPLACE TABLE fundamentals_pit AS SELECT * FROM _f")
    finally:
        store.con.unregister("_f")
    review.to_csv(review_path(store), index=False)
    return df
