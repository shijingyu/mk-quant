export const lowerIsBetter = new Set([
  "max_drawdown",
  "max_consecutive_losses",
]);
export const higherIsBetter = new Set([
  "total_return",
  "annual_return",
  "profit_factor",
  "win_rate",
  "sharpe",
  "final_equity",
  "realized_pnl",
]);
export function preferredMetric(key, a, b) {
  if (!Number.isFinite(a) || !Number.isFinite(b) || a === b) return null;
  if (lowerIsBetter.has(key)) return a < b ? "train" : "validation";
  if (higherIsBetter.has(key)) return a > b ? "train" : "validation";
  return null;
}
export function splitMetrics(result, split) {
  return split === "validation" ? result?.validation_metrics : result?.metrics;
}
