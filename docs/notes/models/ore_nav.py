"""Orezone Gold (TSX/ASX: ORE, OTC: ORZCF) NAV model — public data only, inputs as of 2026-10-14 (v2).

Sources (see docs/notes/ORZCF.md): Q2-2026 MD&A and interim FS, FY2025 AIF (Bomboré 2023 DFS summary, reserves
effective 2023-03-28), the 2026-09-15 Casa Berardi LOM/PEA release (year-by-year table), the 2026-10-14 Q3
production release, ASX Appendix 4A / 3G share filings (the US$35M notes became 50M shares by 2026-08-31).

v2 (2026-10-14) applies the fixes an adversarial review confirmed: Bomboré profile built from contained ounces,
a higher-cost hard-rock tail, the existing ~US$350M tax pool, tax paid in arrears, today's XOF and C$ rates for
costs, Q3 price ex-stream, Hecla in nominal terms, working capital / restricted cash / option proceeds, roll-forward
to the price date, a Stage 2B case capped at the same ounces, and Kiaka/Boungou-calibrated scenarios.

Conventions: US$ millions unless noted; flat real gold (or a {year: price} deck); PVs struck at 2026-07-01 with
mid-period discounting, then rolled forward to 2026-10-14; ASSUMPTION marks numbers that are ours.

    python docs/notes/models/ore_nav.py
"""
from __future__ import annotations

import math

USDCAD = 1.425                      # 2026-10-14
PRICE_CAD = 2.79                    # TSX ORE, 2026-10-14 intraday
SHARES_FD = 718.864 + 18.703 + 4.942 + 1.694 + 1.502 - 0.761   # basic 9/30 + options, RSU, DSU, PSU; overlap removed
OPTION_PROCEEDS = 18.02 / USDCAD    # C$18.0M strike proceeds
ROLL = 105 / 365                    # 2026-07-01 -> 2026-10-14
Q3_PRICE = 4174 * 56461 / (56461 - 0.8 * 1625)   # Q3 realised US$4,174 includes the stream; ex-stream ~4,272


def gold_at(gold, t: float) -> float:
    """A flat price, or a {year: price} deck (years after the last entry keep the last price)."""
    if not isinstance(gold, dict):
        return float(gold)
    y = int(t)
    known = [k for k in gold if k <= y]
    return float(gold[max(known)] if known else gold[min(gold)])


def deck(near: dict[int, float], long_term: float) -> dict[int, float]:
    return {**{y: long_term for y in range(2026, 2045)}, **near}


SPOT = 4150.0
PEA_CONSENSUS = deck({2026: SPOT, 2027: 4632, 2028: 4518, 2029: 4378}, 3570)      # deck in the 2026-09-15 PEA
NBF_SEP = deck({2026: SPOT, 2027: 4700, 2028: 4700, 2029: 4375, 2030: 4050, 2031: 3725}, 3400)  # 2029-31 interpolated

# ---------------------------------------------------------------- Bomboré (100% basis) ---------------------------
STATE_CARRY = 0.15                  # free carry since 2025-08-19
CIT = 0.275 + 0.02 * (1 - 0.275)    # 27.5% + 2% levy on after-tax profit
WHT_2015, WHT_2024 = 0.0625, 0.125  # dividend withholding, 2015 vs 2024 code
BOM_TAX_POOL = 350.0                # MPP&E US$349.6M at 2026-06-30, deducted unit-of-production
BOM_SUSTAINING_PER_OZ = 148.0       # US$21-23M on ~150 koz (2026 guidance); depreciated UOP like growth capex
BOM_TAX_LAG = 0.6                   # Burkina tax is paid in arrears
XOF_COST_CUT = 0.5 * (1 - 563 / (655.957 / 1.1159))   # half the cost base in XOF/EUR; XOF ~563 in H1-26, ~588 now


def royalty_rate(gold: float) -> float:
    """Effective Bomboré government royalty, fitted to what was actually paid: 8.9% at $3,338 (Q2-25), 11.1% at
    $4,129 (Q4-25), ~12.2% at ~$4,887 (Q1-26), 12% guided at $4,500 — about one point above the decree's bands."""
    if gold < 3000:
        return 0.08
    return 0.09 + 0.01 * math.floor((gold - 3000) / 500)


