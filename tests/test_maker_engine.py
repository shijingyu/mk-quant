"""Maker execution invariants: conservative OHLC, durable lots and hedge accounting."""
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from backend.engine import Ledger, MakerOrderBook, _filters_for, run_backtest
from backend.strategy import load_strategy, validate_parameters, validate_strategy_source


SYMBOL = "BTCUSDT"
RULES = {SYMBOL: {"tick_size": ".1", "step_size": ".1", "min_qty": ".1", "min_notional": "5"}}


def book(capital=1000, fee=0, leverage=1):
    ledger = Ledger(capital, "futures", leverage, fee, position_mode="hedge")
    ledger.prices = {SYMBOL: Decimal("100")}
    return MakerOrderBook(ledger, RULES)


def entry(id="e", side="LONG", price=None, quantity=1, tp=None, stop=None, stop_limit=None):
    long = side == "LONG"
    return {"client_order_id": id, "symbol": SYMBOL, "side": "buy" if long else "sell",
            "position_side": side, "type": "LIMIT", "time_in_force": "GTX", "quantity": quantity,
            "price": price if price is not None else 99 if long else 101,
            "bracket": {"take_profit_price": tp if tp is not None else 110 if long else 90,
                        "stop_price": stop if stop is not None else 90 if long else 110,
                        "stop_limit_price": stop_limit if stop_limit is not None else 90.1 if long else 109.9}}


def bar(t, opening=100, high=102, low=98, close=100):
    return {SYMBOL: {"timestamp": t, "open": opening, "high": high, "low": low, "close": close, "volume": 100}}


def opened(**kwargs):
    b = book(**kwargs)
    assert b.submit(entry(), 0, {SYMBOL: 100})["status"] == "NEW"
    b.process_bar(bar(1))
    return b


def test_hedge_legs_coexist_and_cannot_close_the_wrong_leg():
    l = book().ledger
    rules = {SYMBOL: _filters_for(SYMBOL, RULES)}
    for side, position in (("buy", "LONG"), ("sell", "SHORT")):
        assert l.execute({"symbol": SYMBOL, "side": side, "position_side": position, "quantity": 1}, 1, rules, Decimal(0), 0) is None
    l.prices[SYMBOL] = Decimal(110)
    assert l.equity() == 1000
    assert l.margin() == 220
    assert l.position_snapshot()["BTCUSDT:LONG"]["quantity"] == 1
    assert l.position_snapshot()["BTCUSDT:SHORT"]["quantity"] == -1
    assert l.execute({"symbol": SYMBOL, "side": "sell", "position_side": "LONG", "quantity": 1, "reduce_only": True}, 2, rules, Decimal(0), 1) is None
    assert l.balance == 1010
    assert list(l.position_snapshot()) == ["BTCUSDT:SHORT"]
    assert "超过" in l.execute({"symbol": SYMBOL, "side": "buy", "position_side": "SHORT", "quantity": 2, "reduce_only": True}, 3, rules, Decimal(0), 2)
    assert l.position_snapshot()["BTCUSDT:SHORT"]["quantity"] == -1
    assert Ledger.load(json.loads(json.dumps(l.dump()))).dump() == l.dump()


def test_legacy_market_executor_refuses_limit_and_stop_semantics():
    l = Ledger(1000, "futures", 1, 0)
    l.prices = {SYMBOL: Decimal(100)}
    for kind in ("LIMIT", "STOP_LIMIT", "STOP_MARKET"):
        reason = l.execute({"symbol": SYMBOL, "side": "buy", "quantity": 1, "type": kind, "price": 50}, 1, {SYMBOL: _filters_for(SYMBOL, RULES)}, Decimal(0), 0)
        assert "不接受" in reason
    assert not l.orders


def test_gtx_rejection_idempotency_and_conflicting_ids_are_durable():
    b = book()
    rejected = entry(price=100)
    assert b.submit(rejected, 0, {SYMBOL: 100})["status"] == "REJECTED"
    count = len(b.events)
    assert b.submit(rejected, 1, {SYMBOL: 100})["status"] == "REJECTED"
    assert len(b.events) == count
    conflict = b.submit(entry(price=99), 1, {SYMBOL: 100})
    assert conflict["status"] == "REJECTED" and "不同" in conflict["reason"]
    assert b.orders["e"]["price"] == "100"
    assert not b.ledger.orders
    assert b.ledger.pending_margin == 0


