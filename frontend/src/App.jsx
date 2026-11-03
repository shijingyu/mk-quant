import React, { useEffect, useState, useMemo, useRef, useId } from "react";
import { createPortal } from "react-dom";
import {
  formatBeijing,
  beijingInputToISO,
  toBeijingInput,
  displayBeijingTimes,
} from "./time.js";
import {
  Activity,
  ArrowDownLeft,
  ArrowDownToLine,
  ArrowRight,
  ArrowUpRight,
  Bell,
  Blocks,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Clock3,
  ClipboardList,
  Code2,
  Coins,
  Copy,
  Database,
  Download,
  Ellipsis,
  ExternalLink,
  FileCode2,
  FlaskConical,
  Gauge,
  GitCompareArrows,
  History,
  LayoutDashboard,
  ListFilter,
  Loader2,
  LockKeyhole,
  LogOut,
  Menu,
  Moon,
  MoreHorizontal,
  Pause,
  Play,
  Plus,
  Radio,
  RefreshCw,
  Search,
  Settings2,
  Shield,
  ShieldAlert,
  SlidersHorizontal,
  Square,
  Sun,
  Terminal,
  TrendingDown,
  TrendingUp,
  Upload,
  Wallet,
  X,
  XCircle,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  ComposedChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const FMT = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const money = (n) =>
  n == null || !Number.isFinite(Number(n)) ? "—" : FMT.format(Number(n));
const strategyManifest = (strategy) => strategy?.manifest || strategy || {};
const isMakerStrategy = (strategy) =>
  strategyManifest(strategy).execution?.model === "maker_grid_v1";
const strategySymbols = (strategy, fallback = ["BTCUSDT"]) => {
  const values = strategyManifest(strategy).symbols?.values;
  return Array.isArray(values) && values.length ? values : fallback;
};
const tradeNumber = (value) =>
  value == null || !Number.isFinite(Number(value))
    ? "—"
    : Number(value).toLocaleString("en-US", { maximumFractionDigits: 12 });
function MakerNotice({ compact = false }) {
  return (
    <div className={`notice amber maker-notice ${compact ? "compact" : ""}`}>
      <ShieldAlert size={16} />
      <span>
        Maker-only：止损触发后可能被拒或持续未成交，不会使用 Taker
        后备平仓。OHLC 回测与模拟不保证实盘成交。
      </span>
    </div>
  );
}
const pct = (n) =>
  n == null || !Number.isFinite(Number(n))
    ? "—"
    : `${Number(n) > 0 ? "+" : ""}${Number(n).toFixed(2)}%`;
const valueTone = (value) =>
  value == null || !Number.isFinite(Number(value)) || Number(value) === 0
    ? ""
    : Number(value) > 0
      ? "positive"
      : "negative";
const sum = (rows, key) =>
  rows.length && rows.some((r) => r[key] == null)
    ? null
    : rows.reduce((s, r) => s + (Number(r[key]) || 0), 0);
const accountProfit = (a) =>
  a.realized_pnl == null || a.unrealized_pnl == null
    ? null
    : Number(a.realized_pnl) + Number(a.unrealized_pnl);
function aggregateCurves(accounts) {
  const eligible = accounts.filter((a) => a.curve?.length);
  if (!eligible.length) return [];
  const points = [
    ...new Set(eligible.flatMap((a) => a.curve.map((p) => p.timestamp))),
  ].sort((a, b) => a - b);
  const latest = new Map();
  const indexes = new Map(eligible.map((a) => [a.id, 0]));
  return points.map((timestamp) => {
    for (const a of eligible) {
      let index = indexes.get(a.id);
      while (index < a.curve.length && a.curve[index].timestamp <= timestamp) {
        latest.set(a.id, a.curve[index].equity);
        index++;
      }
      indexes.set(a.id, index);
    }
    return {
      timestamp,
      equity: eligible.reduce(
        (total, a) => total + (latest.get(a.id) ?? a.initial_equity),
        0,
      ),
    };
  });
}
const labelStatus = {
  unknown: "状态未知",
  starting: "启动中",
  running: "运行中",
  paused: "已停止开仓",
  stopped: "已停止",
  pending: "等待执行",
  queued: "排队中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  idle: "待启动",
  blocked: "接入未通过",
  stale: "状态过期",
  stopping: "停止中",
  flattening: "正在平仓",
};
const stateClass = (s) =>
  ["running", "completed", "ok", "passed", "pass"].includes(s)
    ? "green"
    : ["failed", "error", "blocked", "fail"].includes(s)
      ? "red"
      : ["pending", "queued", "stopping", "flattening", "stale"].includes(s)
        ? "amber"
        : "muted";
const navs = [
  { group: "总览", items: [["overview", "总览", LayoutDashboard]] },
  {
    group: "研究",
    items: [
      ["datasets", "行情数据", Database],
      ["strategies", "策略管理", Blocks],
      ["backtest", "回测工作台", FlaskConical],
      ["experiments", "实验记录", GitCompareArrows],
    ],
  },
  {
    group: "交易运行",
    items: [
      ["paper", "模拟交易", Wallet],
      ["live", "实盘交易", Radio],
    ],
  },
  {
    group: "系统",
    items: [
      ["logs", "运行日志", Terminal],
      ["protocol", "策略接入协议", BookOpen],
      ["todo", "to-do", ClipboardList],
    ],
  },
];
const titles = Object.fromEntries(
  navs.flatMap((g) => g.items.map(([id, label]) => [id, label])),
);
let csrfToken = "";
function normalizeMetrics(m = {}) {
  return {
    ...m,
    total_return:
      m.total_return ??
      (m.total_return_pct != null ? m.total_return_pct / 100 : null),
    annual_return:
      m.annual_return ??
      (m.annualized_return_pct != null ? m.annualized_return_pct / 100 : null),
    max_drawdown:
      m.max_drawdown ??
      (m.max_drawdown_pct != null ? m.max_drawdown_pct / 100 : null),
    win_rate: m.win_rate != null ? m.win_rate / 100 : null,
    total_orders: m.total_orders ?? m.total_trades,
    long_orders: m.long_orders ?? m.long_trades,
    short_orders: m.short_orders ?? m.short_trades,
    realized_pnl: m.realized_pnl ?? m.net_profit,
  };
}
function normalizeExperiment(e) {
  return {
    ...e,
    progress: (e.progress || 0) / 100,
    metrics: normalizeMetrics(e.metrics),
    validation_metrics: normalizeMetrics(e.validation_metrics),
  };
}
async function api(path, body, method) {
  const res = await fetch(`/api${path}`, {
    method: method || (body ? "POST" : "GET"),
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-MK-CSRF": csrfToken },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!res.ok) {
    let data;
    try {
      data = await res.json();
    } catch {
      data = { detail: `服务响应 ${res.status}` };
    }
    const error = new Error(
      typeof data.detail === "string"
        ? data.detail
        : data.detail?.message || JSON.stringify(data.detail || data),
    );
    error.status = res.status;
    error.details = data.detail;
    throw error;
  }
  if (res.status === 204) return {};
  const data = await res.json();
  if (data.csrf_token) csrfToken = data.csrf_token;
  if (path === "/bootstrap") {
    data.experiments = (data.experiments || []).map(normalizeExperiment);
  } else if (/^\/experiments\/[^/]+$/.test(path) && !body)
    return normalizeExperiment(data);
  return data;
}
function exportJson(name, data) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 500);
}
function Badge({ status, children }) {
  return (
    <span className={`badge ${stateClass(status)}`}>
      <i />
      {children || labelStatus[status] || status}
    </span>
  );
}
function Btn({ children, icon: Icon, kind = "", className = "", ...props }) {
  return (
    <button className={`btn ${kind} ${className}`} {...props}>
      {Icon && <Icon size={15} />} {children}
    </button>
  );
}
function Heading({ eyebrow, title, desc, children }) {
  return (
    <div className="page-heading">
      <div>
        <h1>{title}</h1>
        {desc && <p>{desc}</p>}
      </div>
      <div className="heading-actions">{children}</div>
    </div>
  );
}
function Empty({
  icon: Icon = Database,
  title = "暂无数据",
  desc = "数据生成后会显示在这里。",
  children,
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <Icon size={27} />
      </div>
      <h3>{title}</h3>
      <p>{desc}</p>
      {children}
    </div>
  );
}
function Field({ label, hint, children }) {
  const required = React.Children.toArray(children).some(
    (child) => child?.props?.required,
  );
  return (
    <label className="field">
      <span>
        {label}
        {required && (
          <span className="required-mark" aria-label="必填">
            {" "}
            *
          </span>
        )}
      </span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
function Modal({ title, subtitle, onClose, children, wide = false }) {
  useEffect(() => {
    const old = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const handler = (e) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab") {
        const items = [
          ...document.querySelectorAll(
            ".modal button,.modal input,.modal select,.modal textarea",
          ),
        ].filter((n) => !n.disabled && n.getClientRects().length > 0);
        const first = items[0],
          last = items.at(-1);
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", handler);
    document.querySelector(".modal button")?.focus();
    return () => {
      document.removeEventListener("keydown", handler);
      document.body.style.overflow = previousOverflow;
      old?.focus();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`modal ${wide ? "wide" : ""}`}
      >
        <div className="modal-heading">
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button className="icon-btn" aria-label="关闭" onClick={onClose}>
            <X size={20} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}
function Chart({ data = [], height = 270, comparison = false }) {
  const chartRef = useRef(null),
    gradientId = useId().replaceAll(":", "");
  const normalized = data.map((p, i) => ({
    ...p,
    label:
      p.label ||
      (p.timestamp
        ? new Date(
            Number(p.timestamp) < 1e12
              ? Number(p.timestamp) * 1000
              : Number(p.timestamp),
          ).toLocaleDateString("zh-CN", {
            timeZone: "Asia/Shanghai",
            month: "2-digit",
            day: "2-digit",
          })
        : String(i + 1)),
    equity: p.equity ?? p.value,
  }));
  if (!normalized.length)
    return <Empty title="等待权益数据" desc="曲线来自已持久化的账本快照。" />;
  return (
    <div className="chart" style={{ height }} ref={chartRef}>
      <button
        className="chart-download icon-btn"
        aria-label="导出曲线 SVG"
        title="导出曲线 SVG"
        onClick={() => exportCurveSvg(chartRef.current)}
      >
        <Download size={13} />
      </button>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart
          data={normalized}
          margin={{ top: 12, right: 8, left: 0, bottom: 0 }}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--green)" stopOpacity={0.19} />
              <stop offset="100%" stopColor="var(--green)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid
            stroke="var(--border)"
            strokeDasharray="3 5"
            vertical={false}
          />
          <XAxis
            dataKey="label"
            axisLine={false}
            tickLine={false}
            tick={{ fill: "var(--muted)", fontSize: 12 }}
            minTickGap={45}
            dy={10}
          />
          <YAxis
            axisLine={false}
            tickLine={false}
            domain={["auto", "auto"]}
            tick={{ fill: "var(--muted)", fontSize: 12 }}
            tickFormatter={(n) =>
              Number(n).toLocaleString("en-US", { maximumFractionDigits: 2 })
            }
            width={78}
          />
          <Tooltip
            labelFormatter={(label, payload) =>
              payload?.[0]?.payload?.timestamp
                ? `${formatBeijing(payload[0].payload.timestamp)} 北京时间`
                : label
            }
            contentStyle={{
              background: "var(--panel)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              fontSize: 13,
              color: "var(--text)",
            }}
            formatter={(v, n) => [
              `${money(v)} U`,
              n === "equity" ? "账户权益" : "验证集权益",
            ]}
            labelStyle={{ color: "var(--muted)" }}
          />
          <Area
            type="linear"
            dataKey="equity"
            stroke="var(--green)"
            strokeWidth={2}
            fill={`url(#${gradientId})`}
            isAnimationActive={false}
          />
          {comparison && (
            <Line
              type="linear"
              dataKey="validation"
              stroke="var(--blue)"
              dot={false}
              strokeWidth={2}
              isAnimationActive={false}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
function MiniChart({ data = [], color = "var(--green)" }) {
  if (!data.length) return <span className="dim">—</span>;
  const vals = data.map((x) => x.equity ?? x),
    min = Math.min(...vals),
    range = Math.max(...vals) - min || 1;
  return (
    <svg className="sparkline" viewBox="0 0 104 30" aria-label="权益走势">
      <polyline
        fill="none"
        stroke={color}
        strokeWidth="1.7"
        points={vals
          .map(
            (n, i) =>
              `${(i / (vals.length - 1 || 1)) * 102 + 1},${28 - ((n - min) / range) * 25}`,
          )
          .join(" ")}
      />
    </svg>
  );
}
function Stat({
  label,
  value,
  unit = "U",
  sub,
  trend,
  icon: Icon,
  accent = false,
}) {
  return (
    <div className={`stat-card ${accent ? "accent" : ""}`}>
      <div className="stat-label">
        {label}
        <Icon size={16} />
      </div>
      <div className="stat-number">
        {value}
        {value != null && value !== "—" && <small>{unit}</small>}
      </div>
      <div
        className={`stat-sub ${trend === "up" ? "positive" : trend === "down" ? "negative" : ""}`}
      >
        {trend === "up" ? (
          <TrendingUp size={13} />
        ) : trend === "down" ? (
          <TrendingDown size={13} />
        ) : null}
        {sub}
      </div>
    </div>
  );
}

export default function App() {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("mk-quant-theme") === "dark"
        ? "dark"
        : "light";
    } catch {
      return "light";
    }
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("mk-quant-theme", theme);
    } catch {}
  }, [theme]);
  const toggleTheme = () =>
    setTheme((value) => (value === "dark" ? "light" : "dark"));
  const [data, setData] = useState(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [page, setPage] = useState(() =>
      location.hash.slice(1) === "users"
        ? "paper"
        : location.hash.slice(1) || "overview",
    ),
    [toast, setToast] = useState(""),
    [modal, setModal] = useState(null),
    [side, setSide] = useState(false),
    [search, setSearch] = useState(""),
    [updated, setUpdated] = useState(null),
    [client, setClient] = useState(null),
    [downloadSubmitting, setDownloadSubmitting] = useState(false);
  const downloadRequest = useRef(null);
  const refreshSequence = useRef(0);
  const notify = (m) => {
    setToast(m);
    setTimeout(() => setToast(""), 4500);
  };
  const refresh = async () => {
    const sequence = ++refreshSequence.current;
    try {
      const d = await api("/bootstrap");
      if (sequence !== refreshSequence.current) return d;
      setData(d);
      setError("");
      setUpdated(new Date());
      return d;
    } catch (e) {
      if (sequence !== refreshSequence.current) return;
      if (e.message === "请先登录") {
        setData(null);
        setError("");
      } else {
        setError(`数据刷新失败 · ${e.message}`);
        setData((previous) =>
          previous
            ? {
                ...previous,
                accounts: (previous.accounts || []).map((a) => ({
                  ...a,
                  stale: true,
                  status: [
                    "running",
                    "paused",
                    "starting",
                    "flattening",
                  ].includes(a.status)
                    ? "unknown"
                    : a.status,
                })),
              }
            : previous,
        );
      }
    } finally {
      if (sequence === refreshSequence.current) setLoading(false);
    }
  };
  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 10000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    const fn = () => {
      const next = location.hash.slice(1) || "overview";
      if (next === "users") {
        location.replace("#paper");
        return;
      }
      setPage(next);
    };
    addEventListener("hashchange", fn);
    fn();
    return () => removeEventListener("hashchange", fn);
  }, []);
  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  }, [page]);
  const go = (p) => {
    location.hash = p;
    setPage(p);
    setSide(false);
    setSearch("");
  };
  const mutate = async (path, body, method) => {
    try {
      const r = await api(path, body, method);
      await refresh();
      return r;
    } catch (e) {
      notify(e.message);
      throw e;
    }
  };
  const startDownload = async (config) => {
    if (downloadRequest.current?.pending) return;
    const fingerprint = JSON.stringify(config);
    let attempt;
    try {
      attempt = JSON.parse(
        sessionStorage.getItem("mk-download-attempt") || "null",
      );
    } catch {}
    if (!attempt || attempt.fingerprint !== fingerprint)
      attempt = { fingerprint, key: crypto.randomUUID() };
    try {
      sessionStorage.setItem("mk-download-attempt", JSON.stringify(attempt));
    } catch {}
    downloadRequest.current = { ...attempt, pending: true };
    setDownloadSubmitting(true);
    const remember = (job) =>
      setData((previous) =>
        previous
          ? {
              ...previous,
              download_jobs: [
                job,
                ...(previous.download_jobs || []).filter(
                  (item) => item.id !== job.id,
                ),
              ],
            }
          : previous,
      );
    try {
      const job = await api("/datasets/download", {
        ...config,
        request_key: attempt.key,
      });
      remember(job);
      try {
        sessionStorage.removeItem("mk-download-attempt");
      } catch {}
      notify(
        job.status === "completed"
          ? "该下载任务已完成，数据集已保存"
          : "下载任务已提交，切换页面或关闭网页后仍会继续",
      );
      await refresh();
      return job;
    } catch (error) {
      if (error.details?.job) remember(error.details.job);
      if (error.status && error.status < 500) {
        try {
          sessionStorage.removeItem("mk-download-attempt");
        } catch {}
      }
      notify(error.message);
      await refresh();
      throw error;
    } finally {
      downloadRequest.current = null;
      setDownloadSubmitting(false);
    }
  };
  const accounts = data?.accounts || [],
    strategies = data?.strategies || [],
    experiments = data?.experiments || [];
  if (loading)
    return (
      <div className="initial-loading">
        <div className="brand-mark">
          M<span>K</span>
        </div>
        <Loader2 className="spin" />
        <p>正在连接 MK Quant 控制服务</p>
      </div>
    );
  if (!data?.current_user)
    return (
      <Login
        error={error}
        onLogin={refresh}
        theme={theme}
        toggleTheme={toggleTheme}
      />
    );
  const isClient =
    data.current_user.role !== "admin" ||
    location.pathname.startsWith("/client/");
  return (
    <div className="app-shell">
      <aside className={`sidebar ${side ? "open" : ""}`}>
        <a href="#overview" className="brand" onClick={() => go("overview")}>
          <div className="brand-mark">
            M<span>K</span>
          </div>
          <div>
            MK <b>Quant</b>
            <small>量化策略工作台</small>
          </div>
        </a>
        <nav>
          {(isClient
            ? [
                {
                  group: "我的账户",
                  items: [["overview", "账户总览", LayoutDashboard]],
                },
              ]
            : navs
          ).map((g) => (
            <div className="nav-group" key={g.group}>
              <div className="nav-group-title">{g.group}</div>
              {g.items.map(([id, label, Icon]) => (
                <button
                  key={id}
                  className={`nav-item ${page === id ? "active" : ""}`}
                  onClick={() => go(id)}
                >
                  <Icon size={18} />
                  <span>{label}</span>
                  {id === "experiments" && experiments.length > 0 && (
                    <small>{experiments.length}</small>
                  )}
                  {id === "live" && <span className="nav-dot" />}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="system-health">
            <span className="status-dot" />
            <div>
              <strong>{error ? "控制服务连接异常" : "控制服务已连接"}</strong>
              <small>执行状态以运行端回执为准</small>
            </div>
          </div>
        </div>
      </aside>
      {side && <div className="sidebar-scrim" onClick={() => setSide(false)} />}
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-btn mobile-menu"
              aria-label="展开菜单"
              onClick={() => setSide(!side)}
            >
              <Menu size={20} />
            </button>
            <span>工作空间</span>
            <ChevronRight size={13} />
            <strong>{isClient ? "账户总览" : titles[page] || "总览"}</strong>
          </div>
          <div className="topbar-right">
            {data.demo && (
              <span className="environment">
                <span />
                演示环境
              </span>
            )}
            <ThemeToggle theme={theme} onToggle={toggleTheme} />
            <button
              className="top-profile"
              onClick={() => setModal({ type: "profile" })}
              aria-label="查看当前账户"
            >
              <span className="top-avatar">{isClient ? "U" : "A"}</span>
              <span>
                <strong>
                  {data.current_user.name || data.current_user.username}
                </strong>
                <small>{isClient ? "客户" : "管理员"}</small>
              </span>
            </button>
          </div>
        </header>
        <main>
          {error && (
            <div className="notice error">
              <ShieldAlert size={16} />
              {error}
              <Btn icon={RefreshCw} onClick={refresh}>
                重试
              </Btn>
            </div>
          )}
          {isClient ? (
            <ClientView
              account={
                location.pathname.startsWith("/client/")
                  ? accounts.find(
                      (a) => location.pathname.split("/")[2] === a.route,
                    )
                  : accounts[0]
              }
              user={data.current_user}
            />
          ) : (
            <>
              {page === "overview" && (
                <Overview
                  data={data}
                  go={go}
                  setModal={setModal}
                  updated={updated}
                  refresh={refresh}
                />
              )}
              {page === "datasets" && (
                <Datasets
                  data={data}
                  mutate={mutate}
                  notify={notify}
                  startDownload={startDownload}
                  downloadSubmitting={downloadSubmitting}
                />
              )}
              {page === "strategies" && (
                <Strategies
                  strategies={strategies}
                  mutate={mutate}
                  notify={notify}
                  go={go}
                />
              )}
              {page === "backtest" && (
                <Backtest data={data} mutate={mutate} notify={notify} go={go} />
              )}
              {page === "experiments" && (
                <Experiments
                  data={data}
                  mutate={mutate}
                  notify={notify}
                  go={go}
                />
              )}
              {["paper", "live"].includes(page) && (
                <Accounts
                  page={page}
                  commands={data.commands || []}
                  accounts={accounts}
                  strategies={strategies}
                  setModal={setModal}
                  mutate={mutate}
                  notify={notify}
                />
              )}
              {page === "logs" && (
                <Logs logs={data.logs || []} strategies={strategies} />
              )}
              {page === "protocol" && <Protocol notify={notify} />}
              {page === "todo" && <Todo />}
            </>
          )}
          <footer>
            <span>MK Quant</span>
            <span>北京时间 UTC+8 · 数据与执行状态以最新回执为准</span>
          </footer>
        </main>
      </div>
      {toast && (
        <div className="toast" role="status">
          <CheckCircle2 size={17} />
          {toast}
          <button aria-label="关闭通知" onClick={() => setToast("")}>
            <X size={14} />
          </button>
        </div>
      )}
      {modal?.type === "profile" && (
        <Modal title="当前账户" onClose={() => setModal(null)}>
          <p>
            {data.current_user.name || data.current_user.username} ·{" "}
            {data.current_user.role}
          </p>
          <div className="modal-footer">
            <Btn
              icon={LogOut}
              onClick={async () => {
                await api("/logout", {});
                setData(null);
                setModal(null);
              }}
            >
              退出登录
            </Btn>
          </div>
        </Modal>
      )}
      {modal?.type === "account" && (
        <AccountForm
          account={modal.account}
          defaultMode={modal.defaultMode}
          strategies={strategies}
          mutate={mutate}
          notify={notify}
          onClose={() => setModal(null)}
        />
      )}
      {modal?.type === "command" && (
        <CommandModal
          {...modal}
          mutate={mutate}
          notify={notify}
          onClose={() => setModal(null)}
        />
      )}
      {modal?.type === "check" && (
        <AccountCheck
          account={modal.account}
          mutate={mutate}
          notify={notify}
          onClose={() => setModal(null)}
        />
      )}
      {modal?.type === "client" && (
        <Modal
          wide
          title={`${modal.account.name} · 客户端预览`}
          subtitle="与客户页使用相同的账户数据"
          onClose={() => setModal(null)}
        >
          <div className="client-preview-actions">
            <Btn
              icon={Copy}
              disabled={!modal.account.route}
              onClick={async () => {
                try {
                  await copyText(
                    new URL(
                      `/client/${encodeURIComponent(modal.account.route)}`,
                      window.location.origin,
                    ).href,
                  );
                  notify("客户链接已复制");
                } catch {
                  notify("复制失败，请检查浏览器剪贴板权限");
                }
              }}
            >
              复制客户链接
            </Btn>
          </div>
          <ClientView
            account={
              data.accounts.find(
                (account) => account.id === modal.account.id,
              ) || {
                ...modal.account,
                stale: true,
                status: "unknown",
              }
            }
          />
        </Modal>
      )}
    </div>
  );
}

function ThemeToggle({ theme, onToggle }) {
  const isDark = theme === "dark";
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={onToggle}
      aria-label={isDark ? "切换日间皮肤" : "切换夜间皮肤"}
      title={isDark ? "切换日间皮肤" : "切换夜间皮肤"}
    >
      {isDark ? <Sun size={17} /> : <Moon size={17} />}
      <span>{isDark ? "日间" : "夜间"}</span>
    </button>
  );
}

async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const field = document.createElement("textarea");
  field.value = value;
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.appendChild(field);
  field.select();
  try {
    if (!document.execCommand("copy")) throw new Error("Clipboard unavailable");
  } finally {
    field.remove();
  }
}

function Login({ onLogin, error, theme, toggleTheme }) {
  const [username, setUsername] = useState(""),
    [password, setPassword] = useState(""),
    [err, setErr] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <div className="login-page">
      <div className="login-theme">
        <ThemeToggle theme={theme} onToggle={toggleTheme} />
      </div>
      <div className="login-decoration">
        <div className="eyebrow">RESEARCH. VALIDATE. EXECUTE.</div>
        <h1>
          让策略回归逻辑。
          <br />
          <span>让框架处理其余。</span>
        </h1>
        <p>
          从一个交易想法，到一套可复现的实验。
          <br />
          在统一的工作空间，连接研究与执行。
        </p>
        <div className="login-lines">
          {Array.from({ length: 25 }, (_, i) => (
            <i
              key={i}
              style={{ height: 40 + Math.sin(i * 0.6) * 20 + i * 7 }}
            />
          ))}
        </div>
        <div className="login-caption">
          MK QUANT <span>策略基础设施 / 01</span>
        </div>
      </div>
      <form
        className="login-form"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            await api("/login", { username, password });
            await onLogin();
          } catch (e) {
            setErr(e.message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <a className="brand">
          <div className="brand-mark">
            M<span>K</span>
          </div>
          <div>
            MK <b>Quant</b>
          </div>
        </a>
        <h2>欢迎回来</h2>
        <p>登录你的量化工作空间</p>
        <Field label="账号 / 手机号">
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            placeholder="输入管理员开通的账号"
            required
          />
        </Field>
        <Field label="密码">
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            placeholder="输入密码"
            required
          />
        </Field>
        {(err || error) && <div className="inline-error">{err || error}</div>}
        <Btn kind="primary" disabled={busy} icon={busy ? Loader2 : ArrowRight}>
          {busy ? "正在登录" : "登录工作空间"}
        </Btn>
        <div className="login-help">
          <LockKeyhole size={14} />
          仅限邀请 · 请联系管理员开通账户
        </div>
      </form>
    </div>
  );
}

function Overview({ data, go, setModal, updated, refresh }) {
  const [mode, setMode] = useState("all"),
    [period, setPeriod] = useState("30D");
  const accounts = (data.accounts || []).filter(
    (a) => mode === "all" || a.mode === mode,
  );
  const all = data.accounts || [],
    equity = sum(accounts, "equity"),
    profit =
      accounts.length && accounts.some((a) => accountProfit(a) == null)
        ? null
        : accounts.reduce((total, a) => total + (accountProfit(a) || 0), 0),
    initial = sum(accounts, "initial_equity"),
    running = accounts.filter((a) => a.status === "running").length;
  const fullCurve = aggregateCurves(accounts);
  const end = fullCurve.at(-1)?.timestamp || 0;
  const curve =
    period === "全部"
      ? fullCurve
      : fullCurve.filter(
          (p) => p.timestamp >= end - (period === "7D" ? 7 : 30) * 86400000,
        );
  const tasks = (data.experiments || []).slice(0, 3);
  return (
    <>
      <Heading eyebrow="WORKSPACE OVERVIEW" title="运行总览">
        <span className="update-time">
          <Clock3 size={13} />
          {updated?.toLocaleTimeString("zh-CN", {
            timeZone: "Asia/Shanghai",
            hour12: false,
          })}{" "}
          更新
        </span>
        <Btn icon={RefreshCw} onClick={refresh}>
          刷新
        </Btn>
      </Heading>
      <div className="overview-filter">
        <div className="segmented">
          <button
            className={mode === "all" ? "selected" : ""}
            onClick={() => setMode("all")}
          >
            全部账户 <span>{all.length}</span>
          </button>
          <button
            className={mode === "paper" ? "selected" : ""}
            onClick={() => setMode("paper")}
          >
            模拟交易
          </button>
          <button
            className={mode === "live" ? "selected" : ""}
            onClick={() => setMode("live")}
          >
            实盘交易
          </button>
        </div>
        {(accounts.some((a) => a.source === "demo") ||
          accounts.some((a) => a.equity == null)) && (
          <span className="sample-label">
            <Database size={13} />
            {accounts.some((a) => a.source === "demo")
              ? "包含示例数据 · 不代表实际收益"
              : "部分账户未验证 · 汇总暂不可用"}
          </span>
        )}
      </div>
      <div className="stats-grid">
        <Stat
          label="账户总权益"
          value={money(equity)}
          icon={Wallet}
          sub={`${accounts.length} 个账户 · 初始资金 ${money(initial)} U`}
          accent
        />
        <Stat
          label="累计收益"
          value={`${profit != null && profit > 0 ? "+" : ""}${money(profit)}`}
          icon={TrendingUp}
          sub={`${pct(profit == null ? null : initial ? (profit / initial) * 100 : 0)} 累计收益率`}
          trend={profit > 0 ? "up" : profit < 0 ? "down" : undefined}
        />
        <Stat
          label="保证金占用"
          value={money(sum(accounts, "margin_used"))}
          icon={Shield}
          sub={
            equity != null && sum(accounts, "margin_used") != null
              ? `占总权益 ${equity ? ((sum(accounts, "margin_used") / equity) * 100).toFixed(2) : "0.00"}%`
              : "暂无完整保证金快照"
          }
        />
        <Stat
          label="策略运行实例"
          value={String(running).padStart(2, "0")}
          unit={`/ ${accounts.length}`}
          icon={Activity}
          sub={`${accounts.filter((a) => a.status === "paused").length} 个已停止开仓 · ${accounts.filter((a) => a.status === "stopped").length} 个已停止`}
        />
      </div>
      <div className="overview-main">
        <section className="panel equity-panel">
          <div className="panel-heading">
            <div>
              <h2>
                资金表现 <span className="tag">U</span>
              </h2>
              <p>所有选中账户的权益变化</p>
            </div>
            <div className="segmented small">
              {["7D", "30D", "全部"].map((p) => (
                <button
                  key={p}
                  className={period === p ? "selected" : ""}
                  onClick={() => setPeriod(p)}
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
          <div className="chart-summary">
            <div>
              <span>区间权益变化</span>
              <strong
                className={valueTone(
                  curve.length ? curve.at(-1).equity - curve[0].equity : null,
                )}
              >
                {curve.length && curve[0].equity
                  ? pct((curve.at(-1).equity / curve[0].equity - 1) * 100)
                  : "—"}
                <ArrowUpRight size={18} />
              </strong>
            </div>
            <div className="chart-legend">
              <i />
              账户权益 <span className="legend-divider" />
              未平滑 · 按记录展示
            </div>
          </div>
          <Chart data={curve} height={260} />
          <div className="chart-footer">
            <span>
              <Shield size={13} />
              最大账户回撤{" "}
              <b
                className={
                  accounts.some((a) => a.max_drawdown_pct > 0) ? "negative" : ""
                }
              >
                {accounts.length &&
                !accounts.some((a) => a.max_drawdown_pct == null)
                  ? `${Math.max(...accounts.map((a) => Number(a.max_drawdown_pct))).toFixed(2)}%`
                  : "—"}
              </b>
            </span>
            <span>
              浮动盈亏{" "}
              <b className={valueTone(sum(accounts, "unrealized_pnl"))}>
                {sum(accounts, "unrealized_pnl") > 0 ? "+" : ""}
                {money(sum(accounts, "unrealized_pnl"))} U
              </b>
            </span>
          </div>
        </section>
        <section className="panel runtime-panel">
          <div className="panel-heading">
            <div>
              <h2>运行健康度</h2>
              <p>控制服务与执行服务独立运行</p>
            </div>
            <Activity size={17} className="dim" />
          </div>
          <div className="runtime-visual">
            <div
              className="runtime-ring"
              style={{
                background: `conic-gradient(#8fcbb0 0 ${accounts.length ? (running / accounts.length) * 100 : 0}%,#a89b7b ${accounts.length ? (running / accounts.length) * 100 : 0}% ${accounts.length ? ((running + accounts.filter((a) => a.status === "paused").length) / accounts.length) * 100 : 0}%,#37434c 0 100%)`,
              }}
            >
              <span>{accounts.length}</span>
              <small>账户实例</small>
            </div>
            <div className="runtime-key">
              <span>
                <i className="green-dot" />
                运行中<b>{running}</b>
              </span>
              <span>
                <i className="amber-dot" />
                停止开仓
                <b>{accounts.filter((a) => a.status === "paused").length}</b>
              </span>
              <span>
                <i className="gray-dot" />
                其他状态
                <b>
                  {accounts.length -
                    running -
                    accounts.filter((a) => a.status === "paused").length}
                </b>
              </span>
            </div>
          </div>
          <div className="service-list">
            <div>
              <span>
                <Database size={15} />
                控制服务 / API
              </span>
              <Badge status="running">已连接</Badge>
            </div>
            <div>
              <span>
                <Radio size={15} />
                真实下单网关
              </span>
              <Badge status="stopped">未启用</Badge>
            </div>
            <div>
              <span>
                <FlaskConical size={15} />
                回测队列
              </span>
              <span className="mono">
                {
                  (data.experiments || []).filter((e) =>
                    ["queued", "running"].includes(e.status),
                  ).length
                }{" "}
                个任务
              </span>
            </div>
          </div>
          <button className="panel-link" onClick={() => go("logs")}>
            查看运行日志 <ArrowRight size={14} />
          </button>
        </section>
      </div>
      <section className="panel account-panel">
        <div className="panel-heading">
          <div>
            <h2>
              账户表现 <span className="count-pill">{accounts.length}</span>
            </h2>
            <p>查看资金分布、运行状态和策略表现</p>
          </div>
        </div>
        <AccountTable
          accounts={accounts.slice(0, 5)}
          setModal={setModal}
          compact
        />
      </section>
      <div className="bottom-grid">
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>最近实验</h2>
              <p>每次参数调整，都留下可复现的记录</p>
            </div>
            <button className="text-btn" onClick={() => go("experiments")}>
              全部实验 <ArrowRight size={14} />
            </button>
          </div>
          {tasks.length ? (
            tasks.map((e, i) => (
              <button
                className="experiment-row"
                key={e.id}
                onClick={() => go("experiments")}
              >
                <span className="experiment-icon">
                  <FlaskConical size={17} />
                </span>
                <div>
                  <strong>{e.name || e.strategy_name || `实验 ${e.id}`}</strong>
                  <small>
                    {e.strategy_name} <span>·</span>{" "}
                    {formatBeijing(e.created_at)} 北京时间
                  </small>
                </div>
                <Badge status={e.status} />
                <span
                  className={`experiment-return ${valueTone(e.metrics?.total_return)}`}
                >
                  {pct(
                    e.metrics?.total_return != null
                      ? e.metrics.total_return * 100
                      : null,
                  )}
                </span>
                <ChevronRight size={15} />
              </button>
            ))
          ) : (
            <Empty title="还没有实验" />
          )}
        </section>
        <section className="panel quickstart">
          <span className="quickstart-label">
            <Code2 size={15} /> 策略开发
          </span>
          <h2>你的逻辑，统一的执行语言。</h2>
          <p>
            一个策略接口，连接回测、模拟与实盘。
            <br />
            从接入协议开始构建你的第一个策略。
          </p>
          <button className="text-btn" onClick={() => go("protocol")}>
            查看策略接入协议 <ArrowUpRight size={15} />
          </button>
          <div className="code-decoration">
            strategy.on_bar(context)
            <br />
            <span>return [OrderIntent(...)]</span>
          </div>
        </section>
      </div>
    </>
  );
}

function AssetOverview({ accounts }) {
  const metrics = [
    ["浮动盈亏", "unrealized_pnl", "未平仓头寸的账面变动"],
    ["保证金占用", "margin_used", "当前持仓占用资金"],
    ["累计已平仓收益", "realized_pnl", "已扣除成交手续费"],
  ];
  const equity = sum(accounts, "equity");
  const stale = accounts.some((a) => a.stale);
  return (
    <section className="asset-overview" aria-label="账户资产概览">
      <div className="asset-primary">
        <div className="asset-label">
          账户总权益 <span className="unit">U</span>
        </div>
        <strong
          className="asset-value"
          title={
            equity == null
              ? "存在未取得资金快照的账户，合计暂不可用"
              : undefined
          }
        >
          {money(equity)}
        </strong>
        <div className="asset-context">
          <span>{accounts.length} 个账户</span>
          <span>
            {accounts.filter((a) => a.status === "running").length} 个运行中
          </span>
          {stale && <span className="data-stale">部分数据已过期</span>}
        </div>
      </div>
      <div className="asset-secondary">
        {metrics.map(([label, key, help]) => {
          const value = sum(accounts, key);
          return (
            <div className="asset-metric" key={key}>
              <span>
                {label} <small>U</small>
              </span>
              <strong className={key === "margin_used" ? "" : valueTone(value)}>
                {money(value)}
              </strong>
              <small>{value == null ? "账户资金快照不完整" : help}</small>
            </div>
          );
        })}
      </div>
    </section>
  );
}
function AccountActions({ account: a, setModal, compact = false }) {
  const [open, setOpen] = useState(false),
    [position, setPosition] = useState({});
  const trigger = useRef(null),
    menu = useRef(null);
  const disabledReason = a.source === "demo" ? "示例账户不可执行运行操作" : "";
  useEffect(() => {
    if (!open) return;
    const close = (event) => {
      if (
        !menu.current?.contains(event.target) &&
        !trigger.current?.contains(event.target)
      )
        setOpen(false);
    };
    const key = (event) => {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const items = [
          ...(menu.current?.querySelectorAll("button:not(:disabled)") || []),
        ];
        const index = items.indexOf(document.activeElement);
        const next =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? items.length - 1
              : (index + (event.key === "ArrowUp" ? -1 : 1) + items.length) %
                items.length;
        items[next]?.focus();
      }
    };
    const reposition = () => {
      const r = trigger.current?.getBoundingClientRect();
      if (r)
        setPosition({
          left: Math.max(12, Math.min(r.right - 232, window.innerWidth - 244)),
          ...(window.innerHeight - r.bottom < 300
            ? { bottom: window.innerHeight - r.top + 8 }
            : { top: r.bottom + 8 }),
        });
    };
    reposition();
    menu.current?.querySelector("button:not(:disabled)")?.focus();
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", key);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", key);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open]);
  const command = (action) => {
    setOpen(false);
    setModal({ type: "command", action, accounts: [a] });
  };
  return (
    <div className="account-row-actions">
      <button
        className="row-link"
        onClick={() => setModal({ type: "client", account: a })}
        aria-label={`查看 ${a.name}`}
      >
        查看
      </button>
      {!compact && (
        <button
          className="row-link"
          onClick={() => setModal({ type: "account", account: a })}
          disabled={a.status !== "stopped"}
          title={
            a.status !== "stopped" ? "停止运行并确认后可修改配置" : undefined
          }
          aria-label={`配置 ${a.name}`}
        >
          配置
        </button>
      )}
      {!compact && (
        <button
          ref={trigger}
          className="icon-btn"
          onClick={() => setOpen(!open)}
          aria-label={`更多操作 ${a.name}`}
          aria-expanded={open}
          aria-haspopup="menu"
        >
          <MoreHorizontal size={18} />
        </button>
      )}
      {open &&
        createPortal(
          <div
            ref={menu}
            className="account-menu"
            style={position}
            role="menu"
            aria-label={`${a.name} 的操作`}
          >
            <div className="menu-account">
              {a.name}
              <small>{a.strategy_name || "未分配策略"}</small>
            </div>
            {a.mode === "live" && (
              <button
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  setModal({ type: "check", account: a });
                }}
              >
                <Shield size={16} />
                账户接入检查
              </button>
            )}
            <button
              role="menuitem"
              disabled={
                !!disabledReason || ["paused", "stopped"].includes(a.status)
              }
              title={disabledReason}
              onClick={() => command("pause")}
            >
              <Pause size={16} />
              停止开仓
            </button>
            <button
              role="menuitem"
              disabled={!!disabledReason || a.status === "running"}
              title={disabledReason}
              onClick={() => command("resume")}
            >
              <Play size={16} />
              恢复运行
            </button>
            <div className="menu-danger">
              <small>影响此账户的全部策略持仓</small>
              <button
                role="menuitem"
                disabled={!!disabledReason}
                title={disabledReason}
                onClick={() => command("flatten")}
              >
                <Square size={16} />
                平仓并停止
              </button>
            </div>
            {(disabledReason || a.stale) && (
              <p className="menu-note">
                {disabledReason || "数据已过期，控制指令将等待执行端确认。"}
              </p>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}
function AccountTable({ accounts, setModal, compact = false }) {
  return accounts.length ? (
    <div className="table-wrap account-table-wrap">
      <table className="account-table">
        <thead>
          <tr>
            <th>账户 / 策略</th>
            <th>状态</th>
            <th className="numeric">
              权益资金 <small>U</small>
            </th>
            <th className="numeric">
              累计收益 <small>U</small>
            </th>
            <th className="numeric">
              保证金占用 <small>U</small>
            </th>
            {!compact && <th className="numeric">持仓 多 / 空</th>}
            <th className="actions-heading">操作</th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((a, i) => (
            <tr
              key={a.id}
              className={a.stale ? "account-row stale-row" : "account-row"}
            >
              <td data-label="账户">
                <button
                  className="account-name"
                  onClick={() => setModal({ type: "client", account: a })}
                >
                  <span className={`account-avatar color-${i % 4}`}>
                    {a.name?.slice(0, 1) || "A"}
                  </span>
                  <span>
                    <strong>{a.name}</strong>
                    <small>
                      {a.strategy_name || "未配置策略"}
                      {a.source === "demo" ? " · 示例" : ""}
                    </small>
                  </span>
                </button>
                <span
                  className={`market-tag ${a.mode === "live" ? "purple" : ""}`}
                >
                  <span />
                  {a.mode === "live" ? "实盘" : "模拟"}
                </span>
              </td>
              <td data-label="状态">
                <Badge status={a.status} />
                <small className="account-freshness">
                  {a.stale
                    ? "数据已过期"
                    : a.last_heartbeat
                      ? `${formatBeijing(a.last_heartbeat, { year: undefined, month: undefined, day: undefined, second: "2-digit" })} 更新`
                      : "等待首次回执"}
                </small>
              </td>
              <td data-label="权益资金 U" className="numeric">
                <strong>{money(a.equity)}</strong>
                <small>
                  {a.equity == null
                    ? "尚无账户快照"
                    : `初始 ${money(a.initial_equity)}`}
                </small>
              </td>
              <td
                data-label="累计收益 U"
                className={`numeric ${valueTone(accountProfit(a))}`}
              >
                <strong>{money(accountProfit(a))}</strong>
                <small>
                  {accountProfit(a) == null
                    ? "资金快照不完整"
                    : pct(a.return_pct)}
                </small>
              </td>
              <td data-label="保证金占用 U" className="numeric">
                <strong>{money(a.margin_used)}</strong>
                <small>
                  {a.margin_used == null || a.equity == null
                    ? "尚无保证金快照"
                    : a.equity > 0
                      ? `${((a.margin_used / a.equity) * 100).toFixed(1)}% 占用`
                      : a.margin_used === 0
                        ? "0.0% 占用"
                        : "占用比例不可用"}
                </small>
              </td>
              {!compact && (
                <td data-label="持仓 多 / 空" className="numeric">
                  <span>{a.long_count ?? "—"}</span>
                  <span className="numeric-divider"> / </span>
                  <span>{a.short_count ?? "—"}</span>
                </td>
              )}
              <td data-label="操作" className="account-action-cell">
                <AccountActions
                  account={a}
                  setModal={setModal}
                  compact={compact}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : (
    <Empty
      title="还没有账户"
      desc="创建账户并分配策略后，资金与运行情况会显示在这里。"
    />
  );
}
function RecentOperations({ commands, accounts }) {
  const recent = commands.filter((c) =>
    accounts.some((a) => a.id === c.account_id),
  );
  const active = recent.filter(
    (c) => !["completed", "cancelled"].includes(c.status),
  );
  const complete = recent.filter((c) =>
    ["completed", "cancelled"].includes(c.status),
  );
  const entries = (items) => (
    <div className="operation-list">
      {items.map((c) => (
        <div
          className={`operation-item ${c.status === "failed" ? "needs-attention" : ""}`}
          key={c.id}
        >
          <div className="operation-main">
            <strong>
              {accounts.find((a) => a.id === c.account_id)?.name || "账户"}
            </strong>
            <span>
              {{ pause: "停止开仓", flatten: "平仓并停止", resume: "恢复运行" }[
                c.action
              ] || c.action}
            </span>
            <Badge status={c.status} />
          </div>
          <div className="operation-detail">
            <span className={c.error ? "negative" : ""}>
              {c.error ||
                c.result?.message ||
                (c.status === "pending" ? "等待执行端确认" : "执行结果已保存")}
            </span>
            <span>
              {formatBeijing(c.created_at)} · <code>{c.id.slice(0, 10)}</code>
            </span>
          </div>
        </div>
      ))}
    </div>
  );
  return (
    <section className="panel recent-operations">
      <div className="panel-heading">
        <div>
          <h2>
            近期操作{" "}
            {active.length > 0 && (
              <span className="count-pill">{active.length} 待关注</span>
            )}
          </h2>
          <p>运行指令以执行端确认结果为准。</p>
        </div>
        <History size={18} />
      </div>
      {active.length ? (
        entries(active.slice(0, 8))
      ) : (
        <div className="operations-clear">
          <CheckCircle2 size={18} />
          <span>
            {recent.length ? "当前没有待处理的运行指令" : "暂无运行操作"}
          </span>
        </div>
      )}
      {!!complete.length && (
        <details className="operation-history">
          <summary>
            已完成的操作 <span>{complete.length}</span>
            <ChevronDown size={15} />
          </summary>
          {entries(complete.slice(0, 20))}
        </details>
      )}
    </section>
  );
}
function Accounts({
  page,
  accounts,
  strategies,
  commands = [],
  setModal,
  mutate,
  notify,
}) {
  const [tab, setTab] = useState("accounts"),
    [q, setQ] = useState("");
  const filtered = accounts.filter(
    (a) =>
      a.mode === page &&
      `${a.name} ${a.phone} ${a.strategy_name}`
        .toLowerCase()
        .includes(q.toLowerCase()),
  );
  const allForPage = accounts.filter((a) => a.mode === page);
  const action = (action) =>
    setModal({ type: "command", action, accounts: filtered });
  useEffect(() => {
    setTab("accounts");
    setQ("");
  }, [page]);
  return (
    <>
      <Heading
        title={titles[page]}
        desc={
          page === "live"
            ? "跟踪真实账户的资金、持仓与运行状态。"
            : "观察策略表现，验证执行行为。"
        }
      >
        <Btn
          kind="primary"
          icon={Plus}
          onClick={() =>
            setModal({ type: "account", account: undefined, defaultMode: page })
          }
        >
          新建账户
        </Btn>
      </Heading>
      {page === "live" && (
        <div className="notice amber">
          <ShieldAlert size={18} />
          <div>
            <strong>真实下单未启用</strong>
            <span>账户接入检查可用，完成实盘执行验收后才能恢复真实交易。</span>
          </div>
        </div>
      )}
      <AssetOverview accounts={allForPage} />
      <section className="panel accounts-workspace">
        <div className="panel-toolbar">
          <div className="tabs">
            <button
              className={tab === "accounts" ? "active" : ""}
              onClick={() => setTab("accounts")}
            >
              账户列表 <span>{filtered.length}</span>
            </button>
            <button
              className={tab === "strategies" ? "active" : ""}
              onClick={() => setTab("strategies")}
            >
              策略汇总
            </button>
          </div>
          <label className="search-input">
            <Search size={17} />
            <input
              aria-label="搜索账户、手机号或策略"
              placeholder="搜索账户、手机号或策略"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </label>
        </div>
        {tab === "accounts" ? (
          <AccountTable accounts={filtered} setModal={setModal} />
        ) : (
          <div className="table-wrap">
            <table className="strategy-summary-table">
              <thead>
                <tr>
                  <th>策略 / 版本</th>
                  <th className="numeric">客户数</th>
                  <th className="numeric">总权益 U</th>
                  <th className="numeric">浮动盈亏 U</th>
                  <th className="numeric">已平仓收益 U</th>
                  <th className="numeric">持仓数</th>
                </tr>
              </thead>
              <tbody>
                {strategies
                  .filter((s) => filtered.some((a) => a.strategy_id === s.id))
                  .map((s) => {
                    const aa = filtered.filter((a) => a.strategy_id === s.id);
                    return (
                      <tr key={s.id}>
                        <td>
                          <strong>{s.name}</strong>
                          <small>v{s.version}</small>
                        </td>
                        <td className="numeric">{aa.length}</td>
                        <td className="numeric">{money(sum(aa, "equity"))}</td>
                        <td
                          className={`numeric ${valueTone(sum(aa, "unrealized_pnl"))}`}
                        >
                          {money(sum(aa, "unrealized_pnl"))}
                        </td>
                        <td
                          className={`numeric ${valueTone(sum(aa, "realized_pnl"))}`}
                        >
                          {money(sum(aa, "realized_pnl"))}
                        </td>
                        <td className="numeric">
                          {aa.reduce(
                            (n, a) => n + (a.positions?.length || 0),
                            0,
                          )}
                        </td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
            {!filtered.length && (
              <Empty
                title="暂无运行策略"
                desc="创建账户并分配策略后显示汇总。"
              />
            )}
          </div>
        )}
      </section>
      <RecentOperations commands={commands} accounts={filtered} />
      <details className="danger-zone">
        <summary>
          <ShieldAlert size={19} />
          <span>
            <strong>全局运行控制</strong>
            <small>
              影响当前筛选的 {filtered.length} 个
              {page === "live" ? "实盘" : "模拟"}账户，操作需两次确认。
            </small>
          </span>
          <ChevronDown size={17} />
        </summary>
        <div className="danger-zone-actions">
          <Btn disabled={!filtered.length} onClick={() => action("pause")}>
            全部停止开仓
          </Btn>
          <Btn disabled={!filtered.length} onClick={() => action("resume")}>
            全部恢复
          </Btn>
          <Btn
            disabled={!filtered.length}
            kind="danger"
            onClick={() => action("flatten")}
          >
            全部平仓并停止
          </Btn>
        </div>
      </details>
    </>
  );
}
function AccountForm({
  account,
  defaultMode = "paper",
  strategies,
  mutate,
  notify,
  onClose,
}) {
  const [form, setForm] = useState({
      name: account?.name || "",
      phone: account?.phone || "",
      password: "",
      mode: account?.mode || (defaultMode === "live" ? "live" : "paper"),
      initial_equity: account?.initial_equity || 10000,
      strategy_id: account?.strategy_id || strategies[0]?.id || "",
      symbols: (account?.symbols || strategySymbols(strategies[0])).join(","),
      api_key: "",
      api_secret: "",
      config: account?.config || {},
    }),
    [busy, setBusy] = useState(false),
    [err, setErr] = useState("");
  const change = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  const strategy = strategies.find((s) => s.id === form.strategy_id);
  const maker = isMakerStrategy(strategy);
  const params =
    strategy?.manifest?.parameters?.properties ||
    strategy?.parameters?.properties ||
    {};
  return (
    <Modal
      title={account ? "账户配置" : "创建客户账户"}
      subtitle="账号由管理员开通，客户仅能查看自己的账户。"
      onClose={onClose}
      wide
    >
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr("");
          try {
            const selectedSymbols = form.symbols
              .split(/[,，\s]+/)
              .filter(Boolean);
            if (
              maker &&
              selectedSymbols.some(
                (symbol) => !strategySymbols(strategy).includes(symbol),
              )
            )
              throw new Error(
                `该策略仅允许：${strategySymbols(strategy).join(" / ")}`,
              );
            const body = {
              ...form,
              symbols: form.symbols.split(/[,，\s]+/).filter(Boolean),
              initial_equity: Number(form.initial_equity),
            };
            if (!body.api_key) delete body.api_key;
            if (!body.api_secret) delete body.api_secret;
            if (!body.password) delete body.password;
            await mutate(
              account ? `/accounts/${account.id}` : "/accounts",
              body,
              account ? "PATCH" : "POST",
            );
            notify(account ? "账户配置已保存" : "客户账户已创建");
            onClose();
          } catch (e) {
            setErr(e.message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="form-grid">
          <Field label="客户名称">
            <input
              required
              value={form.name}
              onChange={(e) => change("name", e.target.value)}
            />
          </Field>
          <Field label="手机号 / 登录账号">
            <input
              required
              type="tel"
              value={form.phone}
              onChange={(e) => change("phone", e.target.value)}
            />
          </Field>
          {
            <Field
              label={account ? "重置登录密码" : "初始密码"}
              hint={account ? "留空保留原密码；修改后旧会话失效" : "至少 10 位"}
            >
              <input
                required={!account}
                minLength={10}
                type="password"
                autoComplete="new-password"
                value={form.password}
                onChange={(e) => change("password", e.target.value)}
              />
            </Field>
          }
          <Field label="交易环境">
            <select
              value={form.mode}
              disabled={!!account}
              onChange={(e) => change("mode", e.target.value)}
            >
              <option value="paper">模拟交易</option>
              <option value="live">实盘交易</option>
            </select>
          </Field>
          <Field label="执行策略">
            <select
              value={form.strategy_id}
              onChange={(e) => {
                const selected = strategies.find(
                  (s) => s.id === e.target.value,
                );
                setForm((f) => ({
                  ...f,
                  strategy_id: e.target.value,
                  config: {},
                  symbols: strategySymbols(selected).join(","),
                }));
              }}
            >
              {strategies.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} · {s.version}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="交易对"
            hint={
              maker
                ? `允许选 1–${strategySymbols(strategy).length} 币：${strategySymbols(strategy).join(" / ")}`
                : "多个币种用逗号分隔，提交时校验策略约束"
            }
          >
            <input
              required
              value={form.symbols}
              onChange={(e) => change("symbols", e.target.value.toUpperCase())}
            />
          </Field>
          <Field label="初始资金 / 资金基准（U）">
            <input
              type="number"
              min="1"
              required
              disabled={!!account}
              value={form.initial_equity}
              onChange={(e) => change("initial_equity", e.target.value)}
            />
          </Field>
          {form.mode === "live" && (
            <>
              <Field label="Binance API Key">
                <input
                  autoComplete="off"
                  value={form.api_key}
                  placeholder={account ? "留空保留原凭证" : ""}
                  onChange={(e) => change("api_key", e.target.value)}
                />
              </Field>
              <Field
                label="Binance API Secret"
                hint="仅提交到服务端加密保存，不会返回浏览器"
              >
                <input
                  type="password"
                  autoComplete="new-password"
                  value={form.api_secret}
                  placeholder={account ? "留空保留原凭证" : ""}
                  onChange={(e) => change("api_secret", e.target.value)}
                />
              </Field>
            </>
          )}
        </div>
        {maker && <MakerNotice />}
        <div className="section-divider" />
        <h3>策略参数</h3>
        <div className="form-grid">
          {Object.entries(params).map(([k, p]) => (
            <Field key={k} label={p.title || k} hint={p.description}>
              <ParameterInput
                spec={p}
                value={form.config[k] ?? p.default}
                onChange={(value) =>
                  change("config", { ...form.config, [k]: value })
                }
              />
            </Field>
          ))}
        </div>
        {account?.route && (
          <div className="notice">
            <LockKeyhole size={16} />
            <code>/client/{account.route}</code>
          </div>
        )}
        {err && <div className="inline-error">{err}</div>}
        <div className="modal-footer">
          <Btn type="button" onClick={onClose}>
            取消
          </Btn>
          <Btn kind="primary" disabled={busy} icon={busy ? Loader2 : Check}>
            保存账户
          </Btn>
        </div>
      </form>
    </Modal>
  );
}
function CommandModal({ action, accounts, mutate, notify, onClose }) {
  const [step, setStep] = useState(1),
    [busy, setBusy] = useState(false),
    [err, setErr] = useState("");
  const name = { pause: "停止开仓", flatten: "平仓并停止", resume: "恢复运行" }[
    action
  ];
  return (
    <Modal
      title={`${step === 2 ? "再次确认：" : ""}${name}`}
      subtitle={`将影响 ${accounts.length} 个账户`}
      onClose={onClose}
    >
      <div
        className={`command-warning ${action === "flatten" ? "negative" : ""}`}
      >
        <ShieldAlert size={28} />
        <p>
          {action === "flatten"
            ? "将请求撤销待成交开仓单并平掉受管理持仓；执行端确认全部成交后才停止。Maker 平仓可能持续未成交，期间状态为正在平仓，无 Taker 后备。实盘执行仍受验收闸门保护。"
            : action === "pause"
              ? "停止新开仓，继续管理已有持仓。页面状态将在执行端确认后更新。"
              : "将重新检查账户与策略约束。未通过接入检查的实盘账户不会下单。"}
        </p>
      </div>
      <div className="affected-accounts">
        {accounts.map((a) => (
          <span key={a.id}>
            {a.name} <small>{a.mode === "live" ? "实盘" : "模拟"}</small>
          </span>
        ))}
      </div>
      <div className="notice">
        确认步骤 {step} / 2 · 命令与执行结果将持久保存。
      </div>
      {err && <div className="inline-error">{err}</div>}
      <div className="modal-footer">
        <Btn onClick={onClose}>取消</Btn>
        <Btn
          kind={action === "flatten" ? "danger" : "primary"}
          disabled={busy}
          onClick={async () => {
            if (step === 1) {
              setStep(2);
              return;
            }
            setBusy(true);
            try {
              await mutate("/commands", {
                account_ids: accounts.map((a) => a.id),
                action,
                confirmed: true,
                confirmed_twice: true,
              });
              notify("控制指令已入队，等待执行端确认");
              onClose();
            } catch (e) {
              setErr(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          {step === 1 ? "继续确认" : "确认提交指令"}
        </Btn>
      </div>
    </Modal>
  );
}
const positionName = (side, quantity) => {
  const normalized = String(side || "").toUpperCase();
  if (normalized === "LONG") return "多头";
  if (normalized === "SHORT") return "空头";
  if (quantity != null && Number(quantity) !== 0)
    return Number(quantity) > 0 ? "多头" : "空头";
  return "—";
};
const orderRoleName = (role) =>
  ({
    entry: "入场",
    take_profit: "止盈",
    tp: "止盈",
    stop_loss: "止损",
    sl: "止损",
    manual_exit: "手动退出",
    manual: "手动退出",
    flatten: "平仓并停止",
  })[role] ||
  role ||
  "—";
function openOrderState(order) {
  const status = String(order.status || "").toUpperCase();
  if (status === "PENDING_TRIGGER" || status === "UNTRIGGERED")
    return ["pending", "条件未触发"];
  if (status === "FILLED") return ["completed", "已成交"];
  if (["CANCELED", "CANCELLED"].includes(status))
    return ["cancelled", "已撤销"];
  if (status === "REJECTED") return ["failed", "已拒单"];
  if (status === "PARTIALLY_FILLED") return ["pending", "部分成交 · 余量挂单"];
  if (order.triggered === true || status === "TRIGGERED")
    return ["pending", "已触发 · 待成交"];
  if (["NEW", "OPEN", "PENDING"].includes(status))
    return ["pending", "挂单待成交"];
  return ["unknown", status ? `状态未知 · ${status}` : "状态未知"];
}
function OpenOrders({ orders }) {
  const known = Array.isArray(orders);
  return (
    <section className="panel open-orders-panel">
      <div className="panel-heading">
        <div>
          <h2>当前挂单</h2>
          <p>条件触发与实际成交分别记录；挂单不计入已成交收益。</p>
        </div>
        <span className="dim">
          {known ? `${orders.length} 笔` : "快照缺失"}
        </span>
      </div>
      {known && orders.length ? (
        <div className="table-wrap">
          <table className="open-orders-table">
            <thead>
              <tr>
                <th>交易对 / 方向</th>
                <th>用途</th>
                <th>订单类型</th>
                <th className="numeric">委托价格</th>
                <th className="numeric">触发价格</th>
                <th className="numeric">数量</th>
                <th>状态</th>
                <th>关联信息</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order, index) => {
                const [state, label] = openOrderState(order);
                const position = positionName(
                  order.position_side ?? order.positionSide,
                );
                const side = String(order.side || "").toUpperCase();
                const details = [
                  ["客户订单号", order.client_order_id ?? order.clientOrderId],
                  ["订单 ID", order.id ?? order.orderId],
                  ["仓位批次", order.lot_id ?? order.lotId],
                  ["父订单", order.parent_id ?? order.parentId],
                  ["说明", order.reason],
                ];
                return (
                  <tr key={order.id || order.client_order_id || index}>
                    <td data-label="交易对 / 方向">
                      <strong>{order.symbol || "—"}</strong>
                      <small
                        className={
                          position === "多头"
                            ? "positive"
                            : position === "空头"
                              ? "negative"
                              : ""
                        }
                      >
                        {position} ·{" "}
                        {side === "BUY"
                          ? "买入"
                          : side === "SELL"
                            ? "卖出"
                            : "—"}
                      </small>
                    </td>
                    <td data-label="用途">{orderRoleName(order.role)}</td>
                    <td data-label="订单类型">
                      {{
                        LIMIT: "限价",
                        STOP_LIMIT: "条件限价",
                        STOP: "条件限价",
                      }[String(order.type || "").toUpperCase()] ||
                        order.type ||
                        "—"}
                      <small>
                        {order.time_in_force ??
                          order.timeInForce ??
                          "有效期未知"}
                      </small>
                    </td>
                    <td data-label="委托价格" className="numeric">
                      {tradeNumber(order.price ?? order.limit_price)}
                    </td>
                    <td data-label="触发价格" className="numeric">
                      {tradeNumber(order.stop_price ?? order.stopPrice)}
                    </td>
                    <td data-label="数量" className="numeric">
                      {tradeNumber(order.quantity ?? order.origQty)}
                    </td>
                    <td data-label="状态">
                      <Badge status={state}>{label}</Badge>
                    </td>
                    <td data-label="关联信息">
                      <details className="order-metadata">
                        <summary>详情</summary>
                        <dl>
                          {details.map(([name, value]) => (
                            <div key={name}>
                              <dt>{name}</dt>
                              <dd>
                                <code>
                                  {value == null || value === ""
                                    ? "—"
                                    : String(value)}
                                </code>
                              </dd>
                            </div>
                          ))}
                        </dl>
                      </details>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty
          icon={ListFilter}
          title={known ? "当前没有挂单" : "尚无挂单快照"}
          desc={
            known
              ? "委托创建后，会显示价格、触发状态和关联仓位。"
              : "等待执行端提供挂单快照，缺失数据不代表挂单数量为零。"
          }
        />
      )}
    </section>
  );
}
function ClientView({ account: a, user }) {
  if (!a) return <Empty title="尚未分配交易账户" />;
  const positions = Array.isArray(a.positions)
    ? a.positions
    : Object.entries(a.positions || {}).map(([symbol, p]) => ({
        symbol,
        ...p,
      }));
  const settings = a.check_result?.settings || {};
  const labelMode = (v) =>
    ({
      one_way: "单向持仓",
      hedge: "双向持仓",
      cross: "全仓",
      isolated: "逐仓",
      cash: "现货现金",
    })[v] ||
    v ||
    "未知";
  const snapshotTime = a.data_updated_at || a.check_result?.checked_at;
  const maker =
    (a.execution_policy ||
      a.execution_model?.model ||
      a.manifest_execution?.model) === "maker_grid_v1";
  return (
    <div className="client-view">
      <div className="client-heading">
        <div>
          <div className="eyebrow">
            {a.mode === "live" ? "实盘交易账户" : "模拟交易账户"}
          </div>
          <h1>{a.name}的账户</h1>
          <p>
            {a.strategy_name} · {(a.symbols || []).join(" / ")}
          </p>
        </div>
        <Badge status={a.status} />
      </div>
      {a.source === "demo" && (
        <div className="notice amber">
          示例账户 · 以下数据用于界面演示，不代表真实交易收益。
        </div>
      )}
      {(a.stale || a.runtime_error) && (
        <div className="notice amber">
          <ShieldAlert size={18} />
          <div>
            <strong>{a.stale ? "账户数据过期或尚未验证" : "运行提示"}</strong>
            <span>{a.runtime_error || "当前数值不能视为实时交易所状态。"}</span>
          </div>
        </div>
      )}
      {maker && <MakerNotice />}
      {a.status === "flattening" && (
        <div className="notice amber">
          <Clock3 size={17} />
          <span>
            正在平仓：已有平仓委托仍待成交，账户尚未停止。以执行端确认的最终结果为准。
          </span>
        </div>
      )}
      <div className="client-balance">
        <span>
          账户权益 <small>U</small>
        </span>
        <strong>{money(a.equity)}</strong>
        <p
          className={
            accountProfit(a) == null ? "dim" : valueTone(accountProfit(a))
          }
        >
          累计收益 {money(accountProfit(a))} U <span>{pct(a.return_pct)}</span>
        </p>
      </div>
      <div className="stats-grid">
        <Stat
          label="保证金占用"
          value={money(a.margin_used)}
          icon={Shield}
          sub="持仓保证金"
        />
        <Stat
          label="浮动盈亏"
          value={money(a.unrealized_pnl)}
          icon={Activity}
          sub="最后账本快照"
        />
        <Stat
          label="最大回撤"
          value={money(a.max_drawdown_pct)}
          unit="%"
          icon={TrendingDown}
          sub={`${money(a.max_drawdown_u)} U`}
        />
        <Stat
          label="交易胜率"
          value={money(a.win_rate)}
          unit="%"
          icon={Gauge}
          sub={`PF ${a.profit_factor ?? "—"}`}
        />
      </div>
      <section className="panel account-settings">
        <div className="panel-heading">
          <h2>账户状态与资金风险</h2>
          <span className="tag">
            {a.mode === "live" ? "交易所检查快照" : "模拟账本"}
          </span>
        </div>
        <div className="client-settings-grid">
          <div>
            <span>持仓模式</span>
            <strong>
              {labelMode(a.position_mode || settings.position_mode)}
            </strong>
          </div>
          <div>
            <span>保证金模式</span>
            <strong>{labelMode(a.margin_mode)}</strong>
          </div>
          <div>
            <span>账户杠杆</span>
            <strong>
              {a.leverage != null ? `${a.leverage}×` : "按币种设置 / 未知"}
            </strong>
          </div>
          <div>
            <span>可用资金 U</span>
            <strong>{money(a.available)}</strong>
          </div>
          {maker && (
            <>
              <div>
                <span>开仓挂单预留保证金 U</span>
                <strong>{money(a.pending_margin)}</strong>
              </div>
              <div>
                <span>挂单手续费预留 U</span>
                <strong>{money(a.pending_fee_reserve)}</strong>
              </div>
            </>
          )}
          <div>
            <span>多头 / 空头持仓数</span>
            <strong>
              {a.long_count ?? "—"} / {a.short_count ?? "—"}
            </strong>
          </div>
          <div>
            <span>最后数据时间</span>
            <strong>
              {snapshotTime
                ? `${formatBeijing(snapshotTime)} 北京时间`
                : "尚无快照"}
            </strong>
          </div>
        </div>
        {Object.keys(settings.symbols || {}).length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>交易对</th>
                  <th>杠杆</th>
                  <th>保证金模式</th>
                </tr>
              </thead>
              <tbody>
                {Object.values(settings.symbols).map((item) => (
                  <tr key={item.symbol}>
                    <td>{item.symbol}</td>
                    <td>{item.leverage ?? "—"}×</td>
                    <td>{item.marginType ?? "未知"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>权益曲线</h2>
          <span className="tag">
            {a.source === "demo"
              ? "示例数据"
              : a.stale
                ? "数据过期 / 未验证"
                : "账本数据"}
          </span>
        </div>
        <Chart data={a.curve || []} />
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>当前持仓</h2>
          <span className="dim">{positions.length} 个币种</span>
        </div>
        {positions.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>交易对</th>
                  <th>方向</th>
                  <th className="numeric">数量</th>
                  <th className="numeric">平均价格</th>
                  <th className="numeric">浮盈亏 U</th>
                  <th className="numeric">保证金 U</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p, i) => (
                  <tr key={`${p.symbol || i}:${p.position_side || "net"}`}>
                    <td>{p.symbol}</td>
                    <td
                      className={
                        positionName(p.position_side, p.quantity) === "多头"
                          ? "positive"
                          : positionName(p.position_side, p.quantity) === "空头"
                            ? "negative"
                            : ""
                      }
                    >
                      {positionName(p.position_side, p.quantity)}
                    </td>
                    <td className="numeric">
                      {p.quantity == null
                        ? "—"
                        : tradeNumber(Math.abs(p.quantity))}
                    </td>
                    <td className="numeric">
                      {tradeNumber(p.average_price ?? p.entry_price)}
                    </td>
                    <td className={`numeric ${valueTone(p.unrealized_pnl)}`}>
                      {money(p.unrealized_pnl)}
                    </td>
                    <td className="numeric">{money(p.margin_used)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title="暂无持仓记录"
            desc="仅显示已记录在受管理账本中的持仓。"
          />
        )}
      </section>
      <OpenOrders orders={a.open_orders} />
      <section className="panel">
        <div className="panel-heading">
          <h2>成交与订单记录</h2>
          <span className="dim">{a.orders?.length || 0} 笔记录</span>
        </div>
        {a.orders?.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>交易对</th>
                  <th>方向</th>
                  <th className="numeric">数量</th>
                  <th className="numeric">成交价格</th>
                  <th className="numeric">已实现盈亏 U</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {a.orders.map((o, i) => (
                  <tr key={i}>
                    <td>{o.symbol}</td>
                    <td
                      className={
                        String(o.side || "").toLowerCase() === "buy"
                          ? "positive"
                          : String(o.side || "").toLowerCase() === "sell"
                            ? "negative"
                            : ""
                      }
                    >
                      {String(o.side || "").toLowerCase() === "buy"
                        ? "买入"
                        : String(o.side || "").toLowerCase() === "sell"
                          ? "卖出"
                          : "—"}
                      {o.position_side
                        ? ` · ${positionName(o.position_side)}`
                        : ""}
                    </td>
                    <td className="numeric">{tradeNumber(o.quantity)}</td>
                    <td className="numeric">{tradeNumber(o.price)}</td>
                    <td className={`numeric ${valueTone(o.realized_pnl)}`}>
                      {money(o.realized_pnl)}
                    </td>
                    <td>
                      {o.status
                        ? {
                            filled: "已成交",
                            FILLED: "已成交",
                            NEW: "待成交",
                            PENDING_TRIGGER: "条件未触发",
                            CANCELED: "已撤销",
                            REJECTED: "已拒单",
                          }[o.status] || o.status
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="暂无订单记录" />
        )}
      </section>
      <p className="dim">
        账户模式、杠杆与保证金设置以接入检查返回的交易所实际状态为准。缺失信息显示为未知。
      </p>
    </div>
  );
}

function downloadForm(config = {}) {
  return {
    market: config.market || "futures",
    symbols: config.symbols?.join(",") || "BTCUSDT,ETHUSDT",
    interval: config.interval || "1h",
    start: config.start ? toBeijingInput(config.start) : "2025-01-01T00:00",
    end: config.end ? toBeijingInput(config.end) : "2025-04-01T00:00",
  };
}
function DownloadTasks({ jobs, onReuse, disabled }) {
  if (!jobs.length) return null;
  const labels = {
    queued: "排队中",
    running: "下载中",
    completed: "已完成",
    failed: "失败",
  };
  return (
    <section className="panel download-jobs" aria-label="后台下载任务">
      <div className="panel-heading">
        <div>
          <h2>后台下载任务</h2>
          <p>离开页面后继续执行，完成后自动加入本地数据集。</p>
        </div>
      </div>
      <div className="download-job-list">
        {jobs.slice(0, 5).map((job) => {
          const config = job.config || {};
          const pending = ["queued", "running"].includes(job.status);
          const progress =
            job.status === "completed"
              ? 100
              : Math.min(
                  99,
                  Math.max(0, Math.floor(Number(job.progress) || 0)),
                );
          return (
            <article className="download-job" key={job.id}>
              <div className="download-job-heading">
                <strong>
                  {config.symbols?.join(" / ") || config.name || "K 线下载"} ·{" "}
                  {config.interval}
                </strong>
                <Badge status={job.status}>
                  {labels[job.status] || "状态未知"}
                </Badge>
              </div>
              <p className="download-job-range">
                {config.market === "spot" ? "现货" : "永续合约"} ·{" "}
                {formatBeijing(config.start)} — {formatBeijing(config.end)}{" "}
                北京时间
              </p>
              {pending && (
                <progress
                  aria-label={`下载进度 ${job.id}`}
                  value={progress}
                  max="100"
                />
              )}
              <div className="download-job-status">
                <span>
                  {job.status === "queued"
                    ? "等待研究任务进程调度"
                    : job.status === "completed"
                      ? "数据集已保存"
                      : job.status === "failed"
                        ? "任务已停止，可修正配置后重试"
                        : `${progress}% · ${job.current_symbol || "准备下载"}`}
                </span>
                <span>
                  已读取{" "}
                  {Number(job.downloaded_bars || 0).toLocaleString("en-US")} 根
                  · 已下载 {job.completed_symbols || 0}/
                  {job.total_symbols || config.symbols?.length || 0} 个币种
                </span>
              </div>
              {job.error && <div className="inline-error">{job.error}</div>}
              <div className="download-job-footer">
                <span title={job.id}>
                  任务 {job.id.slice(0, 12)} ·{" "}
                  {formatBeijing(job.updated_at || job.created_at)}
                </span>
                {job.status === "failed" && (
                  <Btn disabled={disabled} onClick={() => onReuse(config)}>
                    载入配置重试
                  </Btn>
                )}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
function Datasets({ data, mutate, notify, startDownload, downloadSubmitting }) {
  const jobs = data.download_jobs || [];
  const activeDownload = jobs.find((job) =>
    ["queued", "running"].includes(job.status),
  );
  const [tab, setTab] = useState("download"),
    [form, setForm] = useState(() => downloadForm(activeDownload?.config)),
    [busy, setBusy] = useState(false),
    [filename, setFilename] = useState(""),
    [raw, setRaw] = useState(""),
    [err, setErr] = useState("");
  const change = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  const downloadLocked = Boolean(activeDownload || downloadSubmitting);
  const submitting = busy || (tab === "download" && downloadLocked);
  useEffect(() => {
    if (activeDownload?.config) setForm(downloadForm(activeDownload.config));
  }, [activeDownload?.id]);
  return (
    <>
      <Heading
        eyebrow="MARKET DATA"
        title="行情数据"
        desc="从币安公开接口获取 K 线，或导入本地数据。每份数据都保留版本指纹。"
      />
      <div className="split-layout">
        <section className="panel">
          <div className="panel-toolbar">
            <div className="tabs">
              <button
                className={tab === "download" ? "active" : ""}
                onClick={() => setTab("download")}
              >
                币安公开数据
              </button>
              <button
                className={tab === "import" ? "active" : ""}
                onClick={() => setTab("import")}
              >
                本地文件导入
              </button>
            </div>
          </div>
          <form
            className="panel-body"
            onSubmit={async (e) => {
              e.preventDefault();
              if (submitting) return;
              setBusy(true);
              setErr("");
              try {
                if (tab === "download") {
                  await startDownload({
                    ...form,
                    symbols: form.symbols.split(/[,，\s]+/).filter(Boolean),
                    start: beijingInputToISO(form.start),
                    end: beijingInputToISO(form.end),
                  });
                } else {
                  const parsed = JSON.parse(raw);
                  await mutate("/datasets", {
                    name: filename || "导入数据",
                    market: form.market,
                    interval: form.interval,
                    symbols: form.symbols.split(/[,，\s]+/).filter(Boolean),
                    candles: parsed.candles || parsed,
                  });
                  notify("本地 K 线数据已导入");
                }
              } catch (e) {
                setErr(e.message);
              } finally {
                setBusy(false);
              }
            }}
          >
            <fieldset disabled={submitting} className="download-fields">
              <div className="form-grid">
                <Field label="交易市场">
                  <select
                    value={form.market}
                    onChange={(e) => change("market", e.target.value)}
                  >
                    <option value="futures">U 本位永续合约</option>
                    <option value="spot">现货</option>
                  </select>
                </Field>
                <Field label="K 线周期">
                  <select
                    value={form.interval}
                    onChange={(e) => change("interval", e.target.value)}
                  >
                    {[
                      "1m",
                      "3m",
                      "5m",
                      "15m",
                      "30m",
                      "1h",
                      "2h",
                      "4h",
                      "6h",
                      "12h",
                      "1d",
                    ].map((x) => (
                      <option key={x}>{x}</option>
                    ))}
                  </select>
                </Field>
              </div>
              <Field
                label="交易对"
                hint="支持 USDT / USDC；多个交易对用逗号分隔，服务端校验交易所可用性"
              >
                <input
                  value={form.symbols}
                  onChange={(e) =>
                    change("symbols", e.target.value.toUpperCase())
                  }
                  required
                />
              </Field>
              {tab === "download" ? (
                <div className="form-grid">
                  <Field label="开始时间（北京时间）">
                    <input
                      type="datetime-local"
                      value={form.start}
                      onChange={(e) => change("start", e.target.value)}
                      required
                    />
                  </Field>
                  <Field label="结束时间（北京时间，不含）">
                    <input
                      type="datetime-local"
                      value={form.end}
                      min={form.start}
                      onChange={(e) => change("end", e.target.value)}
                      required
                    />
                  </Field>
                </div>
              ) : (
                <>
                  <label className="file-drop">
                    <Upload size={25} />
                    <strong>{filename || "选择本地 JSON / CSV 文件"}</strong>
                    <span>
                      CSV 包含 timestamp, open, high, low, close, volume 列
                    </span>
                    <input
                      type="file"
                      accept=".json,.csv"
                      onChange={async (e) => {
                        const file = e.target.files?.[0];
                        if (!file) return;
                        setFilename(file.name);
                        const txt = await file.text();
                        if (file.name.endsWith(".csv")) {
                          try {
                            const lines = txt.trim().split(/\r?\n/),
                              keys = lines
                                .shift()
                                .split(",")
                                .map((x) => x.trim()),
                              candles = lines.map((line) =>
                                Object.fromEntries(
                                  line
                                    .split(",")
                                    .map((v, i) => [keys[i], Number(v)]),
                                ),
                              );
                            setRaw(
                              JSON.stringify({
                                [form.symbols.split(",")[0]]: candles,
                              }),
                            );
                            notify("CSV 已解析，请确认交易对后导入");
                          } catch {
                            setErr("CSV 格式无法解析");
                          }
                        } else setRaw(txt);
                      }}
                    />
                  </label>
                  <small className="dim">
                    JSON 格式：
                    {`{"BTCUSDT":[{"timestamp":1735689600000,"open":...}]}`}
                  </small>
                </>
              )}
            </fieldset>
            {err && <div className="inline-error">{err}</div>}
            <div className="form-bottom">
              <Btn
                kind="primary"
                icon={
                  submitting ? Loader2 : tab === "download" ? Download : Upload
                }
                disabled={submitting}
              >
                {tab === "download" && downloadSubmitting
                  ? "正在提交"
                  : tab === "download" && activeDownload
                    ? activeDownload.status === "queued"
                      ? "排队中"
                      : "正在下载"
                    : busy
                      ? "处理中"
                      : tab === "download"
                        ? "下载 K 线"
                        : "验证并导入"}
              </Btn>
            </div>
          </form>
        </section>
        <section className="panel data-notes">
          <div className="panel-heading">
            <h2>数据质量约定</h2>
            <Shield size={17} />
          </div>
          {[
            [
              "01",
              "仅使用已收盘 K 线",
              "按北京时间选择区间；保留币安原始时间戳与分桶，不重采样。",
            ],
            [
              "02",
              "数据集具有独立版本",
              "内容哈希绑定实验，参数变化不会覆盖原始数据。",
            ],
            [
              "03",
              "市场与策略必须匹配",
              "现货策略使用现货行情，不支持杠杆和做空。",
            ],
            ["04", "异常不会静默填补", "缺失、重复或无序的数据需要通过校验。"],
          ].map(([n, t, d]) => (
            <div className="data-note" key={n}>
              <span>{n}</span>
              <div>
                <h3>{t}</h3>
                <p>{d}</p>
              </div>
            </div>
          ))}
        </section>
      </div>
      <DownloadTasks
        jobs={jobs}
        disabled={downloadLocked || busy}
        onReuse={(config) => {
          setForm(downloadForm(config));
          setTab("download");
          setErr("");
          notify("已载入失败任务配置，确认后点击下载 K 线");
        }}
      />
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>
              本地数据集{" "}
              <span className="count-pill">{data.datasets?.length || 0}</span>
            </h2>
          </div>
        </div>
        {data.datasets?.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>数据集</th>
                  <th>市场</th>
                  <th>交易对</th>
                  <th>周期</th>
                  <th>开始时间</th>
                  <th>结束时间</th>
                  <th className="numeric">数据量</th>
                  <th>版本</th>
                  <th>来源</th>
                </tr>
              </thead>
              <tbody>
                {data.datasets.map((d) => (
                  <tr key={d.id}>
                    <td>
                      <strong>{d.name}</strong>
                    </td>
                    <td>{d.market === "spot" ? "现货" : "永续"}</td>
                    <td>{(d.symbols || []).join(" / ")}</td>
                    <td className="mono">{d.interval}</td>
                    <td className="mono">{formatBeijing(d.start)}</td>
                    <td className="mono">{formatBeijing(d.end)}</td>
                    <td className="numeric">
                      {d.row_count != null || d.count != null
                        ? Number(d.row_count ?? d.count).toLocaleString("en-US")
                        : "—"}
                    </td>
                    <td>
                      <code>
                        {(d.version || d.sha256 || d.hash || d.id).slice(0, 10)}
                      </code>
                    </td>
                    <td>
                      <span className="tag">
                        {["demo", "synthetic"].includes(d.source)
                          ? "合成示例"
                          : d.source || "导入"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="尚无行情数据" />
        )}
      </section>
    </>
  );
}
function Strategies({ strategies, mutate, notify, go }) {
  const [selected, setSelected] = useState(null),
    [detail, setDetail] = useState(null),
    [source, setSource] = useState(""),
    [name, setName] = useState(""),
    [err, setErr] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <>
      <Heading
        eyebrow="STRATEGY LIBRARY"
        title="策略管理"
        desc="统一的策略契约，让研究代码与执行环境保持一致。"
      >
        <Btn
          kind="primary"
          icon={Plus}
          onClick={() => {
            setSelected({});
            setSource(
              'MANIFEST = {\n    "name": "新策略",\n    "version": "1.0.0",\n    "description": "策略描述",\n    "market": "spot",\n    "symbols": {"mode": "any", "values": [], "min_count": 1},\n    "account": {"position_modes": ["one_way"], "margin_modes": ["cash"], "leverage_min": 1, "leverage_max": 1, "min_capital": 100},\n    "parameters": {"type": "object", "properties": {}}\n}\n\nclass Strategy:\n    def on_bar(self, context):\n        return []\n',
            );
            setName("新策略");
          }}
        >
          添加策略
        </Btn>
      </Heading>
      <div className="strategy-grid">
        {strategies.map((s, i) => {
          const m = s.manifest || s;
          return (
            <section className="panel strategy-card" key={s.id}>
              <div className="strategy-card-top">
                <div className={`strategy-icon color-${i % 4}`}>
                  <Blocks size={20} />
                </div>
                <div className="strategy-title">
                  <h2>{s.name}</h2>
                  <span>v{s.version}</span>
                </div>
                <Badge status={s.trusted ? "completed" : "pending"}>
                  {s.trusted
                    ? isMakerStrategy(s)
                      ? "回测 / 模拟"
                      : "可运行"
                    : "待审核"}
                </Badge>
              </div>
              <p>{s.description || m.description || "框架标准策略接口"}</p>
              <div className="strategy-tags">
                <span>
                  {m.market === "spot" ? "现货 · 无杠杆" : "U 本位永续"}
                </span>
                <span>
                  {m.symbols?.values?.length
                    ? (m.symbols.values || []).join(" / ")
                    : m.symbols?.mode === "multi"
                      ? "多币种策略"
                      : "交易对可配置"}
                </span>
              </div>
              <div className="strategy-detail">
                <span>
                  最低资金<b>{money(m.account?.min_capital)} U</b>
                </span>
                <span>
                  杠杆范围
                  <b>
                    {m.account?.leverage_min || 1}–
                    {m.account?.leverage_max || 1}×
                  </b>
                </span>
                <span>
                  保证金模式
                  <b>
                    {(m.account?.margin_modes || [])
                      .map(
                        (mode) =>
                          ({
                            cross: "全仓",
                            isolated: "逐仓",
                            cash: "现货现金",
                          })[mode] || mode,
                      )
                      .join(" / ") || "—"}
                  </b>
                </span>
                <span>
                  持仓模式
                  <b>
                    {(m.account?.position_modes || [])
                      .map(
                        (mode) =>
                          ({ one_way: "单向持仓", hedge: "双向持仓" })[mode] ||
                          mode,
                      )
                      .join(" / ") || "—"}
                  </b>
                </span>
                <span>
                  参数数量
                  <b>{Object.keys(m.parameters?.properties || {}).length} 项</b>
                </span>
                {isMakerStrategy(s) && (
                  <span>
                    执行模型<b>Maker 限价挂单</b>
                  </span>
                )}
              </div>
              <div className="strategy-card-bottom">
                <Btn onClick={() => setDetail(s)}>详情</Btn>
                <Btn
                  icon={Code2}
                  onClick={() => {
                    setSelected(s);
                    setSource(s.source || "");
                    setName(s.name);
                  }}
                >
                  编辑
                </Btn>
                <Btn
                  kind="primary-ghost"
                  icon={Play}
                  onClick={() => {
                    sessionStorage.setItem("mk-strategy", s.id);
                    go("backtest");
                  }}
                >
                  回测
                </Btn>
              </div>
            </section>
          );
        })}
      </div>
      <div className="strategy-help">
        <CircleHelp size={16} />
        <span>策略保存时会校验账户要求与参数定义。</span>
        <button className="text-btn" onClick={() => go("protocol")}>
          接入文档 <ArrowRight size={14} />
        </button>
      </div>
      {detail && (
        <Modal
          wide
          title={detail.name}
          subtitle={`版本 ${detail.version} · ${detail.trusted ? (isMakerStrategy(detail) ? "回测 / 模拟可用" : "可运行") : "待审核"}`}
          onClose={() => setDetail(null)}
        >
          <p className="strategy-description">
            {detail.description ||
              detail.manifest?.description ||
              "暂无策略说明"}
          </p>
          <div className="strategy-detail-modal">
            <div>
              <span>交易市场</span>
              <strong>
                {(detail.manifest || detail).market === "spot"
                  ? "现货，无杠杆、无做空"
                  : "U 本位永续"}
              </strong>
            </div>
            <div>
              <span>最低资金</span>
              <strong>
                {money((detail.manifest || detail).account?.min_capital)} U
              </strong>
            </div>
            <div>
              <span>持仓模式</span>
              <strong>
                {((detail.manifest || detail).account?.position_modes || [])
                  .map(
                    (v) => ({ one_way: "单向持仓", hedge: "双向持仓" })[v] || v,
                  )
                  .join(" / ") || "—"}
              </strong>
            </div>
            <div>
              <span>保证金模式</span>
              <strong>
                {((detail.manifest || detail).account?.margin_modes || [])
                  .map(
                    (v) =>
                      ({ cross: "全仓", isolated: "逐仓", cash: "现货现金" })[
                        v
                      ] || v,
                  )
                  .join(" / ") || "—"}
              </strong>
            </div>
          </div>
          <div className="strategy-execution-detail">
            <span>交易对约束</span>
            <strong>
              {strategyManifest(detail).symbols?.values?.length
                ? strategySymbols(detail).join(" / ")
                : "交易对可配置"}
            </strong>
            <span>执行模型</span>
            <strong>
              {isMakerStrategy(detail)
                ? "maker_grid_v1 · Maker 限价与条件限价挂单"
                : "市价意图"}
            </strong>
          </div>
          {isMakerStrategy(detail) && <MakerNotice />}
          <h3 className="form-section-title">默认参数</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>参数</th>
                  <th>默认值</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(
                  (detail.manifest || detail).parameters?.properties || {},
                ).map(([key, p]) => (
                  <tr key={key}>
                    <td>{p.title || key}</td>
                    <td>
                      {typeof p.default === "boolean"
                        ? p.default
                          ? "允许"
                          : "不允许"
                        : String(p.default ?? "—")}
                    </td>
                    <td>{p.description || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="modal-footer">
            <Btn
              onClick={() => {
                setDetail(null);
                go("protocol");
              }}
            >
              接入协议
            </Btn>
            <Btn
              kind="primary"
              onClick={() => {
                setSelected(detail);
                setSource(detail.source || "");
                setName(detail.name);
                setDetail(null);
              }}
            >
              编辑策略
            </Btn>
          </div>
        </Modal>
      )}
      {selected && (
        <Modal
          wide
          title={selected.id ? "策略代码与版本" : "添加策略"}
          subtitle="保存会创建新的不可变策略版本，历史实验继续绑定原版本。"
          onClose={() => setSelected(null)}
        >
          {selected.manifest && (
            <div className="strategy-requirements">
              <Shield size={14} />
              <span>
                保证金：
                {(selected.manifest.account?.margin_modes || [])
                  .map(
                    (mode) =>
                      ({ cross: "全仓", isolated: "逐仓", cash: "现货现金" })[
                        mode
                      ] || mode,
                  )
                  .join(" / ") || "—"}
                {" · "}持仓：
                {(selected.manifest.account?.position_modes || [])
                  .map(
                    (mode) =>
                      ({ one_way: "单向持仓", hedge: "双向持仓" })[mode] ||
                      mode,
                  )
                  .join(" / ") || "—"}
              </span>
            </div>
          )}
          <Field label="策略名称" hint="保存时以代码中的 MANIFEST.name 为准">
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <div className="editor-toolbar">
            <span>
              <FileCode2 size={14} /> strategy.py
            </span>
            <label className="text-btn">
              上传 .py 文件
              <input
                className="hidden"
                type="file"
                accept=".py"
                onChange={async (e) => {
                  if (e.target.files[0])
                    setSource(await e.target.files[0].text());
                }}
              />
            </label>
          </div>
          <textarea
            className="code-editor"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            spellCheck={false}
            rows={18}
          />
          {err && <div className="inline-error">{err}</div>}
          <div className="modal-footer">
            <Btn
              icon={Copy}
              onClick={async () => {
                await navigator.clipboard.writeText(source);
                notify("策略代码已复制");
              }}
            >
              复制代码
            </Btn>
            <Btn
              kind="primary"
              icon={busy ? Loader2 : Check}
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setErr("");
                try {
                  await mutate("/strategies", { name, source });
                  notify("策略契约校验通过，新版本已保存");
                  setSelected(null);
                } catch (e) {
                  setErr(e.message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              校验并保存
            </Btn>
          </div>
        </Modal>
      )}
    </>
  );
}
const metricDefs = [
  ["total_return", "总收益率", "%"],
  ["annual_return", "年化收益率", "%"],
  ["max_drawdown", "最大回撤", "%"],
  ["profit_factor", "Profit Factor", "n"],
  ["win_rate", "胜率", "%"],
  ["sharpe", "夏普比率", "n"],
  ["final_equity", "最终资产", "U"],
  ["total_orders", "总订单数", "int"],
  ["long_orders", "多头订单数", "int"],
  ["short_orders", "空头订单数", "int"],
  ["max_consecutive_losses", "最大连续亏损", "int"],
  ["realized_pnl", "已实现收益", "U"],
];
function MetricComparison({ train = {}, validation = {} }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>指标</th>
            <th className="numeric">回测集</th>
            <th className="numeric">验证集</th>
            <th className="numeric">差异</th>
          </tr>
        </thead>
        <tbody>
          {metricDefs.map(([key, label, unit]) => {
            const format = (v) =>
              v == null
                ? "—"
                : unit === "%"
                  ? `${(v * 100).toFixed(2)}%`
                  : unit === "int"
                    ? v
                    : money(v) + (unit === "U" ? " U" : "");
            return (
              <tr key={key}>
                <td>{label}</td>
                <td className="numeric">{format(train[key])}</td>
                <td className="numeric">{format(validation[key])}</td>
                <td className="numeric dim">
                  {train[key] != null && validation[key] != null
                    ? format(validation[key] - train[key])
                    : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
function Backtest({ data, mutate, notify, go }) {
  const strategies = data.strategies || [],
    datasets = data.datasets || [];
  const initialStrategy =
    strategies.find((x) => x.id === sessionStorage.getItem("mk-strategy")) ||
    strategies[0];
  const [sid, setSid] = useState(initialStrategy?.id || ""),
    [params, setParams] = useState({}),
    [preset, setPreset] = useState(""),
    [selectedPreset, setSelectedPreset] = useState(""),
    [presets, setPresets] = useState([]),
    [feeAccount, setFeeAccount] = useState(""),
    [feeBusy, setFeeBusy] = useState(false),
    [feeSource, setFeeSource] = useState("手动配置 / 默认估计"),
    [configTab, setConfigTab] = useState("strategy"),
    [form, setForm] = useState({
      name: "",
      dataset_id: datasets[0]?.id || "",
      symbols: strategySymbols(initialStrategy).join(","),
      initial_capital: 10000,
      fee_rate: isMakerStrategy(initialStrategy) ? 0.0002 : 0.0004,
      slippage_bps: isMakerStrategy(initialStrategy) ? 0 : 2,
      train_start: "2026-09-01T00:00",
      train_end: "2026-09-30T00:00",
      validation_start: "2026-10-01T00:00",
      validation_end: "2026-10-30T00:00",
      seed: 42,
      leverage: 1,
    }),
    [busy, setBusy] = useState(false),
    [active, setActive] = useState(null),
    [err, setErr] = useState("");
  const s = strategies.find((x) => x.id === sid) || strategies[0],
    manifest = s?.manifest || s || {},
    props = manifest.parameters?.properties || {},
    maker = isMakerStrategy(s),
    strategyPresets = presets.filter((item) => item.strategy_id === sid);
  useEffect(() => {
    setSelectedPreset("");
    setPreset("");
    setActive(null);
  }, [sid]);
  const selectStrategy = (id) => {
    const selected = strategies.find((strategy) => strategy.id === id);
    setSid(id);
    setParams({});
    setForm((f) => ({
      ...f,
      symbols: strategySymbols(selected).join(","),
      slippage_bps: isMakerStrategy(selected) ? 0 : 2,
      fee_rate: isMakerStrategy(selected) ? 0.0002 : 0.0004,
    }));
    setFeeSource("手动配置 / 默认估计");
  };
  const change = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  const job =
    (data.experiments || []).find((e) => e.id === active) ||
    (data.experiments || []).find((e) => e.strategy_id === sid);
  const pvalues = Object.fromEntries(
    Object.entries(props).map(([k, p]) => [k, params[k] ?? p.default]),
  );
  useEffect(() => {
    api("/presets")
      .then(setPresets)
      .catch((e) => notify(e.message));
    const raw = sessionStorage.getItem("mk-experiment-copy");
    if (raw) {
      try {
        const copy = JSON.parse(raw);
        setSid(copy.strategy_id);
        setParams(copy.parameters || {});
        setForm((f) => ({
          ...f,
          ...Object.fromEntries(
            Object.keys(f)
              .filter((k) => copy[k] != null)
              .map((k) => [
                k,
                [
                  "train_start",
                  "train_end",
                  "validation_start",
                  "validation_end",
                ].includes(k)
                  ? toBeijingInput(copy[k])
                  : copy[k],
              ]),
          ),
          name: `${copy.name || "实验"} · 副本`,
          symbols: (
            copy.symbols ||
            strategySymbols(strategies.find((x) => x.id === copy.strategy_id))
          ).join(","),
          slippage_bps: isMakerStrategy(
            strategies.find((x) => x.id === copy.strategy_id),
          )
            ? 0
            : Number(copy.slippage_bps ?? f.slippage_bps),
        }));
        setFeeSource("复制实验的费用模型");
      } catch {
        notify("待复制实验读取失败，请重新选择");
      }
      sessionStorage.removeItem("mk-experiment-copy");
    }
  }, []);
  const savePreset = async () => {
    try {
      const title =
        preset.trim() || `${s.name} 参数 ${strategyPresets.length + 1}`;
      await mutate("/presets", {
        name: title,
        strategy_id: sid,
        parameters: pvalues,
      });
      setPresets(await api("/presets"));
      notify("参数预设已保存到服务器");
    } catch {}
  };
  const readFee = async () => {
    setFeeBusy(true);
    try {
      const symbol = form.symbols.split(/[,，\s]+/).filter(Boolean)[0];
      const result = await api(
        `/accounts/${feeAccount}/commission?symbol=${encodeURIComponent(symbol || "")}`,
      );
      const fee = result.rates?.[maker ? "maker" : "taker"];
      if (fee == null || !Number.isFinite(Number(fee)))
        throw new Error(`账户未返回 ${maker ? "Maker" : "Taker"} 费率`);
      change("fee_rate", Number(fee));
      setFeeSource(
        `币安 ${maker ? "Maker" : "Taker"} 账户费率 · ${symbol} · ${formatBeijing(result.fetched_at)} 北京时间`,
      );
      notify(
        result.rates.note ||
          `已读取账户 ${maker ? "Maker" : "Taker"} 费率，仍可手动调整`,
      );
    } catch (e) {
      notify(e.message);
    } finally {
      setFeeBusy(false);
    }
  };
  return (
    <>
      <Heading
        eyebrow="BACKTEST LAB"
        title="回测工作台"
        desc="定义实验，验证假设。训练与验证分离，完整保留每次运行的上下文。"
      />
      <div className="backtest-layout">
        <section className="panel backtest-config">
          <div className="panel-heading">
            <h2>
              <SlidersHorizontal size={17} /> 实验配置
            </h2>
            <span className="config-context">配置</span>
          </div>
          <form
            className="panel-body"
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setErr("");
              try {
                if (
                  [
                    "train_start",
                    "train_end",
                    "validation_start",
                    "validation_end",
                  ].some((key) => !form[key])
                ) {
                  setConfigTab("data");
                  throw new Error("请补全回测集与验证集的北京时间区间");
                }
                const intervals = Object.fromEntries(
                  [
                    "train_start",
                    "train_end",
                    "validation_start",
                    "validation_end",
                  ].map((key) => [key, beijingInputToISO(form[key])]),
                );
                const ranges = Object.values(intervals).map(Date.parse);
                if (
                  !form.dataset_id ||
                  !form.symbols.trim() ||
                  !(
                    ranges[0] < ranges[1] &&
                    ranges[1] <= ranges[2] &&
                    ranges[2] < ranges[3]
                  )
                ) {
                  setConfigTab("data");
                  throw new Error(
                    "请选择数据集与交易对，并按顺序设置不重叠的回测集和验证集区间",
                  );
                }
                if (
                  maker &&
                  form.symbols
                    .split(/[,，\s]+/)
                    .filter(Boolean)
                    .some((symbol) => !strategySymbols(s).includes(symbol))
                ) {
                  setConfigTab("data");
                  throw new Error(
                    `该策略仅允许：${strategySymbols(s).join(" / ")}`,
                  );
                }
                const r = await mutate("/experiments", {
                  ...form,
                  ...intervals,
                  name: form.name.trim() || `${s.name} · 参数实验`,
                  strategy_id: sid,
                  parameters: pvalues,
                  params: pvalues,
                  symbols: form.symbols.split(/[,，\s]+/).filter(Boolean),
                  market: manifest.market || "futures",
                  initial_capital: Number(form.initial_capital),
                  fee_rate: Number(form.fee_rate),
                  slippage_bps: maker ? 0 : Number(form.slippage_bps),
                  seed: Number(form.seed),
                  leverage: Number(form.leverage),
                });
                setActive(r.id);
                notify("回测已入队，独立进程开始后将推送进度");
              } catch (e) {
                setErr(e.message);
              } finally {
                setBusy(false);
              }
            }}
          >
            <div className="backtest-config-scroll">
              <div className="form-grid experiment-identity">
                <Field label="实验名称">
                  <input
                    value={form.name}
                    placeholder="留空按策略命名"
                    onChange={(e) => change("name", e.target.value)}
                  />
                </Field>
                <Field label="策略">
                  <select
                    value={sid}
                    onChange={(e) => {
                      selectStrategy(e.target.value);
                    }}
                  >
                    {strategies.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name} · v{s.version}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              <div className="strategy-requirements">
                <Shield size={14} />
                <span>
                  {manifest.market === "spot"
                    ? "现货 · 禁止做空 · 1×"
                    : "U 本位永续"}{" "}
                  · 最低资金 {manifest.account?.min_capital ?? "—"} U
                </span>
              </div>
              {maker && <MakerNotice compact />}
              <div className="backtest-form-tabs" aria-label="实验配置分组">
                {[
                  ["strategy", "策略与参数"],
                  ["data", "行情与区间"],
                  ["execution", "资金与费用"],
                ].map(([key, label], index) => (
                  <button
                    type="button"
                    key={key}
                    aria-pressed={configTab === key}
                    className={configTab === key ? "active" : ""}
                    onClick={() => setConfigTab(key)}
                  >
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    {label}
                  </button>
                ))}
              </div>
              <fieldset
                className="config-tab-panel"
                hidden={configTab !== "strategy"}
                disabled={configTab !== "strategy"}
              >
                <div className="config-section-grid">
                  <section>
                    <h3 className="form-section-title">
                      策略参数 <span>{Object.keys(props).length}</span>
                    </h3>
                    <div className="form-grid">
                      {Object.entries(props).map(([k, p]) => (
                        <Field
                          key={k}
                          label={p.title || k}
                          hint={p.description}
                        >
                          <ParameterInput
                            spec={p}
                            value={params[k] ?? p.default}
                            onChange={(value) =>
                              setParams({ ...params, [k]: value })
                            }
                          />
                        </Field>
                      ))}
                    </div>
                    {Object.keys(props).length === 0 && (
                      <p className="dim">此策略没有需要调整的参数。</p>
                    )}
                  </section>
                  <section>
                    <h3 className="form-section-title">参数预设</h3>
                    <Field label="参数预设名称">
                      <input
                        placeholder="输入名称后另存为"
                        value={preset}
                        onChange={(e) => setPreset(e.target.value)}
                      />
                    </Field>
                    <div className="preset-row">
                      <select
                        aria-label="加载参数预设"
                        value={selectedPreset}
                        onChange={(e) => {
                          const p = strategyPresets.find(
                            (item) => item.id === e.target.value,
                          );
                          setSelectedPreset(e.target.value);
                          if (!p) return;
                          setParams(p.parameters);
                          notify("预设已加载");
                        }}
                      >
                        <option value="">加载服务器参数预设</option>
                        {strategyPresets.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                      </select>
                      <Btn type="button" icon={Copy} onClick={savePreset}>
                        另存为
                      </Btn>
                    </div>
                  </section>
                </div>
              </fieldset>
              <fieldset
                className="config-tab-panel"
                hidden={configTab !== "data"}
                disabled={configTab !== "data"}
              >
                <div className="form-grid">
                  <Field label="K 线数据集">
                    <select
                      required
                      value={form.dataset_id}
                      onChange={(e) => {
                        change("dataset_id", e.target.value);
                        const d = datasets.find((x) => x.id === e.target.value);
                        if (d?.symbols) {
                          const selectedSymbols = maker
                            ? d.symbols.filter((symbol) =>
                                strategySymbols(s).includes(symbol),
                              )
                            : d.symbols;
                          if (selectedSymbols.length)
                            change("symbols", selectedSymbols.join(","));
                        }
                      }}
                    >
                      <option value="">请选择数据集</option>
                      {datasets.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name}{" "}
                          {["demo", "synthetic"].includes(d.source)
                            ? "[合成数据]"
                            : ""}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field
                    label="交易对"
                    hint={
                      maker
                        ? `可选：${strategySymbols(s).join(" / ")}`
                        : undefined
                    }
                  >
                    <input
                      value={form.symbols}
                      required
                      onChange={(e) =>
                        change("symbols", e.target.value.toUpperCase())
                      }
                    />
                  </Field>
                </div>
                <p className="fee-source">
                  数据覆盖（北京时间）：
                  {formatBeijing(
                    datasets.find((d) => d.id === form.dataset_id)?.start,
                  )}{" "}
                  —{" "}
                  {formatBeijing(
                    datasets.find((d) => d.id === form.dataset_id)?.end,
                  )}
                  ，结束为末根 K 线开盘时间。
                </p>
                <div className="config-section-grid">
                  <div className="date-group">
                    <span>
                      <i />
                      回测集 · 北京时间
                    </span>
                    <div className="form-grid">
                      <Field label="开始">
                        <input
                          type="datetime-local"
                          step="1"
                          required
                          value={form.train_start}
                          onChange={(e) =>
                            change("train_start", e.target.value)
                          }
                        />
                      </Field>
                      <Field label="结束（不含）">
                        <input
                          type="datetime-local"
                          step="1"
                          required
                          value={form.train_end}
                          onChange={(e) => change("train_end", e.target.value)}
                        />
                      </Field>
                    </div>
                  </div>
                  <div className="date-group validation">
                    <span>
                      <i />
                      验证集 · 北京时间
                    </span>
                    <div className="form-grid">
                      <Field label="开始">
                        <input
                          type="datetime-local"
                          step="1"
                          min={form.train_end}
                          required
                          value={form.validation_start}
                          onChange={(e) =>
                            change("validation_start", e.target.value)
                          }
                        />
                      </Field>
                      <Field label="结束（不含）">
                        <input
                          type="datetime-local"
                          step="1"
                          required
                          value={form.validation_end}
                          onChange={(e) =>
                            change("validation_end", e.target.value)
                          }
                        />
                      </Field>
                    </div>
                  </div>
                </div>
              </fieldset>
              <fieldset
                className="config-tab-panel"
                hidden={configTab !== "execution"}
                disabled={configTab !== "execution"}
              >
                <div className="config-section-grid">
                  <section>
                    <h3 className="form-section-title">账户与实验环境</h3>
                    <div className="form-grid">
                      <Field label="初始资金（U）">
                        <input
                          type="number"
                          min={manifest.account?.min_capital || 1}
                          required
                          value={form.initial_capital}
                          onChange={(e) =>
                            change("initial_capital", e.target.value)
                          }
                        />
                      </Field>
                      <Field label="杠杆">
                        <input
                          type="number"
                          min="1"
                          max={
                            manifest.market === "spot"
                              ? 1
                              : manifest.account?.leverage_max || 1
                          }
                          value={form.leverage}
                          onChange={(e) => change("leverage", e.target.value)}
                        />
                      </Field>
                    </div>
                    <Field
                      label="随机种子"
                      hint="与策略、参数、数据版本一同保存，便于复现实验"
                    >
                      <input
                        type="number"
                        step="1"
                        value={form.seed}
                        onChange={(e) => change("seed", e.target.value)}
                      />
                    </Field>
                  </section>
                  <section>
                    <h3 className="form-section-title">交易成本</h3>
                    <Field
                      label="读取币安账户费率"
                      hint="个人费率需要已接入的实盘凭证，公开 K 线接口不提供个人费率。"
                    >
                      <div className="preset-row">
                        <select
                          value={feeAccount}
                          onChange={(e) => setFeeAccount(e.target.value)}
                        >
                          <option value="">选择具备凭证的账户</option>
                          {(data.accounts || [])
                            .filter(
                              (a) => a.mode === "live" && a.has_credentials,
                            )
                            .map((a) => (
                              <option key={a.id} value={a.id}>
                                {a.name}
                              </option>
                            ))}
                        </select>
                        <Btn
                          type="button"
                          icon={feeBusy ? Loader2 : RefreshCw}
                          disabled={!feeAccount || feeBusy}
                          onClick={readFee}
                        >
                          读取
                        </Btn>
                      </div>
                    </Field>
                    <p className="fee-source">当前费率来源：{feeSource}</p>
                    <div className="form-grid">
                      <Field
                        label={maker ? "Maker 手续费率" : "Taker 手续费率"}
                        hint="默认值为估计，请按账户实际费率修改"
                      >
                        <input
                          type="number"
                          min="0"
                          max="0.1"
                          step="0.00001"
                          value={form.fee_rate}
                          onChange={(e) => {
                            change("fee_rate", e.target.value);
                            setFeeSource("手动配置");
                          }}
                        />
                      </Field>
                      <Field
                        label="滑点（基点）"
                        hint={
                          maker
                            ? "Maker 限价模型固定为 0；成交受挂单与触发规则约束"
                            : "1 bp = 0.01%"
                        }
                      >
                        <input
                          type="number"
                          min="0"
                          step="0.1"
                          value={maker ? 0 : form.slippage_bps}
                          disabled={maker}
                          onChange={(e) =>
                            change("slippage_bps", e.target.value)
                          }
                        />
                      </Field>
                    </div>
                  </section>
                </div>
              </fieldset>
            </div>
            {err && <div className="inline-error">{err}</div>}
            <div className="experiment-summary">
              <div>
                <strong>{s?.name || "请选择策略"}</strong>
                <span>
                  {form.symbols || "未选交易对"} · {money(form.initial_capital)}{" "}
                  U · {form.leverage}× · {maker ? "Maker" : "Taker"} 费率{" "}
                  {pct(Number(form.fee_rate) * 100)}
                </span>
              </div>
              <Btn
                kind="primary"
                disabled={busy || !s}
                icon={busy ? Loader2 : Play}
              >
                {busy ? "正在提交" : "开始回测"}
              </Btn>
            </div>
          </form>
        </section>
        <div className="backtest-results">
          <section className="panel result-overview">
            <div className="panel-heading">
              <div>
                <h2>实验结果</h2>
                {!job && <p>选择策略与数据，开始第一次实验。</p>}
              </div>
              {job && <Badge status={job.status} />}
            </div>
            <div className="result-metrics">
              {[
                ["最终权益", job?.metrics?.final_equity, "U"],
                [
                  "总收益率",
                  job?.metrics?.total_return != null
                    ? job.metrics.total_return * 100
                    : null,
                  "%",
                ],
                [
                  "最大回撤",
                  job?.metrics?.max_drawdown != null
                    ? job.metrics.max_drawdown * 100
                    : null,
                  "%",
                ],
                ["成交订单", job?.metrics?.total_orders, "笔"],
              ].map(([label, value, unit]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong
                    className={
                      label === "总收益率"
                        ? valueTone(value)
                        : label === "最大回撤" && value > 0
                          ? "negative"
                          : ""
                    }
                  >
                    {value == null
                      ? "—"
                      : unit === "%"
                        ? `${Number(value).toFixed(2)}`
                        : unit === "笔"
                          ? value
                          : money(value)}
                    <small>{unit}</small>
                  </strong>
                </div>
              ))}
            </div>
            {job && job.status !== "completed" && (
              <p className="result-incomplete">
                {["queued", "running"].includes(job.status)
                  ? "实验进行中，统计结果尚未完成。"
                  : "实验未完成，详情可在实验记录中查看。"}
              </p>
            )}
          </section>
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>权益变化</h2>
                {!job && <p>运行回测后显示结果</p>}
              </div>
              {job && <Badge status={job.status} />}
            </div>
            {job && ["queued", "running"].includes(job.status) && (
              <div className="progress-block">
                <div>
                  <span>运行中，结果未完成</span>
                  <span>{Math.round((job.progress || 0) * 100)}%</span>
                </div>
                <progress max="1" value={job.progress || 0} />
              </div>
            )}
            {job ? (
              <Chart data={job.curve || []} height={320} />
            ) : (
              <Empty
                icon={FlaskConical}
                title="等待运行回测"
                desc="设置左侧参数后点击「开始回测」，这里会显示权益曲线与实验结果。"
              />
            )}
            {job?.curve_sampling && (
              <p className="chart-data-note">
                概览按区间保留极值 · 实验详情包含完整数据
              </p>
            )}
            {job?.error && <div className="inline-error">{job.error}</div>}
          </section>
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>回测集与验证集对比</h2>
                <p>完整结果仅在任务完成后统计</p>
              </div>
              {job && (
                <Btn
                  icon={Download}
                  onClick={() => exportJson(`experiment-${job.id}.json`, job)}
                >
                  导出
                </Btn>
              )}
            </div>
            <MetricComparison
              train={job?.metrics}
              validation={job?.validation_metrics}
            />
          </section>
          <div className="notice">
            <GitCompareArrows size={18} />
            <div>
              <strong>保持验证集独立</strong>
              <span>
                回测和验证均从独立初始资金开始。数据版本、策略版本、参数和执行环境随实验保存。
              </span>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

function Experiments({ data, mutate, notify, go }) {
  const [chosen, setChosen] = useState([]),
    [detail, setDetail] = useState(null),
    [filter, setFilter] = useState("all"),
    [tab, setTab] = useState("metrics"),
    [batch, setBatch] = useState(false),
    [grid, setGrid] = useState('{"fast_window": [8, 12, 16]}'),
    [busy, setBusy] = useState(false);
  const list = (data.experiments || []).filter(
    (e) => filter === "all" || e.status === filter,
  );
  const load = async (e) => {
    try {
      setDetail(await api(`/experiments/${e.id}`));
      setTab("metrics");
    } catch (err) {
      notify(err.message);
    }
  };
  const compared = list.filter((e) => chosen.includes(e.id));
  return (
    <>
      <Heading
        eyebrow="EXPERIMENT TRACKING"
        title="实验记录"
        desc="把调参变成可追溯的研究。比较结果，也比较产生结果的条件。"
      >
        <Btn
          icon={GitCompareArrows}
          disabled={chosen.length !== 1}
          onClick={() => setBatch(true)}
        >
          批量参数实验
        </Btn>
        <Btn kind="primary" icon={Plus} onClick={() => go("backtest")}>
          新建实验
        </Btn>
      </Heading>
      <section className="panel">
        <div className="panel-toolbar">
          <div className="tabs">
            {[
              ["all", "全部实验"],
              ["completed", "已完成"],
              ["running", "运行中"],
              ["queued", "排队中"],
              ["failed", "失败"],
            ].map(([k, t]) => (
              <button
                className={filter === k ? "active" : ""}
                key={k}
                onClick={() => setFilter(k)}
              >
                {t}
              </button>
            ))}
          </div>
          <span className="dim">选择 2–4 项并排比较</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th />
                <th>实验 / 策略</th>
                <th>创建时间 · 北京时间</th>
                <th>状态</th>
                <th className="numeric">回测收益</th>
                <th className="numeric">验证收益</th>
                <th className="numeric">最大回撤</th>
                <th className="numeric">夏普</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {list.map((e) => (
                <tr key={e.id}>
                  <td>
                    <input
                      aria-label={`选择 ${e.name || e.id}`}
                      type="checkbox"
                      checked={chosen.includes(e.id)}
                      disabled={chosen.length >= 4 && !chosen.includes(e.id)}
                      onChange={() =>
                        setChosen(
                          chosen.includes(e.id)
                            ? chosen.filter((x) => x !== e.id)
                            : [...chosen, e.id],
                        )
                      }
                    />
                  </td>
                  <td>
                    <button className="table-name" onClick={() => load(e)}>
                      <strong>{e.name || e.strategy_name}</strong>
                      <small>
                        {e.strategy_name}
                        {e.illustrative ? " · 示例" : ""} ·{" "}
                        <code>{e.id.slice(0, 8)}</code>
                      </small>
                    </button>
                  </td>
                  <td className="mono">
                    {formatBeijing(e.created_at, { second: "2-digit" })}
                  </td>
                  <td>
                    <Badge status={e.status} />
                  </td>
                  <td
                    className={`mono numeric ${valueTone(e.metrics?.total_return)}`}
                  >
                    {pct(
                      e.metrics?.total_return != null
                        ? e.metrics.total_return * 100
                        : null,
                    )}
                  </td>
                  <td
                    className={`mono numeric ${valueTone(e.validation_metrics?.total_return)}`}
                  >
                    {pct(
                      e.validation_metrics?.total_return != null
                        ? e.validation_metrics.total_return * 100
                        : null,
                    )}
                  </td>
                  <td
                    className={`mono numeric ${e.metrics?.max_drawdown > 0 ? "negative" : ""}`}
                  >
                    {e.metrics?.max_drawdown != null
                      ? `${(e.metrics.max_drawdown * 100).toFixed(2)}%`
                      : "—"}
                  </td>
                  <td className="numeric">{money(e.metrics?.sharpe)}</td>
                  <td>
                    <div className="row-buttons">
                      <button
                        className="icon-btn"
                        aria-label="实验详情"
                        onClick={() => load(e)}
                      >
                        <ArrowUpRight size={17} />
                      </button>
                      {["queued", "running"].includes(e.status) ? (
                        <button
                          className="icon-btn"
                          aria-label="取消任务"
                          onClick={async () => {
                            try {
                              await mutate(`/experiments/${e.id}/cancel`, {});
                              notify("取消请求已保存");
                            } catch {}
                          }}
                        >
                          <XCircle size={16} />
                        </button>
                      ) : (
                        <button
                          className="icon-btn"
                          aria-label="重跑实验"
                          onClick={async () => {
                            try {
                              await mutate(`/experiments/${e.id}/retry`, {});
                              notify("已创建重跑任务");
                            } catch {}
                          }}
                        >
                          <RefreshCw size={15} />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!list.length && <Empty title="没有符合条件的实验" />}
      </section>
      {compared.length > 1 && (
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>并排比较</h2>
              <p>不同数据集和费用模型可能导致不可直接比较的结果</p>
            </div>
            <Btn
              icon={Download}
              onClick={() => exportJson("experiment-comparison.json", compared)}
            >
              导出对比
            </Btn>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>指标 / 参数</th>
                  {compared.map((e) => (
                    <th className="numeric" key={e.id}>
                      {e.name || e.id.slice(0, 8)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {metricDefs.map(([key, label, unit]) => (
                  <tr key={key}>
                    <td>{label}</td>
                    {compared.map((e) => (
                      <td className="numeric" key={e.id}>
                        {e.metrics?.[key] != null
                          ? unit === "%"
                            ? `${(e.metrics[key] * 100).toFixed(2)}%`
                            : money(e.metrics[key])
                          : "—"}
                      </td>
                    ))}
                  </tr>
                ))}
                {[
                  ...new Set(
                    compared.flatMap((e) => Object.keys(e.parameters || {})),
                  ),
                ].map((k) => (
                  <tr className="parameter-row" key={k}>
                    <td>参数 · {k}</td>
                    {compared.map((e) => (
                      <td key={e.id}>
                        <code>{JSON.stringify(e.parameters?.[k]) ?? "—"}</code>
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {detail && (
        <Modal
          wide
          title={detail.name || "实验详情"}
          subtitle={`${detail.strategy_name} · ${detail.id}`}
          onClose={() => setDetail(null)}
        >
          <div className="experiment-times">
            <div>
              <span>创建时间 · 北京时间</span>
              <strong>
                {formatBeijing(detail.created_at, { second: "2-digit" })}
              </strong>
            </div>
            <div>
              <span>开始执行 · 北京时间</span>
              <strong>
                {formatBeijing(detail.started_at, { second: "2-digit" })}
              </strong>
            </div>
            <div>
              <span>完成时间 · 北京时间</span>
              <strong>
                {formatBeijing(detail.completed_at, { second: "2-digit" })}
              </strong>
            </div>
            <div>
              <span>回测集 · 北京时间（结束不含）</span>
              <strong>
                {formatBeijing(detail.train_start)} —{" "}
                {formatBeijing(detail.train_end)}
              </strong>
            </div>
            <div>
              <span>验证集 · 北京时间（结束不含）</span>
              <strong>
                {formatBeijing(detail.validation_start)} —{" "}
                {formatBeijing(detail.validation_end)}
              </strong>
            </div>
          </div>
          <div className="tabs modal-tabs">
            {[
              ["metrics", "指标对比"],
              ["provenance", "版本快照"],
              ["decisions", "决策追踪"],
              ["orders", "交易记录"],
            ].map(([k, v]) => (
              <button
                key={k}
                className={tab === k ? "active" : ""}
                onClick={() => setTab(k)}
              >
                {v}
              </button>
            ))}
          </div>
          {detail.error && (
            <div className="inline-error">失败原因：{detail.error}</div>
          )}
          {tab === "metrics" && (
            <>
              <Chart data={detail.curve || []} height={200} />
              <MetricComparison
                train={detail.metrics}
                validation={detail.validation_metrics}
              />
            </>
          )}
          {tab === "provenance" && (
            <pre className="code-block">
              {JSON.stringify(
                displayBeijingTimes({
                  provenance: detail.provenance,
                  assumptions: detail.assumptions,
                }),
                null,
                2,
              )}
            </pre>
          )}
          {tab === "decisions" && (
            <pre className="code-block">
              {JSON.stringify(
                displayBeijingTimes(
                  detail.decisions || detail.result?.decisions || [],
                ),
                null,
                2,
              )}
            </pre>
          )}
          {tab === "orders" && (
            <pre className="code-block">
              {JSON.stringify(
                displayBeijingTimes(
                  detail.orders || detail.result?.orders || [],
                ),
                null,
                2,
              )}
            </pre>
          )}
          <div className="modal-footer">
            <Btn
              icon={Copy}
              onClick={() => {
                sessionStorage.setItem("mk-strategy", detail.strategy_id);
                sessionStorage.setItem(
                  "mk-experiment-copy",
                  JSON.stringify(experimentConfig(detail)),
                );
                setDetail(null);
                go("backtest");
                notify(
                  "策略、参数、数据、区间与费用已复制，可修改后运行新实验",
                );
              }}
            >
              复制为新实验
            </Btn>
            <Btn
              icon={Download}
              onClick={() =>
                downloadFile(
                  `/api/experiments/${detail.id}/export?format=csv`,
                  `experiment-${detail.id}.csv`,
                )
              }
            >
              导出成交 CSV
            </Btn>
            <Btn
              icon={Download}
              onClick={() => exportJson(`experiment-${detail.id}.json`, detail)}
            >
              导出完整报告
            </Btn>
          </div>
        </Modal>
      )}
      {batch && (
        <Modal
          title="批量参数实验"
          subtitle="基于选中的实验配置运行参数组合，最多 50 组。"
          onClose={() => setBatch(false)}
        >
          <Field
            label="参数网格（JSON）"
            hint="每个参数填写候选值数组，例如 fast_window: [8, 12]"
          >
            <textarea
              rows={6}
              className="code-editor"
              value={grid}
              onChange={(e) => setGrid(e.target.value)}
            />
          </Field>
          <div className="modal-footer">
            <Btn onClick={() => setBatch(false)}>取消</Btn>
            <Btn
              kind="primary"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  const base = await api(`/experiments/${chosen[0]}`);
                  const result = await mutate("/experiments", {
                    ...experimentConfig(base),
                    parameters: base.parameters,
                    parameter_grid: JSON.parse(grid),
                    name: `${base.name} · 批量`,
                  });
                  notify(
                    `批量任务已创建 ${result.experiments?.length || result.ids?.length || ""}`,
                  );
                  setBatch(false);
                } catch (e) {
                  notify(e.message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              创建实验队列
            </Btn>
          </div>
        </Modal>
      )}
    </>
  );
}
function Logs({ logs, strategies }) {
  const [level, setLevel] = useState("all"),
    [strategy, setStrategy] = useState("all"),
    [q, setQ] = useState("");
  const list = logs.filter(
    (l) =>
      (level === "all" || l.level === level) &&
      (strategy === "all" || l.strategy_id === strategy) &&
      `${l.message} ${l.account_name || ""}`
        .toLowerCase()
        .includes(q.toLowerCase()),
  );
  return (
    <>
      <Heading
        eyebrow="OBSERVABILITY"
        title="运行日志"
        desc="从策略决策到执行结果，定位每一步发生了什么。"
      >
        <Btn icon={Download} onClick={() => exportJson("mk-logs.json", list)}>
          导出日志
        </Btn>
      </Heading>
      <section className="panel">
        <div className="panel-toolbar log-filters">
          <div className="segmented">
            {[
              ["all", "全部"],
              ["error", "错误"],
              ["debug", "调试"],
            ].map(([k, v]) => (
              <button
                key={k}
                className={level === k ? "selected" : ""}
                onClick={() => setLevel(k)}
              >
                {v}
              </button>
            ))}
          </div>
          <select
            aria-label="按策略筛选"
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
          >
            <option value="all">所有策略</option>
            {strategies.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          <label className="search-input">
            <Search size={15} />
            <input
              placeholder="搜索日志内容"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </label>
        </div>
        <div className="log-console">
          {list.length ? (
            list.map((l, i) => (
              <div className="log-line" key={l.id || i}>
                <time>
                  {formatBeijing(l.created_at || l.timestamp, {
                    second: "2-digit",
                  })}
                </time>
                <span
                  className={`log-level ${l.level === "error" ? "negative" : "blue"}`}
                >
                  {(l.level || "debug").toUpperCase()}
                </span>
                <span className="log-source">
                  {l.strategy_name || l.source || "framework"}
                </span>
                <span className="log-message">{l.message}</span>
              </div>
            ))
          ) : (
            <Empty icon={Terminal} title="暂无匹配的日志" />
          )}
        </div>
        <div className="panel-bottom">
          <span className="dim">{list.length} 条记录 · 凭证不会写入日志</span>
          <span className="mono dim">SYSTEM EVENT STREAM</span>
        </div>
      </section>
    </>
  );
}
const protocolExample = `MANIFEST = {
    "name": "My Strategy", "version": "1.0.0",
    "description": "策略说明", "market": "spot",
    "symbols": {"mode": "any", "values": [], "min_count": 1},
    "account": {
        "position_modes": ["one_way"], "margin_modes": ["cash"],
        "leverage_min": 1, "leverage_max": 1, "min_capital": 100
    },
    "parameters": {"type": "object", "properties": {
        "quantity": {"type": "number", "default": 0.001, "minimum": 0.0001}
    }}
}

class Strategy:
    def on_bar(self, context):
        # 只能读取当前与历史数据，意图在下一根 K 线开盘执行
        symbol = next(iter(context["candles"]))
        context["log"]("debug", "等待入场条件")
        return []
        # return [{"symbol": symbol, "side": "buy",
        #          "quantity": context["params"]["quantity"],
        #          "reduce_only": False}]
`;
function Todo() {
  return (
    <>
      <Heading title="to-do" desc="框架能力建设建议与策略职责。" />
      <div className="todo-grid">
        <section className="panel panel-body todo-panel">
          <h2>建议框架提供：</h2>
          <ul>
            <li>行情订阅、历史窗口、指标预热。</li>
            <li>统一时钟、定时任务和事件回调。</li>
            <li>账户余额、可用资金、持仓和订单查询。</li>
            <li>下单、撤单、改单、订单分组管理。</li>
            <li>价格和数量规范化。</li>
            <li>手续费、资金费、保证金和风险检查。</li>
            <li>状态保存、恢复、日志、指标和报表。</li>
            <li>常用指标与仓位计算工具，统一算法定义。</li>
          </ul>
        </section>
        <section className="panel panel-body todo-panel">
          <h2>策略负责：</h2>
          <ul>
            <li>什么时候交易。</li>
            <li>交易哪些币。</li>
            <li>目标仓位或挂单结构。</li>
            <li>入场、加减仓、退出逻辑。</li>
            <li>策略特有的状态与解释信息。</li>
          </ul>
        </section>
      </div>
    </>
  );
}
function Protocol({ notify }) {
  const [protocol, setProtocol] = useState(null);
  useEffect(() => {
    api("/protocol")
      .then(setProtocol)
      .catch(() => {});
  }, []);
  const source = protocol?.example || protocolExample;
  return (
    <>
      <Heading
        eyebrow="STRATEGY SDK"
        title="策略接入协议"
        desc="把交易逻辑写进策略，把行情、精度、风控和账本交给框架。"
      >
        <Btn
          icon={Copy}
          kind="primary"
          onClick={async () => {
            await copyText(
              typeof source === "string" ? source : protocolExample,
            );
            notify("策略模板已复制");
          }}
        >
          复制接入模板
        </Btn>
      </Heading>
      <div id="protocol-content">
        <div className="protocol-hero">
          <div className="protocol-icon">
            <Code2 size={34} />
          </div>
          <div>
            <span className="tag mono">CONTRACT v1</span>
            <h2>一个接口，贯穿研究与执行。</h2>
            <p>
              策略输出交易意图。框架校验约束、处理成交、记录决策，并计算统一指标。
            </p>
          </div>
        </div>
        <div className="protocol-flow">
          {[
            ["行情与时钟", Database],
            ["Strategy.on_bar", Code2],
            ["账户风控", Shield],
            ["执行器与账本", Wallet],
          ].map(([t, Icon], i) => (
            <React.Fragment key={t}>
              {i > 0 && <ArrowRight size={18} />}
              <div>
                <Icon size={19} />
                {t}
              </div>
            </React.Fragment>
          ))}
        </div>
        <div className="split-layout protocol-layout">
          <section className="panel">
            <div className="panel-heading">
              <h2>必须声明的字段</h2>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>字段</th>
                    <th>用途 / 约束</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    [
                      "name / version / description",
                      "策略名称、不可变版本、描述",
                    ],
                    ["market", "spot 或 futures；现货禁止杠杆与做空"],
                    ["symbols", "固定币种、多币种数量或不限币种"],
                    ["account", "持仓模式、保证金模式、杠杆范围、最低 U 资金"],
                    [
                      "parameters",
                      "JSON Schema 参数定义、默认值与 description 帮助说明",
                    ],
                    [
                      "execution.model",
                      "maker_grid_v1 使用 Maker 限价、条件限价与双向订单模型；其他策略沿用原执行器",
                    ],
                    [
                      "Strategy.on_bar(context)",
                      "返回交易意图列表；不直接访问交易所",
                    ],
                    ['context["log"]', "debug / error 级别结构化日志"],
                    ['context["emit"]', "策略阶段、指标、图表线和决策原因"],
                  ].map(([k, v]) => (
                    <tr key={k}>
                      <td>
                        <code>{k}</code>
                      </td>
                      <td>{v}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="panel-body">
              <div className="notice amber">
                <ShieldAlert size={18} />
                <div>
                  <strong>执行边界</strong>
                  <span>
                    市价策略沿用单向净持仓模型；maker_grid_v1 的 Maker
                    限价、条件限价、订单关联与双向持仓已支持回测和模拟。OHLC
                    无法还原盘口排队或保证实盘 Maker
                    成交；止损触发后仍可能拒单或未成交，无 Taker
                    后备。资金费与强平模型仍不完整，实盘执行继续受验收闸门保护。
                  </span>
                </div>
              </div>
            </div>
          </section>
          <section className="panel code-panel">
            <div className="editor-toolbar">
              <span>
                <FileCode2 size={15} /> strategy_template.py
              </span>
              <span>PYTHON</span>
            </div>
            <pre className="code-block">
              {typeof source === "string" ? source : protocolExample}
            </pre>
          </section>
        </div>
      </div>
    </>
  );
}

function ParameterInput({ spec, value, onChange }) {
  if (spec.type === "boolean")
    return (
      <select
        value={String(Boolean(value))}
        onChange={(e) => onChange(e.target.value === "true")}
      >
        <option value="true">允许</option>
        <option value="false">不允许</option>
      </select>
    );
  if (spec.enum)
    return (
      <select
        value={value ?? spec.default}
        onChange={(e) =>
          onChange(
            spec.type === "string" ? e.target.value : Number(e.target.value),
          )
        }
      >
        {spec.enum.map((v) => (
          <option key={String(v)} value={v}>
            {String(v)}
          </option>
        ))}
      </select>
    );
  if (spec.type === "string")
    return (
      <input value={value ?? ""} onChange={(e) => onChange(e.target.value)} />
    );
  return (
    <input
      type="number"
      step={spec.type === "integer" ? 1 : "any"}
      min={spec.minimum}
      max={spec.maximum}
      value={value ?? ""}
      required
      onChange={(e) =>
        onChange(e.target.value === "" ? "" : Number(e.target.value))
      }
    />
  );
}
function AccountCheck({ account, mutate, notify, onClose }) {
  const [report, setReport] = useState(account.check_result),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [confirm, setConfirm] = useState(false);
  const run = async (apply = false) => {
    setBusy(true);
    setError("");
    try {
      const result = await mutate(
        `/accounts/${account.id}/check`,
        apply ? { apply_adjustments: true } : {},
      );
      setReport(result);
      setConfirm(false);
      if (apply) notify("账户设置调整结果已返回，请核对检查项与调整记录");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    run();
  }, []);
  const names = {
    position_mode: "持仓模式",
    margin_mode: "保证金模式",
    leverage: "杠杆",
  };
  return (
    <Modal
      wide
      title={`${account.name} · 账户接入检查`}
      subtitle="凭证、交易权限、资产、仓位、挂单与币种设置分别核验。"
      onClose={onClose}
    >
      {busy && (
        <div className="notice">
          <Loader2 className="spin" size={17} />
          正在查询币安账户，请等待接口返回。
        </div>
      )}
      {report && (
        <>
          <div className="check-summary">
            <Badge status={report.ok ? "passed" : "failed"}>
              {report.ok ? "全部检查通过" : "仍有未通过或未知项"}
            </Badge>
            <span className="dim">
              {report.checked_at
                ? `${formatBeijing(report.checked_at)} 北京时间`
                : "尚未查询"}
            </span>
          </div>
          <div className="check-list">
            {(report.checks || []).map((c, i) => (
              <div className="check-item" key={i}>
                <Badge status={c.status}>
                  {c.status === "pass"
                    ? "通过"
                    : c.status === "fail"
                      ? "未通过"
                      : "未知"}
                </Badge>
                <div>
                  <strong>{c.name}</strong>
                  <p>{c.message}</p>
                </div>
              </div>
            ))}
          </div>
          {!!report.adjustments?.length && (
            <>
              <h3 className="form-section-title">建议调整</h3>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>设置</th>
                      <th>交易对</th>
                      <th>当前</th>
                      <th>目标</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.adjustments.map((a, i) => (
                      <tr key={i}>
                        <td>{names[a.setting] || a.setting}</td>
                        <td>{a.symbol || "账户级"}</td>
                        <td>{String(a.current ?? "未知")}</td>
                        <td>{String(a.target)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
          {!!(report.applied_adjustments || report.applied)?.length && (
            <>
              <h3 className="form-section-title">已执行的调整</h3>
              <pre className="code-block">
                {JSON.stringify(
                  report.applied_adjustments || report.applied,
                  null,
                  2,
                )}
              </pre>
            </>
          )}
          <details className="report-details">
            <summary>查看资产、持仓、挂单及账户设置原始报告</summary>
            <pre className="code-block">
              {JSON.stringify(
                {
                  balances: report.balances,
                  positions: report.positions,
                  orders: report.orders,
                  settings: report.settings,
                },
                null,
                2,
              )}
            </pre>
          </details>
        </>
      )}
      {confirm && (
        <div className="notice amber">
          <ShieldAlert size={18} />
          <div>
            <strong>确认调整以上币安账户设置</strong>
            <span>
              会改变交易所实际杠杆或持仓/保证金模式。存在仓位、挂单或未知检查项时，服务端会拒绝调整。
            </span>
          </div>
        </div>
      )}
      {error && <div className="inline-error">{error}</div>}
      <div className="modal-footer">
        <Btn onClick={onClose}>关闭</Btn>
        <Btn icon={RefreshCw} disabled={busy} onClick={() => run()}>
          重新检查
        </Btn>
        {!!report?.adjustments?.length && (
          <Btn
            kind={confirm ? "danger" : "primary"}
            disabled={busy}
            onClick={() => (confirm ? run(true) : setConfirm(true))}
          >
            {confirm ? "确认调整账户设置" : "应用建议设置"}
          </Btn>
        )}
      </div>
    </Modal>
  );
}

function downloadFile(url, name) {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
}
function exportCurveSvg(container) {
  const source = container?.querySelector("svg.recharts-surface");
  if (!source) return;
  const svg = source.cloneNode(true);
  svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  const themeStyles = getComputedStyle(document.documentElement);
  svg.style.background = themeStyles.getPropertyValue("--panel").trim();
  svg.style.fontFamily = "sans-serif";
  const serialized = new XMLSerializer()
    .serializeToString(svg)
    .replace(/var\((--[a-z-]+)\)/g, (_, name) =>
      themeStyles.getPropertyValue(name).trim(),
    );
  const blob = new Blob([serialized], {
    type: "image/svg+xml;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  downloadFile(url, "mk-equity-curve.svg");
  setTimeout(() => URL.revokeObjectURL(url), 500);
}
function experimentConfig(experiment) {
  const keys = [
    "name",
    "strategy_id",
    "dataset_id",
    "symbols",
    "parameters",
    "market",
    "initial_capital",
    "fee_rate",
    "slippage_bps",
    "train_start",
    "train_end",
    "validation_start",
    "validation_end",
    "seed",
    "leverage",
  ];
  return Object.fromEntries(
    keys
      .filter((key) => experiment[key] !== undefined)
      .map((key) => [key, experiment[key]]),
  );
}