def build_profile(oxide_koz: float, hard_rock_koz: float, oxide_rate=102.0, hr_rate=68.0,
                  h2_2026=(58.6, 34.0), hr_rate_from: tuple[int, float] | None = None) -> dict[float, float]:
    """Recovered ounces per period: oxide and hard rock each at its own rate until its ounces run out.
    H2-2026 = Q3 actual 40.6 + Q4 52 koz. hr_rate_from=(year, rate) switches the hard-rock rate (Stage 2B)."""
    prof = {2026.5: sum(h2_2026)}
    ox, hr, y = oxide_koz - h2_2026[0], hard_rock_koz - h2_2026[1], 2027
    while ox > 1e-9 or hr > 1e-9:
        rate = hr_rate_from[1] if hr_rate_from and y >= hr_rate_from[0] else hr_rate
        o, h = min(oxide_rate, ox), min(rate, hr)
        prof[y] = o + h
        ox, hr, y = ox - o, hr - h, y + 1
    return prof


# ASSUMPTION: reserves only (2023 reserve at a US$1,500 pit, depleted): ~0.65 Moz oxide + stockpile contained at
# 87.6% recovery and ~1.2 Moz hard rock at 85%, oxide ~102 koz/yr, Stage-1 hard rock ~68 koz/yr (2.5 Mt x 1.0 g/t).
OXIDE_KOZ, HARD_ROCK_KOZ = 650 * 0.876, 1200 * 0.85
BOM_BASE = build_profile(OXIDE_KOZ, HARD_ROCK_KOZ)
# Stage 2B (5.5 Mtpa): same ounces, hard rock at 150 koz/yr from 2029 — it brings ounces forward, it does not add any.
BOM_2B = build_profile(OXIDE_KOZ, HARD_ROCK_KOZ, hr_rate_from=(2029, 150.0))
BOM_2B[2028] += 15.0                                   # partial-year ramp
BOM_2B[max(BOM_2B)] -= 15.0
BOM_2B_CAPEX = {2027: 45.0, 2028: 40.0}                # 2B remainder ~US$69-77M derived, +10% overrun
BOM_GROWTH = {2026.5: 25.0, 2027: 10.0, 2028: 10.0, 2029: 8.0, 2030: 5.0}   # Stage 2A rest, TSF, RAP (ASSUMPTION)
BOM_CLOSURE = 27.0                                     # undiscounted provision, the year after the last ounce


def bom_aisc_default(t: float) -> float:
    """US$/oz ex-royalty: H1-26 site AISC 2,234 less ~US$566 royalty = 1,668 -> 1,700 while oxide runs; 1,800 for
    the hard-rock-only tail, where site G&A is spread over 60% fewer ounces (ASSUMPTION; 1,700-1,950 sensitivity)."""
    return 1700.0 if t < 2032 else 1800.0


def bomboré_flows(gold, profile=None, aisc=bom_aisc_default, extra_capex=None, wht=WHT_2015,
                  carry=STATE_CARRY) -> dict[float, float]:
    """After-tax cash flow attributable to Orezone, after dividend withholding. Taxable income deducts the opening
    pool and all capex (growth + sustaining) unit-of-production; tax is paid BOM_TAX_LAG years later."""
    profile = profile or BOM_BASE
    ts = sorted(profile)
    total = sum(profile.values())
    att = (1 - carry) * (1 - wht)
    vintages, out = [], {}
    for i, t in enumerate(ts):
        koz = profile[t]
        g = gold_at(gold, t)
        p = Q3_PRICE * 40.6 / 92.6 + g * 52.0 / 92.6 if t == 2026.5 else g
        rev = koz * p / 1000
        roy = rev * royalty_rate(p)
        capex = BOM_GROWTH.get(t, 0.0) + (extra_capex or {}).get(t, 0.0)
        pre = rev - roy - koz * aisc(t) * (1 - XOF_COST_CUT) / 1000 - capex
        sustaining = koz * BOM_SUSTAINING_PER_OZ / 1000
        vintages.append((capex + sustaining, i))
        deduction = BOM_TAX_POOL * koz / total + sum(
            amt * koz / sum(profile[s] for s in ts[j:]) for amt, j in vintages)
        tax = max(0.0, (pre + capex + sustaining - deduction) * CIT)
        out[t] = out.get(t, 0.0) + pre * att
        paid = (2026.75 if t == 2026.5 else t + 0.5) + BOM_TAX_LAG
        out[paid] = out.get(paid, 0.0) - tax * att
    out[max(ts) + 1] = out.get(max(ts) + 1, 0.0) - BOM_CLOSURE * (1 - carry)
    return out


