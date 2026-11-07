"""Execution-contract regression tests; run with python3 -m unittest discover."""

import copy
import json
import unittest

from backend.engine import (BacktestCancelled, calculate_metrics, generate_demo_candles,
                            run_backtest, run_split_backtest)
from backend.strategy import (builtin_strategies, load_strategy, validate_manifest,
                              validate_strategy_source)


BASE = {
    "name": "test", "version": "1.0.0", "description": "deterministic test fixture",
    "market": "futures", "symbols": {"mode": "any", "values": [], "min_count": 1},
    "account": {"position_modes": ["one_way"], "margin_modes": ["cross"],
                "leverage_min": 1, "leverage_max": 5, "min_capital": 1},
    "parameters": {"type": "object", "properties": {}},
}


def source(body, market="futures", manifest=None):
    manifest = copy.deepcopy(manifest or BASE)
    manifest["market"] = market
    if market == "spot":
        manifest["account"].update(margin_modes=["cash"], leverage_max=1)
    return "MANIFEST = " + repr(manifest) + "\nclass Strategy:\n    def on_bar(self, context):\n" + "\n".join("        " + line for line in body.splitlines()) + "\n"


def bars(prices, start=1735689600000, step=60000):
    return [{"timestamp": start + index * step, "open": price, "high": price * 1.1,
             "low": price * 0.9, "close": price, "volume": 100} for index, price in enumerate(prices)]


def run(code, prices=(100, 100, 110, 120), **kwargs):
    options = dict(strategy_source=code, params={}, candles={"BTCUSDT": bars(prices)},
                   initial_capital=1000, fee_rate=0, slippage_bps=0)
    options.update(kwargs)
    return run_backtest(**options)


class StrategyContractTests(unittest.TestCase):
    def test_builtins_validate_and_load(self):
        for strategy in builtin_strategies():
            self.assertEqual(validate_strategy_source(strategy["source"])["name"], strategy["name"])
            self.assertTrue(callable(load_strategy(strategy["source"]).on_bar))

    def test_validation_never_executes_class_body(self):
        code = source("return []") + "\nclass Extra:\n    raise ValueError('must not run')\n"
        self.assertEqual(validate_strategy_source(code)["name"], "test")
        with self.assertRaisesRegex(ValueError, "must not run"):
            load_strategy(code)

    def test_required_fields_and_unsafe_imports_rejected(self):
        with self.assertRaisesRegex(ValueError, "account"):
            validate_manifest({key: value for key, value in BASE.items() if key != "account"})
        with self.assertRaises(ValueError):
            validate_strategy_source("import os\n" + source("return []"))

    def test_spot_manifest_cannot_require_leverage(self):
        bad = copy.deepcopy(BASE)
        bad["market"] = "spot"
        with self.assertRaisesRegex(ValueError, "现货"):
            validate_manifest(bad)

    def test_unsupported_account_modes_fail_explicitly(self):
        for key, value in (("margin_modes", ["isolated"]),):
            manifest = copy.deepcopy(BASE)
            manifest["account"][key] = value
            with self.assertRaisesRegex(ValueError, "当前"):
                run(source("return []", manifest=manifest))

    def test_fixed_symbols_and_parameter_validation(self):
        fixed = copy.deepcopy(BASE)
        fixed["symbols"] = {"mode": "fixed", "values": ["ETHUSDT"], "min_count": 1}
        with self.assertRaisesRegex(ValueError, "固定"):
            run(source("return []", manifest=fixed))
        with self.assertRaisesRegex(ValueError, "未知参数"):
            run(source("return []"), params={"x": 1})


