"""Raw BTC/ETH price-ratio reversion under the MK completed-bar protocol.

The framework, not this strategy, supplies historical bars, applies current
symbol filters, executes orders, and persists JSON state. Order quantities are
sized at the decision close; actual next-minute execution can change notionals.
Account leverage accepts 2x through 100x and controls margin requirements;
strategy gross sizing stays fixed at 1x equity. Pair orders are sequential, not atomic. Failed exits remain
reduce-only until both positions are actually flat. No funding or terminal
close is invented by this module; those are execution-model responsibilities.
"""
import math


MANIFEST = {'id': 'btc_eth_usdc_ratio_1d',
 'name': 'BTC/ETH 比值回归 · 1d · USDC',
 'version': '1.0.1',
 'description': 'BTCUSDC/ETHUSDC原始比率；1d闭合信号，默认3/1/0.5；下一分钟逐腿执行。账户杠杆2–100倍、目标gross1x；模拟费率/滑点0。MK不计资金费，研究排名已使用两段数据。',
 'market': 'futures',
 'symbols': {'mode': 'fixed', 'values': ['BTCUSDC', 'ETHUSDC'], 'min_count': 2},
 'account': {'position_modes': ['one_way'],
             'margin_modes': ['cross'],
             'leverage_min': 2,
             'leverage_max': 100,
             'min_capital': 1000},
 'execution': {'model': 'market_next_open_v1', 'paper_costs': {'fee_rate': 0.0, 'slippage_bps': 0.0}},
 'timeframes': {'primary': '1d', 'additional': []},
 'history_window': 720,
 'parameters': {'type': 'object',
                'properties': {'lookback': {'type': 'integer',
                                            'default': 3,
                                            'minimum': 2,
                                            'maximum': 720,
                                            'title': '滚动窗口（主周期根数）',
                                            'description': '包含当前已闭合主周期K线；不足或不连续时等待，不能用分钟数代替主周期根数。'},
                               'entry_z': {'type': 'number',
                                           'default': 1,
                                           'minimum': 0.1,
                                           'maximum': 10.0,
                                           'title': '入场阈值 Z',
                                           'description': '严格小于负阈值做多BTC/做空ETH；严格大于正阈值反向；等于阈值不入场。'},
                               'exit_z': {'type': 'number',
                                          'default': 0.5,
                                          'minimum': 0.0,
                                          'maximum': 5.0,
                                          'title': '离场带宽 |Z|',
                                          'description': '严格abs(Z)<阈值才触发正常退出；触发后持续只减仓直到实有仓位全平。跨过离场带不算触发。'}}}}