# ---------------------------------------------------------------- Casa Berardi (100%) -----------------------------
# 2026-09-15 PEA, year by year: production koz, AISC US$/oz, non-sustaining capex, after-tax FCF (PEA deck).
CASA = {
    2027: (85, 2967, 15.4, 74), 2028: (95, 2348, 44.4, 95), 2029: (106, 2435, 0.0, 86), 2030: (114, 2138, 0.0, 94),
    2031: (126, 2131, 49.6, 91), 2032: (158, 1452, 37.3, 169), 2033: (178, 1268, 37.8, 196), 2034: (125, 1815, 45.2, 103),
    2035: (111, 2222, 18.8, 66), 2036: (109, 2224, 26.9, 50), 2037: (136, 1604, 26.7, 121), 2038: (127, 1586, 21.5, 130),
    2039: (82, 1631, 0.0, 110), 2040: (79, 1144, 0.0, 131),
}
CASA_2041_FCF = -73.0               # PEA's unlabeled post-production year
PEA_DECK = {2027: 4632, 2028: 4518, 2029: 4378}   # 3,570 from 2030
CASA_TAX_MARGINAL = 0.42            # Quebec mining tax (16-28%, deductible) + 26.5% income tax (ASSUMPTION)
CASA_CLOSURE_EXTRA = 100.0          # MRNF-approved closure US$173M vs PEA's -73 in 2041 (ASSUMPTION)
CASA_FX_K = 1 - 0.85 * (1 - 1.35 / USDCAD)       # PEA costs at C$1.35/US$; 85% of them in C$ (ASSUMPTION)
CASA_H2_2026_KOZ = 19.1 + 23.5      # Q3 actual + Q4 guidance midpoint (21-26 koz), guided AISC 2,700


def stream_loss(year: int, koz: float, gold: float) -> float:
    """Franco-Nevada pays 20% of spot: 1,625 oz/quarter fixed to 2030, then 5% of production."""
    oz = 6.5 if year <= 2030 else 0.05 * koz
    return oz * gold * 0.8 / 1000


def casa_pretax(year: int, gold: float, k: float = 1.0) -> float:
    koz, aisc, nonsust, _ = CASA[year]
    return koz * gold / 1000 - stream_loss(year, koz, gold) - (koz * aisc / 1000 + nonsust) * k


def casa_flows(gold, tax_marginal=CASA_TAX_MARGINAL, closure_extra=CASA_CLOSURE_EXTRA, k=CASA_FX_K):
    """PEA after-tax FCF re-priced: the PEA's own tax is kept and any change in pre-tax cash flow (gold, FX) is
    taxed at the margin."""
    p_h2 = Q3_PRICE * 19.1 / 42.6 + gold_at(gold, 2026) * 23.5 / 42.6
    aisc_h2 = (19.1 * 2700 + 23.5 * 2700 * k) / 42.6
    pre_h2 = CASA_H2_2026_KOZ * (p_h2 - aisc_h2) / 1000 - 3.25 * p_h2 * 0.8 / 1000 - 3.0
    out = {2026.5: pre_h2 * (1 - 0.30)}
    for y, (_, _, _, fcf_pea) in CASA.items():
        base_pre = casa_pretax(y, PEA_DECK.get(y, 3570))
        pre = casa_pretax(y, gold_at(gold, y), k)
        out[y] = pre - max(0.0, base_pre - fcf_pea + tax_marginal * (pre - base_pre))
    out[2041] = (CASA_2041_FCF - closure_extra) * k
    return out


