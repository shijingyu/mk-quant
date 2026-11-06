import CorrelationMatrix from "./CorrelationMatrix.jsx";
import React, { useId, useState } from "react";
import { Calculator, Grid3X3, RotateCcw } from "lucide-react";
import { calculateGrid } from "./gridCalculator.js";
import "./toolbox.css";

const EXAMPLE = {
  lowerPrice: "10.87",
  upperPrice: "24.38",
  densityPercent: "2",
};
const format = (value, digits = 4) =>
  value !== 0 && Math.abs(value) < 10 ** -digits
    ? Math.abs(value) < 1e-8
      ? value.toExponential(3)
      : value.toLocaleString("en-US", { maximumSignificantDigits: 4 })
    : value.toLocaleString("en-US", { maximumFractionDigits: digits });
const money = (value) =>
  value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

function GridResult({ type, result }) {
  const geometric = type === "geometric";
  const grid = result[type];
  return (
    <article
      className="panel grid-result"
      aria-label={geometric ? "等比网格结果" : "等差网格结果"}
    >
      <header className="grid-result-heading">
        <div>
          <h3>{geometric ? "等比网格" : "等差网格"}</h3>
          <p>{geometric ? "固定百分比间距" : "固定价格间距"}</p>
        </div>
        <span className="badge muted">每格 {result.sharesPerGrid} 股</span>
      </header>
      <dl className="grid-result-metrics">
        <div className="grid-count">
          <dt>网格数量</dt>
          <dd>
            {format(grid.gridCount, 0)} <span>格</span>
          </dd>
        </div>
        <div>
          <dt>实际加仓间距</dt>
          <dd className="grid-spacing">
            每跌{" "}
            <strong>
              {geometric
                ? `${format(grid.dropPercent)}%`
                : `${format(grid.spacing, 6)} 元`}
            </strong>
          </dd>
        </div>
      </dl>
      <div className="grid-capital">
        <span>所需资金（逐格合计）</span>
        <strong>
          {money(grid.requiredCapital)} <small>元</small>
        </strong>
        <p>
          {format(grid.gridCount, 0)} 档价格合计 {format(grid.priceSum, 6)} ×{" "}
          {result.sharesPerGrid} 股
        </p>
        <p>各档买入均价约 {format(grid.averageBuyPrice, 6)} 元</p>
      </div>
    </article>
  );
}

