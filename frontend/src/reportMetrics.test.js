import test from "node:test";
import assert from "node:assert/strict";
import { preferredMetric, splitMetrics } from "./reportMetrics.js";
test("validation switches all four summary values", () => {
  const train = {
    final_equity: 11074.54,
    total_return: 0.1075,
    max_drawdown: 0.1813,
    total_orders: 150,
  };
  const validation = {
    final_equity: 12272.03,
    total_return: 0.2272,
    max_drawdown: 0.048,
    total_orders: 172,
  };
  const r = { metrics: train, validation_metrics: validation };
  assert.equal(splitMetrics(r, "train"), train);
  assert.equal(splitMetrics(r, "validation"), validation);
  assert.equal(splitMetrics({ metrics: train }, "validation"), undefined);
});
test("only comparable, directional metrics highlight a strict winner", () => {
  assert.equal(preferredMetric("max_drawdown", 0.18, 0.048), "validation");
  assert.equal(preferredMetric("max_consecutive_losses", 3, 4), "train");
  assert.equal(preferredMetric("total_return", 0.1, 0.2), "validation");
  assert.equal(preferredMetric("total_orders", 150, 172), null);
  for (const value of [null, undefined, NaN, Infinity])
    assert.equal(preferredMetric("sharpe", value, 2), null);
  assert.equal(preferredMetric("sharpe", 1, 1), null);
});
