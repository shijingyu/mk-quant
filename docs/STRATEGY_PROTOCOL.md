# MK Quant 策略接入协议 v1

协议与引擎版本：`1.0.0`。本文以 `backend/strategy.py`、`backend/engine.py`、`backend/worker.py` 的当前实现为准，适用于开发者及协助编写策略的 AI。

策略负责根据已完成的行情生成交易意图。框架负责参数校验、行情对齐、数量及价格规则、资金检查、模拟成交、账本、指标、日志与任务状态。策略不读取 API Key，不连接交易所，不创建线程，也不自行下单。

当前可执行环境是历史回测和持续模拟。正式实盘尚未接入运行主循环，管理端无法启动真实交易；存在 Binance 下单适配器并不代表完整实盘系统已经验收。具体边界见 [验收清单](ACCEPTANCE.md) 与 [Binance 适配器说明](BINANCE.md)。

## 1. 最小可用策略文件

下面的完整 Python 源码可通过当前静态检查。示例用于说明协议，不代表已经验证的盈利策略。它仅做多，在均线下方减仓；`self.calls` 示范可持久化的实例状态。

```python
MANIFEST = {
    "id": "example_trend",
    "name": "接入示例 · 均线做多",
    "version": "1.0.0",
    "description": "收盘价高于均线时做多，低于均线时平仓，下一根开盘撮合。",
    "market": "futures",
    "symbols": {"mode": "fixed", "values": ["BTCUSDT"], "min_count": 1},
    "account": {
        "position_modes": ["one_way"],
        "margin_modes": ["cross"],
        "leverage_min": 1,
        "leverage_max": 1,
        "min_capital": 100,
    },
    "history_window": 200,
    "parameters": {
        "type": "object",
        "properties": {
            "period": {
                "type": "integer", "default": 20,
                "minimum": 2, "maximum": 200, "title": "均线周期",
            },
            "allocation": {
                "type": "number", "default": 0.2,
                "minimum": 0.01, "maximum": 0.8, "title": "可用资金投入比例",
            },
        },
        "required": ["period", "allocation"],
    },
}


class Strategy:
    def __init__(self):
        self.calls = 0

    def on_bar(self, context):
        self.calls += 1
        symbol = "BTCUSDT"
        bars = context["history"][symbol]
        period = context["params"]["period"]
        if len(bars) < period:
            context["emit"](stage="预热", reason="历史 K 线不足")
            return []

        average = sum(bar["close"] for bar in bars[-period:]) / period
        close = context["candles"][symbol]["close"]
        position = context["positions"].get(symbol, {})
        quantity = position.get("quantity", 0)
        context["emit"](
            stage="信号计算",
            indicators={symbol: {"moving_average": average, "close": close}},
            reason="检查均线方向及现有持仓",
        )

        if close < average and quantity > 0:
            context["log"]("debug", "收盘价跌破均线，生成减仓意图", {"symbol": symbol})
            return [{
                "symbol": symbol, "side": "sell", "quantity": quantity,
                "reduce_only": True, "reason": "跌破均线",
            }]
        if close > average and quantity == 0:
            budget = context["account"]["available"] * context["params"]["allocation"]
            if budget > 0:
                return [{
                    "symbol": symbol, "side": "buy", "quantity": budget / close,
                    "reduce_only": False, "reason": "收盘价高于均线且当前空仓",
                }]
        context["emit"](reason="方向未变化或已有持仓，本根不交易")
        return []
```

当前内置示例还包括 `backend/strategies/momentum.py` 的双均线多空策略，以及 `backend/strategies/spot_dca.py` 的现货定投策略。

## 2. MANIFEST 字段

`MANIFEST` 必须是顶层可由 `ast.literal_eval` 读取的字典常量，不能调用函数生成。文件必须且只能有一个 `MANIFEST` 和一个名为 `Strategy` 的类。

