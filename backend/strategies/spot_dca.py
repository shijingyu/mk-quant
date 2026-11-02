"""Cash-only periodic accumulation with an optional take-profit exit."""

MANIFEST = {
    "id": "spot_dca",
    "name": "现货定投",
    "version": "1.0.0",
    "description": "按固定 K 线间隔定额买入现货，到达止盈比例时卖出；不借币、不做空、不使用杠杆。",
    "market": "spot",
    "symbols": {"mode": "any", "values": [], "min_count": 1},
    "account": {"position_modes": ["one_way"], "margin_modes": ["cash"], "leverage_min": 1, "leverage_max": 1, "min_capital": 100},
    "history_window": 2,
    "parameters": {
        "type": "object",
        "properties": {
            "interval_bars": {"type": "integer", "default": 24, "minimum": 1, "maximum": 1000, "title": "定投间隔 K 线数"},
            "amount": {"type": "number", "default": 50, "minimum": 1, "maximum": 100000, "title": "每币种单次投入 U"},
            "take_profit": {"type": "number", "default": 0.12, "minimum": 0.001, "maximum": 5, "title": "止盈比例"},
        },
    },
}


class Strategy:
    def on_bar(self, context):
        orders = []
        for symbol, bar in context["candles"].items():
            position = context["positions"].get(symbol, {})
            quantity = position.get("quantity", 0)
            average = position.get("average_price", 0)
            if quantity > 0 and bar["close"] >= average * (1 + context["params"]["take_profit"]):
                orders.append({"symbol": symbol, "side": "sell", "quantity": quantity, "reduce_only": True, "reason": "达到止盈目标"})
            elif context["bar_index"] % context["params"]["interval_bars"] == 0:
                orders.append({"symbol": symbol, "side": "buy", "quantity": context["params"]["amount"] / bar["close"], "reason": "定投周期到达"})
        context["emit"](stage="现货积累", reason="定投或止盈条件触发" if orders else "等待下个定投周期")
        return orders