def test_pending_orders_reserve_margin_and_release_on_cancel():
    b = book(capital=200, fee=.001)
    assert b.submit(entry("one", quantity=1), 0, {SYMBOL: 100})["status"] == "NEW"
    assert b.ledger.pending_margin == Decimal(99)
    assert b.ledger.pending_fees == Decimal(".099")
    assert b.submit(entry("two", quantity=2), 0, {SYMBOL: 100})["status"] == "REJECTED"
    b.cancel("one", 1)
    assert b.ledger.pending_margin == 0
    assert b.submit(entry("three", quantity=2), 1, {SYMBOL: 100})["status"] == "NEW"
    assert b.ledger.account()["available"] == pytest.approx(1.802)


def test_next_minute_inclusive_touch_and_bracket_no_same_entry_bar_exit():
    b = book()
    b.submit(entry(), 0, {SYMBOL: 100})
    b.process_bar(bar(0, high=120, low=80))
    assert not b.ledger.orders
    b.process_bar(bar(1, low=99))
    assert len(b.ledger.orders) == 1  # Inclusive minute range is the declared model.
    b.process_bar(bar(2, high=105, low=95))
    assert len(b.ledger.orders) == 1
    assert b.ledger.orders[0]["price"] == 99
    assert len(b.open_orders()) == 2
    assert b.lot_snapshot()[0]["protection_status"] == "protected"
    assert all(o["eligible_after"] == 1 for o in b.open_orders())


def test_zero_volume_cannot_fill_entry_or_triggered_stop():
    b = book()
    b.submit(entry(), 0, {SYMBOL: 100})
    zero = bar(1)
    zero[SYMBOL]["volume"] = 0
    b.process_bar(zero)
    assert not b.ledger.orders and b.orders["e"]["status"] == "NEW"
    assert b.events[-1]["type"] == "ZERO_VOLUME_FILL_SKIPPED"
    b.process_bar(bar(2))
    triggered = bar(3, low=89)
    triggered[SYMBOL]["volume"] = 0
    b.process_bar(triggered)
    assert b.orders["e:sl"]["triggered"] is True
    zero_exit = bar(4, opening=90, high=91, low=89, close=90)
    zero_exit[SYMBOL]["volume"] = 0
    b.process_bar(zero_exit)
    assert len(b.ledger.orders) == 1 and b.has_open_positions()
    b.process_bar(bar(5, opening=90, high=91, low=89, close=90))
    assert len(b.ledger.orders) == 2 and not b.has_open_positions()


@pytest.mark.parametrize("intent", [
    entry(stop_limit=110),
    entry(stop_limit=98.99),  # SELL rounds to 99, exactly the entry price.
    entry(side="SHORT", stop_limit=101.01),  # BUY rounds to entry 101.
    entry(tp=98.91),  # SELL TP rounds to 99, so cannot protect a 99 entry.
])
def test_quantized_bracket_cannot_cross_back_to_entry_or_wrong_exit_direction(intent):
    b = book()
    response = b.submit(intent, 0, {SYMBOL: 100})
    assert response["status"] == "REJECTED"
    assert "量化" in response["reason"]
    assert not b.open_orders() and not b.ledger.orders


def test_stop_first_ambiguous_bar_and_next_bar_stop_limit_eligibility():
    b = opened()
    b.process_bar(bar(2, high=120, low=80))
    assert len(b.ledger.orders) == 1
    assert b.orders["e:sl"]["status"] == "NEW"
    assert b.orders["e:sl"]["triggered"] is True
    assert b.lot_snapshot()[0]["protection_status"] == "stop_triggered"
    assert any(e["type"] == "AMBIGUOUS_EXIT_DEFERRED" for e in b.events)
    b.process_bar(bar(3, opening=89, high=90, low=88, close=89))
    assert len(b.ledger.orders) == 1
    b.process_bar(bar(4, opening=89, high=90.1, low=88, close=90))
    assert len(b.ledger.orders) == 2
    assert b.ledger.orders[-1]["role"] == "sl"
    assert b.ledger.orders[-1]["closed_trade_pnl"] == pytest.approx(-8.9)
    assert b.orders["e:tp"]["status"] == "CANCELED"
    assert not b.has_open_positions()


