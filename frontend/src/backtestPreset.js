const FIELDS = ["initial_capital", "leverage", "fee_rate", "slippage_bps"];
const LABELS = {
  initial_capital: "初始资金",
  leverage: "杠杆",
  fee_rate: "手续费率",
  slippage_bps: "滑点",
};

const manifestOf = (strategy) => strategy?.manifest || strategy || {};

export function leverageBounds(strategy) {
  const manifest = manifestOf(strategy);
  if (manifest.market === "spot") return { min: 1, max: 1 };
  const min = manifest.account?.leverage_min ?? 1;
  return { min, max: manifest.account?.leverage_max ?? min };
}

export function constrainLeverage(value, strategy) {
  const { min, max } = leverageBounds(strategy);
  if (
    value == null ||
    typeof value === "boolean" ||
    (typeof value === "string" && !value.trim())
  )
    return min;
  const numeric = Number(value);
  return Number.isFinite(numeric) && Number.isInteger(numeric)
    ? Math.min(max, Math.max(min, numeric))
    : min;
}

function numericField(value, key) {
  if (
    !["number", "string"].includes(typeof value) ||
    (typeof value === "string" && !value.trim()) ||
    !Number.isFinite(Number(value))
  )
    throw new Error(`${LABELS[key]}必须填写有限数字`);
  return Number(value);
}

export function normalizeBacktestConfig(
  values,
  strategy,
  { partial = false } = {},
) {
  if (!values || typeof values !== "object" || Array.isArray(values)) {
    throw new Error("资金与费用预设格式无效");
  }
  const manifest = manifestOf(strategy);
  const config = {};
  for (const key of FIELDS) {
    if (partial && !Object.hasOwn(values, key)) continue;
    config[key] = numericField(values[key], key);
  }
  if (Object.hasOwn(config, "initial_capital")) {
    const minimum = manifest.account?.min_capital ?? 0;
    if (config.initial_capital <= 0 || config.initial_capital < minimum) {
      throw new Error(`初始资金须大于0且不低于策略最低资金 ${minimum} U`);
    }
  }
  if (Object.hasOwn(config, "leverage")) {
    const { min, max } = leverageBounds(manifest);
    if (
      !Number.isInteger(config.leverage) ||
      config.leverage < min ||
      config.leverage > max
    ) {
      throw new Error(`杠杆须为 ${min}–${max} 范围内的整数`);
    }
  }
  if (
    Object.hasOwn(config, "fee_rate") &&
    (config.fee_rate < 0 || config.fee_rate > 0.1)
  ) {
    throw new Error("手续费率范围为0–0.1");
  }
  if (Object.hasOwn(config, "slippage_bps")) {
    if (config.slippage_bps < 0 || config.slippage_bps > 1000) {
      throw new Error("滑点范围为0–1000 bps");
    }
    if (
      manifest.execution?.model === "maker_grid_v1" &&
      config.slippage_bps !== 0
    ) {
      throw new Error("Maker限价撮合的滑点须为0");
    }
  }
  if (Object.hasOwn(values, "position_mode")) {
    const modes = manifest.account?.position_modes || ["one_way"];
    const legacy = manifest.execution?.model === "maker_grid_v1" ? "hedge" : "one_way";
    config.position_mode = values.position_mode || manifest.account?.default_position_mode || (modes.includes(legacy) ? legacy : modes[0]);
    if (!modes.includes(config.position_mode)) throw new Error("持仓模式不符合策略声明");
  }
  return config;
}

export function createBacktestPreset(
  name,
  strategyId,
  parameters,
  form,
  strategy,
) {
  return {
    name,
    strategy_id: strategyId,
    parameters: { ...parameters },
    backtest_config: normalizeBacktestConfig(form, strategy),
  };
}

export function restoreBacktestPreset(form, preset, strategy) {
  // Older server records have no backtest_config (or {}). Only these four
  // explicit fields can affect the execution form; dataset/dates stay local.
  const config = normalizeBacktestConfig(
    preset.backtest_config ?? {},
    strategy,
    { partial: true },
  );
  return { ...form, ...config };
}
