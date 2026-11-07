"""DOGE/SHIB OLS reversion, standalone MK Quant protocol v1.4.0 strategy.

Uses completed 15m/1h/4h/1d histories supplied by the framework. Training ends
before the signal bar opens, including at higher-timeframe boundaries. Each
instance uses its own account equity: 1% empirical risk budget, 1x entry gross
cap, not a leverage multiplier. Quantities are sized at the decision close.

Orders execute sequentially next minute; emitted intents are not fills. Failed
exits and incomplete pairs remain reduce-only until actual positions are flat.
Only JSON-compatible instance state is retained. No I/O, funding calculation,
exchange access or terminal liquidation is performed by this source file.
"""

import math


MANIFEST = {
    "id": "doge_shib_usdc_ols_15m",
    "name": "DOGE/SHIB · USDC 15m OLS独立配对",
    "version": "2.1.0",
    "description": "DOGEUSDC/1000SHIBUSDC；信号前96根15m对数OLS，样本标准差，Z入场2/退出1/止损3。四窗口对数收益相关均≥0.82、正β、有效半衰期；协整按用户假设。独立账户全部权益作为预算基数，1%经验风险预算、双腿总名义上限1倍权益，杠杆不放大仓位。使用框架完整1h/4h/1d历史，需90个完整UTC日预热。冻结模型，逐腿执行异常持续只减仓。",
    "market": "futures",
    "symbols": {"mode": "fixed", "values": ["DOGEUSDC", "1000SHIBUSDC"], "min_count": 2},
    "account": {
        "position_modes": ["hedge"],
        "default_position_mode": "hedge", "margin_modes": ["cross"],
        "leverage_min": 2, "leverage_max": 100, "min_capital": 1000,
    },
    "execution": {
        "model": "market_next_open_v1",
        "paper_costs": {"fee_rate": 0.0, "slippage_bps": 0.0},
    },
    "timeframes": {"primary": "15m", "additional": ["1h", "4h", "1d"]},
    "history_window": 181,
    "parameters": {"type": "object", "properties": {}},
}


def configuration():
    return {
        "pair": ("DOGEUSDC", "1000SHIBUSDC"),
        "entry_z": 2.0, "exit_z": 1.0, "stop_z": 3.0,
        "min_correlation": 0.82, "risk_fraction": 0.01,
        "windows": (("24h", "15m", 900000, 96), ("7d", "1h", 3600000, 168),
                    ("30d", "4h", 14400000, 180), ("90d", "1d", 86400000, 90)),
    }