HECLA_INFLATION = 0.02              # fixed-dollar obligations: discount at (1+r)(1+2%)-1


def hecla_flows(gold) -> dict[float, float]:
    """Deferred consideration (US$30M cut to 18.5 by the closure offset, then 50) and the contingent payments."""
    f = {2027.75: -18.5, 2028.75: -50.0, 2027.25: -5.0}           # year-1 gold-price trigger (>US$4,200)
    if gold_at(gold, 2027) > 4200:
        f[2028.25] = -5.0                                           # year-2 trigger
    f[2030] = -10.0                                                 # Principal pit permit
    f[2033] = -10.0                                                 # WMCP pit permit
    for y in range(2031, 2041):                                     # ~600 koz from those pits: 500k x $80 + 100k x $180
        f[y] = f.get(y, 0.0) - 5.8
    return f


def corporate_flows() -> dict[float, float]:
    f = {2026.5: -8.0 - 9.0}
    for y in range(2027, 2042):
        f[y] = -16.0 - (12.0 if y <= 2029 else 4.0)                 # G&A ~US$4.1M/q; expensed exploration (ASSUMPTION)
    return f


# ---------------------------------------------------------------- balance sheet at 2026-06-30 --------------------
# After the notes' conversion (debt removed, 50M shares added). OBSA (Bomboré company) items belong 85% to Orezone
# and reach it after withholding; the State dividend payable is the State's; corporate/Casa items are 100%.
OBSA_ITEMS = {"OBSA cash (EUR/XOF)": 59.6, "bullion (80% Bomboré)": 0.8 * 21.6, "Coris Phase I": -7.8,
              "Coris Phase II": -59.3, "income tax payable": -64.6, "Genser disputed payable": -8.0,
              "silver-stream shortfall": -4.0, "VAT receivable at 50%": 30.0}
OTHER_ITEMS = {"other cash": 96.7 - 59.6, "other bullion": 0.2 * 21.6, "State dividend payable (100% to the State)": -16.6,
               "net working capital (payables, receivables, prepaids, M&S)": -(86.101 - 8.0) + 10.031 + 16.733 + 34.336,
               "option proceeds": OPTION_PROCEEDS}
RESTRICTED_CASH = {2041.5: 21.9, 2042.5: 3.7 * 0.85}             # posted for closure, released at closure


def balance_sheet(r: float = 0.05) -> float:
    att = (1 - STATE_CARRY) * (1 - WHT_2015)
    obsa = sum(OBSA_ITEMS.values()) * att
    tax_timing = -OBSA_ITEMS["income tax payable"] * (1 - 1 / (1 + r) ** 0.5) * att   # settled ~2027.0
    return obsa + tax_timing + sum(OTHER_ITEMS.values()) + npv(RESTRICTED_CASH, r)


def npv(flows: dict[float, float], rate: float, t0: float = 2026.5) -> float:
    """Mid-period discounting: a calendar year y is centred at y + 0.5; 2026.5 means H2-2026 (centre 2026.75);
    other fractional keys are exact dates."""
    total = 0.0
    for t, cf in flows.items():
        mid = 2026.75 if t == 2026.5 else (t + 0.5 if float(t).is_integer() else t)
        total += cf / (1 + rate) ** (mid - t0)
    return total


