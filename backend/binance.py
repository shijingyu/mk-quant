"""Binance REST boundary. Decimal filters, read-only preflight, durable order IDs.

The live gateway is deliberately opt-in. Nothing in construction, market-data
downloads, or account inspection changes an exchange account.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import ssl
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from functools import lru_cache
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BinanceError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


class OrderUncertain(BinanceError):
    """The same intent MUST be reconciled, never submitted a second time."""


def decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BinanceError("无效数值") from None
    if not result.is_finite():
        raise BinanceError("数值必须是有限数")
    return result


def floor_step(value: Any, step: Any) -> str:
    value, step = decimal(value), decimal(step)
    if value < 0 or step <= 0:
        raise BinanceError("数量/价格不能为负，精度步长必须大于零")
    return format((value / step).to_integral_value(rounding=ROUND_DOWN) * step, "f")


@lru_cache(maxsize=1)
def _ssl_context():
    # Read the operating system trust store without globally modifying ssl.
    # This supports corporate roots installed in the macOS keychain as well as
    # Linux system stores. Verification is always enabled.
    try:
        import truststore
    except ImportError:
        return ssl.create_default_context()
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            return response.status, dict(response.headers), json.loads(response.read())
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read())
        except (ValueError, UnicodeDecodeError):
            payload = {"code": None}
        return exc.code, dict(exc.headers), payload
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise BinanceError("TLS 证书验证失败；请为 Python 配置受信任的 CA 证书，禁止关闭证书验证", code=-90001) from None
        raise BinanceError("币安请求连接失败，请检查网络、DNS 或代理设置") from None
    except (TimeoutError, OSError, ValueError):
        # URLs include signatures. Do not include raw urllib exception text.
        raise BinanceError("币安请求连接失败或响应无法解析") from None


class MySQLOrderJournal:
    """Durable at-most-once submission claims in the platform's MySQL database.

    A unique INSERT acquires the InnoDB record lock even when competing callers
    observe no existing row. UNKNOWN is committed before the exchange request;
    it is never automatically submitted again, including after a restart.
    """
    def __init__(self):
        from . import db
        db.init_schema()

    def claim(self, account: str, client_id: str, payload: dict) -> tuple[bool, dict]:
        from . import db
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with db.connect() as con:
            result = con.execute("INSERT INTO binance_order_intents VALUES(?,?,?,'UNKNOWN',NULL,?) ON DUPLICATE KEY UPDATE client_id=VALUES(client_id)", (account, client_id, encoded, time.time()))
            if result.rowcount == 1:
                return True, {"status": "UNKNOWN"}
            row = con.execute("SELECT * FROM binance_order_intents WHERE account=? AND client_id=? FOR UPDATE", (account, client_id)).fetchone()
            if row is None:
                raise BinanceError("订单意图状态不可确定，禁止再次提交") from None
            if row["payload"] != encoded:
                raise BinanceError("相同 clientOrderId 不允许绑定不同交易意图")
            return False, dict(row)

    def record(self, account: str, client_id: str, status: str, result: dict):
        from . import db
        with db.connect() as con:
            changed = con.execute("UPDATE binance_order_intents SET status=?,result=?,updated_at=? WHERE account=? AND client_id=?", (status, json.dumps(result), time.time(), account, client_id))
            if changed.rowcount == 0 and con.execute("SELECT 1 FROM binance_order_intents WHERE account=? AND client_id=?", (account, client_id)).fetchone() is None:
                raise BinanceError("订单意图不存在，拒绝保存无关联回执")


class BinanceClient:
    INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}

    def __init__(self, market="futures", api_key="", api_secret="", testnet=False,
                 *, transport=None, sleep=None, clock=None, timeout=15, max_retries=3):
        if market == "perpetual":
            market = "futures"
        if market not in {"spot", "futures"}:
            raise ValueError("market 必须是 spot 或 futures")
        self.market, self.api_key, self._secret, self.testnet = market, api_key, api_secret, bool(testnet)
        self.base_url = ("https://demo-fapi.binance.com" if testnet else "https://fapi.binance.com") if market == "futures" else ("https://testnet.binance.vision" if testnet else "https://api.binance.com")
        self._transport = transport or _transport
        self._sleep, self._clock = sleep or time.sleep, clock or time.time
        self.timeout, self.max_retries = timeout, max_retries
        self._offset_ms, self._cooldown_until, self._last_request = 0, 0.0, 0.0
        self.rate_limit_headers: dict[str, str] = {}
        self._exchange_cache = None
        self._preflight = None
        self._live_enabled = False
        self._journal = None
        self._execution_guard = None
        self._lock = threading.RLock()

    def _request(self, method, path, params=None, *, signed=False, wallet=False):
        params = dict(params or {})
        if signed and (not self.api_key or not self._secret):
            raise BinanceError("需要有效 API Key 和 Secret；公开行情不需要凭证")
        if wallet and self.testnet:
            raise BinanceError("测试环境不提供钱包 SAPI 权限查询，检查结果不能作为实盘授权")
        base = "https://api.binance.com" if wallet else self.base_url
        for attempt in range(self.max_retries + 1):
            now = self._clock()
            if now < self._cooldown_until:
                raise BinanceError("币安限流冷却中，请稍后重试", status=429)
            wait = 0.12 - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
            query_params = dict(params)
            headers = {"Accept": "application/json", "User-Agent": "MKQuant/1"}
            if signed:
                query_params.update(timestamp=int(self._clock() * 1000) + self._offset_ms, recvWindow=5000)
                headers["X-MBX-APIKEY"] = self.api_key
            query = urlencode(query_params)
            if signed:
                query += "&signature=" + hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            url, body = base + path, None
            if method in {"GET", "DELETE"}:
                if query:
                    url += "?" + query
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body = query.encode()
            try:
                self._last_request = self._clock()
                status, response_headers, payload = self._transport(method, url, headers, body, self.timeout)
            except (BinanceError, TimeoutError, OSError) as exc:
                if isinstance(exc, BinanceError) and exc.code == -90001:
                    raise
                if method == "GET" and attempt < self.max_retries:
                    self._sleep(min(2 ** attempt, 8))
                    continue
                if isinstance(exc, BinanceError):
                    raise
                raise BinanceError("币安请求连接失败；写入请求结果可能未知") from None
            self.rate_limit_headers = {k.lower(): str(v) for k, v in response_headers.items() if "weight" in k.lower() or "order-count" in k.lower()}
            if status in {418, 429}:
                headers_lower = {k.lower(): v for k, v in response_headers.items()}
                try:
                    retry = max(1.0, float(headers_lower.get("retry-after", 2 ** attempt)))
                except (ValueError, TypeError):
                    retry = 60.0
                self._cooldown_until = self._clock() + retry
                # Do not shorten Retry-After. Long bans return immediately with a cooldown.
                if method == "GET" and status != 418 and retry <= 10 and attempt < self.max_retries:
                    self._sleep(retry)
                    continue
                raise BinanceError("币安请求频率受限，已进入冷却", status=status)
            if status >= 500 and method == "GET" and attempt < self.max_retries:
                self._sleep(min(2 ** attempt, 8))
                continue
            code = payload.get("code") if isinstance(payload, dict) else None
            if status >= 400 or isinstance(code, int) and code < 0:
                # Never echo server text that could include submitted credentials.
                raise BinanceError(f"币安接口返回错误（HTTP {status}，code {code}）", code=code, status=status)
            return payload
        raise BinanceError("币安请求重试次数已用尽")

    def sync_time(self):
        before = self._clock() * 1000
        data = self._request("GET", "/fapi/v1/time" if self.market == "futures" else "/api/v3/time")
        after = self._clock() * 1000
        self._offset_ms = int(data["serverTime"] - (before + after) / 2)
        return self._offset_ms

    def exchange_info(self, refresh=False) -> dict:
        if self._exchange_cache is not None and not refresh:
            return self._exchange_cache
        raw = self._request("GET", "/fapi/v1/exchangeInfo" if self.market == "futures" else "/api/v3/exchangeInfo")
        symbols = []
        for item in raw.get("symbols", []):
            if item.get("quoteAsset") not in {"USDT", "USDC"}:
                continue
            if self.market == "futures" and item.get("contractType") != "PERPETUAL":
                continue
            filters = {f["filterType"]: f for f in item.get("filters", [])}
            price, lot = filters.get("PRICE_FILTER", {}), filters.get("LOT_SIZE", {})
            market_lot = filters.get("MARKET_LOT_SIZE", lot)
            notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
            symbols.append({"symbol": item["symbol"], "market": self.market, "status": item.get("status"),
                            "base_asset": item["baseAsset"], "quote_asset": item["quoteAsset"],
                            "margin_asset": item.get("marginAsset", item["quoteAsset"]),
                            "tick_size": price.get("tickSize", "0"), "step_size": lot.get("stepSize", "0"),
                            "min_qty": lot.get("minQty", "0"), "max_qty": lot.get("maxQty", "0"),
                            "min_notional": notional.get("minNotional", notional.get("notional", "0")),
                            "market_step_size": market_lot.get("stepSize", lot.get("stepSize", "0")),
                            "market_min_qty": market_lot.get("minQty", lot.get("minQty", "0")),
                            "market_max_qty": market_lot.get("maxQty", lot.get("maxQty", "0")),
                            "order_types": item.get("orderTypes", []), "filters": filters,
                            "spot_allowed": item.get("isSpotTradingAllowed", True)})
        self._exchange_cache = {"market": self.market, "symbols": symbols, "rate_limits": raw.get("rateLimits", []),
                                "fetched_at": int(self._clock() * 1000), "source": "binance"}
        return self._exchange_cache

    def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
        if interval not in self.INTERVALS or not isinstance(symbol, str) or not (4 <= len(symbol) <= 50 and symbol.isalnum() and symbol.endswith(("USDT", "USDC"))):
            raise BinanceError("不支持的交易对或 K 线周期")
        if not isinstance(start_ms, int) or not isinstance(end_ms, int) or start_ms < 0 or end_ms <= start_ms:
            raise BinanceError("K 线区间必须是有效毫秒时间戳，结束时间大于开始时间")
        # End is exclusive. Only candles fully closed before end and now are usable.
        cutoff = min(end_ms, int(self._clock() * 1000) + self._offset_ms)
        cursor, result, seen = start_ms, [], set()
        path = "/fapi/v1/klines" if self.market == "futures" else "/api/v3/klines"
        while cursor < cutoff:
            page = self._request("GET", path, {"symbol": symbol, "interval": interval, "startTime": cursor,
                                                   "endTime": cutoff - 1, "limit": 1000})
            if not isinstance(page, list):
                raise BinanceError("K 线响应格式无效")
            if not page:
                break
            for row in page:
                if not isinstance(row, list) or len(row) < 7:
                    raise BinanceError("K 线记录格式无效")
                stamp, close_time = int(row[0]), int(row[6])
                if start_ms <= stamp < cutoff and close_time < cutoff and stamp not in seen:
                    seen.add(stamp)
                    result.append({"timestamp": stamp, "open": float(decimal(row[1])), "high": float(decimal(row[2])),
                                   "low": float(decimal(row[3])), "close": float(decimal(row[4])),
                                   "volume": float(decimal(row[5])), "close_time": close_time})
            next_cursor = int(page[-1][6]) + 1
            if next_cursor <= cursor:
                raise BinanceError("币安 K 线分页没有前进，已停止下载")
            cursor = next_cursor
        return sorted(result, key=lambda candle: candle["timestamp"])

    download_klines = klines
    fetch_klines = klines

    def commission(self, symbol: str) -> dict:
        if self.market == "futures":
            raw = self._request("GET", "/fapi/v1/commissionRate", {"symbol": symbol}, signed=True)
            return {"maker": raw["makerCommissionRate"], "taker": raw["takerCommissionRate"], "source": "binance_account", "symbol": symbol}
        raw = self._request("GET", "/api/v3/account/commission", {"symbol": symbol}, signed=True)
        standard = raw["standardCommission"]
        return {"maker": standard["maker"], "taker": standard["taker"], "source": "binance_account", "symbol": symbol,
                "buyer": standard.get("buyer", "0"), "seller": standard.get("seller", "0"),
                "tax": raw.get("taxCommission", {}), "special": raw.get("specialCommission", {}),
                "discount": raw.get("discount", {}), "note": "基础费率；方向费用、特殊费用及 BNB 抵扣需单独建模"}

    def inspect_account(self, manifest: dict, symbols: list[str]) -> dict:
        self._preflight = None
        report = {"ok": False, "checks": [], "adjustments": [], "identity": None, "balances": [],
                  "positions": [], "orders": [], "settings": {}, "checked_at": int(self._clock() * 1000),
                  "market": self.market, "testnet": self.testnet}

        def check(name, good, message):
            report["checks"].append({"name": name, "status": "unknown" if good is None else "pass" if good else "fail", "message": message})

        def fetch(name, path, *, wallet=False):
            try:
                data = self._request("GET", path, signed=True, wallet=wallet)
                list_paths = {"/fapi/v3/balance", "/fapi/v1/symbolConfig", "/fapi/v3/positionRisk",
                              "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders", "/api/v3/openOrders"}
                expected = list if path in list_paths else dict
                if not isinstance(data, expected) or expected is list and not all(isinstance(item, dict) for item in data):
                    raise BinanceError("币安账户响应结构不完整，不能认定检查通过")
                if path == "/fapi/v3/positionRisk" and any("positionAmt" not in item for item in data):
                    raise BinanceError("币安持仓响应缺少 positionAmt，不能假定空仓")
                return data
            except BinanceError as exc:
                check(name, None, str(exc))
                return None

        requirements = manifest.get("account", manifest.get("account_requirements", {}))
        required_fields = {"position_modes", "margin_modes", "leverage_min", "leverage_max", "min_capital"}
        declaration_ok = required_fields.issubset(requirements) and isinstance(manifest.get("symbols"), dict)
        check("strategy_contract", declaration_ok, "策略必须声明账户模式、杠杆范围、最低资金和币种约束")
        strategy_market = manifest.get("market", "")
        if strategy_market == "perpetual":
            strategy_market = "futures"
        check("market", strategy_market == self.market, f"账户市场 {self.market}，策略市场 {strategy_market}")
        selection = manifest.get("symbols", {})
        values = selection.get("values", []) if isinstance(selection, dict) else []
        mode = selection.get("mode", "any") if isinstance(selection, dict) else "invalid"
        compatible = bool(symbols) and len(symbols) == len(set(symbols)) and len(symbols) >= selection.get("min_count", 1) if isinstance(selection, dict) else False
        if mode == "fixed":
            compatible = compatible and set(symbols) == set(values)
        elif mode == "multi":
            compatible = compatible and len(symbols) >= max(2, selection.get("min_count", 2)) and (not values or set(symbols).issubset(values))
        elif mode != "any":
            compatible = False
        elif values:
            compatible = compatible and set(symbols).issubset(values)
        check("symbol_constraint", compatible, "已校验策略固定币种/多币种约束")
        if not declaration_ok or strategy_market != self.market or not compatible:
            return report
        try:
            minimum = decimal(requirements["min_capital"])
            low, high = int(requirements["leverage_min"]), int(requirements["leverage_max"])
            if minimum < 0 or low < 1 or high < low:
                raise ValueError()
        except (BinanceError, ValueError, TypeError):
            check("requirements", False, "策略资金或杠杆要求无效")
            return report
        if not self.api_key or not self._secret:
            check("credentials", False, "未配置 API Key / Secret")
            return report
        try:
            self.sync_time()
            info = self.exchange_info(refresh=True)
        except BinanceError as exc:
            check("market_data", None, str(exc))
            return report
        rules = {item["symbol"]: item for item in info["symbols"]}
        for symbol in symbols:
            rule = rules.get(symbol)
            check(f"symbol:{symbol}", bool(rule and rule["status"] == "TRADING" and rule["spot_allowed"]), f"{symbol} 必须处于可交易状态且属于 USDT/USDC 市场")
        permissions = fetch("permissions", "/sapi/v1/account/apiRestrictions", wallet=True)
        if permissions is not None:
            permission = "enableFutures" if self.market == "futures" else "enableSpotAndMarginTrading"
            check("reading_permission", permissions.get("enableReading") is True, "API 查询权限")
            check("trading_permission", permissions.get(permission) is True, "所选市场交易权限")
            report["settings"]["permissions"] = {k: v for k, v in permissions.items() if isinstance(v, bool)}
        if self.market == "futures":
            account = fetch("credentials", "/fapi/v3/account")
            balances = fetch("balances", "/fapi/v3/balance")
            config = fetch("account_settings", "/fapi/v1/accountConfig")
            symbol_config = fetch("symbol_settings", "/fapi/v1/symbolConfig")
            positions = fetch("positions", "/fapi/v3/positionRisk")
            orders = fetch("orders", "/fapi/v1/openOrders")
            algo_orders = fetch("conditional_orders", "/fapi/v1/openAlgoOrders")
            if account is not None:
                check("credentials", True, "签名账户接口验证通过")
            report["balances"] = balances or []
            report["positions"] = [p for p in positions or [] if decimal(p.get("positionAmt", "0")) != 0]
            report["orders"] = list(orders or []) + [dict(o, conditional=True) for o in algo_orders or []]
            aliases = {str(b["accountAlias"]) for b in balances or [] if b.get("accountAlias")}
            if len(aliases) == 1:
                report["identity"] = self._identity("futures:" + next(iter(aliases)))
            check("identity", bool(report["identity"]), "使用交易所账户别名哈希识别同一合约账户；更换 API Key 不改变身份")
            if config is not None:
                check("can_trade", config.get("canTrade") is True, "交易所账户允许交易")
                dual = config.get("dualSidePosition")
                actual_mode = ("hedge" if dual else "one_way") if isinstance(dual, bool) else None
                report["settings"].update(position_mode=actual_mode, multi_assets=config.get("multiAssetsMargin"))
                accepted = requirements["position_modes"]
                good = actual_mode in accepted if actual_mode is not None else None
                check("position_mode", good, f"当前 {actual_mode}；允许 {', '.join(accepted)}")
                if good is False and accepted and accepted[0] in {"one_way", "hedge"}:
                    report["adjustments"].append({"setting": "position_mode", "current": actual_mode, "target": accepted[0]})
                check("collateral_mode", config.get("multiAssetsMargin") is False, "当前仅支持单资产保证金模式；跨资产抵押价值不可直接按 U 相加")
            configs = {p["symbol"]: p for p in symbol_config or []}
            report["settings"]["symbols"] = configs
            for symbol in symbols:
                current = configs.get(symbol)
                if not current:
                    check(f"settings:{symbol}", None, "缺少币种级保证金/杠杆设置")
                    continue
                actual_margin = {"CROSSED": "cross", "ISOLATED": "isolated"}.get(current.get("marginType", "").upper())
                good = actual_margin in requirements["margin_modes"] if actual_margin is not None else None
                check(f"margin:{symbol}", good, f"当前保证金模式 {actual_margin}")
                if good is False and requirements["margin_modes"]:
                    report["adjustments"].append({"setting": "margin_mode", "symbol": symbol, "current": actual_margin, "target": requirements["margin_modes"][0]})
                leverage = int(current.get("leverage", 0))
                good = low <= leverage <= high
                check(f"leverage:{symbol}", good, f"当前 {leverage}x；要求 {low}–{high}x")
                if not good:
                    report["adjustments"].append({"setting": "leverage", "symbol": symbol, "current": leverage, "target": low})
            available = {b["asset"]: decimal(b.get("availableBalance", "0")) for b in balances or []}
        else:
            account = fetch("credentials", "/api/v3/account")
            orders = fetch("orders", "/api/v3/openOrders")
            report["orders"] = orders or []
            report["balances"] = account.get("balances", []) if account else []
            if account is not None:
                check("credentials", True, "签名账户接口验证通过")
                check("can_trade", account.get("canTrade") is True and "SPOT" in account.get("permissions", []), "现货交易账户权限")
                if account.get("uid") is not None:
                    report["identity"] = self._identity("spot:" + str(account["uid"]))
            check("identity", bool(report["identity"]), "使用交易所 uid 哈希识别同一现货账户")
            check("spot_rules", low == high == 1 and requirements["margin_modes"] == ["cash"] and requirements["position_modes"] == ["one_way"], "现货必须为现金、单向、1x，不允许借贷和做空")
            available = {b["asset"]: decimal(b.get("free", "0")) for b in report["balances"]}
            base_assets = {rules[s]["base_asset"] for s in symbols if s in rules}
            report["positions"] = [b for b in report["balances"] if b["asset"] in base_assets and decimal(b.get("free", 0)) + decimal(b.get("locked", 0)) > 0]
            report["settings"].update(position_mode="one_way", leverage=1, margin_mode="cash")
        quote_assets = {rules[s]["margin_asset"] if self.market == "futures" else rules[s]["quote_asset"] for s in symbols if s in rules}
        total_available = sum((available.get(q, Decimal(0)) for q in quote_assets), Decimal(0))
        funded = bool(quote_assets) and all(available.get(q, Decimal(0)) > 0 for q in quote_assets) and total_available >= minimum
        check("available_capital", funded, f"正确交易钱包中可用资金 {total_available} U；最低 {minimum} U；币种 {', '.join(sorted(quote_assets))}")
        report["available_capital"] = str(total_available)
        check("existing_positions", not report["positions"], f"已有 {len(report['positions'])} 笔仓位/现货资产，接管前必须核对")
        check("existing_orders", not report["orders"], f"已有 {len(report['orders'])} 笔普通/条件挂单，接管前必须核对")
        report["monitoring_note"] = "账户检查是瞬时快照；运行时仍需成交流、轮询对账和未管理交易检测。禁止平台外共用账户交易。"
        report["ok"] = bool(report["checks"]) and all(c["status"] == "pass" for c in report["checks"])
        if report["ok"]:
            self._preflight = {"identity": report["identity"], "symbols": set(symbols), "at": self._clock(),
                               "position_mode": report["settings"]["position_mode"]}
        return report

    check_account = inspect_account

    def _identity(self, exchange_identity):
        realm = "testnet" if self.testnet else "production"
        return hashlib.sha256(f"binance:{realm}:{exchange_identity}".encode()).hexdigest()

    def apply_adjustments(self, manifest, symbols, *, confirmed=False):
        if confirmed is not True:
            raise BinanceError("调整账户设置需要明确确认")
        if self.market != "futures":
            raise BinanceError("现货账户没有杠杆或保证金模式调整")
        report = self.inspect_account(manifest, symbols)
        adjustable = {"position_mode"} | {f"margin:{s}" for s in symbols} | {f"leverage:{s}" for s in symbols}
        if any(c["status"] == "unknown" or c["status"] != "pass" and c["name"] not in adjustable for c in report["checks"]):
            raise BinanceError("账户有未通过/未知检查项，禁止修改设置")
        applied = []
        for adjustment in report["adjustments"]:
            # Guard again immediately before every mutation, account-wide even for symbol settings.
            positions = self._request("GET", "/fapi/v3/positionRisk", signed=True)
            orders = self._request("GET", "/fapi/v1/openOrders", signed=True)
            algos = self._request("GET", "/fapi/v1/openAlgoOrders", signed=True)
            if any(decimal(p["positionAmt"]) != 0 for p in positions) or orders or algos:
                raise BinanceError("账户出现仓位或挂单，已停止设置调整")
            setting, target = adjustment["setting"], adjustment["target"]
            if setting == "position_mode":
                path, params = "/fapi/v1/positionSide/dual", {"dualSidePosition": "true" if target == "hedge" else "false"}
            elif setting == "margin_mode":
                path, params = "/fapi/v1/marginType", {"symbol": adjustment["symbol"], "marginType": {"cross": "CROSSED", "isolated": "ISOLATED"}[target]}
            else:
                path, params = "/fapi/v1/leverage", {"symbol": adjustment["symbol"], "leverage": target}
            self._request("POST", path, params, signed=True)
            applied.append(adjustment)
        final = self.inspect_account(manifest, symbols)
        final["applied"] = applied
        return final

    def configure_live(self, *, enabled: bool, journal: MySQLOrderJournal, execution_guard: Callable[[], bool]):
        if enabled and (not isinstance(journal, MySQLOrderJournal) or not callable(execution_guard)):
            raise BinanceError("实盘执行需要持久订单账本与有效执行权检查")
        self._live_enabled, self._journal, self._execution_guard = enabled is True, journal, execution_guard

    def normalize_order(self, symbol, quantity, *, price=None, order_type="MARKET", reference_price=None):
        rule = next((r for r in self.exchange_info()["symbols"] if r["symbol"] == symbol), None)
        if not rule or rule["status"] != "TRADING":
            raise BinanceError("交易对不可交易")
        is_market = order_type == "MARKET"
        step = decimal(rule["step_size"])
        market_step = decimal(rule["market_step_size"]) if is_market else Decimal(0)
        if is_market and market_step > 0:
            exponent = min(step.as_tuple().exponent, market_step.as_tuple().exponent)
            scale = Decimal(10) ** -exponent
            step = Decimal(math.lcm(int(step * scale), int(market_step * scale))) / scale
        qty = decimal(floor_step(quantity, step))
        minimum, maximum = decimal(rule["min_qty"]), decimal(rule["max_qty"])
        if is_market:
            minimum = max(minimum, decimal(rule["market_min_qty"]))
            market_maximum = decimal(rule["market_max_qty"])
            if market_maximum > 0:
                maximum = min(maximum, market_maximum) if maximum > 0 else market_maximum
        if qty <= 0 or qty < minimum or maximum > 0 and qty > maximum:
            raise BinanceError("订单数量不满足币安 LOT_SIZE/MARKET_LOT_SIZE")
        normalized = {"quantity": format(qty, "f")}
        if price is not None:
            normalized["price"] = floor_step(price, rule["tick_size"])
        valuation = normalized.get("price", reference_price)
        if valuation is None or decimal(valuation) <= 0:
            raise BinanceError("名义金额校验需要有效价格；市价单必须提供新鲜参考价格")
        notional = qty * decimal(valuation)
        if notional < decimal(rule["min_notional"]):
            raise BinanceError("订单低于币安最低名义金额")
        max_notional = decimal(rule["filters"].get("NOTIONAL", {}).get("maxNotional", "0"))
        if max_notional > 0 and notional > max_notional:
            raise BinanceError("订单超过币安最大名义金额")
        if "price" in normalized:
            price_filter = rule["filters"].get("PRICE_FILTER", {})
            limit_price = decimal(normalized["price"])
            if limit_price < decimal(price_filter.get("minPrice", "0")) or decimal(price_filter.get("maxPrice", "0")) > 0 and limit_price > decimal(price_filter["maxPrice"]):
                raise BinanceError("限价不满足币安 PRICE_FILTER")
        return normalized

    def submit_order(self, symbol, side, quantity, *, client_order_id, order_type="MARKET", price=None,
                     reference_price=None, reference_timestamp_ms=None, reduce_only=False, position_side="BOTH"):
        with self._lock:
            if not self._live_enabled or not self._journal or not self._execution_guard:
                raise BinanceError("实盘网关未启用，拒绝下单")
            preflight = self._preflight
            if not preflight or self._clock() - preflight["at"] > 60 or symbol not in preflight["symbols"]:
                raise BinanceError("账户接入检查缺失或过期，拒绝下单")
            if self._execution_guard() is not True:
                raise BinanceError("执行租约/停止标记检查未通过，拒绝下单")
            if side not in {"BUY", "SELL"} or order_type not in {"MARKET", "LIMIT"}:
                raise BinanceError("当前网关只支持 BUY/SELL 的 MARKET/LIMIT 普通订单")
            if not re.fullmatch(r"mk_[A-Za-z0-9_-]{1,32}", client_order_id):
                raise BinanceError("clientOrderId 必须为 mk_ 前缀且不超过 35 字符")
            if order_type == "LIMIT" and price is None:
                raise BinanceError("限价单必须提供价格")
            if order_type == "MARKET" and (reference_timestamp_ms is None or not 0 <= self._clock() * 1000 - reference_timestamp_ms <= 5000):
                raise BinanceError("市价单参考行情超过 5 秒或时间无效")
            if self.market == "spot" and (position_side != "BOTH" or reduce_only):
                raise BinanceError("现货不支持双向仓位或 reduceOnly")
            if self.market == "futures":
                hedge = preflight["position_mode"] == "hedge"
                if hedge and (position_side not in {"LONG", "SHORT"} or reduce_only):
                    raise BinanceError("双向模式必须指定 LONG/SHORT，且不能发送 reduceOnly")
                if not hedge and position_side != "BOTH":
                    raise BinanceError("单向模式只允许 BOTH")
            normalized = self.normalize_order(symbol, quantity, price=price, order_type=order_type, reference_price=reference_price)
            if self.market == "spot" and side == "SELL":
                raw = self._request("GET", "/api/v3/account", signed=True)
                rule = next(r for r in self.exchange_info()["symbols"] if r["symbol"] == symbol)
                owned = next((decimal(b["free"]) for b in raw.get("balances", []) if b["asset"] == rule["base_asset"]), Decimal(0))
                if decimal(normalized["quantity"]) > owned:
                    raise BinanceError("现货可用资产不足，禁止做空")
            params = dict(symbol=symbol, side=side, type=order_type, newClientOrderId=client_order_id, **normalized)
            if order_type == "LIMIT":
                params["timeInForce"] = "GTC"
            if self.market == "futures":
                params["positionSide"] = position_side
                if reduce_only:
                    params["reduceOnly"] = "true"
            account = preflight["identity"]
            first, old = self._journal.claim(account, client_order_id, params)
            if not first:
                if old["status"] == "CONFIRMED":
                    return json.loads(old["result"])
                if old["status"] == "REJECTED":
                    raise BinanceError("该交易意图已被拒绝；不会重复发送")
                return self.reconcile_order(symbol, client_order_id, identity=account)
            if self._execution_guard() is not True:
                self._journal.record(account, client_order_id, "REJECTED", {"reason": "execution_guard"})
                raise BinanceError("发送前执行权检查未通过")
            try:
                result = self._request("POST", "/fapi/v1/order" if self.market == "futures" else "/api/v3/order", params, signed=True)
            except BinanceError as exc:
                if exc.status and 400 <= exc.status < 500 and exc.status not in {408, 418, 429} and exc.code not in {-1006, -1007}:
                    self._journal.record(account, client_order_id, "REJECTED", {"code": exc.code, "http_status": exc.status})
                    raise
                return self.reconcile_order(symbol, client_order_id, identity=account)
            self._journal.record(account, client_order_id, "CONFIRMED", result)
            return result

    def reconcile_order(self, symbol, client_order_id, *, identity=None):
        if not self._journal:
            raise BinanceError("缺少持久订单账本")
        identity = identity or (self._preflight or {}).get("identity")
        if not identity:
            raise BinanceError("缺少已验证账户身份")
        try:
            result = self._request("GET", "/fapi/v1/order" if self.market == "futures" else "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True)
        except BinanceError:
            raise OrderUncertain("订单状态未知，已禁止重发；必须按 clientOrderId 持续对账") from None
        self._journal.record(identity, client_order_id, "CONFIRMED", result)
        return result
