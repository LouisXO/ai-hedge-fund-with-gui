"""Alpaca PAPER trading client — the only place the agent talks to a broker.

Hard guarantees, checked at construction and on every call:
  * the base URL is the paper endpoint (never api.alpaca.markets);
  * the key id starts with "PK" (Alpaca's paper prefix);
  * the account the key resolves to reports itself as a paper account.
Any of these failing raises before an order can be built. moomoo stays
read-only as before: this module never imports it and the two never share
credentials.

Plain urllib, no SDK: the surface we use is four GETs and one POST.
Order semantics we rely on (docs.alpaca.markets/docs/orders-at-alpaca):
  * time_in_force="opg" + type="limit"  = limit-on-open, the backtest's
    "fill at the next open" with a price cap; submitted after 09:28 ET it
    is queued for the NEXT session's opening auction;
  * time_in_force="opg" + type="market" = market-on-open, used for exits;
  * OPG orders are whole-share only.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request

PAPER_URL = "https://paper-api.alpaca.markets"
MAX_CLIENT_ID = 128


class BrokerError(RuntimeError):
    pass


class NotPaperAccount(BrokerError):
    pass


class PaperBroker:
    def __init__(self, key_id: str, secret: str, base_url: str = PAPER_URL, timeout: float = 20.0):
        if not base_url.startswith(PAPER_URL):
            raise NotPaperAccount(f"refusing non-paper endpoint {base_url}")
        if not key_id.startswith("PK"):
            raise NotPaperAccount("refusing key without the PK (paper) prefix")
        self.base = base_url.rstrip("/") + "/v2"
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret,
                        "Content-Type": "application/json", "User-Agent": "optradar-agent"}
        self.timeout = timeout

    # -- transport -----------------------------------------------------------------
    def _req(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            raise BrokerError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from None

    # -- reads ---------------------------------------------------------------------
    def account(self) -> dict:
        a = self._req("GET", "/account")
        if not str(a.get("account_number", "")).startswith("PA"):
            raise NotPaperAccount(f"account {a.get('account_number')} is not a paper account")
        return a

    def clock(self) -> dict:
        return self._req("GET", "/clock")

    def calendar(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """Trading sessions in [start, end] — the exchange calendar, holidays removed."""
        rows = self._req("GET", "/calendar", {"start": start.isoformat(), "end": end.isoformat()}) or []
        return [dt.date.fromisoformat(r["date"]) for r in rows]

    def positions(self) -> dict[str, dict]:
        return {p["symbol"]: p for p in self._req("GET", "/positions") or []}

    def open_orders(self) -> list[dict]:
        return self._req("GET", "/orders", {"status": "open", "limit": 500, "nested": "false"}) or []

    def orders_since(self, after: dt.datetime, status: str = "all") -> list[dict]:
        out, page_after = [], after.isoformat()
        while True:
            chunk = self._req("GET", "/orders", {"status": status, "limit": 500, "direction": "asc",
                                                 "after": page_after, "nested": "false"}) or []
            out.extend(chunk)
            if len(chunk) < 500:
                return out
            page_after = chunk[-1]["submitted_at"]

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        try:
            return self._req("GET", "/orders:by_client_order_id", {"client_order_id": client_order_id})
        except BrokerError as e:
            if "404" in str(e):
                return None
            raise

    def fills(self, after: dt.datetime) -> list[dict]:
        """Account activities of type FILL after `after` (UTC-aware or naive-UTC)."""
        out, token = [], None
        while True:
            params = {"activity_types": "FILL", "after": after.isoformat(), "direction": "asc", "page_size": 100}
            if token:
                params["page_token"] = token
            chunk = self._req("GET", "/account/activities", params) or []
            out.extend(chunk)
            if len(chunk) < 100:
                return out
            token = chunk[-1]["id"]

    # -- writes --------------------------------------------------------------------
    def submit(self, symbol: str, side: str, qty: int, order_type: str, tif: str,
               client_order_id: str, limit_price: float | None = None) -> dict:
        if side not in ("buy", "sell") or order_type not in ("market", "limit") or tif not in ("opg", "day"):
            raise BrokerError(f"bad order spec {symbol} {side} {order_type} {tif}")
        if qty < 1 or int(qty) != qty:
            raise BrokerError(f"whole shares only: {symbol} qty {qty}")
        if len(client_order_id) > MAX_CLIENT_ID:
            raise BrokerError("client_order_id too long")
        body = {"symbol": symbol, "qty": str(int(qty)), "side": side, "type": order_type,
                "time_in_force": tif, "client_order_id": client_order_id}
        if order_type == "limit":
            if limit_price is None or limit_price <= 0:
                raise BrokerError(f"limit order without price: {symbol}")
            body["limit_price"] = f"{limit_price:.2f}" if limit_price >= 1 else f"{limit_price:.4f}"
        return self._req("POST", "/orders", body=body)

    def cancel(self, order_id: str) -> None:
        self._req("DELETE", f"/orders/{order_id}")


def from_env(path: str | None = None) -> PaperBroker:
    """Credentials from ~/.hedge-fund/.env (chmod 600), same file the bars fetcher uses."""
    from agent.sources.price_probe import keys_from_env
    key, secret = keys_from_env("alpaca")
    return PaperBroker(key, secret)