def nav(gold, r_bom=0.05, r_casa=0.05, bom_kwargs=None, casa_kwargs=None, addons=0.0) -> dict[str, float]:
    roll_b, roll_c = (1 + r_bom) ** ROLL, (1 + r_casa) ** ROLL
    r_hecla = (1 + r_casa) * (1 + HECLA_INFLATION) - 1
    parts = {
        "Bomboré (85%)": npv(bomboré_flows(gold, **(bom_kwargs or {})), r_bom) * roll_b,
        "Casa Berardi": npv(casa_flows(gold, **(casa_kwargs or {})), r_casa) * roll_c,
        "Hecla deferred + contingent": npv(hecla_flows(gold), r_hecla) * (1 + r_hecla) ** ROLL,
        "Corporate G&A + exploration": npv(corporate_flows(), r_casa) * roll_c,
        "Net balance sheet": balance_sheet(r_casa),
        "Add-ons (resources outside the plans)": addons,
    }
    parts["NAV"] = sum(parts.values())
    parts["NAV/share C$"] = parts["NAV"] / SHARES_FD * USDCAD
    parts["P/NAV"] = PRICE_CAD / parts["NAV/share C$"]
    return parts


# Resources outside the mine plans, shown separately, never in the headline (ASSUMPTION: in-situ values).
ADDON_BOM_MI = 2214 * 25 / 1000 * 0.85   # Bomboré M&I outside reserves (~0.6 Moz oxide + 1.6 Moz hard rock) at US$25/oz
ADDON_HEVA = 1179 * 50 / 1000 * 0.975    # Heva-Hosco M&I at US$50/oz, net of the 2.5% stream


def market_cap_usd() -> float:
    return PRICE_CAD * SHARES_FD / USDCAD


def solve(f, lo, hi, target, tol=1e-6):
    """Bisection for f(x) = target on a monotone f; refuses a bracket that does not contain the target."""
    flo, fhi = f(lo) - target, f(hi) - target
    assert (flo > 0) != (fhi > 0), "target outside bracket"
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = f(mid) - target
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
        if hi - lo < tol:
            break
    x = (lo + hi) / 2
    assert abs(f(x) - target) < 1e-3 * max(1.0, abs(target))
    return x


def self_check() -> None:
    """Re-pricing at the PEA's own deck and FX must give back the PEA FCF year by year; the royalty fit must match
    the observed rates; the 2B case must not add ounces."""
    flows = casa_flows(PEA_CONSENSUS, k=1.0)
    for y, (_, _, _, fcf) in CASA.items():
        assert abs(flows[y] - fcf) < 1e-6, (y, flows[y], fcf)
    assert royalty_rate(3338) == 0.09 and royalty_rate(4129) == 0.11 and royalty_rate(4500) == 0.12
    assert abs(sum(k for k, *_ in CASA.values()) - 1630) < 10           # PEA: 1,630 koz
    assert abs(sum(BOM_2B.values()) - sum(BOM_BASE.values())) < 1e-6


