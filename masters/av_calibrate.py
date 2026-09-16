"""用 Alpha Vantage HISTORICAL_OPTIONS 校准事件库的「30 日 IV 代理」。

抽 150 个财报事件(按 iv_pre 分 5 档,每档 30),取事件前最后一个收盘日的期权链,
选事件后第一个到期日的 ATM 跨式中间价(以 |call−put| 最小的行权价为 ATM),
straddle% = (call+put)/strike。这是市场对事件的真实隐含波动。
和代理 implied5 = iv_pre×√(4/252) 比 → ratio 随 IV 档的变化;和 |move_d4| 比 → 真实的
「实际 vs 隐含」。免费档 25 次/天,所以可断点续跑:每次 --max N 次。
用法: av_calibrate.py [--max 20] [--plan]   状态在 site-data/earnings/av_calibration.json"""
import datetime as dt, json, math, os, random, sys, time, urllib.parse, urllib.request, ssl
import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "site-data", "events.db")
STATE = os.path.join(ROOT, "site-data", "earnings", "av_calibration.json")
ENV = "/Users/louis/optradar/.env"
K = math.sqrt(4 / 252) * 100
BUCKETS = [(5, 40), (40, 60), (60, 90), (90, 120), (120, 300)]
PER_BUCKET = 30
try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL = ssl.create_default_context()


def key():
    for line in open(ENV):
        if line.startswith("ALPHAVANTAGE_KEY="):
            return line.strip().split("=", 1)[1]
    raise SystemExit("no key")


def plan():
    con = duckdb.connect(DB, read_only=True)
    rng = random.Random(7)
    items = []
    for lo, hi in BUCKETS:
        rows = con.execute("""SELECT ticker, filing_date, filing_window, iv_pre, move_d4 FROM earnings_events
                              WHERE iv_pre >= ? AND iv_pre < ? AND move_d4 IS NOT NULL AND filing_window IN ('PRE_MARKET','AFTER_MARKET')
                              AND filing_date >= '2024-01-01'""", [lo, hi]).fetchall()
        rng.shuffle(rows)
        for t, fd, w, iv, m4 in rows[:PER_BUCKET]:
            items.append(dict(symbol=t.replace("US.", ""), filing_date=str(fd), window=w, iv_pre=float(iv),
                              implied5=float(iv) / 100 * K, realised=abs(float(m4)), bucket=f"{lo}-{hi}", status="pending"))
    con.close()
    return items


def prev_trading_day(d):
    d = d - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def fetch_chain(sym, date, k):
    url = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(dict(function="HISTORICAL_OPTIONS", symbol=sym, date=date, apikey=k))
    with urllib.request.urlopen(url, timeout=60, context=_SSL) as r:
        return json.load(r)


def atm_straddle(data, after_date):
    """first expiry >= after_date; ATM = strike minimizing |call.mark − put.mark|."""
    exps = sorted({r["expiration"] for r in data if r.get("expiration", "") >= after_date})
    if not exps:
        return None
    exp = exps[0]
    calls = {float(r["strike"]): r for r in data if r["expiration"] == exp and r["type"] == "call"}
    puts = {float(r["strike"]): r for r in data if r["expiration"] == exp and r["type"] == "put"}
    best = None
    for s in calls.keys() & puts.keys():
        c, p = calls[s], puts[s]
        try:
            cm, pm = float(c.get("mark") or 0), float(p.get("mark") or 0)
        except ValueError:
            continue
        if cm <= 0 or pm <= 0:
            continue
        d = abs(cm - pm)
        if best is None or d < best[0]:
            best = (d, s, cm, pm, c.get("implied_volatility"), c.get("bid"), c.get("ask"), p.get("bid"), p.get("ask"))
    if not best:
        return None
    _, s, cm, pm, iv, cb, ca, pb, pa = best
    return dict(expiry=exp, strike=s, call_mark=cm, put_mark=pm, straddle_pct=(cm + pm) / s * 100, chain_iv=iv,
                call_bid=cb, call_ask=ca, put_bid=pb, put_ask=pa)