| 字段 | 必需 | 约束与含义 |
| --- | --- | --- |
| `id` | 否 | 内置策略的标识。上传接口另生成不可猜测的数据库 ID，不把该值用作权限凭据。 |
| `name` | 是 | 非空策略名称。 |
| `version` | 是 | 非空版本字符串。建议每次逻辑变更更新版本；实验同时绑定实际源码 SHA-256。 |
| `description` | 是 | 非空说明，写清交易逻辑及适用条件。 |
| `market` | 是 | `futures` 为 USDⓈ-M 永续；`spot` 为无借贷现货。 |
| `symbols` | 是 | `{mode, values, min_count}`，下文说明。 |
| `account` | 是 | 持仓模式、保证金模式、杠杆范围、最低资金，所有字段均须声明。 |
| `parameters` | 是 | 当前支持的 JSON Schema 子集，必须为 `type: object`。 |
| `history_window` | 否 | 保留的每币种历史长度，整数 1–10000，默认 1000。持续模拟行情目前最多提供 1000 根。 |

币种约束：

| `symbols.mode` | 选择规则 |
| --- | --- |
| `any` | 可选任意符合市场条件的交易对，数量不少于 `min_count`；若 `values` 非空，它仍构成允许列表。 |
| `fixed` | 选择集合必须与 `values` 完全相同，不能漏选或额外添加交易对。 |
| `multi` | 至少选择 `min_count` 个交易对，且 `min_count >= 2`；若 `values` 非空，只能从该列表选择。 |

`values` 为不重复的交易对字符串列表，例如 `BTCUSDT`、`ETHUSDC`。使用交易所返回的完整交易对标识，不写 `BTC/USDT`。控制服务只允许 USDT、USDC 计价交易对。

账户要求：

| 字段 | 合法声明 | 当前可执行范围 |
| --- | --- | --- |
| `position_modes` | 非空列表，值为 `one_way` 或 `hedge` | 当前回测/模拟按单向净持仓执行；必须接受 `one_way`。 |
| `margin_modes` | 非空列表，值为 `cross`、`isolated` 或 `cash` | 历史合约引擎使用全仓权益模型，必须接受 `cross`；现货必须为 `["cash"]`。 |
| `leverage_min` | 大于等于 1 的整数 | 实际杠杆必须在声明范围内；持续模拟取声明的最小值。 |
| `leverage_max` | 大于等于最小杠杆的整数 | 现货最小及最大值都必须为 1。 |
| `min_capital` | 大于等于 0 的有限数字 | 统一以 U 表示最低资金；回测/模拟初始资金仍必须大于 0。 |

现货还必须声明 `position_modes: ["one_way"]`，不能做空、借贷或加杠杆。协议能够表达逐仓/双向要求，静态保存成功不表示当前引擎已支持运行；仅支持逐仓或双向的策略不应启用。账户接入检查的真实钱包与保证金要求见 Binance 文档。

参数结构示例见完整模板。当前支持 `integer`、`number`、`boolean`、`string`，每项必须有 `default`；支持 `minimum`、`maximum`、`enum`，`title` 用于界面标签。未提供的参数使用默认值，未知参数被拒绝；布尔值不作为数字接受，NaN/Infinity 被拒绝。复杂嵌套对象、数组、动态表单、JSON Schema 的其他关键字尚未实现。

`required` 仅允许引用已定义的参数。默认值使缺省参数可补齐，不要求用户再次输入。跨字段约束（例如快均线周期必须小于慢均线）应在策略内明确检查并抛出 `ValueError`；这些检查会使任务失败并记录原因，当前表单不能自动从 Python 推断跨字段约束。

## 3. 生命周期函数与上下文

唯一必需方法为 `on_bar(self, context)`。构造函数如有定义，必须支持无参数实例化。当前没有 `on_tick`、`on_fill`、定时器、订单簿订阅或异步回调协议。