def finite(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def timestamp(value, interval_ms=900000):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0 and value % interval_ms == 0


def center(values):
    mean = math.fsum(values) / len(values)
    return mean, [value - mean for value in values]


def analyze(prices_x, prices_y, interval_ms):
    if len(prices_x) != len(prices_y) or len(prices_x) < 4:
        raise ValueError("训练价格必须等长且至少4根")
    if any(not finite(price) or price <= 0 for price in (*prices_x, *prices_y)):
        raise ValueError("训练价格必须为有限正数")
    lx, ly = [math.log(p) for p in prices_x], [math.log(p) for p in prices_y]
    mx, xc = center(lx)
    my, yc = center(ly)
    denominator = math.fsum(y * y for y in yc)
    if denominator <= 1e-20:
        raise ValueError("回归价格无有效方差")
    beta = math.fsum(x * y for x, y in zip(xc, yc)) / denominator
    alpha = mx - beta * my
    spread = [x - alpha - beta * y for x, y in zip(lx, ly)]
    mean, centered = center(spread)
    std = math.sqrt(math.fsum(value * value for value in centered) / (len(spread) - 1))
    if std <= 1e-12:
        raise ValueError("价差样本标准差退化")
    rx = [right - left for left, right in zip(lx, lx[1:])]
    ry = [right - left for left, right in zip(ly, ly[1:])]
    _, cx = center(rx)
    _, cy = center(ry)
    vx = math.fsum(value * value for value in cx)
    vy = math.fsum(value * value for value in cy)
    if math.sqrt(vx / len(rx)) <= 1e-12 or math.sqrt(vy / len(ry)) <= 1e-12:
        raise ValueError("对数收益率无有效方差")
    correlation = math.fsum(x * y for x, y in zip(cx, cy)) / (math.sqrt(vx) * math.sqrt(vy))
    lag_power = math.fsum(value * value for value in centered[:-1])
    if lag_power <= 1e-20:
        raise ValueError("滞后价差退化")
    phi = math.fsum(a * b for a, b in zip(centered, centered[1:])) / lag_power
    half_life = -math.log(2) / math.log(phi) if 0 < phi < 1 else None
    metrics = {
        "alpha": alpha, "beta": beta, "spread_mean": mean, "spread_std": std,
        "correlation": max(-1.0, min(1.0, correlation)), "ar1_phi": phi,
        "half_life_bars": half_life,
        "half_life_hours": None if half_life is None else half_life * interval_ms / 3600000,
        "z": centered[-1] / std, "price_count": len(lx), "return_count": len(rx),
    }
    if any(value is not None and not finite(value) for value in metrics.values()):
        raise ValueError("指标超出有限数字范围")
    return metrics


def score(model, price_x, price_y):
    z = (math.log(price_x) - model["alpha"] - model["beta"] * math.log(price_y)
         - model["spread_mean"]) / model["spread_std"]
    if not finite(z):
        raise ValueError("Z分数超出有限数字范围")
    return z


def valid_model(model):
    return (isinstance(model, dict)
            and all(finite(model.get(key)) for key in ("alpha", "beta", "spread_mean", "spread_std", "half_life_bars"))
            and model["beta"] > 0 and model["spread_std"] > 1e-12 and model["half_life_bars"] > 0)


def entry_side(z, model, correlations):
    cfg = configuration()
    if not finite(z) or abs(z) > cfg["stop_z"] or not valid_model(model):
        return 0
    if any(not finite(correlations.get(name)) or correlations[name] < cfg["min_correlation"]
           for name, _, _, _ in cfg["windows"]):
        return 0
    return 1 if z < -cfg["entry_z"] else (-1 if z > cfg["entry_z"] else 0)


def exit_reason(z, side):
    cfg = configuration()
    if abs(z) > cfg["stop_z"]:
        return "stop_loss"
    if (side == 1 and z > -cfg["exit_z"]) or (side == -1 and z < cfg["exit_z"]):
        return "mean_reversion"
    return None


def new_state(last_bar=None):
    return {"last_bar": last_bar, "entry_bar": None, "side": 0, "model": None,
            "entry_correlations": {}, "exit_pending": False}


def current(context, pair):
    candles = context.get("candles")
    if not isinstance(candles, dict) or set(candles) != set(pair):
        raise ValueError("闭合行情必须且只能包含本策略两个固定合约")
    a, b = (candles[symbol] for symbol in pair)
    if not isinstance(a, dict) or not isinstance(b, dict):
        raise ValueError("缺少本配对已闭合15m行情")
    stamp = a.get("timestamp")
    if not timestamp(stamp) or not timestamp(b.get("timestamp")) or stamp != b["timestamp"]:
        raise ValueError("双腿K线必须按UTC 15m开盘毫秒时间戳对齐")
    if "decision_time" in context and context["decision_time"] != stamp + 900000:
        raise ValueError("决策边界必须为本次15m K线收盘边界")
    if "timestamp" in context and context["timestamp"] != stamp + 840000:
        raise ValueError("上下文timestamp必须为最后执行分钟开盘时间，不是主周期时间")
    px, py = a.get("close"), b.get("close")
    if not all(finite(price) and price > 0 for price in (px, py)):
        raise ValueError("收盘价必须为有限正数")
    return stamp, px, py


def window_prices(source, pair, interval, count, stamp, candles):
    if not isinstance(source, dict):
        raise ValueError("缺少框架提供的完整周期历史，不能用15m根数替代高周期")
    end = stamp // interval * interval
    first = end - count * interval
    legs = []
    for symbol in pair:
        bars = source.get(symbol, [])
        if not isinstance(bars, list):
            raise ValueError("历史必须是按时间升序排列的K线列表")
        if any(not isinstance(bar, dict) or not timestamp(bar.get("timestamp"), interval) for bar in bars):
            raise ValueError("历史时间戳必须是UTC周期对齐的开盘毫秒数")
        recent = [bar for bar in bars if first <= bar["timestamp"] < end]
        if len(recent) != count or any(bar["timestamp"] != first + i * interval for i, bar in enumerate(recent)):
            raise ValueError(f"{symbol}周期{interval}ms需要信号前连续{count}根完整历史；不足、缺口或重复时等待")
        prices = [bar.get("close") for bar in recent]
        if any(not finite(price) or price <= 0 for price in prices):
            raise ValueError("历史收盘价必须为有限正数")
        if interval == 900000:
            present = [bar for bar in bars if bar["timestamp"] == stamp]
            if len(present) != 1 or present[0].get("close") != candles[symbol]["close"]:
                raise ValueError("主周期历史须包含且仅包含一次本次闭合K线，与当前收盘价一致")
        legs.append(prices)
    return legs


def training(context, pair, stamp):
    frames = context.get("timeframe_history", {})
    if not isinstance(frames, dict):
        raise ValueError("缺少按周期索引的timeframe_history")
    fits, correlations = {}, {}
    for name, interval_name, interval, count in configuration()["windows"]:
        source = context.get("history") if interval_name == "15m" else frames.get(interval_name)
        x, y = window_prices(source, pair, interval, count, stamp, context["candles"])
        try:
            fits[name] = analyze(x, y, interval)
            correlations[name] = fits[name]["correlation"]
        except ValueError:
            fits[name], correlations[name] = None, None
    return fits["24h"], correlations


class Strategy:
    def __init__(self):
        self.state_version = 2
        self.state = new_state()

    def restored_state(self):
        if self.state_version != 2 or not isinstance(self.state, dict):
            self.state_version = 2
            self.state = new_state()
        if not timestamp(self.state.get("last_bar")):
            self.state["last_bar"] = None
        return self.state

    def display(self, context, state, stage, reason, **details):
        context["emit"](pair="/".join(configuration()["pair"]), stage=stage, reason=reason,
                        exit_pending=bool(state.get("exit_pending")),
                        cointegration_status="assumed_by_user_not_tested", **details)

    def close_positions(self, context, pair, quantities, state, reason):
        state["exit_pending"] = True
        self.display(context, state, "持续只减仓", reason, pair_quantities=dict(zip(pair, quantities)))
        return [{"symbol": symbol, "side": "sell" if quantity > 0 else "buy",
                 "quantity": abs(quantity), "reduce_only": True, "reason": reason}
                for symbol, quantity in zip(pair, quantities) if quantity]

    def on_bar(self, context):
        # Hedge legs remain independent in the framework. The pair signal core
        # sees one signed leg per asset only after checking for unexpected overlap.
        if context.get("account", {}).get("position_mode", "hedge") != "hedge":
            raise ValueError("该策略要求 hedge 双向持仓账本")
        pair = ('DOGEUSDC', '1000SHIBUSDC')
        positions = context.get("positions")
        if not isinstance(positions, dict):
            raise ValueError("持仓快照必须为字典")
        normalized = {}
        exits = []
        overlap = False
        for symbol in pair:
            legs = []
            for side in ("LONG", "SHORT"):
                position = positions.get(symbol + ":" + side, {})
                quantity = position.get("quantity", 0)
                if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity):
                    raise ValueError("持仓数量必须为有限数字")
                if quantity:
                    if (quantity > 0) != (side == "LONG"):
                        raise ValueError("持仓腿与数量方向不一致")
                    legs.append(position)
                    exits.append({"symbol":symbol, "position_side":side,
                        "side":"sell" if side=="LONG" else "buy", "quantity":abs(quantity),
                        "reduce_only":True, "reason":"检测到同币多空重叠，逐腿只减仓恢复"})
            if positions.get(symbol, {}).get("quantity", 0):
                raise ValueError("检测到单向持仓键，禁止当作双向账本继续运行")
            overlap = overlap or len(legs) > 1
            if legs:
                normalized[symbol] = legs[0]
        if overlap:
            context["emit"](stage="双向持仓恢复", reason="同币多空重叠，先逐腿平仓；禁止净额抵消或重复开仓")
            return exits
        inputs = dict(context)
        inputs["positions"] = normalized
        intents = self.pair_decision(inputs)
        for intent in intents:
            if intent.get("reduce_only"):
                intent["position_side"] = "LONG" if intent["side"] == "sell" else "SHORT"
            else:
                intent["position_side"] = "LONG" if intent["side"] == "buy" else "SHORT"
        return intents

    def pair_decision(self, context):
        pair = configuration()["pair"]
        state = self.restored_state()
        positions = context.get("positions")
        legs = [positions.get(symbol, {}) if isinstance(positions, dict) else None for symbol in pair]
        quantities = tuple(leg.get("quantity", 0) if isinstance(leg, dict) else None for leg in legs)
        if not all(finite(quantity) for quantity in quantities):
            state["exit_pending"] = True
            context["log"]("error", "配对持仓数量必须为有限数字；不生成无效订单")
            self.display(context, state, "持仓数据异常", "等待框架恢复有效持仓数量；恢复后只减仓")
            return []
        qx, qy = quantities
        one_leg = bool(qx) != bool(qy)
        same_direction = qx and qy and ((qx > 0) == (qy > 0))
        reason = None
        if one_leg or same_direction:
            reason = "单腿残留或双腿同向，禁止补腿，持续只减仓至全平"
        elif (qx or qy) and state.get("exit_pending"):
            reason = "此前离场未完成，继续只减仓，不取消退出"
        elif qx and qy and (not valid_model(state.get("model"))
                           or state.get("side") != (1 if qx > 0 else -1)
                           or not timestamp(state.get("entry_bar"))):
            reason = "入场冻结状态丢失或与实仓方向不符，不重建模型，安全退出"
        if reason:
            context["log"]("error", reason)
            return self.close_positions(context, pair, quantities, state, reason)
        if not qx and not qy and state.get("exit_pending"):
            state = new_state(state["last_bar"])
            self.state = state
        if (context.get("signal_interval", "15m") != "15m"
                or context.get("base_interval", "1m") != "1m" or context.get("params", {})):
            reason = "配置不符：固定15m及选定参数；禁止新增敞口，有仓则持续只减仓"
            if qx or qy:
                return self.close_positions(context, pair, quantities, state, reason)
            self.display(context, state, "配置不符", reason)
            return []
        try:
            return self.decide(context, pair, quantities, state)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.display(context, state, "等待有效行情", str(exc))
            return []

    def decide(self, context, pair, quantities, state):
        stamp, px, py = current(context, pair)
        if state["last_bar"] is not None and stamp <= state["last_bar"]:
            self.display(context, state, "忽略重复或倒序K线", "同一闭合K线不重复产生开仓或正常离场指令")
            return []
        qx, qy = quantities
        if qx and qy:
            if stamp <= state["entry_bar"]:
                self.display(context, state, "忽略入场前行情", "不得用入场信号及更早K线评估下一分钟才成交的持仓")
                return []
            model = state["model"]
            z = score(model, px, py)
            state["last_bar"] = stamp
            indicators = {"/".join(pair): {**model, "z_score": z, "model_frozen": True,
                                           "entry_correlations": state.get("entry_correlations", {})}}
            reason = exit_reason(z, state["side"])
            self.display(context, state, "冻结模型检查", reason or "尚未回归；冻结原模型和数量，不以滚动筛选阻挡退出",
                         indicators=indicators)
            if reason:
                return self.close_positions(context, pair, quantities, state, reason)
            return []

        # Failed entry intents do not imply a position; retry only on a later bar.
        state = new_state(state["last_bar"])
        self.state = state
        model, correlations = training(context, pair, stamp)
        if model is None:
            state["last_bar"] = stamp
            self.display(context, state, "等待有效统计", "15m训练模型退化，不入场", correlations=correlations)
            return []
        z = score(model, px, py)
        state["last_bar"] = stamp
        cfg = configuration()
        indicators = {"/".join(pair): {**model, "z_score": z, "model_frozen": False,
                                       "correlations": correlations, "entry_z": cfg["entry_z"],
                                       "exit_z": cfg["exit_z"], "stop_z": cfg["stop_z"],
                                       "min_correlation": cfg["min_correlation"], "training_cutoff_ms": stamp}}
        side = entry_side(z, model, correlations)
        if not side:
            self.display(context, state, "等待入场", "需四相关≥0.82、β>0、有效半衰期，且2<|Z|≤3", indicators=indicators)
            return []
        equity = context["account"]["equity"]
        if not finite(equity) or equity <= 0:
            self.display(context, state, "暂停开仓", "账户权益非正或无效", indicators=indicators)
            return []
        denominator = model["beta"] * cfg["stop_z"] * model["spread_std"]
        if not finite(denominator) or denominator <= 0:
            raise ValueError("风险公式分母无效")
        x_budget = equity * cfg["risk_fraction"] / denominator
        gross = min(x_budget * (1 + model["beta"]), equity)
        nx = gross / (1 + model["beta"])
        ny = nx * model["beta"]
        sizes = (nx / px, ny / py)
        if not all(finite(value) and value > 0 for value in sizes):
            raise ValueError("下单数量超出有限正数范围")
        state.update(model=model, side=side, entry_bar=stamp, entry_correlations=correlations)
        reason = "negative_spread_deviation" if side > 0 else "positive_spread_deviation"
        self.display(context, state, "建立配对", reason, indicators=indicators,
                     allocation_fraction=1.0, allocated_equity=equity,
                     risk_fraction=cfg["risk_fraction"], target_gross_notional=gross,
                     target_notional_x=nx, target_notional_y=ny,
                     sizing_reference="本次完整15m收盘；下一分钟实际成交名义额可变化",
                     precision_policy="框架负责交易规则取整；数量直接使用所选合约单位，不额外乘除1000",
                     pair_execution="逐腿执行；异常在后续回调持续只减仓；账户杠杆不放大策略仓位")
        return [{"symbol": pair[0], "side": "buy" if side > 0 else "sell",
                 "quantity": sizes[0], "reduce_only": False, "reason": reason},
                {"symbol": pair[1], "side": "sell" if side > 0 else "buy",
                 "quantity": sizes[1], "reduce_only": False, "reason": reason}]
