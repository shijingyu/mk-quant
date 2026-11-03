"""Equal-ratio USDC hedge grid. Execution and protection belong to the framework."""
import math

MANIFEST = {
    "id": "usdc_hedge_grid",
    "name": "等比网格 · USDC 双向 Maker",
    "version": "1.1.0",
    "description": "五种 USDC 永续可组合运行；独立多空等比网格，入场成交后由执行器立即建立对应止盈和条件止损。全部 Post Only，止损可能触发后仍未成交，不转市价。",
    "market": "futures",
    "symbols": {"mode": "any", "values": ["BTCUSDC", "ETHUSDC", "SOLUSDC", "BNBUSDC", "XRPUSDC"], "min_count": 1},
    "account": {
        "position_modes": ["hedge"], "margin_modes": ["cross"],
        "leverage_min": 1, "leverage_max": 5, "min_capital": 1000,
    },
    "execution": {"model": "maker_grid_v1", "fee_role": "maker", "time_in_force": "GTX"},
    "timeframes": {"primary": "1m", "additional": []},
    "history_window": 2,
    "parameters": {
        "type": "object",
        "properties": {
            "grid_spacing_pct": {"type": "number", "default": 0.6, "minimum": 0.05, "maximum": 20, "title": "每格间距（%）", "description": "等比公比 q = 1 + 间距 / 100；每个币独立锚定首次已收盘价格。"},
            "levels_per_side": {"type": "integer", "default": 4, "minimum": 1, "maximum": 12, "title": "每侧网格层数", "description": "锚点下方挂多头买单，上方挂空头卖单，不以市价初始化持仓。"},
            "order_notional_u": {"type": "number", "default": 100, "minimum": 5, "maximum": 100000, "title": "每格名义金额（U）", "description": "数量 = 名义金额 / 网格价；框架按交易对精度向下规范化。低于交易所最低金额将拒单。"},
            "take_profit_steps": {"type": "integer", "default": 1, "minimum": 1, "maximum": 20, "title": "止盈距离（格）", "description": "相对于该格入场限价，以同一公比计算目标价。"},
            "stop_loss_steps": {"type": "integer", "default": 4, "minimum": 1, "maximum": 100, "title": "止损触发距离（格）", "description": "触发后只挂 Maker 限价单；触发不等于已止损，不保证退出。"},
            "stop_limit_offset_bps": {"type": "number", "default": 2, "minimum": 0.1, "maximum": 100, "title": "止损被动限价偏移（bps）", "description": "多头卖价高于止损触发价，空头买价低于触发价；1 bps = 0.01%。偏移越大，退出可能越慢。"},
            "max_lots_per_side": {"type": "integer", "default": 4, "minimum": 1, "maximum": 24, "title": "每币每侧最大格仓", "description": "已成交格仓与尚未成交入场单共同计数；多空分别限制。"},
            "max_margin_pct": {"type": "number", "default": 50, "minimum": 1, "maximum": 90, "title": "最大保证金预算（%）", "description": "全部币种均分预算，持仓与入场挂单共同占用；超预算撤销新开仓挂单，保留已有保护单。"},
        },
    },
}


