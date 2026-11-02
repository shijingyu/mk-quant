"""Deterministic, bar-based execution shared by strategy experiments.

Signals use completed candles and fill at the *next* common timestamp's open.
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


ENGINE_VERSION = "1.0.0"
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


def _filters_for(symbol, filters):
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

    def __init__(self, initial_capital, market, leverage, fee_rate):
        self.initial = initial_capital
        self.balance = initial_capital
        self.market = market
        self.leverage = leverage
        self.fee_rate = fee_rate
        self.positions = {}
        self.prices = {}
        self.orders = []
        self.closed_pnls = []
        self.fees = ZERO

    def unrealized(self):
        return sum(((self.prices[symbol] - position.average_price) * position.quantity
                    for symbol, position in self.positions.items()), ZERO)

    def equity(self):
        return self.balance + self.unrealized()

    def margin(self):
        return sum((abs(position.quantity) * self.prices[symbol] / self.leverage
                    for symbol, position in self.positions.items()), ZERO)

    def account(self):
        equity = self.equity()
        margin = self.margin()
        return {"initial_equity": _float(self.initial), "balance": _float(self.balance),
                "equity": _float(equity), "available": _float(max(ZERO, equity - margin)),
                "margin_used": _float(margin), "unrealized_pnl": _float(self.unrealized()),
                "realized_pnl": _float(self.balance - self.initial),
                "leverage": _float(self.leverage), "market": self.market,
                "position_mode": "one_way", "margin_mode": "cash" if self.market == "spot" else "cross"}

    def position_snapshot(self):
        return {symbol: {"quantity": _float(p.quantity), "average_price": _float(p.average_price),
                         "unrealized_pnl": _float((self.prices[symbol] - p.average_price) * p.quantity),
                         "margin_used": _float(abs(p.quantity) * self.prices[symbol] / self.leverage)}
                for symbol, p in self.positions.items() if p.quantity}

    def execute(self, intent, timestamp, filters, slippage, signal_timestamp):
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
        position = self.positions.setdefault(symbol, Position())
        old_quantity = position.quantity
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
        raw_price = self.prices[symbol] * (ONE + slippage if side == "buy" else ONE - slippage)
        price = _quantize(raw_price, rule["tick_size"], ROUND_CEILING if side == "buy" else ROUND_FLOOR)
        if price <= 0:
            return "成交价格量化后不大于零"
        notional = quantity * price
        if notional < rule["min_notional"] and not (self.market == "futures" and intent.get("reduce_only")):
            return "委托名义金额小于交易对最小金额"
        fee = notional * self.fee_rate
        closing = min(abs(old_quantity), quantity) if old_quantity * signed < 0 else ZERO
        opening = quantity - closing
        # Mark-to-market equity immediately after a fill includes fees and the
        # adverse execution-vs-open difference. Check the entire prospective
        # book before mutating it; pure reductions are always permitted.
        projected_equity = self.equity() - fee + (self.prices[symbol] - price) * signed
        projected_margin = self.margin() + (abs(new_quantity) - abs(old_quantity)) * self.prices[symbol] / self.leverage
        if opening and (projected_equity <= 0 or projected_margin > projected_equity):
            return "可用资金或保证金不足（含手续费与滑点）"
        gross = closing * (price - position.average_price) * (ONE if old_quantity > 0 else -ONE)
        allocated_entry_fee = position.entry_fees * closing / abs(old_quantity) if closing else ZERO
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
    if market != manifest["market"]:
        raise ValueError("所选市场与策略要求不一致")
    if "one_way" not in manifest["account"]["position_modes"]:
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