每根已完成的公共时间戳 K 线调用一次。多币种回测取时间戳交集，不向策略补造 K 线；不对齐而被丢弃的根数写入 `assumptions.dropped_unaligned_bars`。`timestamp` 是蜡烛开盘时间标识，调用发生在其 OHLC 已完整可见之后。控制服务统一存储 UTC 毫秒；直接引擎调用也可输入统一的秒时间戳，禁止在同一数据集中混用单位。

| 上下文键 | 类型与内容 |
| --- | --- |
| `params` | 已验证、填充默认值的参数字典。 |
| `timestamp` | 当前共同 K 线的时间标识。 |
| `bar_index` | 从 0 开始的本次策略实例计数；不是 Unix 时间。 |
| `candles` | `{symbol: {timestamp, open, high, low, close, volume}}`，仅含当前完成的 K 线。 |
| `history` | `{symbol: [bar, ...]}`，时间升序，包含当前 K 线，仅保留 `history_window` 根，不包含未来数据。 |
| `positions` | 当前非零净持仓；键为 symbol，结构见下表，空仓币种不一定存在。 |
| `account` | 统一模拟账户快照，结构见下表。 |
| `log` | `log(level, message, data=None)`，`level` 使用 `debug` 或 `error`。 |
| `emit` | `emit(**display_fields)`，输出阶段、指标、原因等扩展展示数据。 |

持仓字段：`quantity` 为带符号数量（正数多仓，负数空仓）；`average_price` 为剩余净持仓加权成本；`unrealized_pnl` 为按当前收盘价计算的浮盈亏；`margin_used` 为该币种的保证金/现货占用额。

账户字段：

| 字段 | 含义 |
| --- | --- |
| `initial_equity` | 本账户初始模拟权益，单位 U。 |
| `balance` | 初始权益 + 已实现毛收益 − 已支付的全部交易费。它是统一账本余额，不能直接当作交易所某个现货币种的钱包现金余额。 |
| `equity` | `balance + unrealized_pnl`。 |
| `available` | `max(0, equity - margin_used)`，策略可用资金估算，最终仍由执行器检查。 |
| `margin_used` | 合约为各持仓 `abs(quantity) × mark_price / leverage` 的和；现货按 1 倍计算资产占用。 |
| `realized_pnl` | `balance - initial_equity`，包含尚未平仓头寸已支付的开仓费。 |
| `unrealized_pnl` | 各净持仓 `(mark_price - average_price) × quantity` 的和。 |
| `leverage`、`market`、`position_mode`、`margin_mode` | 执行环境的实际模型设置；当前为单向，合约全仓或现货 cash。 |

上下文是输入快照。不要通过修改 `context`、`positions` 或 `account` 来改变真实账本；输出交易意图才会触发执行器。策略必须自行处理预热不足，通常返回 `[]` 并输出未交易原因。

## 4. 交易意图与成交时序

`on_bar` 必须返回列表，无交易返回 `[]`，每次最多 1000 个字典。当前只模拟市价意图：

| 字段 | 必需 | 含义 |
| --- | --- | --- |
| `symbol` | 是 | 本实例已选择且行情可用的交易对。 |
| `side` | 是 | 小写 `buy` / `sell`。合约卖出可开空，也可减少多仓。 |
| `quantity` | 是 | 大于 0 的有限数字，以基础资产数量计，不是 U 金额；例：BTC 数量。 |
| `reduce_only` | 否 | 布尔值，默认 false；true 只能减少相反方向现有仓位，数量超出时裁到剩余持仓，不能反手。 |
| `reason` | 否 | 交易原因，保存到成交及决策记录。 |

不要使用 `price`、`stop_price`、`order_type` 等字段期望得到限价/止损行为；当前引擎不会实现这些订单语义。也不要用返回 `None` 表示空闲。

处理顺序为：上一根意图 → 当前共同 K 线开盘价 → 资金/交易规则检查及模拟成交 → 按当前收盘价盯市 → 调用策略 → 把新意图排入下一根。意图按返回列表顺序执行，因此同根多币种订单会竞争可用资金。最后一根产生的意图标记 `expired`，不使用未来价格补成交。

