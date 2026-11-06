import React, { useState } from "react";
import { formatBeijing } from "./time.js";
import "./report-views.css";
const labels = {
  provenance: "版本与运行环境",
  assumptions: "撮合与费用假设",
  parameters: "策略参数",
  strategy_version: "策略版本",
  strategy_version_id: "策略版本编号",
  strategy_hash: "策略源码哈希",
  parameters_hash: "参数版本哈希",
  data_version: "数据版本",
  data_hash: "数据内容哈希",
  engine_version: "引擎版本",
  fee_model: "费用模型",
  slippage_model: "滑点模型",
  seed: "随机种子",
  intervals: "运行区间",
  environment: "运行环境",
  source: "来源",
  symbol_filters: "交易规则",
  base_interval: "基础周期",
  timeframes: "策略周期",
  fill_model: "成交模型",
  symbol_attribution: "交易对归因",
  market: "市场",
  execution: "执行模型",
  execution_model: "执行模型",
  position_mode: "持仓模式",
  fee_rate: "手续费率",
  maker_rate: "Maker 费率",
  taker_rate: "Taker 费率",
  slippage_bps: "滑点（bps）",
  bps: "基点",
  type: "类型",
  rules: "规则",
  python: "Python 版本",
  platform: "操作系统",
  primary: "信号周期",
  additional: "附加周期",
  timestamp: "时间",
  decision_time: "决策时间",
  signal_timestamp: "信号时间",
  execution_timestamp: "执行时间",
  train_start: "回测开始",
  train_end: "回测结束（不含）",
  validation_start: "验证开始",
  validation_end: "验证结束（不含）",
  inputs: "策略行情输入",
  execution_inputs: "撮合行情",
  indicators: "指标",
  account: "账户快照",
  positions: "持仓",
  display: "策略说明",
  intents: "交易意图",
  risk: "风控结果",
  logs: "策略日志",
  events: "订单事件",
  reason: "原因",
  stage: "策略阶段",
  status: "状态",
  split: "数据区间",
  signal_interval: "信号周期",
  symbol: "交易对",
  side: "买卖方向",
  direction: "持仓方向",
  quantity: "数量",
  price: "价格",
  fee: "手续费（U）",
  notional: "成交金额（U）",
  realized_pnl: "已实现盈亏（U）",
  closed_trade_pnl: "平仓净盈亏（U）",
  id: "编号",
  order_id: "订单编号",
  client_order_id: "客户订单编号",
  position_side: "持仓方向",
  reduce_only: "只减仓",
  open: "开盘价",
  high: "最高价",
  low: "最低价",
  close: "收盘价",
  volume: "成交量",
  balance: "余额（U）",
  equity: "权益（U）",
  available: "可用资金（U）",
  margin_used: "保证金（U）",
  unrealized_pnl: "浮动盈亏（U）",
  message: "内容",
  level: "级别",
  data: "附加数据",
};
const values = {
  train: "回测集",
  validation: "验证集",
  buy: "买入",
  sell: "卖出",
  long: "多头",
  short: "空头",
  LONG: "多头",
  SHORT: "空头",
  BUY: "买入",
  SELL: "卖出",
  executed: "已执行",
  pending: "待执行",
  no_trade: "无交易",
  expired: "已过期",
  accepted: "通过",
  rejected: "拒绝",
  not_executed: "未执行",
  futures: "永续合约",
  spot: "现货",
  hedge: "双向持仓",
  one_way: "单向持仓",
  manual: "手动设置",
  historical: "历史行情",
  debug: "调试",
  error: "错误",
  FILLED: "已成交",
  CANCELLED: "已撤销",
  REJECTED: "已拒绝",
  OPEN: "挂单中",
};
Object.assign(labels, {
  funding_included: "计入资金费",
  liquidation_simulated: "模拟强制平仓",
  order_book_simulated: "模拟深度订单簿",
  margin_model: "保证金模型",
  isolated_margin_simulated: "模拟逐仓保证金",
  quote_currency_model: "计价币模型",
  final_positions_closed: "期末强制平仓",
  dropped_unaligned_bars: "剔除未对齐行情",
  trade_metric_basis: "交易统计口径",
  sharpe_basis: "夏普计算口径",
  insolvent: "账户资不抵债",
  higher_timeframe_buckets: "高周期合成规则",
  lookback: "回看周期",
  entry_z: "入场 Z 值",
  exit_z: "退出 Z 值",
  gross_realized_pnl: "平仓毛盈亏（U）",
  closed_quantity: "平仓数量",
  opened_quantity: "开仓数量",
});
Object.assign(values, {
  filled: "已成交",
  provided: "实验固定规则",
  cross_equity: "全仓权益",
  minute_ohlc_inclusive_range_v1: "逐分钟高低价区间内撮合（含边界）",
  closed_primary_signal_fill_next_1m_open_within_inclusive_high_low:
    "主周期收盘生成信号，下一分钟开盘价在高低区间内成交",
  UTC_aligned_complete_minutes_only: "UTC 对齐，仅使用完整分钟行情",
  equal_initial_capital_shared_execution: "初始资金等分，同次组合执行归因",
  equal_initial_capital_shared_execution_not_independent_reruns:
    "初始资金等分，同次组合执行归因",
  shared_U_balance_USDT_and_USDC_assumed_at_par:
    "共享 U 余额，USDT 与 USDC 按等值假设",
  fee_adjusted_position_reductions: "扣除分摊费用后的减仓收益",
  daily_equity_zero_risk_free_365_days: "每日权益，零无风险利率，365 天年化",
});
const isObject = (v) => v !== null && typeof v === "object";
function cell(v, key = "") {
  if (v == null || v === "") return "—";
  if (/timestamp|decision_time|execution_time|_start$|_end$/.test(key))
    return formatBeijing(v, { second: "2-digit" });
  if (typeof v === "boolean") return v ? "是" : "否";
  if (typeof v === "number")
    return Number.isFinite(v)
      ? v.toLocaleString("en-US", { maximumFractionDigits: 8 })
      : "—";
  return values[v] || String(v);
}
function Expand({ title, children }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="report-expand">
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "收起" : "展开"} · {title}
      </button>
      {open && <div className="report-expanded">{children}</div>}
    </div>
  );
}
export function DataTable({ value }) {
  if (!isObject(value)) return <span>{cell(value)}</span>;
  const rows = Array.isArray(value) ? value : Object.entries(value);
  if (!rows.length) return <span className="dim">无记录</span>;
  if (Array.isArray(value)) {
    const keys = [
      ...new Set(
        value.flatMap((v) =>
          isObject(v) && !Array.isArray(v) ? Object.keys(v) : ["value"],
        ),
      ),
    ];
    return (
      <div className="table-wrap report-table">
        <table>
          <thead>
            <tr>
              {keys.map((k) => (
                <th key={k}>{labels[k] || k}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {value.map((row, i) => (
              <tr key={i}>
                {keys.map((k) => (
                  <td key={k}>
                    <Field value={isObject(row) ? row[k] : row} name={k} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return (
    <div className="table-wrap report-table">
      <table>
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}>
              <th scope="row">{labels[k] || k}</th>
              <td>
                <Field name={k} value={v} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function Field({ value, name }) {
  return isObject(value) ? (
    <Expand
      title={`${labels[name] || name}（${Object.keys(value).length} 项）`}
    >
      <DataTable value={value} />
    </Expand>
  ) : (
    <span className="report-value">{cell(value, name)}</span>
  );
}
export function VersionSnapshot({ detail }) {
  return (
    <div className="report-sections">
      {[
        ["版本与运行环境", detail.provenance],
        ["策略参数", detail.parameters],
        ["撮合与费用假设", detail.assumptions],
      ].map(([title, value]) => (
        <section key={title}>
          <h3>{title}</h3>
          <DataTable value={value} />
        </section>
      ))}
    </div>
  );
}
export function ExperimentRecords({ rows, kind }) {
  const decisions = kind === "decisions";
  const columns = decisions
    ? [
        ["decision_time", "决策时间 · 北京时间"],
        ["split", "区间"],
        ["stage", "阶段"],
        ["status", "状态"],
        ["reason", "决策原因"],
      ]
    : [
        ["timestamp", "成交时间 · 北京时间"],
        ["split", "区间"],
        ["symbol", "交易对"],
        ["side", "买卖"],
        ["direction", "方向"],
        ["quantity", "数量"],
        ["price", "成交价"],
        ["fee", "手续费（U）"],
        ["closed_trade_pnl", "平仓净盈亏（U）"],
      ];
  return (
    <div className="table-wrap report-records">
      <table>
        <thead>
          <tr>
            {columns.map(([k, title]) => (
              <th
                key={k}
                className={
                  ["quantity", "price", "fee", "closed_trade_pnl"].includes(k)
                    ? "numeric"
                    : ""
                }
              >
                {title}
              </th>
            ))}
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <RecordRow
              key={row.id || i}
              row={row}
              columns={columns}
              decisions={decisions}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
function RecordRow({ row, columns, decisions }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr>
        {columns.map(([k]) => (
          <td
            key={k}
            className={`${["quantity", "price", "fee", "closed_trade_pnl"].includes(k) ? "numeric" : ""} ${k === "closed_trade_pnl" && row[k] != null ? (row[k] > 0 ? "positive" : row[k] < 0 ? "negative" : "") : ""}`}
          >
            {cell(
              row[k] ?? (k === "decision_time" ? row.timestamp : undefined),
              k,
            )}
          </td>
        ))}
        <td>
          <button
            className="report-detail-button"
            type="button"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            {open ? "收起详情" : "查看详情"}
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={columns.length + 1}>
            <section className="report-row-detail">
              <h3>{decisions ? "决策输入、指标与风控" : "成交明细"}</h3>
              <DataTable value={row} />
            </section>
          </td>
        </tr>
      )}
    </>
  );
}