def main() -> None:
    self_check()
    mcap = market_cap_usd()
    print(f"fully diluted shares {SHARES_FD:.1f}M; market cap at C${PRICE_CAD} = US${mcap:,.0f}M")
    print("Bomboré base profile (koz):", {k: round(v, 1) for k, v in BOM_BASE.items()}, f"= {sum(BOM_BASE.values()):,.0f}")

    def row(name, n):
        print(f"  {name:34s} {n['Bomboré (85%)']:6.0f} {n['Casa Berardi']:6.0f} {n['Hecla deferred + contingent']:6.0f} "
              f"{n['Corporate G&A + exploration']:6.0f} {n['Net balance sheet']:6.0f} {n['NAV']:6.0f} "
              f"{n['NAV/share C$']:6.2f} {n['P/NAV']:5.2f}")

    print("\nNAV (US$M) at 5%/5%:                 Bomboré   Casa  Hecla   Corp     BS    NAV  C$/sh P/NAV")
    for g in (3500, 4000, 4150, 4500, 5000):
        row(f"flat US${g:,}", nav(g))
    for name, g in (("PEA consensus (LT 3,570)", PEA_CONSENSUS), ("NBF Sep-26 (LT 3,400)", NBF_SEP)):
        row(name, nav(g))
    print("\nBomboré at 10% (Casa 5%):")
    for name, g in (("spot 4,150", SPOT), ("PEA consensus", PEA_CONSENSUS), ("NBF Sep-26", NBF_SEP)):
        row(name, nav(g, r_bom=0.10))

    print("\nImplied flat gold price (5%/5% and Bomboré 10%):")
    for m in (1.0, 0.83):
        for rb in (0.05, 0.10):
            g = solve(lambda x: nav(x, r_bom=rb)["NAV"] * m, 2000, 8000, mcap)
            print(f"  P/NAV {m:.2f}, Bomboré {rb:.0%}: US${g:,.0f}/oz")

    print("\nImplied Bomboré haircut (everything else at the multiple; haircut = 1 - (mcap - m*rest)/(m*Bomboré)):")
    for name, g in (("spot", SPOT), ("PEA consensus", PEA_CONSENSUS), ("NBF Sep-26", NBF_SEP)):
        for m in (1.0, 0.83):
            for rb in (0.05, 0.10):
                n = nav(g, r_bom=rb)
                rest = n["NAV"] - n["Bomboré (85%)"]
                hair = 1 - (mcap - m * rest) / (m * n["Bomboré (85%)"])
                print(f"  {name:14s} x{m:.2f} Bomboré {rb:.0%}: {hair:+.0%}")

    print("\nStage 2B (same ounces, mined faster), Bomboré value change:")
    a2 = lambda t: 1700.0 if t < 2029 else 1600.0
    for rb in (0.05, 0.10):
        b0 = npv(bomboré_flows(SPOT), rb)
        b2 = npv(bomboré_flows(SPOT, profile=BOM_2B, aisc=a2, extra_capex=BOM_2B_CAPEX), rb)
        print(f"  {rb:.0%}: {b2 - b0:+,.0f} (C${(b2 - b0) / SHARES_FD * USDCAD:+.2f}/sh)")

    print("\nSensitivities at spot, 5%/5% (NAV C$/sh):")
    base = nav(SPOT)["NAV/share C$"]
    cases = {
        "base": {},
        "hard-rock tail AISC 1,950": dict(bom_kwargs=dict(aisc=lambda t: 1700.0 if t < 2032 else 1950.0)),
        "Bomboré AISC +US$200 throughout": dict(bom_kwargs=dict(aisc=lambda t: bom_aisc_default(t) + 200)),
        "Casa extra closure 0": dict(casa_kwargs=dict(closure_extra=0.0)),
        "Casa extra closure 173": dict(casa_kwargs=dict(closure_extra=173.0)),
        "Casa at the PEA's own FX (1.35)": dict(casa_kwargs=dict(k=1.0)),
        "2024-code withholding 12.5%": dict(bom_kwargs=dict(wht=WHT_2024)),
        "add-ons: Bomboré M&I + Heva-Hosco": dict(addons=ADDON_BOM_MI + ADDON_HEVA),
    }
    for k, kw in cases.items():
        v = nav(SPOT, **kw)["NAV/share C$"]
        print(f"  {k:36s} {v:5.2f} ({v - base:+.2f})")

    print("\nBomboré jurisdiction scenarios at spot, 5% (Orezone's attributable Bomboré value, US$M):")
    v85 = npv(bomboré_flows(SPOT), 0.05)
    v100 = v85 / ((1 - STATE_CARRY) * (1 - WHT_2015))
    v_new = npv(bomboré_flows(SPOT, wht=WHT_2024), 0.05)
    scen = {
        "S0 status quo (15% carry, 2015 code)": v85,
        "S1 2024 code on the 2027 renewal (12.5% WHT)": v_new,
        "S2 S1 + State buys 25% at ~20% of value (Kiaka)": v_new * 0.60 / 0.85 + 0.25 * v100 * 0.20,
        "S3 nationalised, ~5% compensation (Boungou/Wahgnion)": 0.05 * v85,
    }
    for k, v in scen.items():
        print(f"  {k:54s} {v:6.0f} ({v / v85 - 1:+.0%})")
    w = dict(zip(scen, (0.50, 0.25, 0.15, 0.10)))
    ev_b = sum(w[k] * v for k, v in scen.items())
    print(f"  illustrative weights 50/25/15/10 (ours, not data): expected {ev_b:,.0f} ({ev_b / v85 - 1:+.0%})")


if __name__ == "__main__":
    main()