数量以 Decimal 向下量化，价格按买入向上、卖出向下对齐 tick；滑点对买卖两边都不利。检查最小数量、最大数量、最小名义金额以及 `LOT_SIZE`/`MARKET_LOT_SIZE` 的共同步长。合约只减仓可按模型豁免最小名义金额；现货卖出仍受最小金额约束，尘埃仓位不会被伪造成已成交。成交并不自动保证所有交易所过滤器都已模拟，例如动态百分比价格限制和盘口流动性尚未实现。

每次成交扣费，增加新风险前按成交滑点和费用后的权益检查保证金；纯减仓仍允许执行。现货卖出后净数量不得小于零。回测结束后保留未平仓头寸，按最后收盘价计权益，不人为创造末尾平仓订单。

## 5. 扩展展示、日志与状态

推荐调用 `context["emit"](stage="...", indicators={symbol: {"rsi": 50}}, reason="...")`。同一次 `on_bar` 内多次调用按键覆盖合并。回测保存完整 `display`，并另外提取 `stage`、`indicators`、`reason`；模拟账户保存最新 `display`。可以加入 JSON 兼容的 `chart_lines` 等自定义键，当前通用前端尚不会自动把任意自定义线绘制成图表。

`log("debug", "...", {"symbol": "BTCUSDT"})` 用于观察决策；`log("error", "...", details)` 用于策略识别到的问题。记录日志本身不会终止策略，需要中止时抛出异常。历史回测日志存放在对应 decision 内，持续模拟日志写入按策略/账户筛选的日志表。不要写入 API 密钥、口令或其他凭据。

决策记录包含当前输入、账户快照、指标、阶段、原因、交易意图、风控结果和日志。风控结果含通过/拒绝、原因及关联成交 ID；无信号、区间尾未成交与执行完成分别可辨。一个 decision 的 `status="executed"` 只表示其意图处理完成，是否真正成交必须读每项 `risk.status`。

策略可以把运行状态保存为 `self.calls`、`self.last_signal` 等实例字段。持续模拟逐根恢复并保存 `strategy_obj.__dict__`，崩溃重启后从账本快照恢复；策略 ID、源码哈希或配置修订发生变化时，清除旧策略状态和待处理意图，保留金融账本。

实例状态必须可以使用严格 JSON 序列化：字符串键字典、列表、字符串、有限数字、布尔值、null。不要存放 Decimal、set、datetime、生成器、文件、连接、函数或对象实例；需要 Decimal 时临时计算，持久化时转字符串。不要依赖类变量或模块全局变量的可变状态，它们不属于恢复协议。历史回测保留同一内存实例，但为跨环境一致性仍须遵守此限制。

## 6. Python 源码检查与信任流程

保存策略时只做 AST 语法/契约检查，不执行上传代码。源码限 256 KB；顶层允许文档字符串、`MANIFEST`、函数/类声明及白名单导入。可导入 `math`、`statistics`、`decimal`、`collections`；不支持通配符导入、装饰器、继承、元类或私有/双下划线属性访问，不允许文件、动态执行、环境访问等常见危险入口。

上传入口仅向已登录管理员开放。管理员保存源码即表示将其作为可信策略接入；契约检查通过后可直接选择回测或模拟，无需再次审批。客户端没有上传或运行任意源码的权限。AST 检查、受限名字空间和子进程资源限制都不是恶意 Python 的安全沙箱，部署者必须信任管理员及其上传代码；如果将来开放外部作者或多租户上传，必须另建操作系统/容器级网络、文件、凭据和资源隔离。

对于旧数据库中尚未标记可信的策略记录，保留下面的可选运维命令；正常管理员上传流程不需要它：

```bash
python -m backend.worker trust-strategy STRATEGY_ID --source-hash SHA256 --accept-trusted-code
```