export default function Toolbox({ section = "grid" }) {
  const [input, setInput] = useState(EXAMPLE);
  const id = useId();
  const { errors, result } = calculateGrid(input);
  const fields = [
    {
      name: "lowerPrice",
      label: "下限价格（元）",
      hint: "网格覆盖区间的最低价",
    },
    { name: "upperPrice", label: "上限价格（元）", hint: "必须高于下限价格" },
    {
      name: "densityPercent",
      label: "网格密度（%）",
      hint: "例如输入 2，表示目标格距为 2%",
    },
  ];

  return (
    <div className="toolbox-page">
      <div className="page-heading">
        <div>
          <h1>工具箱</h1>
          <p>测算网格参数、资金需求与永续合约相关性。</p>
        </div>
      </div>
      <nav className="toolbox-categories" aria-label="工具栏目">
        <a
          href="#toolbox"
          aria-current={section === "grid" ? "page" : undefined}
        >
          <Calculator size={17} aria-hidden="true" />
          网格计算器
        </a>
        <a
          href="#toolbox/correlation"
          aria-current={section === "correlation" ? "page" : undefined}
        >
          <Grid3X3 size={17} aria-hidden="true" />
          相关性矩阵
        </a>
      </nav>

      {section === "correlation" ? (
        <CorrelationMatrix />
      ) : (
        <>
          <section
            className="panel grid-input-panel"
            aria-labelledby={`${id}-title`}
          >
            <div className="panel-heading">
              <div>
                <h2 id={`${id}-title`}>网格计算器</h2>
                <p>输入价格区间与密度，自动比较等差和等比方案。</p>
              </div>
              <button
                className="btn"
                type="button"
                onClick={() => setInput({ ...EXAMPLE })}
              >
                <RotateCcw size={15} aria-hidden="true" />
                重置示例
              </button>
            </div>
            <div className="panel-body">
              <div className="grid-inputs">
                {fields.map(({ name, label, hint }) => (
                  <div className="field" key={name}>
                    <label htmlFor={`${id}-${name}`}>{label}</label>
                    <input
                      id={`${id}-${name}`}
                      name={name}
                      type="number"
                      inputMode="decimal"
                      min="0"
                      step="any"
                      required
                      value={input[name]}
                      onChange={(event) =>
                        setInput({ ...input, [name]: event.target.value })
                      }
                      aria-invalid={Boolean(errors[name])}
                      aria-describedby={`${id}-${name}-hint`}
                    />
                    <small
                      id={`${id}-${name}-hint`}
                      className={errors[name] ? "grid-field-error" : ""}
                    >
                      {errors[name] || hint}
                    </small>
                  </div>
                ))}
              </div>
              <p className="grid-density-help">
                等差：均价 ×
                密度换算目标价差。等比：密度表示相邻网格的向上涨幅。
                网格数向上取整后重新均分区间，使两端对齐；下方显示实际间距。
              </p>
              {errors.general && (
                <p className="grid-field-error" role="alert">
                  {errors.general}
                </p>
              )}
              <dl className="grid-shared-metrics">
                <div>
                  <dt>估算股价 · 区间均价</dt>
                  <dd>
                    {result ? format(result.midpointPrice, 6) : "—"}{" "}
                    <span>元</span>
                  </dd>
                </div>
                <div>
                  <dt>买入 100 股所需资金</dt>
                  <dd>
                    {result ? money(result.costPerGrid) : "—"} <span>元</span>
                  </dd>
                </div>
              </dl>
            </div>
          </section>

          <div aria-live="polite" aria-atomic="true">
            {result ? (
              <div className="grid-comparison">
                <GridResult type="arithmetic" result={result} />
                <GridResult type="geometric" result={result} />
              </div>
            ) : (
              <div className="panel grid-empty-result">
                请输入有效的价格区间与网格密度后查看计算结果。
              </div>
            )}
          </div>

          <p className="grid-capital-note">
            资金口径：总资金 = 各格买入价格之和 × 100 股。
            从上限向下跨一格开始计入，直至下限，共计与网格数相同的买入档数；不计上限处底仓和手续费。
            “买入 100 股所需资金”仍按区间均价计算。逐格价格按未舍入值求和。
          </p>
          <details className="panel grid-formulas">
            <summary>查看计算公式</summary>
            <div>
              <p>
                设下限为 L，上限为 U，密度为 d（输入 2 即 0.02），均价 M = (U +
                L) ÷ 2。
              </p>
              <p>
                等差：理论格数 = (U − L) ÷ (M × d)；向上取整为 N；实际价差 = (U
                − L) ÷ N。
              </p>
              <p>
                等比：理论格数 = ln(U ÷ L) ÷ ln(1 + d)；向上取整为
                N；相邻价格倍率 r = (U ÷ L)^(1 ÷ N)。
              </p>
              <p>
                等比向上格距 = (r − 1) × 100%；向下加仓跌幅 = (1 − 1 ÷ r) ×
                100%。
              </p>
              <p>
                买入档位 i = 1…N：等差价格 Pᵢ = U − i × 实际价差；等比价格 Pᵢ =
                U ÷ r^i。 上限不计入，下限计入。
              </p>
              <p>
                每格固定买入 100 股，总资金 = (P₁ + P₂ + … + Pₙ) × 100；
                单独的“买入 100 股所需资金” = M × 100。
              </p>
            </div>
          </details>
        </>
      )}
    </div>
  );
}