class EngineExecutionTests(unittest.TestCase):
    def test_close_signal_executes_next_open_without_future_history(self):
        code = source("assert len(context['history']['BTCUSDT']) == context['bar_index'] + 1\n"
                      "if context['bar_index'] == 0:\n"
                      "    return [{'symbol':'BTCUSDT','side':'buy','quantity':1}]\n"
                      "return []")
        result = run(code, prices=(100, 200, 300))
        order = result["orders"][0]
        self.assertEqual(order["price"], 200)
        self.assertEqual(order["timestamp"] - order["signal_timestamp"], 60000)
        self.assertEqual(result["metrics"]["final_equity"], 1100)
        self.assertEqual(result["equity"][0]["equity"], 1000)

    def test_fees_count_both_legs_and_entry_fee_allocated_to_trade(self):
        code = source("if context['bar_index'] == 0:\n"
                      "    return [{'symbol':'BTCUSDT','side':'buy','quantity':1}]\n"
                      "if context['bar_index'] == 1:\n"
                      "    return [{'symbol':'BTCUSDT','side':'sell','quantity':1,'reduce_only':True}]\n"
                      "return []")
        result = run(code, prices=(100, 100, 110), fee_rate=0.001)
        self.assertAlmostEqual(result["metrics"]["final_equity"], 1009.79)
        self.assertAlmostEqual(result["orders"][1]["closed_trade_pnl"], 9.79)
        self.assertAlmostEqual(result["metrics"]["realized_pnl"], 9.79)
        self.assertEqual(result["metrics"]["win_rate"], 1)
        self.assertIsNone(result["metrics"]["profit_factor"])

    def test_decimal_precision_and_adverse_slippage(self):
        code = source("if context['bar_index'] == 0:\n"
                      "    return [{'symbol':'BTCUSDT','side':'buy','quantity':1.23456789}]\n"
                      "return []")
        result = run(code, slippage_bps=10, symbol_filters={"BTCUSDT": {"tick_size": "0.25", "step_size": "0.01"}})
        order = result["orders"][0]
        self.assertEqual(order["quantity"], 1.23)
        self.assertEqual(order["price"], 100.25)

    def test_spot_cannot_short_or_borrow_cash(self):
        for intent in ({"side": "sell", "quantity": 1}, {"side": "buy", "quantity": 11}):
            code = source("return [dict(symbol='BTCUSDT', **" + repr(intent) + ")]", market="spot")
            result = run(code, market="spot")
            self.assertEqual(result["orders"], [])
            self.assertEqual(result["decisions"][0]["risk"][0]["status"], "rejected")
        with self.assertRaisesRegex(ValueError, "杠杆"):
            run(source("return []", market="spot"), market="spot", leverage=2)

    def test_market_quantity_filters_and_maximum_are_enforced(self):
        code = source("return [{'symbol':'BTCUSDT','side':'buy','quantity':1.13}]")
        filters = {"BTCUSDT": {"step_size": "0.03", "market_step_size": "0.05", "market_max_qty": "1.10"}}
        result = run(code, prices=(100, 100), symbol_filters=filters)
        # Least common multiple of .03 and .05 is .15, giving 1.05.
        self.assertEqual(result["orders"][0]["quantity"], 1.05)
        filters["BTCUSDT"]["market_max_qty"] = "1.00"
        rejected = run(code, prices=(100, 100), symbol_filters=filters)
        self.assertEqual(rejected["orders"], [])
        self.assertIn("最大数量", rejected["decisions"][0]["risk"][0]["reason"])

    def test_spot_reduce_only_does_not_bypass_minimum_notional(self):
        code = source("if context['bar_index'] == 0:\n"
                      "    return [{'symbol':'BTCUSDT','side':'buy','quantity':1}]\n"
                      "if context['bar_index'] == 1:\n"
                      "    return [{'symbol':'BTCUSDT','side':'sell','quantity':1,'reduce_only':True}]\n"
                      "return []", market="spot")
        result = run(code, prices=(100,100,90), market="spot", symbol_filters={"BTCUSDT":{"min_notional":"95"}})
        self.assertEqual(len(result["orders"]), 1)
        self.assertIn("最小金额", result["decisions"][1]["risk"][0]["reason"])

    def test_reduce_only_cannot_flip_position(self):
        code = source("if context['bar_index'] == 0:\n"
                      "    return [{'symbol':'BTCUSDT','side':'buy','quantity':1}]\n"
                      "if context['bar_index'] == 1:\n"
                      "    return [{'symbol':'BTCUSDT','side':'sell','quantity':10,'reduce_only':True}]\n"
                      "return []")
        result = run(code)
        self.assertEqual(result["orders"][1]["quantity"], 1)
        self.assertEqual(result["positions"], {})

    def test_multi_symbol_alignment_has_no_future_bar(self):
        code = source("assert len(context['candles']) == 2\n"
                      "assert context['candles']['BTCUSDT']['timestamp'] == context['candles']['ETHUSDT']['timestamp']\n"
                      "return []")
        btc = bars([100, 110, 120, 130])
        eth = bars([10, 11, 12, 13])[1:]
        with self.assertRaisesRegex(ValueError, "相同完整"):
            run(code, candles={"BTCUSDT": btc, "ETHUSDT": eth})
        result = run(code, candles={"BTCUSDT": btc[1:], "ETHUSDT": eth})
        self.assertEqual(len(result["equity"]), 3)
        self.assertEqual(result["assumptions"]["dropped_unaligned_bars"]["BTCUSDT"], 0)

    def test_reversal_short_profit_and_partial_fee_allocation(self):
        code = source("orders = {0: ('sell', 2), 1: ('buy', 1), 2: ('buy', 2)}\n"
                      "if context['bar_index'] in orders:\n"
                      "    side, quantity = orders[context['bar_index']]\n"
                      "    return [{'symbol':'BTCUSDT','side':side,'quantity':quantity}]\n"
                      "return []")
        result = run(code, prices=(100, 100, 90, 80, 85), fee_rate=0.001)
        self.assertAlmostEqual(result["orders"][1]["closed_trade_pnl"], 9.81)
        self.assertAlmostEqual(result["orders"][2]["closed_trade_pnl"], 19.82)
        self.assertEqual(result["positions"]["BTCUSDT"]["quantity"], 1)
        self.assertAlmostEqual(result["metrics"]["final_equity"], 1034.55)

    def test_final_signal_is_never_filled(self):
        result = run(source("return [{'symbol':'BTCUSDT','side':'buy','quantity':1}]"), prices=(100, 100))
        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(result["decisions"][-1]["status"], "expired")

    def test_progress_and_cancellation(self):
        events = []
        run(source("return []"), progress=events.append)
        self.assertEqual(events[0]["status"], "running")
        self.assertEqual(events[-1]["status"], "completed")
        self.assertEqual(sum(len(event["equity"]) for event in events), 4)
        with self.assertRaises(BacktestCancelled):
            run(source("return []"), cancelled=lambda: True)

    def test_metrics_drawdown_pf_losing_streak_and_nulls(self):
        curve = [{"timestamp": 1735689600000 + index * 86400000, "equity": value} for index, value in enumerate((100, 120, 90, 110))]
        result = calculate_metrics(curve, [], 100, closed_pnls=[10, -5, -3, 8, 0, -2])
        self.assertEqual(result["max_drawdown"], 0.25)
        self.assertEqual(result["max_drawdown_amount"], 30)
        self.assertEqual(result["profit_factor"], 1.8)
        self.assertEqual(result["max_consecutive_losses"], 2)
        self.assertAlmostEqual(result["win_rate"], 1 / 3)
        self.assertIsNotNone(result["sharpe"])
        json.dumps(result, allow_nan=False)

    def test_train_validation_have_fresh_accounts_and_no_overlap(self):
        code = source("if context['bar_index'] == 0:\n"
                      "    assert context['account']['equity'] == 1000\n"
                      "    assert not context['positions']\n"
                      "    return [{'symbol':'BTCUSDT','side':'buy','quantity':1}]\n"
                      "return []")
        candles = {"BTCUSDT": bars([100, 110, 120, 130, 140, 150])}
        timestamps = [row["timestamp"] for row in candles["BTCUSDT"]]
        result = run_split_backtest(code, {}, candles, 1000, 0, 0,
                                    timestamps[0], timestamps[3], timestamps[3], timestamps[-1] + 1)
        self.assertEqual(result["train"]["metrics"]["initial_equity"], 1000)
        self.assertEqual(result["validation"]["orders"][0]["price"], 140)
        self.assertEqual(result["reproducibility"]["validation_state"], "fresh_account_and_strategy")
        with self.assertRaisesRegex(ValueError, "重叠"):
            run_split_backtest(code, {}, candles, 1000, 0, 0,
                               timestamps[0], timestamps[4], timestamps[3], timestamps[-1] + 1)

    def test_deterministic_demo_and_invalid_data_rejected(self):
        self.assertEqual(generate_demo_candles(seed=42, count=10), generate_demo_candles(seed=42, count=10))
        self.assertNotEqual(generate_demo_candles(seed=42, count=10), generate_demo_candles(seed=43, count=10))
        bad = bars([100, 110])
        bad[1]["timestamp"] = bad[0]["timestamp"]
        with self.assertRaisesRegex(ValueError, "递增"):
            run(source("return []"), candles={"BTCUSDT": bad})


if __name__ == "__main__":
    unittest.main()