将占位符替换为实际策略 ID 与完整源码 SHA-256；命令同时核对数据库哈希与当前源码计算值，防止待启用内容变化。该命令只改变策略的可信标记，不开放实盘。

接口入口：`validate_strategy_source(source) -> manifest`、`validate_manifest(manifest)`、`validate_parameters(manifest, params)`、`validate_symbols(manifest, symbols)`。`load_strategy(source)` 会执行代码，只能由检查过策略可信标记的工作进程使用，控制 API 不调用它。

## 7. 回测 API、训练验证与可复现性

```python
run_backtest(
    strategy_source, params, candles, initial_capital, fee_rate, slippage_bps,
    market="futures", leverage=1, symbol_filters=None,
    progress=None, cancelled=None,
)
```

`candles` 为 `{symbol: [bar, ...]}`，时间必须严格递增且不重复，价格为正、成交量非负，OHLC 范围须一致。`fee_rate=0.0004` 表示 0.04%，`slippage_bps=2` 表示 0.02%。`symbol_filters` 为按 symbol 索引的规则快照，包括 `tick_size`、`step_size`、`min_qty`、`max_qty`、`min_notional` 等。

返回 `metrics`、`equity`、`orders`、`decisions`、`positions`、`account`、`parameters`、`engine_version`、`status`、`assumptions`。`equity` 每点包含 `timestamp/equity/drawdown/margin_used/unrealized_pnl`。`orders` 是实际完成的模拟成交记录，不把被拒绝的意图记为成交。

`progress(event)` 分批收到 `{processed,total,status,equity}`；中途状态为 `running`，末尾完成事件附最终指标。不要把中途曲线当作完整统计。`cancelled()` 为真会抛出 `BacktestCancelled`；如果策略自身卡在 `on_bar` 内，合作式取消无法立即生效，回测协调器会终止收到取消请求但无响应的子进程，并另设 CPU/墙钟上限。持续模拟在 Unix 上对每次 on_bar 设置 10 秒截止时间，Linux 上对账户进程默认限制 384 MB 地址空间（MK_ACCOUNT_MEMORY_MB）；这些限额不保证任意恶意代码均可安全运行。

`run_split_backtest` 在上述基础上接受 `train_start/train_end/validation_start/validation_end` 和 `seed=42`；要求 `train_start < train_end <= validation_start < validation_end`，区间均为 `[start,end)`。训练与验证使用各自全新策略实例和初始账户，不继承参数搜索中的仓位、状态或未来信息；验证开始阶段自行预热，不自动读取训练末尾历史。

控制服务的实验绑定策略版本/源码哈希、参数哈希、数据 ID/内容哈希、引擎版本、费率/滑点、交易规则快照、种子、区间及 Python/系统环境。直接 `run_split_backtest` 也返回 `reproducibility` 哈希与区间信息。种子用于固定演示数据/执行环境，不意味着可以导入不受控随机模块。`generate_demo_candles` 生成确定性合成数据，必须始终标注为合成数据。

当前持续模拟固定使用完整 1 分钟 K 线、同一净持仓账本与下一根开盘撮合语义。行情轮询得到已完成蜡烛后，才补记该根开盘的模拟成交；这是一种基于闭合 K 线的模拟，不是盘口实时撮合。行情断档超过 1 分钟会废弃原待执行意图，行情缓存或最后完整 K 线过期时禁止开仓。历史数据若选 1 小时周期，`period=20` 的含义与模拟的 20 分钟不同，不能直接把两个收益结果视作同条件对照。

## 8. 指标定义与边界

引擎比例用小数：`0.12` 表示 12%。API 的 UI 适配层把收益、回撤、胜率转换为百分数展示；不要重复乘以 100。金额统一按账本 U 计，模型将 USDT 与 USDC 视作等值，不模拟稳定币汇率、真实币种钱包分配或兑换成本。