def test_stop_cross_rejection_keeps_open_risk_and_cancels_further_entries():
    b = book()
    b.submit(entry(stop_limit=89), 0, {SYMBOL: 100})
    b.process_bar(bar(1))
    b.submit(entry("deeper", price=80, tp=100, stop=70, stop_limit=70.1), 1, {SYMBOL: 100})
    b.process_bar(bar(2, high=120, low=70))
    assert b.orders["e:sl"]["status"] == "REJECTED"
    assert b.orders["deeper"]["status"] == "CANCELED"
    assert len(b.ledger.orders) == 1
    assert b.lot_snapshot()[0]["protection_status"] == "stop_rejected"
    assert b.submit(entry("new", price=98), 2, {SYMBOL: 100})["status"] == "REJECTED"
    assert b.has_open_positions()
    # The rejected stop does not delete the still-valid TP. It can exit on a
    # later bar, but never retroactively on the ambiguous trigger bar.
    b.process_bar(bar(3, high=111, low=100, close=105))
    assert b.ledger.orders[-1]["role"] == "tp"
    assert not b.has_open_positions()
    count = len(b.ledger.orders)
    b.process_bar(bar(4, high=120, low=80))
    assert len(b.ledger.orders) == count


def test_intrabar_stop_reference_is_trigger_not_earlier_open():
    b = opened()
    b.process_bar(bar(2, opening=100, high=101, low=89, close=90))
    # Sell 90.1 is passive versus trigger 90, although it crosses the earlier open 100.
    assert b.orders["e:sl"]["status"] == "NEW"
    trigger = next(e for e in b.events if e["type"] == "STOP_TRIGGERED")
    assert trigger["reference_price"] == "90"
    assert len(b.ledger.orders) == 1


def test_short_bracket_stop_mirrors_long_and_does_not_net_with_long():
    b = book()
    b.submit(entry("short", side="SHORT"), 0, {SYMBOL: 100})
    b.process_bar(bar(1))
    assert b.ledger.position_snapshot()["BTCUSDT:SHORT"]["quantity"] == -1
    b.process_bar(bar(2, opening=100, high=111, low=89, close=110))
    assert b.orders["short:sl"]["status"] == "NEW"
    assert len(b.ledger.orders) == 1
    b.process_bar(bar(3, opening=110, high=112, low=109.8, close=110))
    assert b.ledger.orders[-1]["closed_trade_pnl"] == pytest.approx(-8.9)
    assert b.orders["short:tp"]["status"] == "CANCELED"


def test_lot_specific_cost_basis_and_fees_survive_other_lots():
    b = book(fee=.001)
    b.submit(entry("first", price=99, tp=109), 0, {SYMBOL: 100})
    b.submit(entry("second", price=98, tp=120), 0, {SYMBOL: 100})
    b.process_bar(bar(1, low=97))
    assert b.ledger.position_snapshot()["BTCUSDT:LONG"]["average_price"] == 98.5
    b.process_bar(bar(2, high=110, low=100, close=105))
    assert b.ledger.orders[-1]["lot_id"] == "first"
    assert b.ledger.orders[-1]["closed_trade_pnl"] == pytest.approx(9.792)
    assert b.ledger.balance == Decimal("1009.694")
    assert b.ledger.position_snapshot()["BTCUSDT:LONG"]["average_price"] == 98
    assert len(b.lot_snapshot()) == 1


