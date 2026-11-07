import assert from "node:assert/strict";
import test from "node:test";
import {
  constrainLeverage,
  createBacktestPreset,
  leverageBounds,
  normalizeBacktestConfig,
  restoreBacktestPreset,
} from "./backtestPreset.js";

const pair = {
  market: "futures",
  account: { min_capital: 1000, leverage_min: 2, leverage_max: 100 },
  execution: { model: "market_next_open_v1" },
};
const form = {
  initial_capital: "12500",
  leverage: "8",
  fee_rate: "0",
  slippage_bps: "0",
  dataset_id: "data-current",
  train_start: "2026-01-01T00:00",
  validation_end: "2026-11-01T00:00",
  symbols: "BTCUSDC,ETHUSDC",
  seed: 42,
};

test("saving and JSON-loading a preset restores numeric capital, leverage and zero costs", () => {
  const parameters = { lookback: 168, entry_z: 3, exit_z: 0.25 };
  const preset = createBacktestPreset(
    "零费率",
    "ratio",
    parameters,
    form,
    pair,
  );
  assert.deepEqual(preset.backtest_config, {
    initial_capital: 12500,
    leverage: 8,
    fee_rate: 0,
    slippage_bps: 0,
  });
  assert.deepEqual(preset.parameters, parameters);
  assert.notEqual(preset.parameters, parameters);
  assert.deepEqual(Object.keys(preset).sort(), [
    "backtest_config",
    "name",
    "parameters",
    "strategy_id",
  ]);
  const current = {
    ...form,
    initial_capital: 1000,
    leverage: 2,
    fee_rate: 0.0004,
    slippage_bps: 2,
    dataset_id: "another-dataset",
    train_start: "2025-01-01T00:00",
  };
  const saved = JSON.parse(JSON.stringify(preset));
  const restored = restoreBacktestPreset(current, saved, pair);
  for (const key of [
    "initial_capital",
    "leverage",
    "fee_rate",
    "slippage_bps",
  ]) {
    assert.equal(restored[key], preset.backtest_config[key]);
  }
  for (const key of [
    "dataset_id",
    "train_start",
    "validation_end",
    "symbols",
    "seed",
  ]) {
    assert.equal(restored[key], current[key]);
    assert.ok(!(key in preset.backtest_config));
  }
  assert.equal(current.fee_rate, 0.0004);
});

test("nonzero fee and fractional bps survive save-load without unit conversion", () => {
  const preset = createBacktestPreset(
    "成本",
    "ratio",
    {},
    { ...form, fee_rate: "0.00025", slippage_bps: "1.5" },
    { manifest: pair },
  );
  const loaded = restoreBacktestPreset(
    form,
    JSON.parse(JSON.stringify(preset)),
    pair,
  );
  assert.equal(loaded.fee_rate, 0.00025);
  assert.equal(loaded.slippage_bps, 1.5);
});

test("legacy presets with missing or empty config leave every form value alone", () => {
  for (const preset of [
    { parameters: { lookback: 24 } },
    { parameters: {}, backtest_config: {} },
  ]) {
    assert.deepEqual(restoreBacktestPreset(form, preset, pair), form);
  }
});

test("loading a partial config restores only present fields and ignores dataset/date keys", () => {
  const result = restoreBacktestPreset(
    form,
    {
      backtest_config: {
        fee_rate: 0,
        dataset_id: "wrong",
        train_start: "wrong",
        seed: 99,
      },
    },
    pair,
  );
  assert.deepEqual(result, { ...form, fee_rate: 0 });
});

test("all four save fields reject empty, non-finite and nonnumeric inputs rather than coercing zero", () => {
  for (const field of [
    "initial_capital",
    "leverage",
    "fee_rate",
    "slippage_bps",
  ]) {
    for (const value of [
      "",
      "  ",
      null,
      undefined,
      true,
      false,
      [],
      {},
      Infinity,
      NaN,
      "NaN",
      "Infinity",
    ]) {
      assert.throws(
        () =>
          createBacktestPreset(
            "bad",
            "ratio",
            {},
            { ...form, [field]: value },
            pair,
          ),
        /有限数字/,
      );
    }
  }
});

test("save and run normalization enforce the same manifest and cost bounds", () => {
  for (const invalid of [
    { initial_capital: 0 },
    { initial_capital: 999 },
    { leverage: 1 },
    { leverage: 101 },
    { leverage: 2.5 },
    { fee_rate: -0.0001 },
    { fee_rate: 0.1001 },
    { slippage_bps: -1 },
    { slippage_bps: 1001 },
  ]) {
    assert.throws(() => normalizeBacktestConfig({ ...form, ...invalid }, pair));
  }
  assert.deepEqual(
    normalizeBacktestConfig(
      {
        ...form,
        initial_capital: 1000,
        leverage: 100,
        fee_rate: 0.1,
        slippage_bps: 1000,
      },
      pair,
    ),
    {
      initial_capital: 1000,
      leverage: 100,
      fee_rate: 0.1,
      slippage_bps: 1000,
    },
  );
});

test("an invalid preset fails without partially changing the current form", () => {
  const original = { ...form };
  assert.throws(() =>
    restoreBacktestPreset(
      form,
      {
        backtest_config: {
          initial_capital: 50000,
          leverage: 10,
          fee_rate: "",
          slippage_bps: 0,
        },
      },
      pair,
    ),
  );
  assert.deepEqual(form, original);
});

test("initial and switched leverage follow strategy bounds while preserving valid custom leverage", () => {
  assert.deepEqual(leverageBounds(pair), { min: 2, max: 100 });
  assert.equal(constrainLeverage(1, pair), 2);
  assert.equal(constrainLeverage(8, pair), 8);
  assert.equal(constrainLeverage(125, pair), 100);
  assert.equal(constrainLeverage("", pair), 2);
  assert.equal(constrainLeverage("3.5", pair), 2);
  const spot = {
    market: "spot",
    account: { leverage_min: 1, leverage_max: 1 },
  };
  assert.deepEqual(leverageBounds(spot), { min: 1, max: 1 });
  assert.equal(constrainLeverage(50, spot), 1);
  assert.throws(() => normalizeBacktestConfig({ ...form, leverage: 2 }, spot));
});

test("Maker cost settings keep zero slippage and reject nonzero presets", () => {
  const maker = { ...pair, execution: { model: "maker_grid_v1" } };
  assert.equal(normalizeBacktestConfig(form, maker).slippage_bps, 0);
  assert.throws(
    () => normalizeBacktestConfig({ ...form, slippage_bps: 1 }, maker),
    /Maker/,
  );
  assert.throws(
    () =>
      restoreBacktestPreset(
        form,
        { backtest_config: { slippage_bps: 1 } },
        maker,
      ),
    /Maker/,
  );
});

test('position mode survives preset save/load and follows the declared default', () => {
  const strategy = {market:'futures',account:{position_modes:['one_way','hedge'],default_position_mode:'hedge',leverage_min:1,leverage_max:5,min_capital:1}};
  const form={initial_capital:1000,leverage:1,fee_rate:0,slippage_bps:0,position_mode:''};
  const preset=createBacktestPreset('mode','s',{},form,strategy);
  assert.equal(preset.backtest_config.position_mode,'hedge');
  assert.equal(restoreBacktestPreset({...form,position_mode:'one_way'},preset,strategy).position_mode,'hedge');
  assert.throws(()=>normalizeBacktestConfig({...form,position_mode:'unknown'},strategy),/持仓模式/);
});
