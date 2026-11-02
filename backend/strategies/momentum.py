"""Teaching example. This is not an investment recommendation."""

MANIFEST = {
    "id": "momentum",
    "name": "双均线趋势",
    "version": "1.0.0",
    "description": "已收盘快慢均线交叉产生交易意图，下一根开盘执行。支持多币种单向合约持仓。",
    "market": "futures",
    "symbols": {"mode": "any", "values": [], "min_count": 1},
    "account": {
        "position_modes": ["one_way"],
        "margin_modes": ["cross", "isolated"],
        "leverage_min": 1,
        "leverage_max": 5,
        "min_capital": 100,
    },
    "history_window": 1000,
    "parameters": {
        "type": "object",
        "properties": {
            "fast_period": {"type": "integer", "default": 12, "minimum": 2, "maximum": 200, "title": "快均线周期"},
            "slow_period": {"type": "integer", "default": 36, "minimum": 3, "maximum": 500, "title": "慢均线周期"},
            "allocation": {"type": "number", "default": 0.25, "minimum": 0.01, "maximum": 0.9, "title": "每币种资金占比"},
            "allow_short": {"type": "boolean", "default": True, "title": "允许做空"},
        },
    },
}


class Strategy:
    def on_bar(self, context):
        params = context["params"]
        fast_period = params["fast_period"]
        slow_period = params["slow_period"]
        if fast_period >= slow_period:
            raise ValueError("快均线周期必须小于慢均线周期")
        orders = []
        indicators = {}
        for symbol, bars in context["history"].items():
            if len(bars) < slow_period:
                continue
            fast = sum(bar["close"] for bar in bars[-fast_period:]) / fast_period
            slow = sum(bar["close"] for bar in bars[-slow_period:]) / slow_period
            indicators[symbol] = {"fast_ma": fast, "slow_ma": slow}
            position = context["positions"].get(symbol, {}).get("quantity", 0)
            target = 1 if fast > slow else (-1 if params["allow_short"] and fast < slow else 0)
            current = 1 if position > 0 else (-1 if position < 0 else 0)
            if current == target:
                continue
            reason = "快慢均线方向改变"
            if position:
                orders.append({"symbol": symbol, "side": "sell" if position > 0 else "buy", "quantity": abs(position), "reduce_only": True, "reason": reason})
            if target:
                quantity = context["account"]["equity"] * params["allocation"] / len(context["candles"]) / bars[-1]["close"]
                orders.append({"symbol": symbol, "side": "buy" if target > 0 else "sell", "quantity": quantity, "reason": reason})
        context["emit"](stage="趋势跟踪", indicators=indicators,
                        reason="均线方向变化" if orders else "等待均线方向变化或预热完成")
        return orders
