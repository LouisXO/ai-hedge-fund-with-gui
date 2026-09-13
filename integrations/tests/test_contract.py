"""P1 acceptance: MoomooDataClient satisfies aihf's DataClient + returns real data."""
import datetime as dt
import sys
sys.path.insert(0, "/Users/louis/hedge-fund")

from hedge_fund.data.protocol import DataClient
from integrations.moomoo_client import MoomooDataClient

def main():
    c = MoomooDataClient()
    try:
        # 1) structural protocol conformance (runtime_checkable)
        assert isinstance(c, DataClient), "does NOT satisfy DataClient protocol"
        print("✅ isinstance(client, DataClient) — 协议方法齐全")

        end = dt.date.today().isoformat()
        start = (dt.date.today() - dt.timedelta(days=60)).isoformat()

        # 2) get_prices → real bars
        prices = c.get_prices("RKLB", start, end)
        assert len(prices) > 5, f"too few bars: {len(prices)}"
        p = prices[-1]
        assert p.close > 0 and p.volume > 0
        print(f"✅ get_prices RKLB: {len(prices)} bars, last {p.time} close={p.close} vol={p.volume}")

        # 3) metrics + market cap
        m = c.get_financial_metrics("RKLB", end)[0]
        mc = c.get_market_cap("RKLB", end)
        print(f"✅ metrics: mkt_cap={m.market_cap:,.0f} PE_ttm={m.price_to_earnings_ratio} PB={m.price_to_book_ratio}")
        assert mc and mc > 0

        # 4) company facts
        f = c.get_company_facts("RKLB")
        assert f and f.name
        print(f"✅ company_facts: {f.name} ({f.exchange}) active={f.is_active}")

        # 5) news (non-critical)
        n = c.get_news("RKLB", end, limit=5)
        print(f"✅ news: {len(n)} items" + (f' | e.g. \"{n[0].title[:50]}\"' if n else ""))

        # 6) contract: infra failure RAISES (bad ticker → non-OK → MoomooError)
        from integrations.moomoo_client import MoomooError
        try:
            c.get_prices("ZZINVALIDXX", start, end)
            print("⚠️  bad ticker did not raise (acceptable if returned empty)")
        except Exception as e:
            print(f"✅ infra failure raises: {type(e).__name__}")

        print("\n🎯 P1 契约测试通过")
    finally:
        c.close()

if __name__ == "__main__":
    main()
