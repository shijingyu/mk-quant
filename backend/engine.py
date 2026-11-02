"""Deterministic, bar-based execution shared by strategy experiments.

Legacy market signals fill at the *next* common timestamp's open. An explicit
``execution.model='maker_grid_v1'`` instead selects the persistent hedge/lot
order book: resting GTX limits, contingent GTX stop-limits, strict penetration
and conservative later-bar eligibility. OHLC never establishes real maker
queue priority, intrabar price ordering or liquidity.
No terminal close is invented: final positions remain marked at the last close.
Bars are intersected across symbols, fees are charged on every fill and Decimal
arithmetic enforces tick/quantity filters. Market fills use adverse fixed
slippage; this model does not simulate order books, funding, borrowing or
exchange liquidation. Those omissions are returned in ``assumptions``.

Run train and validation separately with a new strategy and starting capital.
``run_split_backtest`` enforces disjoint chronological intervals and records
source/data/config hashes so experiment results can be reproduced.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from .strategy import load_strategy, validate_parameters, validate_strategy_source, validate_symbols


ENGINE_VERSION = "1.2.0"
ZERO = Decimal("0")
ONE = Decimal("1")


class BacktestCancelled(RuntimeError):
    pass


def _decimal(value, label="value"):
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须为有限数字")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须为有限数字") from exc
    if not result.is_finite():
        raise ValueError(f"{label} 必须为有限数字")
    return result


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("计算结果超出有限数字范围")
    return result


def _timestamp(value):
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("日期字符串必须包含时区")
            return int(parsed.timestamp() * 1000)
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError("K 线 timestamp 必须为秒/毫秒时间戳或带时区的 ISO 日期")
    if value < 0 or int(value) != value:
        raise ValueError("K 线时间戳必须为非负整数")
    return int(value)


def _prepare_candles(candles):
    if not isinstance(candles, dict) or not candles:
        raise ValueError("需要按交易对分组的 K 线数据")
    prepared = {}
    for symbol, rows in candles.items():
        if not isinstance(symbol, str) or not symbol.isupper() or not symbol.isalnum():
            raise ValueError("交易对必须为大写字母数字，如 BTCUSDT")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{symbol} 没有 K 线数据")
        clean = {}
        previous = -1
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{symbol} K 线必须为字典")
            timestamp = _timestamp(row.get("timestamp"))
            if timestamp <= previous:
                raise ValueError(f"{symbol} K 线时间必须严格递增，不能重复")
            bar = {"timestamp": timestamp}
            for key in ("open", "high", "low", "close", "volume"):
                value = _decimal(row.get(key, 0 if key == "volume" else None), f"{symbol}.{key}")
                if value < 0 or (key != "volume" and value == 0):
                    raise ValueError(f"{symbol}.{key} 必须为正数；成交量可为零")
                bar[key] = _float(value)
            if bar["high"] < max(bar["open"], bar["low"], bar["close"]) or bar["low"] > min(bar["open"], bar["high"], bar["close"]):
                raise ValueError(f"{symbol} 的 K 线高低价不一致")
            clean[timestamp] = bar
            previous = timestamp
        prepared[symbol] = clean
    timestamps = sorted(set.intersection(*(set(rows) for rows in prepared.values())))
    if len(timestamps) < 2:
        raise ValueError("所有交易对至少需要两根共同时间戳的 K 线")
    return prepared, timestamps


def _filters_for(symbol, filters, *, market_order=True):
    value = (filters or {}).get(symbol, {})
    if isinstance(value, list):
        value = {item["filterType"]: item for item in value}
    if not isinstance(value, dict):
        raise ValueError(f"{symbol} 交易规则无效")
    result = {}
    alternatives = {
        "tick_size": ("tickSize", "PRICE_FILTER", "tickSize", "0.00000001"),
        "step_size": ("stepSize", "LOT_SIZE", "stepSize", "0.00000001"),
        "min_qty": ("minQty", "LOT_SIZE", "minQty", "0"),
        "max_qty": ("maxQty", "LOT_SIZE", "maxQty", "0"),
        "min_notional": ("minNotional", "MIN_NOTIONAL", "minNotional", "0"),
    }
    for key, (camel, nested, inner, default) in alternatives.items():
        raw = value.get(key, value.get(camel, value.get(nested, {}).get(inner, default)))
        if key == "min_notional" and raw == default:
            raw = value.get("NOTIONAL", {}).get("minNotional", value.get("MIN_NOTIONAL", {}).get("notional", raw))
        result[key] = _decimal(raw, f"{symbol}.{key}")
        if result[key] < 0 or (key in {"tick_size", "step_size"} and result[key] == 0):
            raise ValueError(f"{symbol}.{key} 无效")
    if not market_order:
        return result
    # MARKET_LOT_SIZE uses zero to disable an individual constraint. Respect
    # both lot filters; a coarser market step must still be a lot-step multiple.
    market_filter = value.get("MARKET_LOT_SIZE", value.get("filters", {}).get("MARKET_LOT_SIZE", {}))
    market_step = _decimal(value.get("market_step_size", market_filter.get("stepSize", "0")))
    if market_step > 0:
        exponent = min(result["step_size"].as_tuple().exponent, market_step.as_tuple().exponent)
        scale = Decimal(10) ** -exponent
        result["step_size"] = Decimal(math.lcm(int(result["step_size"] * scale), int(market_step * scale))) / scale
    market_min = _decimal(value.get("market_min_qty", market_filter.get("minQty", "0")))
    market_max = _decimal(value.get("market_max_qty", market_filter.get("maxQty", "0")))
    if min(market_step, market_min, market_max) < 0:
        raise ValueError(f"{symbol} 市价数量过滤器不可为负")
    result["min_qty"] = max(result["min_qty"], market_min)
    if market_max > 0:
        result["max_qty"] = min(result["max_qty"], market_max) if result["max_qty"] > 0 else market_max
    return result


def _quantize(value, quantum, rounding=ROUND_FLOOR):
    return (value / quantum).to_integral_value(rounding=rounding) * quantum


@dataclass
class Position:
    quantity: Decimal = ZERO
    average_price: Decimal = ZERO
    entry_fees: Decimal = ZERO


class Ledger:
    """One-way position ledger. Spot is the same cash equity identity at 1x."""

    def __init__(self, initial_capital, market, leverage, fee_rate, position_mode="one_way"):
        if position_mode not in {"one_way", "hedge"} or (market == "spot" and position_mode != "one_way"):
            raise ValueError("现货只支持 one_way；合约持仓模式必须为 one_way 或 hedge")
        self.position_mode = position_mode
        self.pending_margin = ZERO
        self.pending_fees = ZERO
        self.initial = _decimal(initial_capital)
        self.balance = self.initial
        self.market = market
        self.leverage = _decimal(leverage)
        self.fee_rate = _decimal(fee_rate)
        if self.initial <= 0 or self.leverage < 1 or self.leverage != self.leverage.to_integral_value() or not ZERO <= self.fee_rate <= Decimal(".1"):
            raise ValueError("账本初始资金、杠杆或手续费参数无效")
        if market == "spot" and self.leverage != ONE:
            raise ValueError("现货账本不可使用杠杆")
        self.positions = {}
        self.prices = {}
        self.orders = []
        self.closed_pnls = []
        self.fees = ZERO

    def unrealized(self):
        return sum(((self.prices[symbol.split(":")[0]] - position.average_price) * position.quantity
                    for symbol, position in self.positions.items()), ZERO)

    def equity(self):
        return self.balance + self.unrealized()

    def margin(self):
        return sum((abs(position.quantity) * self.prices[symbol.split(":")[0]] / self.leverage
                    for symbol, position in self.positions.items()), ZERO)

    def account(self):
        equity = self.equity()
        margin = self.margin()
        return {"initial_equity": _float(self.initial), "balance": _float(self.balance),
                "equity": _float(equity), "available": _float(max(ZERO, equity - margin - self.pending_margin - self.pending_fees)),
                "pending_margin": _float(self.pending_margin), "pending_fee_reserve": _float(self.pending_fees),
                "margin_available": _float(max(ZERO, equity - margin - self.pending_margin - self.pending_fees)),
                "margin_used": _float(margin), "unrealized_pnl": _float(self.unrealized()),
                "realized_pnl": _float(self.balance - self.initial),
                "leverage": _float(self.leverage), "market": self.market,
                "position_mode": self.position_mode, "margin_mode": "cash" if self.market == "spot" else "cross"}

    def position_snapshot(self):
        return {symbol: {"symbol": symbol.split(":")[0], "position_side": symbol.split(":")[1] if ":" in symbol else ("LONG" if p.quantity > 0 else "SHORT"),
                         "quantity": _float(p.quantity), "average_price": _float(p.average_price),
                         "unrealized_pnl": _float((self.prices[symbol.split(":")[0]] - p.average_price) * p.quantity),
                         "margin_used": _float(abs(p.quantity) * self.prices[symbol.split(":")[0]] / self.leverage)}
                for symbol, p in self.positions.items() if p.quantity}

    def dump(self):
        return {"version": 1, "initial": str(self.initial), "balance": str(self.balance),
                "market": self.market, "leverage": str(self.leverage), "fee_rate": str(self.fee_rate),
                "position_mode": self.position_mode, "pending_margin": str(self.pending_margin),
                "pending_fees": str(self.pending_fees), "fees": str(self.fees),
                "positions": {key: {"quantity": str(p.quantity), "average_price": str(p.average_price), "entry_fees": str(p.entry_fees)} for key, p in self.positions.items()},
                "prices": {key: str(value) for key, value in self.prices.items()},
                "orders": copy.deepcopy(self.orders), "closed_pnls": [str(v) for v in self.closed_pnls]}

    @classmethod
    def load(cls, state):
        obj = cls(state["initial"], state["market"], state["leverage"], state["fee_rate"], state.get("position_mode", "one_way"))
        obj.balance = _decimal(state["balance"])
        obj.fees = _decimal(state["fees"])
        obj.pending_margin = _decimal(state.get("pending_margin", 0))
        obj.pending_fees = _decimal(state.get("pending_fees", 0))
        obj.positions = {key: Position(*(_decimal(value[k]) for k in ("quantity", "average_price", "entry_fees"))) for key, value in state["positions"].items()}
        obj.prices = {key: _decimal(value) for key, value in state["prices"].items()}
        obj.orders = copy.deepcopy(state["orders"])
        obj.closed_pnls = [_decimal(v) for v in state["closed_pnls"]]
        return obj

    def execute(self, intent, timestamp, filters, slippage, signal_timestamp, *, fill_price=None, fee_rate=None, lot_basis=None):
        # Legacy market execution must never silently reinterpret conditional or limit orders.
        if str(intent.get("type", "MARKET")).upper() != "MARKET":
            return "当前市价执行器不接受 LIMIT/STOP；请声明 maker_grid_v1 并使用订单簿"
        symbol = intent.get("symbol")
        if symbol not in self.prices:
            return "交易对不在本次数据集中"
        side = intent.get("side")
        if side not in {"buy", "sell"}:
            return "side 必须为 buy 或 sell"
        if "reduce_only" in intent and type(intent["reduce_only"]) is not bool:
            return "reduce_only 必须为布尔值"
        try:
            quantity = _decimal(intent.get("quantity"), "quantity")
        except ValueError as exc:
            return str(exc)
        if quantity <= 0:
            return "委托数量必须大于零"
        rule = filters[symbol]
        quantity = _quantize(quantity, rule["step_size"])
        if quantity == 0 or quantity < rule["min_qty"]:
            return "数量量化后低于最小数量"
        signed = quantity if side == "buy" else -quantity
        key = symbol
        if self.position_mode == "hedge":
            position_side = str(intent.get("position_side", "")).upper()
            if position_side not in {"LONG", "SHORT"}:
                return "双向模式必须明确 position_side LONG 或 SHORT"
            key = f"{symbol}:{position_side}"
        position = self.positions.setdefault(key, Position())
        old_quantity = position.quantity
        if self.position_mode == "hedge":
            opening_side = (position_side == "LONG" and side == "buy") or (position_side == "SHORT" and side == "sell")
            if not opening_side and (not old_quantity or quantity > abs(old_quantity)):
                return "双向减仓数量超过所选持仓腿，禁止开反向仓"
            if intent.get("reduce_only") and opening_side:
                return "只减仓方向与所选持仓腿不匹配"
        if intent.get("reduce_only"):
            if not old_quantity or old_quantity * signed >= 0:
                return "只减仓委托没有可减少的相反持仓"
            quantity = min(abs(old_quantity), quantity)
            signed = quantity if side == "buy" else -quantity
        if quantity < rule["min_qty"]:
            return "减仓数量低于交易对最小数量，无法按该交易规则成交"
        if rule["max_qty"] > 0 and quantity > rule["max_qty"]:
            return "数量超过市价委托最大数量，策略需要分单"
        new_quantity = old_quantity + signed
        if self.market == "spot" and new_quantity < 0:
            return "现货不可做空，卖出数量超过持仓"
        raw_price = _decimal(fill_price) if fill_price is not None else self.prices[symbol] * (ONE + slippage if side == "buy" else ONE - slippage)
        price = _quantize(raw_price, rule["tick_size"], ROUND_CEILING if side == "buy" else ROUND_FLOOR)
        if price <= 0:
            return "成交价格量化后不大于零"
        notional = quantity * price
        if notional < rule["min_notional"] and not (self.market == "futures" and intent.get("reduce_only")):
            return "委托名义金额小于交易对最小金额"
        fee = notional * (self.fee_rate if fee_rate is None else _decimal(fee_rate))
        closing = min(abs(old_quantity), quantity) if old_quantity * signed < 0 else ZERO
        opening = quantity - closing
        # Mark-to-market equity immediately after a fill includes fees and the
        # adverse execution-vs-open difference. Check the entire prospective
        # book before mutating it; pure reductions are always permitted.
        projected_equity = self.equity() - fee + (self.prices[symbol] - price) * signed
        projected_margin = self.margin() + (abs(new_quantity) - abs(old_quantity)) * self.prices[symbol] / self.leverage
        if opening and (projected_equity <= 0 or projected_margin + self.pending_margin + self.pending_fees > projected_equity):
            return "可用资金或保证金不足（含手续费与滑点）"
        basis = _decimal(lot_basis["entry_price"]) if closing and lot_basis else position.average_price
        gross = closing * (price - basis) * (ONE if old_quantity > 0 else -ONE)
        allocated_entry_fee = _decimal(lot_basis["entry_fee"]) if closing and lot_basis else position.entry_fees * closing / abs(old_quantity) if closing else ZERO
        if allocated_entry_fee < 0 or allocated_entry_fee > position.entry_fees:
            return "lot 入场手续费与持仓账本不一致"
        closing_fee = fee * closing / quantity
        closed_net = gross - allocated_entry_fee - closing_fee
        self.balance += gross - fee
        self.fees += fee
        if closing:
            self.closed_pnls.append(closed_net)
        remaining_old = abs(old_quantity) - closing
        if old_quantity * signed > 0:
            position.average_price = (abs(old_quantity) * position.average_price + quantity * price) / abs(new_quantity)
            position.entry_fees += fee
        elif opening:
            position.average_price = price
            position.entry_fees = fee - closing_fee
        elif remaining_old:
            if lot_basis:
                position.average_price = (abs(old_quantity) * position.average_price - closing * basis) / remaining_old
            position.entry_fees -= allocated_entry_fee
        else:
            position.average_price = ZERO
            position.entry_fees = ZERO
        position.quantity = new_quantity
        direction = ("long" if old_quantity > 0 else "short") if closing else ("long" if signed > 0 else "short")
        self.orders.append({
            "id": len(self.orders) + 1, "timestamp": timestamp, "signal_timestamp": signal_timestamp,
            "symbol": symbol, "side": side, "direction": direction,
            "position_side": direction.upper(), "quantity": _float(quantity), "price": _float(price),
            "notional": _float(notional), "fee": _float(fee), "realized_pnl": _float(gross - fee),
            "gross_realized_pnl": _float(gross), "closed_trade_pnl": _float(closed_net) if closing else None,
            "closed_quantity": _float(closing), "opened_quantity": _float(opening),
            "reduce_only": bool(intent.get("reduce_only")), "status": "filled",
            "reason": str(intent.get("reason", "策略信号"))[:1000],
        })
        return None


class MakerOrderBook:
    """Durable conservative OHLC maker simulation, not a real bid/ask queue.

    Close-generated LIMITs rest from the next bar. A touch never fills. Bracket
    children rest only after their entry bar; a triggered STOP_LIMIT also waits
    a complete bar boundary. Stop precedence suppresses a same-lot TP whenever
    the bar cannot establish which happened first. There is no taker fallback.
    """

    OPEN = {"NEW", "PENDING_TRIGGER"}
    NUMBERS = {"quantity", "price", "stop_price", "filled_quantity", "entry_price",
               "remaining_quantity", "entry_quantity", "entry_fee_remaining"}

    def __init__(self, ledger, symbol_filters=None, maker_fee_rate=None):
        if ledger.market != "futures" or ledger.position_mode != "hedge":
            raise ValueError("maker_grid_v1 需要合约 hedge 双向账本")
        self.ledger = ledger
        self.symbol_filters = copy.deepcopy(symbol_filters or {})
        self.maker_fee_rate = ledger.fee_rate if maker_fee_rate is None else _decimal(maker_fee_rate)
        if not ZERO <= self.maker_fee_rate <= Decimal(".1"):
            raise ValueError("Maker 手续费必须在 0 至 0.1 之间")
        self.orders = {}
        self.lots = {}
        # Ephemeral indexes are rebuilt from the durable snapshot. They never
        # replace lifecycle history, but keep per-bar work proportional to the
        # currently active book instead of all orders since account inception.
        self._active_order_ids = {}
        self._open_lot_ids = {}
        self._orders_by_lot = {}
        self.events = []
        self.last_timestamp = None
        self.flattening = False
        self._sync_reservations()

    def _rule(self, symbol):
        return _filters_for(symbol, self.symbol_filters, market_order=False)

    def _event(self, kind, timestamp, order=None, **fields):
        record = {"id": len(self.events) + 1, "type": kind, "timestamp": timestamp, **fields}
        if order:
            record.update(order_id=order["id"], client_order_id=order["client_order_id"],
                          symbol=order.get("symbol"), lot_id=order.get("lot_id"),
                          role=order.get("role"), status=order.get("status"))
        self.events.append(record)
        return record

    @classmethod
    def _view(cls, record):
        result = copy.deepcopy(record)
        for key in cls.NUMBERS:
            if result.get(key) is not None:
                result[key] = _float(_decimal(result[key]))
        return result

    def open_orders(self):
        return [self._view(self.orders[key]) for key in self._active_order_ids]

    def _store_order(self, order):
        key = order["id"]
        self.orders[key] = order
        lot_id = order.get("lot_id")
        if isinstance(lot_id, str):
            self._orders_by_lot.setdefault(lot_id, {})[key] = None
        if order["status"] in self.OPEN:
            self._active_order_ids[key] = None
        else:
            self._active_order_ids.pop(key, None)

    def _lot_orders(self, lot_id):
        return [self.orders[key] for key in self._orders_by_lot.get(lot_id, {})]

    def _reindex(self):
        self._active_order_ids = {}
        self._open_lot_ids = {}
        self._orders_by_lot = {}
        for order in self.orders.values():
            self._store_order(order)
        for key, lot in self.lots.items():
            if _decimal(lot["remaining_quantity"]) > 0:
                self._open_lot_ids[key] = None
            else:
                lot["protection_status"] = "closed"

    def lot_snapshot(self):
        self._refresh_protection()
        return [self._view(self.lots[key]) for key in self._open_lot_ids]

    def _refresh_protection(self):
        for key in self._open_lot_ids:
            lot = self.lots[key]
            children = [o for o in self._lot_orders(key) if o.get("reduce_only")]
            active = [o for o in children if o["status"] in self.OPEN]
            stop = next((o for o in active if o["role"] == "sl"), None)
            take = next((o for o in active if o["role"] == "tp"), None)
            if any(o["role"] == "flatten" for o in active):
                status = "flattening"
            elif any(o["role"] == "sl" and o["status"] == "REJECTED" for o in children) and not stop:
                status = "stop_rejected"
            elif not stop or not take:
                status = "unprotected"
            else:
                status = "stop_triggered" if stop.get("triggered") else "protected"
            lot["protection_status"] = status

    def has_open_positions(self):
        return bool(self._open_lot_ids)

    def _sync_reservations(self, exclude=None):
        opening = [self.orders[key] for key in self._active_order_ids if key != exclude and not self.orders[key].get("reduce_only")]
        notional = sum((_decimal(o["quantity"]) * _decimal(o["price"]) for o in opening), ZERO)
        self.ledger.pending_margin = notional / self.ledger.leverage
        self.ledger.pending_fees = notional * self.maker_fee_rate

    def _references(self, reference_prices):
        references = reference_prices or self.ledger.prices
        return {symbol: _decimal(value.get("close") if isinstance(value, dict) else value) for symbol, value in references.items()}

    def _reject(self, record, timestamp, reason):
        record["status"] = "REJECTED"
        record["reason"] = str(reason)
        self._store_order(record)
        self._event("ORDER_REJECTED", timestamp, record, reason=str(reason))
        self._sync_reservations()
        return self._view(record)

    @staticmethod
    def _crosses(side, price, reference):
        return price >= reference if side == "buy" else price <= reference

    def submit_many(self, intents, timestamp, reference_prices=None):
        if not isinstance(intents, list) or len(intents) > 1000:
            raise ValueError("订单意图需要最多 1000 个对象的列表")
        return [self.submit(intent, timestamp, reference_prices) for intent in intents]

    def submit(self, intent, timestamp, reference_prices=None):
        if not isinstance(intent, dict):
            raise ValueError("委托意图必须是字典")
        timestamp = _timestamp(timestamp)
        action = intent.get("action", "place")
        if action == "cancel":
            return self.cancel(intent.get("order_id", intent.get("client_order_id")), timestamp, intent.get("reason", "策略撤单"))
        if action == "cancel_entries":
            cancelled = self.cancel_entries(timestamp, intent.get("reason", "停止开仓"))
            return {"status": "CANCELED", "action": action, "orders": cancelled}
        client_id = intent.get("client_order_id")
        if not isinstance(client_id, str) or not client_id or len(client_id) > 160:
            # A deterministic diagnostic ID never turns a missing client ID into an accepted order.
            client_id = f"invalid:{len(self.events) + 1}"
            error = "每个委托必须提供 1 至 160 字符的稳定 client_order_id"
        else:
            error = None
        fingerprint = _hash(intent)
        if client_id in self.orders:
            previous = self.orders[client_id]
            if previous.get("fingerprint") == fingerprint:
                return self._view(previous)
            self._event("IDEMPOTENCY_CONFLICT", timestamp, previous, reason="相同 client_order_id 的参数不同")
            return {"id": client_id, "client_order_id": client_id, "status": "REJECTED", "reason": "相同 client_order_id 的参数不同；原单保持不变"}
        order = {"id": client_id, "client_order_id": client_id, "fingerprint": fingerprint,
                 "symbol": intent.get("symbol"), "side": str(intent.get("side", "")).lower(),
                 "position_side": str(intent.get("position_side", "")).upper(),
                 "type": str(intent.get("type", "")).upper(), "time_in_force": str(intent.get("time_in_force", "")).upper(),
                 "reduce_only": intent.get("reduce_only", False), "lot_id": intent.get("lot_id", client_id),
                 "parent_id": intent.get("parent_id"), "role": intent.get("role", "manual" if intent.get("reduce_only") else "entry"),
                 "quantity": "0", "price": "0", "filled_quantity": "0", "status": "NEW",
                 "submitted_at": timestamp, "eligible_after": timestamp, "triggered": False,
                 "reason": str(intent.get("reason", "策略挂单"))[:1000]}
        if error:
            return self._reject(order, timestamp, error)
        try:
            if action != "place":
                raise ValueError("不支持该订单 action")
            if order["type"] not in {"LIMIT", "STOP_LIMIT"} or order["time_in_force"] != "GTX":
                raise ValueError("Maker 模型只接受 LIMIT / STOP_LIMIT 且 time_in_force=GTX；不接受市价回退")
            if order["side"] not in {"buy", "sell"} or order["position_side"] not in {"LONG", "SHORT"}:
                raise ValueError("必须声明 buy/sell 和 LONG/SHORT")
            if type(order["reduce_only"]) is not bool:
                raise ValueError("reduce_only 必须为布尔值")
            symbol = order["symbol"]
            refs = self._references(reference_prices)
            if not isinstance(symbol, str) or symbol not in refs or refs[symbol] <= 0:
                raise ValueError("交易对缺少有效参考价格，禁止猜测 GTX 可挂性")
            rule = self._rule(symbol)
            quantity = _quantize(_decimal(intent.get("quantity")), rule["step_size"])
            price = _quantize(_decimal(intent.get("price")), rule["tick_size"], ROUND_FLOOR if order["side"] == "buy" else ROUND_CEILING)
            order.update(quantity=str(quantity), price=str(price))
            if quantity <= 0 or quantity < rule["min_qty"] or (rule["max_qty"] > 0 and quantity > rule["max_qty"]):
                raise ValueError("限价委托数量不符合 LOT_SIZE 最小/最大/步长要求")
            if price <= 0:
                raise ValueError("限价价格必须大于零")
            if quantity * price < rule["min_notional"] and not order["reduce_only"]:
                raise ValueError("开仓委托小于最小名义金额")
            opening_side = (order["position_side"] == "LONG" and order["side"] == "buy") or (order["position_side"] == "SHORT" and order["side"] == "sell")
            if order["reduce_only"]:
                lot = self.lots.get(order["lot_id"])
                if not lot or lot["symbol"] != symbol or lot["position_side"] != order["position_side"] or opening_side:
                    raise ValueError("退出单没有对应的同币种同方向 lot，或退出方向错误")
                if quantity > _decimal(lot["remaining_quantity"]):
                    raise ValueError("退出数量超过该 lot 剩余数量")
            else:
                if self.flattening:
                    raise ValueError("账户正在 Maker 平仓，禁止新开仓")
                if any(lot["protection_status"] in {"unprotected", "stop_rejected"} for lot in self.lot_snapshot()):
                    raise ValueError("存在保护单缺失/被拒的持仓 lot，禁止继续补仓")
                if not opening_side or order["type"] != "LIMIT":
                    raise ValueError("开仓只允许匹配持仓方向的 LIMIT GTX")
                if order["lot_id"] in self.lots or any(not o.get("reduce_only") and o["status"] in self.OPEN for o in self._lot_orders(order["lot_id"])):
                    raise ValueError("lot_id 已存在，不允许重复开仓")
                bracket = intent.get("bracket")
                if not isinstance(bracket, dict):
                    raise ValueError("开仓必须同时声明完整 TP / STOP_LIMIT 保护单")
                exit_rounding = ROUND_CEILING if order["position_side"] == "LONG" else ROUND_FLOOR
                trigger_rounding = ROUND_FLOOR if order["position_side"] == "LONG" else ROUND_CEILING
                tp = _quantize(_decimal(bracket.get("take_profit_price")), rule["tick_size"], exit_rounding)
                stop = _quantize(_decimal(bracket.get("stop_price")), rule["tick_size"], trigger_rounding)
                stop_limit = _quantize(_decimal(bracket.get("stop_limit_price")), rule["tick_size"], exit_rounding)
                if min(tp, stop, stop_limit) <= 0 or not (stop < price < tp if order["position_side"] == "LONG" else tp < price < stop):
                    raise ValueError("量化后的保护价必须位于入场价的正确两侧，且为正数")
                if (order["position_side"] == "LONG" and stop_limit >= price) or (order["position_side"] == "SHORT" and stop_limit <= price):
                    raise ValueError("量化后止损限价已跨回入场价或止盈方向；请减小止损限价偏移")
                order["bracket"] = {"take_profit_price": str(tp), "stop_price": str(stop), "stop_limit_price": str(stop_limit)}
                self._sync_reservations()
                required = quantity * price / self.ledger.leverage + quantity * price * self.maker_fee_rate
                available = self.ledger.equity() - self.ledger.margin() - self.ledger.pending_margin - self.ledger.pending_fees
                if required > available:
                    raise ValueError("持仓及已有挂单已占用保证金，新单资金不足（含手续费预留）")
            if order["type"] == "LIMIT":
                if self._crosses(order["side"], price, refs[symbol]):
                    raise ValueError("GTX 拒单：限价相对参考价立即可成交；未改成市价")
            else:
                trigger = _quantize(_decimal(intent.get("stop_price")), rule["tick_size"], ROUND_FLOOR if order["side"] == "sell" else ROUND_CEILING)
                if trigger <= 0:
                    raise ValueError("STOP_LIMIT 触发价格必须大于零")
                if (order["side"] == "sell" and trigger >= refs[symbol]) or (order["side"] == "buy" and trigger <= refs[symbol]):
                    raise ValueError("STOP_LIMIT 提交时触发条件已满足，不能伪装为尚未触发的保护单")
                basis = _decimal(lot["entry_price"])
                if (order["position_side"] == "LONG" and price >= basis) or (order["position_side"] == "SHORT" and price <= basis):
                    raise ValueError("量化后止损限价跨回该 lot 入场价，不能作为有效止损保护")
                order.update(stop_price=str(trigger), status="PENDING_TRIGGER")
            self._store_order(order)
            self._sync_reservations()
            self._event("ORDER_ACCEPTED", timestamp, order)
            return self._view(order)
        except (ValueError, TypeError, KeyError) as exc:
            return self._reject(order, timestamp, str(exc))

    def cancel(self, order_id, timestamp, reason="cancelled"):
        order = self.orders.get(order_id)
        if not order:
            return {"id": order_id, "status": "REJECTED", "reason": "待撤委托不存在"}
        if order["status"] in self.OPEN:
            order.update(status="CANCELED", cancelled_at=timestamp, cancel_reason=str(reason))
            self._store_order(order)
            self._event("ORDER_CANCELED", timestamp, order, reason=str(reason))
            self._sync_reservations()
        return self._view(order)

    def cancel_entries(self, timestamp, reason="停止开仓"):
        return [self.cancel(key, timestamp, reason) for key in list(self._active_order_ids) if not self.orders[key]["reduce_only"]]

    def request_flatten(self, timestamp, reference_prices=None):
        self.flattening = True
        self.cancel_entries(timestamp, "Maker 平仓停止开仓")
        refs = self._references(reference_prices)
        results = []
        for lot_key in list(self._open_lot_ids):
            lot = self.lots[lot_key]
            base_id = f"{lot['id']}:flatten"
            lot_orders = self._lot_orders(lot["id"])
            existing = [o for o in lot_orders if o.get("role") == "flatten" and o["status"] in self.OPEN]
            if existing:
                results.append(self._view(existing[0])); continue
            symbol = lot["symbol"]
            if symbol not in refs:
                results.append({"lot_id": lot["id"], "status": "REJECTED", "reason": "缺少平仓参考价，保留原保护单"}); continue
            for order in lot_orders:
                if order["status"] in self.OPEN:
                    self.cancel(order["id"], timestamp, "改为管理员 Maker 平仓")
            side = "sell" if lot["position_side"] == "LONG" else "buy"
            price = refs[symbol] + self._rule(symbol)["tick_size"] * (ONE if side == "sell" else -ONE)
            client_id = base_id
            if client_id in self.orders:
                client_id = f"{base_id}:{sum(o.get('role') == 'flatten' for o in lot_orders)}"
            results.append(self.submit({"client_order_id": client_id, "symbol": symbol, "side": side,
                "position_side": lot["position_side"], "type": "LIMIT", "time_in_force": "GTX",
                "quantity": lot["remaining_quantity"], "price": str(price), "reduce_only": True,
                "lot_id": lot["id"], "parent_id": lot["entry_order_id"], "role": "flatten", "reason": "管理员 Maker 平仓"}, timestamp, refs))
        return results

    def _children(self, entry, timestamp):
        quantity, price = _decimal(entry["quantity"]), _decimal(entry["price"])
        lot_id = entry["lot_id"]
        fill = self.ledger.orders[-1]
        lot = {"id": lot_id, "lot_id": lot_id, "entry_order_id": entry["id"], "symbol": entry["symbol"],
               "position_side": entry["position_side"], "entry_quantity": str(quantity), "quantity": str(quantity),
               "remaining_quantity": str(quantity), "entry_price": str(price), "entry_fee_remaining": str(quantity * price * self.maker_fee_rate),
               "opened_at": timestamp, "status": "OPEN", "bracket": copy.deepcopy(entry["bracket"])}
        self.lots[lot_id] = lot
        self._open_lot_ids[lot_id] = None
        side = "sell" if entry["position_side"] == "LONG" else "buy"
        base = {"symbol": entry["symbol"], "side": side, "position_side": entry["position_side"],
                "time_in_force": "GTX", "quantity": str(quantity), "reduce_only": True,
                "lot_id": lot_id, "parent_id": entry["id"]}
        self.submit({**base, "client_order_id": entry["id"] + ":tp", "type": "LIMIT", "role": "tp",
                     "price": entry["bracket"]["take_profit_price"], "reason": "lot 止盈"}, timestamp, {entry["symbol"]: price})
        self.submit({**base, "client_order_id": entry["id"] + ":sl", "type": "STOP_LIMIT", "role": "sl",
                     "price": entry["bracket"]["stop_limit_price"], "stop_price": entry["bracket"]["stop_price"],
                     "reason": "lot Maker 条件止损；不保证退出"}, timestamp, {entry["symbol"]: price})
        self._event("LOT_OPENED", timestamp, entry, quantity=str(quantity), entry_price=str(price))
        if any(item["protection_status"] in {"unprotected", "stop_rejected"} for item in self.lot_snapshot()):
            self.cancel_entries(timestamp, "lot 保护单未能完整创建，停止继续开仓")

    def _fill(self, order, timestamp):
        quantity = _decimal(order["quantity"])
        basis = None
        if order["reduce_only"]:
            lot = self.lots.get(order["lot_id"])
            remaining = _decimal(lot["remaining_quantity"]) if lot else ZERO
            if remaining <= 0:
                self.cancel(order["id"], timestamp, "lot 已关闭，禁止重复退出"); return
            quantity = min(quantity, remaining)
            basis = {"entry_price": lot["entry_price"], "entry_fee": str(_decimal(lot["entry_fee_remaining"]) * quantity / remaining)}
        self._sync_reservations(exclude=order["id"])
        intent = {"symbol": order["symbol"], "side": order["side"], "position_side": order["position_side"],
                  "quantity": str(quantity), "reduce_only": order["reduce_only"], "type": "MARKET", "reason": order["reason"]}
        reason = self.ledger.execute(intent, timestamp, {order["symbol"]: self._rule(order["symbol"])}, ZERO,
                                     order["submitted_at"], fill_price=order["price"], fee_rate=self.maker_fee_rate, lot_basis=basis)
        if reason:
            self._reject(order, timestamp, reason); return
        order.update(status="FILLED", filled_at=timestamp, filled_quantity=str(quantity))
        self._store_order(order)
        fill = self.ledger.orders[-1]
        fill.update(client_order_id=order["id"], order_type=order["type"], type=order["type"], time_in_force="GTX",
                    order_id=order["id"], parent_id=order["parent_id"], lot_id=order["lot_id"], role=order["role"], liquidity="simulated_maker", maker_simulated=True)
        self._event("ORDER_FILLED", timestamp, order, quantity=str(quantity), price=order["price"], ledger_order_id=fill["id"])
        if not order["reduce_only"]:
            self._children(order, timestamp)
        else:
            remaining -= quantity
            lot.update(remaining_quantity=str(remaining), quantity=str(remaining),
                       entry_fee_remaining=str(_decimal(lot["entry_fee_remaining"]) - _decimal(basis["entry_fee"])))
            if remaining == 0:
                lot.update(status="CLOSED", protection_status="closed", closed_at=timestamp, exit_order_id=order["id"])
                self._open_lot_ids.pop(lot["id"], None)
                for sibling in self._lot_orders(lot["id"]):
                    if sibling["id"] != order["id"] and sibling["status"] in self.OPEN:
                        self.cancel(sibling["id"], timestamp, "lot 已平仓，取消兄弟保护/手动退出单")
                self._event("LOT_CLOSED", timestamp, order)
            else:
                for sibling in self._lot_orders(lot["id"]):
                    if sibling["status"] in self.OPEN:
                        sibling["quantity"] = str(min(_decimal(sibling["quantity"]), remaining))
        self._sync_reservations()

    def process_bar(self, bars):
        if not isinstance(bars, dict) or not bars:
            raise ValueError("process_bar 需要按币种组织的完整 OHLC K 线")
        timestamps = {_timestamp(bar["timestamp"]) for bar in bars.values()}
        if len(timestamps) != 1:
            raise ValueError("process_bar 多币种必须使用相同已收盘时间戳")
        timestamp = timestamps.pop()
        if self.last_timestamp is not None and timestamp <= self.last_timestamp:
            if timestamp == self.last_timestamp:
                return []
            raise ValueError("不能倒序处理已持久化的 K 线")
        clean = {}
        for symbol, bar in bars.items():
            values = {k: _decimal(bar[k]) for k in ("open", "high", "low", "close")}
            if min(values.values()) <= 0 or values["high"] < max(values.values()) or values["low"] > min(values.values()):
                raise ValueError("OHLC 必须为有限正数且高低价格范围有效")
            values["volume"] = _decimal(bar.get("volume", 0))
            if values["volume"] < 0:
                raise ValueError("成交量不可为负")
            clean[symbol] = values
        if any(self.orders[key]["symbol"] not in clean for key in self._active_order_ids):
            raise ValueError("当前 K 线未覆盖全部挂单交易对，禁止用过期行情撮合")
        start = len(self.events)
        self.ledger.prices.update({s: bar["open"] for s, bar in clean.items()})
        if any(lot["protection_status"] in {"unprotected", "stop_rejected"} for lot in self.lot_snapshot()):
            self.cancel_entries(timestamp, "已有 lot 缺少有效保护，禁止继续成交新开仓单")
        eligible = [self.orders[key] for key in self._active_order_ids if self.orders[key]["eligible_after"] < timestamp]
        suppressed_tp_lots = set()
        # Conditional stops are observed before any profitable exit in an ambiguous candle.
        for order in eligible:
            if order["status"] != "PENDING_TRIGGER":
                continue
            bar, stop = clean[order["symbol"]], _decimal(order["stop_price"])
            triggered = bar["low"] <= stop if order["side"] == "sell" else bar["high"] >= stop
            if not triggered:
                continue
            suppressed_tp_lots.add(order["lot_id"])
            order.update(triggered=True, triggered_at=timestamp, eligible_after=timestamp)
            reference = min(bar["open"], stop) if order["side"] == "sell" else max(bar["open"], stop)
            self._event("STOP_TRIGGERED", timestamp, order, reference_price=str(reference))
            if self._crosses(order["side"], _decimal(order["price"]), reference):
                self._reject(order, timestamp, "STOP_LIMIT 触发后 GTX 拒单：限价已跨参考价；lot 仍有风险，无市价回退")
                self.cancel_entries(timestamp, "止损 GTX 被拒，停止继续开仓")
            else:
                order["status"] = "NEW"
                self._event("STOP_LIMIT_RESTING", timestamp, order, reason="触发 bar 不撮合，最早下一 bar 严格穿价成交")
        eligible.sort(key=lambda o: (0 if o["role"] == "sl" else 1 if o["reduce_only"] else 2, o["submitted_at"], o["id"]))
        for order in eligible:
            if order["status"] != "NEW" or order["eligible_after"] >= timestamp:
                continue
            if order["reduce_only"] and order["role"] != "sl" and order["lot_id"] in suppressed_tp_lots:
                self._event("AMBIGUOUS_EXIT_DEFERRED", timestamp, order, reason="同一 bar 触发止损，不能凭未知先后假设止盈")
                continue
            bar, price = clean[order["symbol"]], _decimal(order["price"])
            crossed = bar["low"] < price if order["side"] == "buy" else bar["high"] > price
            if crossed:
                if bar["volume"] == 0:
                    self._event("ZERO_VOLUME_FILL_SKIPPED", timestamp, order, reason="完整 K 线成交量为零，严格穿价也不模拟成交")
                    continue
                if order["role"] == "sl":
                    suppressed_tp_lots.add(order["lot_id"])
                self._fill(order, timestamp)
        self.ledger.prices.update({s: bar["close"] for s, bar in clean.items()})
        self.last_timestamp = timestamp
        self._sync_reservations()
        return copy.deepcopy(self.events[start:])

    def dump(self):
        # Filters can arrive as Decimal values from an already-normalized adapter.
        self._refresh_protection()
        filters = json.loads(json.dumps(self.symbol_filters, default=str, allow_nan=False))
        return {"version": 1, "model": "maker_grid_v1", "ledger": self.ledger.dump(),
                "symbol_filters": filters, "maker_fee_rate": str(self.maker_fee_rate),
                "orders": copy.deepcopy(self.orders), "lots": copy.deepcopy(self.lots),
                "events": copy.deepcopy(self.events), "last_timestamp": self.last_timestamp, "flattening": self.flattening}

    @classmethod
    def load(cls, state):
        if state.get("model") != "maker_grid_v1" or state.get("version") != 1:
            raise ValueError("不支持该 Maker 订单簿快照版本")
        book = cls(Ledger.load(state["ledger"]), state["symbol_filters"], state["maker_fee_rate"])
        book.orders = copy.deepcopy(state["orders"])
        book.lots = copy.deepcopy(state["lots"])
        book.events = copy.deepcopy(state["events"])
        book.last_timestamp = state["last_timestamp"]
        book.flattening = bool(state.get("flattening", False))
        book._reindex()
        book._sync_reservations()
        return book


def calculate_metrics(equity, orders, initial_capital, *, closed_pnls=None, account=None):
    """Returns fractions, not percentage points; PF is null without losses.

    Win rate and losing streak use realized reduction events including their
    allocated entry/exit fees. Open positions never count as winning trades.
    Sharpe uses daily last-observation equity and zero risk-free rate, 365 days.
    Annual return is null for periods shorter than one day or nonpositive NAV.
    """
    initial = float(initial_capital)
    final = equity[-1]["equity"] if equity else initial
    peak = initial
    max_drawdown = max_drawdown_amount = 0.0
    for item in equity:
        peak = max(peak, item["equity"])
        dd_amount = peak - item["equity"]
        max_drawdown_amount = max(max_drawdown_amount, dd_amount)
        max_drawdown = max(max_drawdown, dd_amount / peak if peak > 0 else 0)
    closed = [float(v) for v in closed_pnls] if closed_pnls is not None else [float(o["closed_trade_pnl"]) for o in orders if o.get("closed_trade_pnl") is not None]
    wins = [value for value in closed if value > 0]
    losses = [value for value in closed if value < 0]
    longest = streak = 0
    for value in closed:
        streak = streak + 1 if value < 0 else 0
        longest = max(longest, streak)
    scale = 1000 if equity and equity[-1]["timestamp"] > 100000000000 else 1
    seconds = (equity[-1]["timestamp"] - equity[0]["timestamp"]) / scale if len(equity) >= 2 else 0
    annual_return = None
    if seconds >= 86400 and final > 0:
        exponent = math.log(final / initial) * (365 * 86400 / seconds)
        if -700 < exponent < 700:
            annual_return = math.expm1(exponent)
    days = {}
    for item in equity:
        days[int(item["timestamp"] / scale // 86400)] = item["equity"]
    daily = list(days.values())
    daily_returns = [daily[i] / daily[i - 1] - 1 for i in range(1, len(daily)) if daily[i - 1] > 0]
    sharpe = None
    if len(daily_returns) >= 2 and statistics.stdev(daily_returns) > 0:
        sharpe = statistics.mean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(365)
    account = account or {}
    return {
        "initial_equity": initial, "final_equity": final, "total_return": final / initial - 1,
        "annual_return": annual_return, "max_drawdown": max_drawdown,
        "max_drawdown_amount": max_drawdown_amount, "win_rate": len(wins) / len(closed) if closed else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None, "sharpe": sharpe,
        "total_orders": len(orders), "long_orders": sum(o.get("direction") == "long" for o in orders),
        "short_orders": sum(o.get("direction") == "short" for o in orders),
        "closed_trades": len(closed), "max_consecutive_losses": longest,
        "realized_pnl": account.get("realized_pnl", final - initial),
        "unrealized_pnl": account.get("unrealized_pnl", 0.0), "margin_used": account.get("margin_used", 0.0),
        "total_fees": sum(order.get("fee", 0.0) for order in orders),
    }


def run_backtest(strategy_source, params, candles, initial_capital, fee_rate, slippage_bps,
                 market="futures", leverage=1, symbol_filters=None, progress=None, cancelled=None):
    """Run a fresh deterministic strategy with optional batched progress callback.

    ``progress({processed,total,status,equity:[new points]})`` marks intermediate
    results as running; full metrics are computed only after every bar. A true
    ``cancelled()`` raises BacktestCancelled and does not return partial metrics.
    """
    manifest = validate_strategy_source(strategy_source)
    parameters = validate_parameters(manifest, params)
    maker = manifest.get("execution", {}).get("model") == "maker_grid_v1"
    if market != manifest["market"]:
        raise ValueError("所选市场与策略要求不一致")
    if not maker and "one_way" not in manifest["account"]["position_modes"]:
        raise ValueError("当前引擎只支持单向净持仓；不能运行仅支持双向持仓的策略")
    if market == "futures" and "cross" not in manifest["account"]["margin_modes"]:
        raise ValueError("当前历史引擎使用全仓权益模型，不能运行仅支持逐仓的策略")
    initial = _decimal(initial_capital, "initial_capital")
    fees = _decimal(fee_rate, "fee_rate")
    slippage = _decimal(slippage_bps, "slippage_bps") / Decimal("10000")
    lev = _decimal(leverage, "leverage")
    if initial <= 0 or initial < _decimal(manifest["account"]["min_capital"]):
        raise ValueError("初始资金低于策略最低要求")
    if not (ZERO <= fees <= Decimal("0.1")) or not (ZERO <= slippage < ONE):
        raise ValueError("手续费率须在 0 至 0.1 之间，滑点须在 0 至 10000 bps 之间（不含 10000）")
    if lev != lev.to_integral_value() or not (_decimal(manifest["account"]["leverage_min"]) <= lev <= _decimal(manifest["account"]["leverage_max"])):
        raise ValueError("杠杆不符合策略声明范围")
    if market == "spot" and lev != ONE:
        raise ValueError("现货不可使用杠杆")
    prepared, timestamps = _prepare_candles(candles)
    symbols = sorted(prepared)
    validate_symbols(manifest, symbols)
    if maker:
        if market != "futures" or manifest["account"]["position_modes"] != ["hedge"] or manifest["account"]["margin_modes"] != ["cross"]:
            raise ValueError("maker_grid_v1 当前需要 futures、hedge-only 与 cross-only 账户声明")
        if slippage != ZERO:
            raise ValueError("maker_grid_v1 使用限价成交模型，slippage_bps 必须为 0，不能套用市价滑点")
        return _run_maker_backtest(strategy_source, manifest, parameters, prepared, timestamps,
                                  initial, fees, lev, symbol_filters, progress, cancelled)
    filters = {symbol: _filters_for(symbol, symbol_filters) for symbol in symbols}
    strategy = load_strategy(strategy_source)
    ledger = Ledger(initial, market, lev, fees)
    histories = {symbol: [] for symbol in symbols}
    curve, decisions, pending = [], [], []
    peak = initial
    batch_start = 0
    batch_size = max(1, min(100, len(timestamps) // 30))
    for index, timestamp in enumerate(timestamps):
        if cancelled and cancelled():
            raise BacktestCancelled("回测任务已取消")
        bars = {symbol: prepared[symbol][timestamp] for symbol in symbols}
        ledger.prices = {symbol: _decimal(bar["open"]) for symbol, bar in bars.items()}
        for intent, decision_index, signal_timestamp in pending:
            reason = ledger.execute(intent, timestamp, filters, slippage, signal_timestamp)
            decisions[decision_index]["risk"].append({"symbol": intent.get("symbol"),
                "status": "rejected" if reason else "accepted", "reason": reason or "通过资金、市场和交易精度检查",
                "execution_timestamp": timestamp, "order_id": None if reason else ledger.orders[-1]["id"]})
            decisions[decision_index]["status"] = "executed"
        pending = []
        ledger.prices = {symbol: _decimal(bar["close"]) for symbol, bar in bars.items()}
        for symbol, bar in bars.items():
            histories[symbol].append(bar)
            if len(histories[symbol]) > manifest["history_window"]:
                histories[symbol].pop(0)
        net_asset = ledger.equity()
        peak = max(peak, net_asset)
        curve.append({"timestamp": timestamp, "equity": _float(net_asset),
                      "drawdown": _float((peak - net_asset) / peak) if peak > 0 else 0.0,
                      "margin_used": _float(ledger.margin()), "unrealized_pnl": _float(ledger.unrealized())})
        logs, display = [], {}

        def log(level, message, data=None):
            if level not in {"debug", "error"}:
                raise ValueError("策略日志级别仅支持 debug 和 error")
            logs.append({"level": level, "message": str(message)[:4000], "data": _json_safe(data)})

        def emit(**fields):
            display.update(_json_safe(fields))

        context = {"params": copy.deepcopy(parameters), "candles": copy.deepcopy(bars),
                   "history": copy.deepcopy(histories), "positions": ledger.position_snapshot(),
                   "account": ledger.account(), "timestamp": timestamp, "bar_index": index,
                   "log": log, "emit": emit}
        intents = strategy.on_bar(context)
        if not isinstance(intents, list) or len(intents) > 1000 or any(not isinstance(item, dict) for item in intents):
            raise ValueError("on_bar 必须返回最多 1000 项的委托字典列表；无交易时返回 []")
        intents = _json_safe(intents)
        decision = {"timestamp": timestamp, "status": "pending" if intents else "no_trade",
                    "inputs": {symbol: dict(bar) for symbol, bar in bars.items()},
                    "account": ledger.account(), "indicators": display.get("indicators", {}),
                    "stage": display.get("stage", ""), "reason": display.get("reason", "无交易信号"),
                    "display": display, "intents": intents, "risk": [], "logs": logs}
        decisions.append(decision)
        if index < len(timestamps) - 1:
            pending = [(intent, len(decisions) - 1, timestamp) for intent in intents]
        elif intents:
            decision["status"] = "expired"
            decision["risk"] = [{"symbol": item.get("symbol"), "status": "not_executed", "reason": "区间结束，没有下一根 K 线"} for item in intents]
        if progress and ((index + 1) % batch_size == 0 or index == len(timestamps) - 1):
            progress({"processed": index + 1, "total": len(timestamps),
                      "status": "running", "equity": copy.deepcopy(curve[batch_start:])})
            batch_start = index + 1
    metrics = calculate_metrics(curve, ledger.orders, initial, closed_pnls=ledger.closed_pnls, account=ledger.account())
    if progress:
        progress({"processed": len(timestamps), "total": len(timestamps), "status": "completed", "equity": [], "metrics": metrics})
    dropped = {symbol: len(prepared[symbol]) - len(timestamps) for symbol in symbols}
    return {"metrics": metrics, "equity": curve, "orders": ledger.orders, "decisions": decisions,
            "positions": ledger.position_snapshot(), "account": ledger.account(),
            "engine_version": ENGINE_VERSION, "status": "completed", "parameters": parameters,
            "assumptions": {"execution": "signal_at_close_fill_next_common_open",
                "fee_rate": float(fees), "slippage_bps": float(slippage_bps), "funding_included": False,
                "liquidation_simulated": False, "order_book_simulated": False,
                "margin_model": "cash" if market == "spot" else "cross_equity",
                "isolated_margin_simulated": False,
                "quote_currency_model": "shared_U_balance_USDT_and_USDC_assumed_at_par",
                "final_positions_closed": False, "dropped_unaligned_bars": dropped,
                "symbol_filters": "provided" if symbol_filters else "fallback_1e-8_not_exchange_verified",
                "trade_metric_basis": "fee_adjusted_position_reductions",
                "sharpe_basis": "daily_equity_zero_risk_free_365_days",
                "insolvent": ledger.equity() <= 0}}


def _run_maker_backtest(source, manifest, parameters, prepared, timestamps,
                       initial, fee, leverage, symbol_filters, progress, cancelled):
    ledger = Ledger(initial, "futures", leverage, fee, position_mode="hedge")
    book = MakerOrderBook(ledger, symbol_filters, maker_fee_rate=fee)
    strategy = load_strategy(source)
    histories = {symbol: [] for symbol in prepared}
    curve, decisions = [], []
    peak, batch_start = initial, 0
    batch_size = max(1, min(100, len(timestamps) // 30))
    for index, timestamp in enumerate(timestamps):
        if cancelled and cancelled():
            raise BacktestCancelled("回测任务已取消")
        bars = {symbol: values[timestamp] for symbol, values in prepared.items()}
        event_start = len(book.events)
        execution_events = book.process_bar(bars)
        for symbol, bar in bars.items():
            histories[symbol].append(bar)
            histories[symbol] = histories[symbol][-manifest["history_window"]:]
        equity = ledger.equity()
        peak = max(peak, equity)
        point = {"timestamp": timestamp, "equity": _float(equity),
                 "drawdown": _float((peak - equity) / peak) if peak > 0 else 0,
                 "margin_used": _float(ledger.margin()), "pending_margin": _float(ledger.pending_margin),
                 "unrealized_pnl": _float(ledger.unrealized())}
        logs, display = [], {}

        def log(level, message, data=None):
            if level not in {"debug", "error"}:
                raise ValueError("策略日志级别仅支持 debug 和 error")
            logs.append({"level": level, "message": str(message)[:4000], "data": _json_safe(data)})

        def emit(**fields):
            display.update(_json_safe(fields))

        context = {"params": copy.deepcopy(parameters), "candles": copy.deepcopy(bars),
                   "history": copy.deepcopy(histories), "positions": ledger.position_snapshot(),
                   "account": ledger.account(), "timestamp": timestamp, "bar_index": index,
                   "open_orders": book.open_orders(), "lots": book.lot_snapshot(),
                   "execution_events": execution_events, "log": log, "emit": emit}
        intents = strategy.on_bar(context)
        if not isinstance(intents, list) or len(intents) > 1000 or any(not isinstance(i, dict) for i in intents):
            raise ValueError("on_bar 必须返回最多 1000 项的委托字典列表")
        intents = _json_safe(intents)
        submitted = book.submit_many(intents, timestamp, {s: bar["close"] for s, bar in bars.items()})
        risk = [{"order_id": result.get("id"), "status": "rejected" if result["status"] == "REJECTED" else "accepted",
                 "order_status": result["status"], "reason": result.get("reason", "已记录委托，尚不代表成交")}
                for result in submitted]
        decisions.append({"timestamp": timestamp, "status": "submitted" if intents else "no_trade",
                          "inputs": copy.deepcopy(bars), "account": ledger.account(),
                          "indicators": display.get("indicators", {}), "stage": display.get("stage", ""),
                          "reason": display.get("reason", "无交易信号"), "display": display,
                          "intents": intents, "risk": risk, "logs": logs,
                          "execution_events": copy.deepcopy(book.events[event_start:])})
        point["pending_margin"] = _float(ledger.pending_margin)
        curve.append(point)
        if progress and ((index + 1) % batch_size == 0 or index == len(timestamps) - 1):
            progress({"processed": index + 1, "total": len(timestamps), "status": "running", "equity": copy.deepcopy(curve[batch_start:])})
            batch_start = index + 1
    metrics = calculate_metrics(curve, ledger.orders, initial, closed_pnls=ledger.closed_pnls, account=ledger.account())
    if progress:
        progress({"processed": len(timestamps), "total": len(timestamps), "status": "completed", "equity": [], "metrics": metrics})
    return {"metrics": metrics, "equity": curve, "orders": ledger.orders, "decisions": decisions,
            "positions": ledger.position_snapshot(), "account": ledger.account(), "open_orders": book.open_orders(),
            "lots": book.lot_snapshot(), "events": copy.deepcopy(book.events), "order_book": book.dump(),
            "engine_version": ENGINE_VERSION, "status": "completed", "parameters": parameters,
            "assumptions": {"execution": "maker_grid_v1", "position_mode": "hedge", "fee_rate": _float(fee),
                "fee_kind": "maker", "slippage_bps": 0, "funding_included": False, "liquidation_simulated": False,
                "order_book_simulated": False, "real_bid_ask_queue_available": False, "partial_fills_simulated": False,
                "maker_fill_model": "next_bar_resting_strict_price_penetration_full_fill_at_limit_no_volume_queue_model",
                "zero_volume_bars": "never_fill_orders_price_conditions_can_trigger_without_a_fill",
                "gtx_check": "submission_reference_price_only_not_real_bid_ask",
                "stop_trigger_reference": "sell_min_open_trigger_buy_max_open_trigger",
                "bracket_activation": "entry_fill_creates_siblings_immediately_but_neither_eligible_until_next_bar",
                "stop_execution": "conditional_limit_gtx_only_no_same_trigger_bar_fill_no_taker_fallback_exit_not_guaranteed",
                "ambiguous_ohlc_ordering": "stop_priority_suppress_same_lot_profit_exit_on_trigger_bar",
                "margin_model": "cross_equity_sum_of_both_hedge_legs_plus_pending_entry_margin_and_fees",
                "isolated_margin_simulated": False, "quote_currency_model": "shared_U_balance_USDT_and_USDC_assumed_at_par",
                "final_positions_closed": False, "final_unfilled_orders_retained": True,
                "dropped_unaligned_bars": {s: len(rows) - len(timestamps) for s, rows in prepared.items()},
                "symbol_filters": "provided" if symbol_filters else "fallback_1e-8_not_exchange_verified",
                "trade_metric_basis": "fee_adjusted_lot_reductions", "sharpe_basis": "daily_equity_zero_risk_free_365_days",
                "insolvent": ledger.equity() <= 0}}


def _json_safe(value):
    """Reject objects/NaN from uploaded code before crossing process boundaries."""
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
        if len(encoded) > 250000:
            raise ValueError("单次策略输出不能超过 250 KB")
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"策略输出必须是有限且可 JSON 序列化的数据：{exc}") from exc


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def run_split_backtest(strategy_source, params, candles, initial_capital, fee_rate, slippage_bps,
                       train_start, train_end, validation_start, validation_end, market="futures",
                       leverage=1, symbol_filters=None, progress=None, cancelled=None, seed=42):
    """Independently evaluate [start, end) train/validation periods without leakage."""
    a, b, c, d = map(_timestamp, (train_start, train_end, validation_start, validation_end))
    if not a < b <= c < d:
        raise ValueError("要求训练开始 < 训练结束 <= 验证开始 < 验证结束，区间不可重叠")
    outputs = {}
    for name, start, end in (("train", a, b), ("validation", c, d)):
        subset = {symbol: [row for row in rows if start <= _timestamp(row["timestamp"]) < end] for symbol, rows in candles.items()}
        callback = (lambda event, current=name: progress(dict(event, split=current))) if progress else None
        outputs[name] = run_backtest(strategy_source, params, subset, initial_capital, fee_rate,
                                    slippage_bps, market, leverage, symbol_filters, callback, cancelled)
    outputs["differences"] = {key: outputs["validation"]["metrics"][key] - value
        if type(value) in {float, int} and type(outputs["validation"]["metrics"].get(key)) in {float, int} else None
        for key, value in outputs["train"]["metrics"].items()}
    outputs["reproducibility"] = {"strategy_hash": hashlib.sha256(strategy_source.encode()).hexdigest(),
        "strategy_version": validate_strategy_source(strategy_source)["version"],
        "parameter_hash": _hash(validate_parameters(validate_strategy_source(strategy_source), params)),
        "data_hash": _hash(candles), "engine_version": ENGINE_VERSION, "seed": seed,
        "fee_rate": fee_rate, "slippage_bps": slippage_bps, "market": market, "leverage": leverage,
        "intervals": {"train": [a, b], "validation": [c, d]}, "interval_boundary": "[start,end)",
        "validation_state": "fresh_account_and_strategy"}
    return outputs


def generate_demo_candles(symbols=None, count=720, interval_ms=3600000, seed=42, start_ms=1735689600000):
    """Synthetic OHLCV fixture. Never describe this output as Binance history."""
    symbols = symbols or ["BTCUSDT", "ETHUSDT"]
    if type(count) is not int or not 2 <= count <= 100000:
        raise ValueError("演示数据根数必须为 2 至 100000")
    if type(interval_ms) is not int or interval_ms <= 0:
        raise ValueError("interval_ms 必须为正整数")
    result = {}
    for symbol in sorted(symbols):
        digest = int(hashlib.sha256(symbol.encode()).hexdigest()[:16], 16)
        rng = random.Random(seed + digest)
        price = 65000.0 if symbol.startswith("BTC") else 3200.0 if symbol.startswith("ETH") else 140.0
        bars = []
        for index in range(count):
            opening = price
            drift = 0.0012 * math.sin(index / 45) + rng.gauss(0, 0.006)
            closing = max(0.01, opening * (1 + drift))
            spread = abs(rng.gauss(0.002, 0.001))
            bars.append({"timestamp": start_ms + index * interval_ms, "open": opening,
                         "high": max(opening, closing) * (1 + spread),
                         "low": min(opening, closing) * (1 - min(spread, 0.1)),
                         "close": closing, "volume": rng.uniform(100, 2000)})
            price = closing
        result[symbol] = bars
    return result