class Strategy:
    def __init__(self):
        self.exit_pending = False

    def close_positions(self, positions, reason):
        intents = []
        for symbol in ("BTCUSDC", "ETHUSDC"):
            quantity = positions.get(symbol, {}).get("quantity", 0)
            if quantity:
                intents.append({
                    "symbol": symbol,
                    "side": "sell" if quantity > 0 else "buy",
                    "quantity": abs(quantity),
                    "reduce_only": True,
                    "reason": reason,
                })
        return intents

    def on_bar(self, context):
        positions = context["positions"]
        quantity_a = positions.get("BTCUSDC", {}).get("quantity", 0)
        quantity_b = positions.get("ETHUSDC", {}).get("quantity", 0)
        if not all(math.isfinite(value) for value in (quantity_a, quantity_b)):
            raise ValueError("配对持仓数量必须为有限数字")

        # Execution recovery takes priority over indicator warmup. A rejected
        # second entry/exit leg must never cause us to add a replacement leg.
        one_leg = bool(quantity_a) != bool(quantity_b)
        same_direction = bool(quantity_a) and bool(quantity_b) and ((quantity_a > 0) == (quantity_b > 0))
        if one_leg or same_direction:
            self.exit_pending = True
            reason = "检测到单腿残留，持续只减仓直至配对全平" if one_leg else "检测到双腿同向，持续只减仓直至配对全平"
            context["log"]("error", reason, {"BTCUSDC": quantity_a, "ETHUSDC": quantity_b})
            context["emit"](stage="配对执行恢复", reason=reason, exit_pending=True,
                            pair_quantities={"BTCUSDC": quantity_a, "ETHUSDC": quantity_b})
            return self.close_positions(positions, reason)
        if self.exit_pending and (quantity_a or quantity_b):
            reason = "此前离场已触发但仓位尚未全平，继续只减仓，不新增敞口"
            context["emit"](stage="等待双腿退出", reason=reason, exit_pending=True)
            return self.close_positions(positions, reason)
        if not quantity_a and not quantity_b:
            self.exit_pending = False

        params = context["params"]
        lookback = params["lookback"]
        entry_z = params["entry_z"]
        exit_z = params["exit_z"]
        if exit_z >= entry_z:
            raise ValueError("离场阈值必须小于入场阈值")
        signal_interval = context.get("signal_interval", "1h")
        interval_ms = {"15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}
        if signal_interval not in interval_ms:
            raise ValueError("比值策略主周期仅支持15m、1h、4h或1d")
        candles = context["candles"]
        if set(candles) != {"BTCUSDC", "ETHUSDC"}:
            raise ValueError("比值策略固定使用BTCUSDC和ETHUSDC两个完整行情")
        history = context["history"]
        bars_a = history.get("BTCUSDC", [])
        bars_b = history.get("ETHUSDC", [])
        available_bars = min(len(bars_a), len(bars_b))
        if available_bars < lookback:
            context["emit"](stage="等待历史预热", reason="完整主周期数据不足，继续积累而不以分钟代替",
                            warmup_required=lookback, warmup_available=available_bars, exit_pending=False)
            return []

        recent_a = bars_a[-lookback:]
        recent_b = bars_b[-lookback:]
        aligned = all(a["timestamp"] == b["timestamp"] for a, b in zip(recent_a, recent_b))
        continuous = all(right["timestamp"] - left["timestamp"] == interval_ms[signal_interval] for left, right in zip(recent_a, recent_a[1:]))
        current = (recent_a[-1]["timestamp"] == candles["BTCUSDC"]["timestamp"]
                   and recent_b[-1]["timestamp"] == candles["ETHUSDC"]["timestamp"]
                   and recent_a[-1]["close"] == candles["BTCUSDC"]["close"]
                   and recent_b[-1]["close"] == candles["ETHUSDC"]["close"])
        if not aligned or not continuous or not current:
            context["emit"](stage="等待连续行情", reason="双腿历史须按主周期对齐、连续并包含本次闭合K线",
                            history_aligned=aligned, history_continuous=continuous, current_bar_included=current)
            return []
        if any(not math.isfinite(bar["close"]) or bar["close"] <= 0 for bar in recent_a + recent_b):
            raise ValueError("比值策略只接受有限正数收盘价")

        ratios = [a["close"] / b["close"] for a, b in zip(recent_a, recent_b)]
        mean = math.fsum(ratios) / lookback
        # fsum()/n can round a constant window's mean one ULP away from its
        # identical values; preserve the mathematical zero-variance case.
        constant_ratio = min(ratios) == max(ratios)
        if constant_ratio:
            mean = ratios[-1]
        variance = 0.0 if constant_ratio else math.fsum((ratio - mean) ** 2 for ratio in ratios) / lookback
        standard_deviation = math.sqrt(variance)
        ratio = ratios[-1]
        if not all(math.isfinite(value) for value in (ratio, mean, standard_deviation)):
            raise ValueError("比值指标超出有限数字范围")
        if standard_deviation == 0:
            context["emit"](stage="等待比值波动", reason="滚动总体标准差为0，不生成交易信号",
                            indicators={"BTCUSDC/ETHUSDC": {"ratio": ratio, "mean": mean, "std": 0.0, "z_score": None}})
            return []
        z_score = (ratio - mean) / standard_deviation
        indicators = {"BTCUSDC/ETHUSDC": {
            "ratio": ratio, "mean": mean, "std": standard_deviation,
            "z_score": z_score, "lookback": lookback, "entry_z": entry_z, "exit_z": exit_z,
        }}

        if quantity_a and quantity_b:
            if abs(z_score) < exit_z:
                self.exit_pending = True
                reason = "比值Z进入严格离场带；退出未全部成交时持续只减仓"
                context["emit"](stage="退出配对", indicators=indicators, reason=reason, exit_pending=True)
                return self.close_positions(positions, reason)
            context["emit"](stage="持有配对", indicators=indicators, exit_pending=False,
                            reason="尚未进入严格离场带；冻结原数量，不按时间或穿零额外退出")
            return []

        direction = 1 if z_score < -entry_z else (-1 if z_score > entry_z else 0)
        if not direction:
            context["emit"](stage="等待入场", indicators=indicators, exit_pending=False,
                            reason="未严格越过入场阈值，保持空仓")
            return []
        equity = context["account"]["equity"]
        if not math.isfinite(equity) or equity <= 0:
            context["emit"](stage="暂停开仓", indicators=indicators, reason="账户权益非正或无效，不增加敞口")
            return []
        target_per_leg = equity * 0.5
        reason = "比值偏低：多BTC空ETH" if direction > 0 else "比值偏高：空BTC多ETH"
        context["emit"](stage="建立配对", indicators=indicators, reason=reason, exit_pending=False,
                        target_gross_notional=equity, target_notional_per_leg=target_per_leg,
                        sizing_reference="本次完整主周期收盘价；下一分钟实际名义额可变化",
                        precision_policy="数量及价格由框架按交易规则规范化",
                        pair_execution="逐腿顺序执行；异常腿在下一主周期回调只减仓恢复")
        return [
            {"symbol": "BTCUSDC", "side": "buy" if direction > 0 else "sell",
             "quantity": target_per_leg / candles["BTCUSDC"]["close"], "reduce_only": False, "reason": reason},
            {"symbol": "ETHUSDC", "side": "sell" if direction > 0 else "buy",
             "quantity": target_per_leg / candles["ETHUSDC"]["close"], "reduce_only": False, "reason": reason},
        ]