def test_partial_manual_lot_reduction_resizes_protection_and_cannot_overclose():
    b = opened(fee=.001)
    manual = {"client_order_id": "partial", "symbol": SYMBOL, "side": "sell", "position_side": "LONG",
              "type": "LIMIT", "time_in_force": "GTX", "quantity": .4, "price": 105,
              "reduce_only": True, "lot_id": "e"}
    b.submit(manual, 1, {SYMBOL: 100})
    b.process_bar(bar(2, high=106, low=99, close=102))
    assert b.lot_snapshot()[0]["remaining_quantity"] == .6
    assert all(o["quantity"] == .6 for o in b.open_orders())
    b.process_bar(bar(3, high=111, low=100, close=105))
    assert not b.has_open_positions()
    assert sum(o["closed_quantity"] for o in b.ledger.orders) == 1
    assert not b.ledger.position_snapshot()


def test_json_restart_is_identical_and_same_bar_is_idempotent():
    original = opened(fee=.0002)
    restored = MakerOrderBook.load(json.loads(json.dumps(original.dump(), allow_nan=False)))
    count = len(restored.events)
    assert restored.process_bar(bar(1)) == []
    assert len(restored.events) == count
    future = [bar(2, high=120, low=80), bar(3, opening=89, high=91, low=88, close=90)]
    for candle in future:
        assert restored.process_bar(candle) == original.process_bar(candle)
    assert restored.dump() == original.dump()


def test_flatten_is_passive_idempotent_and_never_claims_closed_before_fill():
    b = opened()
    first = b.request_flatten(1, {SYMBOL: 100})
    second = b.request_flatten(1, {SYMBOL: 100})
    assert first == second and len(first) == 1
    assert first[0]["price"] == 100.1
    assert b.has_open_positions()
    assert b.lot_snapshot()[0]["protection_status"] == "flattening"
    assert b.orders["e:tp"]["status"] == b.orders["e:sl"]["status"] == "CANCELED"
    b.process_bar(bar(2, high=100, low=99))
    assert b.has_open_positions()
    b.process_bar(bar(3, high=100.2, low=99))
    assert not b.has_open_positions()
    assert b.ledger.orders[-1]["role"] == "flatten"
    assert b.request_flatten(4, {SYMBOL: 100}) == []


def test_maker_limit_uses_lot_size_not_market_quantity_filter():
    b = book()
    b.symbol_filters = {SYMBOL: {"tick_size": ".1", "step_size": ".01", "market_step_size": "1", "market_min_qty": "1"}}
    assert b.submit(entry(quantity=.13), 0, {SYMBOL: 100})["quantity"] == .13
    b.process_bar(bar(1))
    assert b.ledger.orders[0]["quantity"] == .13


def test_missing_protection_wrong_side_and_market_orders_fail_closed():
    b = book()
    missing = entry(); missing.pop("bracket")
    for i, intent in enumerate((missing, {**entry(), "side": "sell"}, {**entry(), "type": "MARKET"})):
        assert b.submit({**intent, "client_order_id": str(i)}, 0, {SYMBOL: 100})["status"] == "REJECTED"
    assert not b.open_orders() and not b.ledger.orders


def test_already_triggered_conditional_submission_is_explicitly_rejected():
    b = opened()
    response = b.submit({"client_order_id": "bad-stop", "symbol": SYMBOL, "side": "sell", "position_side": "LONG",
                        "type": "STOP_LIMIT", "time_in_force": "GTX", "quantity": 1, "price": 111,
                        "stop_price": 110, "reduce_only": True, "lot_id": "e"}, 1, {SYMBOL: 100})
    assert response["status"] == "REJECTED"
    assert "条件已满足" in response["reason"]
    assert b.has_open_positions() and len(b.ledger.orders) == 1


def source():
    manifest = {"name": "maker test", "version": "1", "description": "fixture", "market": "futures",
                "symbols": {"mode": "fixed", "values": [SYMBOL], "min_count": 1},
                "account": {"position_modes": ["hedge"], "margin_modes": ["cross"], "leverage_min": 1, "leverage_max": 1, "min_capital": 1},
                "execution": {"model": "maker_grid_v1"}, "parameters": {"type": "object", "properties": {}}}
    return "MANIFEST = " + repr(manifest) + "\nclass Strategy:\n    def on_bar(self, context):\n        if context['bar_index'] == 0:\n            return [" + repr(entry()) + "]\n        return []\n"


