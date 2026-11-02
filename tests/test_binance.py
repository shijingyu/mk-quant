import hashlib
import hmac
import json
import unittest
import pytest
from urllib.parse import parse_qs, urlsplit

from backend.binance import BinanceClient, BinanceError, OrderUncertain, MySQLOrderJournal, floor_step


MANIFEST = {
    "market": "futures", "symbols": {"mode": "any", "values": [], "min_count": 1},
    "account": {"position_modes": ["one_way"], "margin_modes": ["cross"],
                "leverage_min": 1, "leverage_max": 5, "min_capital": 100},
}


def exchange_symbol(symbol="BTCUSDT", quote="USDT"):
    return {"symbol": symbol, "baseAsset": "BTC", "quoteAsset": quote, "marginAsset": quote,
            "contractType": "PERPETUAL", "status": "TRADING", "orderTypes": ["LIMIT", "MARKET"],
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "1", "maxPrice": "10000000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]}


class FakeExchange:
    def __init__(self):
        self.now = 2000.0
        self.calls = []
        self.sleeps = []
        self.routes = {
            "/fapi/v1/time": {"serverTime": 2000000},
            "/fapi/v1/exchangeInfo": {"symbols": [exchange_symbol(), exchange_symbol("BTCUSDC", "USDC")]},
            "/sapi/v1/account/apiRestrictions": {"enableReading": True, "enableFutures": True},
            "/fapi/v3/account": {"canTrade": True},
            "/fapi/v3/balance": [{"asset": "USDT", "accountAlias": "account-7", "availableBalance": "1000"}],
            "/fapi/v1/accountConfig": {"canTrade": True, "dualSidePosition": False, "multiAssetsMargin": False},
            "/fapi/v1/symbolConfig": [{"symbol": "BTCUSDT", "marginType": "CROSSED", "leverage": 3}],
            "/fapi/v3/positionRisk": [], "/fapi/v1/openOrders": [], "/fapi/v1/openAlgoOrders": [],
        }

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def transport(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        params = parse_qs(body.decode() if body else parts.query)
        self.calls.append((method, parts.path, params, headers, body))
        value = self.routes.get(parts.path)
        if callable(value):
            value = value(method, params)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return value
        if value is None:
            return 404, {}, {"code": -2013}
        return 200, {}, value

    def client(self, **kwargs):
        return BinanceClient(api_key="test-key", api_secret="test-secret", transport=self.transport,
                             sleep=self.sleep, clock=lambda: self.now, max_retries=1, **kwargs)


@pytest.mark.usefixtures("mysql_database")
class BinanceTests(unittest.TestCase):
    def test_decimal_filters_use_step_not_display_precision(self):
        self.assertEqual(floor_step("1.29999999", "0.05"), "1.25")
        exchange = FakeExchange()
        client = exchange.client()
        normalized = client.normalize_order("BTCUSDT", "0.001999", price="50000.19", order_type="LIMIT")
        self.assertEqual(normalized, {"quantity": "0.001", "price": "50000.10"})
        with self.assertRaises(BinanceError):
            client.normalize_order("BTCUSDT", "0.001", reference_price="1000")
        with self.assertRaises(BinanceError):
            client.normalize_order("BTCUSDT", "11", reference_price="50000")

    def test_signing_hmac_does_not_send_secret(self):
        exchange = FakeExchange()
        client = exchange.client()
        client._request("GET", "/fapi/v3/account", signed=True)
        _, _, params, headers, _ = exchange.calls[-1]
        self.assertEqual(headers["X-MBX-APIKEY"], "test-key")
        encoded = "timestamp=2000000&recvWindow=5000"
        signature = hmac.new(b"test-secret", encoded.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(params["signature"], [signature])
        self.assertNotIn("test-secret", repr(exchange.calls))

    def test_public_download_paginates_and_excludes_open_candles(self):
        exchange = FakeExchange()
        pages = [
            [[0, "1", "2", "1", "2", "10", 59999], [60000, "2", "3", "2", "3", "20", 119999]],
            [[120000, "3", "4", "3", "4", "30", 179999], [180000, "4", "5", "4", "5", "40", 239999]],
        ]
        exchange.routes["/fapi/v1/klines"] = lambda method, params: pages.pop(0)
        result = exchange.client().klines("BTCUSDT", "1m", 0, 200000)
        self.assertEqual([c["timestamp"] for c in result], [0, 60000, 120000])
        self.assertEqual(exchange.calls[1][2]["startTime"], ["120000"])
        self.assertNotIn("X-MBX-APIKEY", exchange.calls[0][3])

    def test_read_retry_honors_retry_after(self):
        exchange = FakeExchange()
        responses = [(429, {"Retry-After": "3"}, {"code": -1003}), (200, {}, {"serverTime": 2000000})]
        exchange.routes["/fapi/v1/time"] = lambda method, params: responses.pop(0)
        exchange.client().sync_time()
        self.assertIn(3, exchange.sleeps)
        self.assertEqual(len(exchange.calls), 2)

    def test_long_rate_limit_does_not_retry_early(self):
        exchange = FakeExchange()
        exchange.routes["/fapi/v1/time"] = (429, {"Retry-After": "120"}, {"code": -1003})
        client = exchange.client()
        with self.assertRaises(BinanceError):
            client.sync_time()
        with self.assertRaises(BinanceError):
            client.sync_time()
        self.assertEqual(len(exchange.calls), 1)

    def test_account_check_is_read_only_and_identity_survives_key_rotation(self):
        exchange = FakeExchange()
        client = exchange.client()
        report = client.inspect_account(MANIFEST, ["BTCUSDT"])
        self.assertTrue(report["ok"], report)
        self.assertTrue(all(c[0] == "GET" for c in exchange.calls))
        client.api_key = "different-key"
        rotated = client.inspect_account(MANIFEST, ["BTCUSDT"])
        self.assertEqual(report["identity"], rotated["identity"])
        self.assertEqual(report["available_capital"], "1000")

    def test_conditional_orders_block_account_adoption_and_setting_changes(self):
        exchange = FakeExchange()
        exchange.routes["/fapi/v1/openAlgoOrders"] = [{"algoId": 1, "symbol": "BTCUSDT"}]
        exchange.routes["/fapi/v1/symbolConfig"][0]["leverage"] = 20
        client = exchange.client()
        report = client.inspect_account(MANIFEST, ["BTCUSDT"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["adjustments"][0]["target"], 1)
        with self.assertRaises(BinanceError):
            client.apply_adjustments(MANIFEST, ["BTCUSDT"], confirmed=True)
        self.assertTrue(all(c[0] == "GET" for c in exchange.calls))

    def test_unknown_permissions_never_pass(self):
        exchange = FakeExchange()
        exchange.routes["/sapi/v1/account/apiRestrictions"] = (403, {}, {"code": -2015})
        report = exchange.client().inspect_account(MANIFEST, ["BTCUSDT"])
        self.assertFalse(report["ok"])
        self.assertIn("unknown", [c["status"] for c in report["checks"]])

    def test_usdt_balance_does_not_fund_usdc_market(self):
        exchange = FakeExchange()
        report = exchange.client().inspect_account(MANIFEST, ["BTCUSDC"])
        capital = next(c for c in report["checks"] if c["name"] == "available_capital")
        self.assertEqual(capital["status"], "fail")

    def test_live_disabled_by_default(self):
        exchange = FakeExchange()
        with self.assertRaises(BinanceError):
            exchange.client().submit_order("BTCUSDT", "BUY", "0.01", client_order_id="mk_first")
        self.assertEqual(exchange.calls, [])

    def live_client(self, exchange):
        client = exchange.client()
        self.assertTrue(client.inspect_account(MANIFEST, ["BTCUSDT"])["ok"])
        client.configure_live(enabled=True, journal=MySQLOrderJournal(), execution_guard=lambda: True)
        return client

    def submit(self, client, exchange, **kwargs):
        return client.submit_order("BTCUSDT", "BUY", "0.01", client_order_id="mk_first", order_type="LIMIT", price="50000", **kwargs)

    def test_timeout_reconciles_once_by_id_without_second_post(self):
        exchange = FakeExchange()
        exchange.routes["/fapi/v1/order"] = lambda method, params: TimeoutError() if method == "POST" else {"orderId": 42, "status": "FILLED", "clientOrderId": "mk_first"}
        client = self.live_client(exchange)
        result = self.submit(client, exchange)
        self.assertEqual(result["orderId"], 42)
        restarted = self.live_client(exchange)
        self.assertEqual(self.submit(restarted, exchange)["orderId"], 42)
        posts = [c for c in exchange.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        queries = [c for c in exchange.calls if c[1] == "/fapi/v1/order" and c[0] == "GET"]
        self.assertEqual(queries[0][2]["origClientOrderId"], ["mk_first"])

    def test_not_found_after_timeout_remains_unknown_across_restart(self):
        exchange = FakeExchange()
        exchange.routes["/fapi/v1/order"] = lambda method, params: TimeoutError() if method == "POST" else (400, {}, {"code": -2013})
        client = self.live_client(exchange)
        with self.assertRaises(OrderUncertain):
            self.submit(client, exchange)
        restarted = self.live_client(exchange)
        with self.assertRaises(OrderUncertain):
            self.submit(restarted, exchange)
        self.assertEqual(len([c for c in exchange.calls if c[0] == "POST"]), 1)

    def test_same_intent_id_cannot_change_quantity(self):
        journal = MySQLOrderJournal()
        self.assertTrue(journal.claim("account", "mk_one", {"quantity": "1"})[0])
        self.assertFalse(journal.claim("account", "mk_one", {"quantity": "1"})[0])
        with self.assertRaises(BinanceError):
            journal.claim("account", "mk_one", {"quantity": "2"})

    def test_fencing_guard_prevents_submission(self):
        exchange = FakeExchange()
        client = self.live_client(exchange)
        client._execution_guard = lambda: False
        with self.assertRaises(BinanceError):
            self.submit(client, exchange)
        self.assertFalse(any(c[0] == "POST" for c in exchange.calls))

    def test_missing_strategy_contract_fails_without_network(self):
        exchange = FakeExchange()
        report = exchange.client().inspect_account({"market": "futures"}, ["BTCUSDT"])
        self.assertFalse(report["ok"])
        self.assertEqual(exchange.calls, [])


if __name__ == "__main__":
    unittest.main()