def main():
    a = sys.argv[1:]
    mx = int(a[a.index("--max") + 1]) if "--max" in a else 20
    if os.path.exists(STATE):
        st = json.load(open(STATE))
    else:
        st = dict(created=dt.datetime.now().isoformat(timespec="seconds"), items=plan())
        json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=1)
        print(f"plan: {len(st['items'])} events")
    if "--plan" in a:
        return
    k = key(); done = 0
    for it in st["items"]:
        if it["status"] != "pending":
            continue
        if done >= mx:
            break
        fd = dt.date.fromisoformat(it["filing_date"])
        chain_date = fd if it["window"] == "AFTER_MARKET" else prev_trading_day(fd)
        while chain_date.weekday() >= 5:
            chain_date = prev_trading_day(chain_date + dt.timedelta(days=1))
        react = fd + dt.timedelta(days=1) if it["window"] == "AFTER_MARKET" else fd
        try:
            j = fetch_chain(it["symbol"], chain_date.isoformat(), k)
        except Exception as e:
            print(f"  {it['symbol']} {chain_date}: HTTP {e}"); break
        done += 1
        data = j.get("data")
        if data is None:
            msg = (j.get("Information") or j.get("Note") or j.get("Error Message") or str(j))[:160]
            print(f"  {it['symbol']} {chain_date}: no data — {msg}")
            if "premium" in msg.lower():
                it["status"] = "premium"; break
            if "limit" in msg.lower() or "25 requests" in msg:
                done = mx; break
            it["status"] = "nodata"; it["note"] = msg[:80]; continue
        s = atm_straddle(data, react.isoformat())
        if not s:
            it["status"] = "noatm"; print(f"  {it['symbol']} {chain_date}: chain {len(data)} rows, no ATM straddle"); continue
        it.update(s); it["chain_date"] = chain_date.isoformat(); it["status"] = "done"
        it["ratio"] = s["straddle_pct"] / it["implied5"] if it["implied5"] else None
        print(f"  {it['symbol']:5} {chain_date} exp {s['expiry']} K{s['strike']:g} 跨式 {s['straddle_pct']:.2f}% 代理 {it['implied5']:.2f}% 比 {it['ratio']:.2f} 实际 {it['realised']:.2f}%")
        time.sleep(1.2)
    json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=1)
    items = st["items"]; fin = [i for i in items if i["status"] == "done"]
    print(f"progress: done {len(fin)} / pending {sum(i['status']=='pending' for i in items)} / other {sum(i['status'] not in ('done','pending') for i in items)}")
    if fin:
        import statistics as S
        print(f"{'IV档':<8}{'n':>4}{'跨式%':>8}{'代理%':>8}{'比值中位':>9}{'实际>跨式':>10}{'真实edge':>9}")
        for lo, hi in BUCKETS:
            b = [i for i in fin if i["bucket"] == f"{lo}-{hi}"]
            if not b:
                continue
            print(f"{lo}-{hi:<5}{len(b):>4}{S.mean(i['straddle_pct'] for i in b):>8.2f}{S.mean(i['implied5'] for i in b):>8.2f}"
                  f"{S.median(i['ratio'] for i in b):>9.2f}{100*sum(i['realised']>i['straddle_pct'] for i in b)/len(b):>9.0f}%"
                  f"{S.mean(i['straddle_pct']-i['realised'] for i in b):>+9.2f}")
        print(f"{'ALL':<8}{len(fin):>4}{S.mean(i['straddle_pct'] for i in fin):>8.2f}{S.mean(i['implied5'] for i in fin):>8.2f}"
              f"{S.median(i['ratio'] for i in fin):>9.2f}{100*sum(i['realised']>i['straddle_pct'] for i in fin)/len(fin):>9.0f}%"
              f"{S.mean(i['straddle_pct']-i['realised'] for i in fin):>+9.2f}")


if __name__ == "__main__":
    main()