| 指标 | 当前计算口径 |
| --- | --- |
| `initial_equity` / `final_equity` | 初始权益 / 最后一个估值点的权益，后者包含浮盈亏。 |
| `total_return` | `final_equity / initial_equity - 1`。 |
| `annual_return` | `(final / initial) ** (365 × 86400 / elapsed_seconds) - 1`；不足 1 天、期末权益非正或数值溢出时为 null。 |
| `max_drawdown` | 所有点中 `(历史权益峰值 - 当前权益) / 历史峰值` 的最大值，初始权益参与峰值。 |
| `max_drawdown_amount` | 上述峰值到当前权益的金额差最大值；最大金额回撤与最大比例回撤不必出现在同一时刻。 |
| `win_rate` | 扣除分摊开仓费及本次平仓费后，正收益减仓事件数 / 全部减仓事件数；没有减仓事件为 null。 |
| `profit_factor` | 全部正收益减仓事件收益和 / 负收益减仓事件亏损绝对值和；没有亏损事件为 null，不能把 null 显示为 0 或无穷大。 |
| `sharpe` | UTC 每日最后权益构成日收益，均值 / 样本标准差 × `sqrt(365)`；无风险利率为 0，少于 2 个日收益或标准差为 0 时为 null。很短样本即使有数值也不代表稳定性。 |
| `total_orders` / `long_orders` / `short_orders` | 成交笔数及其方向分类。包含开仓、减仓；不是完成整轮交易的次数。单笔反手按原减仓方向归类，另有 `opened_quantity/closed_quantity` 可追溯。 |
| `closed_trades` | 实际减仓事件数量；分批平仓会计为多个事件。 |
| `max_consecutive_losses` | 依时间排序的扣费减仓收益中，连续负数的最长长度；零收益打断序列。 |
| `realized_pnl` / `unrealized_pnl` / `margin_used` | 与账户快照定义相同。 |
| `total_fees` | 全部成交的费用总和。 |

模拟成本和交易所真实结算存在明确差距：未模拟资金费、借贷费、盘口冲击、部分成交、维护保证金、强平、逐仓爆仓及稳定币偏离；普通固定滑点不等于深度模型。`assumptions` 返回这些开关、过滤器来源及是否资不抵债。直接调用时未提供真实交易规则会使用 1e-8 兜底并标记未核验；合成数据使用明确标注的合成规则。不能把这些结果称为交易所精确复盘或已验证实盘收益。

## 9. 接入前自测

1. 用 `validate_strategy_source` 校验完整源码；覆盖缺失参数、币种不匹配及账户模式不匹配。
2. 在至少两个区间独立回测，确认信号在下一根执行、最后信号不补成交、没有未来数据。
3. 覆盖最小数量/金额、手续费、资金不足、亏损、空仓减仓、现货禁止做空。
4. 让状态跨数根 K 线变化后停止并恢复模拟进程，确认 JSON 状态、成交及停止标记保持一致。
5. 阅读 `decisions/risk/assumptions`；对所有 null 指标和拒绝原因保留明确显示。

仓库回归测试入口：`python -m pytest -q`。测试通过证明已覆盖样例满足预期，不能替代下面文档中的真实部署与交易所验收。


## 10. to-do：框架与策略的职责规划

下列内容与界面 `to-do` 标签页一致，是接口规划清单；当前已经提供的具体字段和行为以本文前述协议为准。

建议框架提供：

- 行情订阅、历史窗口、指标预热。
- 统一时钟、定时任务和事件回调。
- 账户余额、可用资金、持仓和订单查询。
- 下单、撤单、改单、订单分组管理。
- 价格和数量规范化。
- 手续费、资金费、保证金和风险检查。
- 状态保存、恢复、日志、指标和报表。
- 常用指标与仓位计算工具，统一算法定义。

策略负责：

- 什么时候交易。
- 交易哪些币。
- 目标仓位或挂单结构。
- 入场、加减仓、退出逻辑。
- 策略特有的状态与解释信息。