def test_backtest_uses_same_book_contract_and_reports_assumptions_and_orders():
    candles = {SYMBOL: [bar(1735689600000 + i * 60000, high=h, low=l)[SYMBOL] for i, (h, l) in enumerate(((102, 98), (120, 98), (111, 100)))]}
    result = run_backtest(source(), {}, candles, 1000, .0002, 0, symbol_filters=RULES)
    assert len(result["orders"]) == 2
    assert result["orders"][1]["role"] == "tp"
    assert result["orders"][1]["timestamp"] > result["orders"][0]["timestamp"]
    assert result["order_book"]["orders"]["e:sl"]["status"] == "CANCELED"
    assert result["assumptions"]["execution"] == "maker_grid_v1"
    assert result["assumptions"]["real_bid_ask_queue_available"] is False
    assert result["engine_version"] == "1.3.0"
    assert [event for decision in result["decisions"] for event in decision["execution_events"]] == result["events"]
    assert any(event["type"] == "ORDER_ACCEPTED" for event in result["decisions"][0]["execution_events"])
    json.dumps(result, allow_nan=False)
    with pytest.raises(ValueError, match="slippage_bps"):
        run_backtest(source(), {}, candles, 1000, .0002, 2)


def test_last_bar_signal_stays_unfilled_and_no_terminal_liquidation_is_invented():
    code = source().replace("== 0", "== 1")
    candles = {SYMBOL: [bar(1735689600000 + i*60000)[SYMBOL] for i in range(2)]}
    result = run_backtest(code, {}, candles, 1000, 0, 0, symbol_filters=RULES)
    assert not result["orders"]
    assert result["open_orders"][0]["status"] == "NEW"
    assert result["account"]["pending_margin"] == 99
    assert result["metrics"]["final_equity"] == 1000


def test_grid_mark_margin_budget_cancels_entries_preserves_lots_and_can_replenish_later():
    """A mark-driven budget breach stops new risk; it is not an automatic liquidation."""
    symbol = "BTCUSDC"
    ledger = Ledger(1000, "futures", 1, 0, position_mode="hedge")
    ledger.prices = {symbol: Decimal(100)}
    b = MakerOrderBook(ledger, {symbol: RULES[SYMBOL]})
    b.submit({**entry("held"), "symbol": symbol}, 0, {symbol: 100})
    b.process_bar({symbol: bar(1)[SYMBOL]})
    b.submit({**entry("parked", price=98, quantity=2), "symbol": symbol}, 1, {symbol: 100})
    # A zero-volume price observation updates mark margin without inventing TP fills.
    b.process_bar({symbol: {"timestamp": 2, "open": 1000, "high": 1000, "low": 1000, "close": 1000, "volume": 0}})
    code = (Path(__file__).parents[1] / "backend/strategies/usdc_hedge_grid.py").read_text()
    manifest = validate_strategy_source(code)
    params = validate_parameters(manifest, {"max_margin_pct": 30})
    strategy = load_strategy(code)
    display = {}

    def context(price):
        return {"params": params, "account": ledger.account(), "positions": ledger.position_snapshot(),
                "candles": {symbol: {"close": price}}, "open_orders": b.open_orders(), "lots": b.lot_snapshot(),
                "emit": lambda **fields: display.update(fields)}

    assert ledger.margin() + ledger.pending_margin > ledger.equity() * Decimal(".3")
    intents = strategy.on_bar(context(1000))
    assert intents and all(intent["action"] == "cancel" for intent in intents)
    b.submit_many(intents, 2, {symbol: 1000})
    assert ledger.pending_margin == 0
    assert b.has_open_positions()
    assert {o["role"] for o in b.open_orders()} == {"tp", "sl"}
    assert display["stage"] == "暂停补仓"
    b.process_bar({symbol: {"timestamp": 3, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 0}})
    restored = strategy.on_bar(context(100))
    assert any(intent["action"] == "place" for intent in restored)