class Strategy:
    def __init__(self):
        self.anchors = {}
        self.cycles = {}

    def on_bar(self, context):
        params = context["params"]
        account = context["account"]
        if account.get("position_mode") != "hedge":
            raise ValueError("该策略需要 maker_grid_v1 双向账本，禁止降级为单向净持仓")
        orders = context.get("open_orders", [])
        lots = context.get("lots", [])
        candles = context["candles"]
        selected = sorted(candles)
        if not selected or any(s not in MANIFEST["symbols"]["values"] for s in selected):
            raise ValueError("该网格仅支持声明的五种 USDC 永续")
        ratio = 1 + params["grid_spacing_pct"] / 100
        offset = params["stop_limit_offset_bps"] / 10000
        if offset >= 1 - ratio ** (-params["stop_loss_steps"]):
            raise ValueError("止损被动限价偏移必须小于止损距离，调整偏移或止损格数")
        equity = account["equity"]
        used = account["margin_used"]
        reserved = account.get("pending_margin", 0)
        budget = max(0, equity * params["max_margin_pct"] / 100 - used - reserved)
        intents = []
        indicators = {}
        active_lots = [lot for lot in lots if lot.get("remaining_quantity", lot.get("quantity", 0)) > 0 and lot.get("status") not in ("CLOSED", "closed")]
        protection_failed = any(lot.get("protection_status") in ("REJECTED", "FAILED", "UNPROTECTED", "rejected", "failed", "unprotected", "stop_rejected") for lot in active_lots)
        if equity <= 0 or used + reserved > equity * params["max_margin_pct"] / 100 or protection_failed:
            for order in orders:
                if order.get("role") == "entry":
                    intents.append({"action": "cancel", "order_id": order.get("id", order.get("client_order_id")), "symbol": order["symbol"], "reason": "保证金预算或保护状态不满足，停止补仓"})
            context["emit"](stage="暂停补仓", reason="存在未受保护格仓" if protection_failed else "保证金占用已达预算", pending_margin=reserved, maker_only=True)
            return intents
        for symbol in selected:
            close = candles[symbol]["close"]
            anchor = self.anchors.setdefault(symbol, close)
            indicators[symbol] = {"anchor": anchor, "ratio": ratio, "lower": anchor / ratio ** params["levels_per_side"], "upper": anchor * ratio ** params["levels_per_side"], "close": close}
            symbol_used = sum(position.get("margin_used", 0) for key, position in context["positions"].items() if position.get("symbol", key.split(":")[0]) == symbol)
            symbol_reserved = sum(order["quantity"] * order["price"] / account["leverage"] for order in orders if order.get("symbol") == symbol and order.get("role") == "entry")
            symbol_budget = max(0, equity * params["max_margin_pct"] / 100 / len(selected) - symbol_used - symbol_reserved)
            proposed = {"LONG": 0, "SHORT": 0}
            # Alternate the two legs at each level. Limited capital must not
            # give every slot to LONG or to the first alphabetic symbol.
            for level, leg in [(level, leg) for level in range(1, params["levels_per_side"] + 1) for leg in ("LONG", "SHORT")]:
                leg_lots = [lot for lot in active_lots if lot.get("symbol") == symbol and lot.get("position_side") == leg]
                leg_entries = [order for order in orders if order.get("symbol") == symbol and order.get("position_side") == leg and order.get("role") == "entry"]
                occupied = len(leg_lots) + len(leg_entries) + proposed[leg]
                if occupied >= params["max_lots_per_side"]:
                    continue
                prefix = "g-" + symbol + "-" + ("L" if leg == "LONG" else "S") + "-" + str(level) + "-"
                lot_ids = [str(lot.get("lot_id", lot.get("id", ""))) for lot in leg_lots]
                order_ids = [str(order.get("client_order_id", "")) for order in leg_entries]
                if any(value.startswith(prefix) for value in lot_ids + order_ids):
                    continue
                price = anchor / ratio ** level if leg == "LONG" else anchor * ratio ** level
                if (leg == "LONG" and price >= close) or (leg == "SHORT" and price <= close):
                    continue
                notional = params["order_notional_u"]
                required = notional / account["leverage"] + notional * 0.01
                if required > min(budget, symbol_budget):
                    continue
                cycle = self.cycles.get(prefix, 0) + 1
                self.cycles[prefix] = cycle
                take_profit = price * ratio ** params["take_profit_steps"] if leg == "LONG" else price / ratio ** params["take_profit_steps"]
                stop = price / ratio ** params["stop_loss_steps"] if leg == "LONG" else price * ratio ** params["stop_loss_steps"]
                stop_limit = stop * (1 + offset if leg == "LONG" else 1 - offset)
                if not all(math.isfinite(x) and x > 0 for x in (price, take_profit, stop, stop_limit)):
                    raise ValueError("网格价格超出有效范围")
                intents.append({
                    "action": "place", "client_order_id": prefix + str(cycle),
                    "symbol": symbol, "side": "buy" if leg == "LONG" else "sell", "position_side": leg,
                    "type": "LIMIT", "time_in_force": "GTX", "quantity": notional / price, "price": price,
                    "reduce_only": False,
                    "bracket": {"take_profit_price": take_profit, "stop_price": stop, "stop_limit_price": stop_limit},
                    "reason": "等比网格第 " + str(level) + " 格 " + leg,
                })
                proposed[leg] += 1
                budget -= required
                symbol_budget -= required
        context["emit"](stage="双向等比网格", indicators=indicators, reason="补充空闲网格" if intents else "等待网格成交、资金预算或价格回到挂单范围", active_lots=len(active_lots), entry_orders=sum(order.get("role") == "entry" for order in orders), maker_only=True, stop_policy="触发后仅挂GTX；未成交不视为止损完成")
        return intents
