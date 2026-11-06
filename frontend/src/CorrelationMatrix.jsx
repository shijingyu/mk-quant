import React, { useEffect, useRef, useState } from "react";
import { formatBeijing } from "./time.js";
import "./correlation.css";
const windows = [
  ["24h", "24 小时", "15m · 96 根"],
  ["7d", "7 天", "1h · 168 根"],
  ["30d", "30 天", "4h · 180 根"],
  ["90d", "90 天", "1d · 90 根"],
];
const num = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
const short = (s) => s.replace(/USDT$/, "");
const statusTone = (status) =>
  ({ 显著偏离: "high", 偏离: "away", 接近均值: "near" })[status] || "unknown";
const PairStatus = ({ status }) => (
  <span className={`correlation-state ${statusTone(status)}`}>{status}</span>
);
const samePair = (pair, a, b) =>
  pair && ((pair.a === a && pair.b === b) || (pair.a === b && pair.b === a));
const ZValue = ({ value }) => {
  const magnitude = value == null ? null : Math.abs(value);
  const tone = magnitude >= 2 ? "high" : magnitude >= 1 ? "away" : "neutral";
  return (
    <span
      className={`correlation-z ${tone}`}
      title={
        value == null
          ? "价差数据不足"
          : "按绝对值分色：|Z| ≥ 2 红色；1 ≤ |Z| < 2 琥珀色；|Z| < 1 中性"
      }
    >
      {value > 0 ? "+" : ""}
      {num(value)}
    </span>
  );
};
export default function CorrelationMatrix() {
  const [data, setData] = useState(null),
    [error, setError] = useState(""),
    [window, setWindow] = useState("7d"),
    [ten, setTen] = useState(false),
    [pairSelection, setPair] = useState(null),
    [revision, setRevision] = useState(0);
  const detailRef = useRef(null);
  useEffect(() => {
    if (pairSelection?.source === "ranking")
      detailRef.current?.scrollIntoView({
        block: "nearest",
        behavior: "instant",
      });
  }, [pairSelection]);
  useEffect(() => {
    let active = true,
      timer,
      controller;
    const update = async () => {
      controller = new AbortController();
      let timeout = setTimeout(() => controller.abort(), 15000);
      try {
        const r = await fetch("/api/tools/correlation", {
          credentials: "include",
          signal: controller.signal,
        });
        if (!r.ok) throw Error("相关性服务暂不可用");
        const next = await r.json();
        if (active) {
          setData(next);
          setError("");
          timer = setTimeout(update, next.refreshing ? 3000 : 60000);
        }
      } catch (e) {
        if (active) {
          setError(e.name === "AbortError" ? "请求超时，请重试" : e.message);
          timer = setTimeout(update, 30000);
        }
      } finally {
        clearTimeout(timeout);
      }
    };
    update();
    return () => {
      active = false;
      clearTimeout(timer);
      controller?.abort();
    };
  }, [revision]);
  const selected = data?.windows?.[window],
    seven = data?.windows?.["7d"],
    symbols = (selected?.symbols || []).slice(0, ten ? 10 : 20);
  const pair =
    selected?.pairs.find((p) => samePair(pairSelection, p.a, p.b)) || null;
  const choose = (a, b) => setPair({ a, b });
  const chooseRanking = (p) => {
    setWindow("7d");
    if (ten && ![p.a, p.b].every((s) => seven.symbols.slice(0, 10).includes(s)))
      setTen(false);
    setPair({ a: p.a, b: p.b, source: "ranking" });
  };
  return (
    <section id="correlation-matrix" className="panel correlation-panel">
      <header className="panel-heading">
        <div>
          <h2>实时相关性矩阵</h2>
          <p>币安 USDT 本位永续合约 · 20 个品种 · 公开行情，每 15 分钟更新</p>
        </div>
        <button
          className="btn"
          disabled={data?.refreshing}
          onClick={() => setRevision((v) => v + 1)}
        >
          {data?.refreshing ? "正在更新…" : "检查更新"}
        </button>
      </header>
      <div className="panel-body">
        <div className="correlation-status" role="status">
          {data?.updated_at
            ? `更新于 ${formatBeijing(data.updated_at, { second: "2-digit" })} · 北京时间`
            : "正在获取公开行情，首次计算约需一分钟"}
          <span className={data?.stale || error ? "negative" : "positive"}>
            {data?.updated_at
              ? data.stale || error
                ? " · 数据已过期"
                : " · 数据有效"
              : ""}
          </span>
        </div>
        {(error || data?.error) && (
          <p className="inline-error">
            {error || data.error}；保留的旧结果不代表当前行情。
          </p>
        )}
        <div className="correlation-toolbar">
          <div className="segmented" role="group" aria-label="相关性时间窗口">
            {windows.map(([key, label, note]) => (
              <button
                key={key}
                aria-pressed={window === key}
                className={window === key ? "selected" : ""}
                onClick={() => {
                  setWindow(key);
                  setPair(null);
                }}
              >
                {label}
                <small>{note}</small>
              </button>
            ))}
          </div>
          <label>
            <input
              type="checkbox"
              checked={ten}
              onChange={(e) => setTen(e.target.checked)}
            />
            仅显示前 10 个
          </label>
        </div>
        <p className="dim">
          对齐已收盘 K 线的对数收益率，计算 Pearson
          相关系数。点击色块查看配对统计；缺失数据不补零。
        </p>
        {selected ? (
          <>
            <div className="table-wrap correlation-heatmap">
              <table style={{ minWidth: `${symbols.length * 50 + 60}px` }}>
                <thead>
                  <tr>
                    <th>{selected.label}</th>
                    {symbols.map((s) => (
                      <th scope="col" key={s}>
                        {short(s)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {symbols.map((a, i) => (
                    <tr key={a}>
                      <th scope="row">{short(a)}</th>
                      {symbols.map((b, j) => {
                        const v = selected.matrix[i][j];
                        const isPair = samePair(pair, a, b);
                        return (
                          <td key={b}>
                            <button
                              disabled={i === j || v == null}
                              className={
                                i === j
                                  ? "correlation-self"
                                  : isPair
                                    ? "correlation-selected"
                                    : ""
                              }
                              aria-pressed={
                                i === j ? undefined : Boolean(isPair)
                              }
                              title={
                                i === j
                                  ? "同一币种：自身相关系数恒为 1.00"
                                  : isPair
                                    ? "已选配对"
                                    : "查看配对统计"
                              }
                              aria-label={`${a} / ${b} 相关系数 ${num(v)}`}
                              style={{
                                "--cell-background":
                                  v == null
                                    ? "var(--panel-alt)"
                                    : `color-mix(in srgb, ${v >= 0 ? "var(--green)" : "var(--red)"} ${Math.abs(v) * 38}%, var(--panel))`,
                              }}
                              onClick={() => choose(a, b)}
                            >
                              {num(v)}
                            </button>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="correlation-key">
              <span>
                <i className="self" />
                对角线：自身相关 1.00
              </span>
              <span>
                <i className="selected" />
                {pair
                  ? `已选：${short(pair.a)} / ${short(pair.b)}`
                  : "蓝色边框：已选配对"}
              </span>
            </div>
            <div className="correlation-legend">
              <span>−1 负相关</span>
              <span>0 无线性相关</span>
              <span>+1 正相关</span>
            </div>
            <p className="dim">
              {selected.interval} K 线 {selected.bars} 根 · {selected.returns}{" "}
              个收益率样本 · {formatBeijing(selected.start)} 至{" "}
              {formatBeijing(selected.end)}（结束不含，北京时间）
            </p>
            {Object.keys(selected.missing).length > 0 && (
              <p className="inline-error">
                {Object.entries(selected.missing)
                  .map(([s, r]) => `${s}：${r}`)
                  .join("；")}
              </p>
            )}
          </>
        ) : (
          <div className="correlation-empty">
            {error || data?.error
              ? "行情暂不可用，等待重试。"
              : "正在计算四个时间窗口…"}
          </div>
        )}
        {pair && (
          <section
            className="correlation-pair"
            ref={detailRef}
            aria-label="已选配对详情"
          >
            <h3>
              {pair.a} / {pair.b} · {selected.label}
            </h3>
            <dl>
              {[
                ["相关系数", num(pair.correlation)],
                ["对冲比率 β", num(pair.beta, 4)],
                ["价差 Z 值", <ZValue value={pair.zscore} />],
                [
                  "半衰期",
                  pair.half_life == null
                    ? "未检出有效均值回归"
                    : num(pair.half_life, 1) + " 根 K 线",
                ],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
            <p>
              回归方向：ln({pair.a}) = α + β × ln({pair.b}) + 残差。
              <PairStatus status={pair.status} />
            </p>
          </section>
        )}
        <h3 className="correlation-ranking-title">相关性最高的配对 — 7 天</h3>
        <p className="dim">
          固定使用 7 天、1h K 线，按正相关系数降序。点击行可切换至 7
          天矩阵并查看配对详情。
        </p>
        <div className="table-wrap">
          <table className="correlation-ranking">
            <thead>
              <tr>
                {[
                  "排名",
                  "配对",
                  "相关系数",
                  "对冲比率 β",
                  "价差 Z 值",
                  "半衰期（1h K 线）",
                  "状态",
                ].map((t) => (
                  <th key={t}>{t}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {seven?.pairs
                .filter((p) => p.correlation != null && p.correlation > 0)
                .slice(0, 12)
                .map((p, i) => (
                  <tr
                    key={p.a + p.b}
                    className={
                      window === "7d" && samePair(pair, p.a, p.b)
                        ? "correlation-row-selected"
                        : ""
                    }
                    onClick={() => chooseRanking(p)}
                  >
                    <td>{i + 1}</td>
                    <td>
                      <button
                        type="button"
                        className="correlation-pair-button"
                        aria-pressed={
                          window === "7d" && Boolean(samePair(pair, p.a, p.b))
                        }
                        onClick={(event) => {
                          event.stopPropagation();
                          chooseRanking(p);
                        }}
                      >
                        {short(p.a)} / {short(p.b)}
                        {window === "7d" && samePair(pair, p.a, p.b) && (
                          <span className="correlation-row-marker">已选</span>
                        )}
                      </button>
                    </td>
                    <td className="positive">{num(p.correlation)}</td>
                    <td>{num(p.beta, 4)}</td>
                    <td>
                      <ZValue value={p.zscore} />
                    </td>
                    <td
                      title={
                        p.half_life == null
                          ? "AR(1) 系数不在 (0,1) 内或价差方差不足"
                          : undefined
                      }
                    >
                      {num(p.half_life, 1)}
                    </td>
                    <td>
                      <PairStatus status={p.status} />
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
        <details className="correlation-method">
          <summary>算法与数据口径</summary>
          <p>
            收益率 rₜ = ln(Cₜ / Cₜ₋₁)。对数价格 OLS
            含截距；价差残差使用窗口内总体标准差计算 Z 值。半衰期由残差的 AR(1)
            回归估计：−ln(2) / ln(φ)，仅在 0 &lt; φ &lt; 1 时显示。|Z| ≥ 2
            为显著偏离，1 ≤ |Z| &lt; 2
            为偏离，其余接近均值。零方差、缺口或历史不足时显示“—”。相关性不等于协整，本工具未进行
            ADF 检验。
          </p>
          <p>
            窗口与指标参考{" "}
            <a
              href="https://bjftradinggroup.cn/配对交易/"
              target="_blank"
              rel="noreferrer"
            >
              BJF 配对交易页面
            </a>
            。行情来自币安 /fapi/v1/klines，时间按 UTC
            对齐、北京时间显示。1000SHIB 为合约名称。
          </p>
        </details>
      </div>
    </section>
  );
}
