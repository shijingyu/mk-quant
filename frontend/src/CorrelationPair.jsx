import React, { useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatBeijing } from "./time.js";
const fmt = (v, digits = 2) => (v == null ? "—" : Number(v).toFixed(digits));
const short = (s) => s.replace(/USDT$|USDC$/, "");
const frames = [
  ["24h", "24 小时"],
  ["7d", "7 天"],
  ["30d", "30 天"],
  ["90d", "90 天"],
];
const matches = (p, pair) =>
  (p.a === pair.a && p.b === pair.b) || (p.a === pair.b && p.b === pair.a);
const tooltip = {
  background: "var(--panel)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  color: "var(--text)",
};
function Funding({ symbol, value }) {
  return (
    <span className="correlation-funding-item">
      {short(symbol)}：
      <strong
        className={
          value?.rate > 0 ? "positive" : value?.rate < 0 ? "negative" : ""
        }
      >
        {value?.rate == null
          ? "—"
          : `${value.rate > 0 ? "+" : ""}${fmt(value.rate * 100, 4)}%`}
      </strong>
      {value?.interval_hours ? ` / ${Number(Number(value.interval_hours).toFixed(4))}h` : " / 间隔未知"}
      <small>
        {value?.time
          ? `${formatBeijing(value.time)} · 北京时间`
          : "资金费率暂不可用"}
      </small>
    </span>
  );
}
export default function CorrelationPair({
  pair,
  data,
  frame,
  quote,
  onFrame,
  onClose,
  ZValue,
  PairStatus,
}) {
  const [chart, setChart] = useState(null);
  const [error, setError] = useState("");
  const current = data.windows[frame];
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setChart(null);
    setError("");
    const timeout = setTimeout(() => controller.abort(), 15000);
    fetch(
      `/api/tools/correlation/pair?${new URLSearchParams({ a: pair.a, b: pair.b, quote, window: frame })}`,
      { credentials: "include", signal: controller.signal },
    )
      .then(async (response) => {
        if (!response.ok) throw Error("配对图表暂不可用");
        return response.json();
      })
      .then((result) => {
        if (controller.signal.aborted) return;
        if (result.updated_at !== data.updated_at)
          throw Error("行情快照已更新，请点击检查更新后查看");
        setChart(result);
      })
      .catch((e) => {
        if (active)
          setError(
            e.name === "AbortError"
              ? "配对图表请求超时，请重新选择配对"
              : e.message,
          );
      })
      .finally(() => clearTimeout(timeout));
    return () => {
      active = false;
      clearTimeout(timeout);
      controller.abort();
    };
  }, [pair.a, pair.b, quote, frame, data.updated_at]);
  return (
    <>
      <div className="correlation-detail-heading">
        <h3>
          {short(pair.a)} / {short(pair.b)}{" "}
          <small>
            {quote} 永续合约 · {current.label} · {current.interval} K 线
          </small>
        </h3>
        <button className="btn" onClick={onClose} aria-label="关闭配对详情">
          ×
        </button>
      </div>
      <div
        className="correlation-cross-windows"
        aria-label="配对跨周期相关系数"
      >
        {frames.map(([key, label]) => {
          const metric = data.windows[key]?.pairs.find((p) => matches(p, pair));
          return (
            <button
              key={key}
              aria-pressed={frame === key}
              onClick={() => onFrame(key)}
            >
              <strong
                className={
                  metric?.correlation > 0
                    ? "positive"
                    : metric?.correlation < 0
                      ? "negative"
                      : ""
                }
              >
                {fmt(metric?.correlation)}
              </strong>
              <span>{label}</span>
            </button>
          );
        })}
      </div>
      <dl className="correlation-detail-metrics">
        <div>
          <dt>相关系数</dt>
          <dd>{fmt(pair.correlation)}</dd>
          <small>Pearson · 对数收益率</small>
        </div>
        <div>
          <dt>对冲比率 β</dt>
          <dd>{fmt(pair.beta, 4)}</dd>
          <small>
            ln {short(pair.a)} = α + β × ln {short(pair.b)}
          </small>
        </div>
        <div>
          <dt>价差 Z 值</dt>
          <dd>
            <ZValue value={pair.zscore} />
          </dd>
          <PairStatus status={pair.status} />
        </div>
        <div>
          <dt>半衰期</dt>
          <dd>{fmt(pair.half_life, 1)}</dd>
          <small>
            {pair.half_life == null
              ? "未检出有效均值回归"
              : `回归一半所需的 ${current.interval} K 线数`}
          </small>
        </div>
      </dl>
      <div className="correlation-funding">
        <span>最近一次已结算资金费率</span>
        <Funding symbol={pair.a} value={data.funding?.[pair.a]} />
        <Funding symbol={pair.b} value={data.funding?.[pair.b]} />
      </div>
      {error ? (
        <p className="inline-error">{error}</p>
      ) : !chart ? (
        <div className="correlation-chart-loading" role="status">
          正在加载配对曲线…
        </div>
      ) : (
        <div className="correlation-charts">
          <article>
            <h4>标准化价格（起点 = 100）</h4>
            <div className="correlation-chart">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart
                  data={chart.points}
                  margin={{ top: 12, right: 12, left: 0, bottom: 4 }}
                >
                  <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
                  <XAxis
                    dataKey="time"
                    tickFormatter={(v) => formatBeijing(v).slice(5, 10)}
                    tick={{ fill: "var(--muted)", fontSize: 11 }}
                    minTickGap={35}
                  />
                  <YAxis
                    domain={["auto", "auto"]}
                    tickFormatter={(v) => fmt(v, 1)}
                    width={48}
                    tick={{ fill: "var(--muted)", fontSize: 11 }}
                  />
                  <Tooltip
                    contentStyle={tooltip}
                    labelFormatter={(v) => formatBeijing(v)}
                    formatter={(v, n) => [fmt(v), n]}
                  />
                  <Line
                    name={short(pair.a)}
                    dataKey="a"
                    type="linear"
                    stroke="var(--blue)"
                    strokeWidth={2}
                    dot={false}
                    isAnimationActive={false}
                  />
                  <Line
                    name={short(pair.b)}
                    dataKey="b"
                    type="linear"
                    stroke="var(--amber)"
                    strokeWidth={2}
                    dot={false}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <p className="correlation-chart-legend">
              <span style={{ color: "var(--blue)" }}>━ {short(pair.a)}</span>
              <span style={{ color: "var(--amber)" }}>━ {short(pair.b)}</span>
              <span>{chart.samples} 个收益率样本</span>
            </p>
          </article>
          <article>
            <h4>价差 Z 值（±2σ 区间）</h4>
            <div className="correlation-chart">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart
                  data={chart.points}
                  margin={{ top: 12, right: 12, left: 0, bottom: 4 }}
                >
                  <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
                  <XAxis
                    dataKey="time"
                    tickFormatter={(v) => formatBeijing(v).slice(5, 10)}
                    tick={{ fill: "var(--muted)", fontSize: 11 }}
                    minTickGap={35}
                  />
                  <YAxis
                    ticks={[-2, 0, 2]}
                    domain={([lo, hi]) => {
                      const extent = Math.max(
                        2.5,
                        Math.abs(lo || 0),
                        Math.abs(hi || 0),
                      );
                      return [-extent, extent];
                    }}
                    tickFormatter={(v) => fmt(v, 1)}
                    width={48}
                    tick={{ fill: "var(--muted)", fontSize: 11 }}
                  />
                  <Tooltip
                    contentStyle={tooltip}
                    labelFormatter={(v) => formatBeijing(v)}
                    formatter={(v) => [fmt(v), "Z 值"]}
                  />
                  <ReferenceLine
                    y={2}
                    stroke="var(--red)"
                    strokeDasharray="5 4"
                  />
                  <ReferenceLine
                    y={-2}
                    stroke="var(--red)"
                    strokeDasharray="5 4"
                  />
                  <ReferenceLine
                    y={0}
                    stroke="var(--muted)"
                    strokeDasharray="5 4"
                  />
                  <Line
                    name="Z 值"
                    dataKey="z"
                    type="linear"
                    stroke="var(--green)"
                    strokeWidth={2}
                    dot={false}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <p>
              当前 {chart.rolling_bars} 根 {current.interval} K 线滚动相关系数：
              <strong>{fmt(chart.rolling_correlation)}</strong>
            </p>
          </article>
        </div>
      )}
    </>
  );
}
