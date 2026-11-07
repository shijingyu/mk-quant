# MK Quant 策略接入协议 v1.3.1

协议版本：`1.3.1`；当前引擎版本：`1.4.0`。本文以 `backend/strategy.py`、`backend/engine.py`、`backend/worker.py` 的当前实现为准，适用于开发者及协助编写策略的 AI。未声明执行模型的已有策略继续使用单向市价模型；`maker_grid_v1` 增加双向限价订单簿与逐格保护，新的实验统一使用 1m 成交检查及完整高周期合成；旧实验保留原结果与引擎版本。

策略负责根据已完成的行情生成交易意图。框架负责参数校验、行情对齐、数量及价格规则、资金检查、模拟成交、账本、指标、日志与任务状态。策略不读取 API Key，不连接交易所，不创建线程，也不自行下单。

当前可执行环境是历史回测和持续模拟。正式实盘尚未接入运行主循环，管理端无法启动真实交易；存在 Binance 下单适配器并不代表完整实盘系统已经验收。具体边界见 [验收清单](ACCEPTANCE.md) 与 [Binance 适配器说明](BINANCE.md)。

## 0. 给开发者与 AI 的接入指引

本文是可一次复制的完整接入说明，包含中性骨架、字段与条件约束、上下文、交易意图、状态、成本和验收口径。复制内容为 Markdown 文档；提交策略时只提交 Python 源码，不要把全文作为策略上传。文中的路径和链接用于查阅源码或扩展资料，完成基本接入所需的协议已写在本文。

- 用户的策略需求决定信号、币种、周期、仓位和退出规则。本协议不提供或推荐任何具体交易策略；骨架中的数值仅是合法占位，不得当成用户已经选择的配置。
- 先选市场、执行模型与账户模式，再声明币种约束、周期、历史窗口和参数。只使用本文明确存在的接口，不要自行发明回调、隐含订单类型或借助额外字段改变执行器。
- 区分“必填”“可选”和“条件必填”：可选字段省略会采用文中默认值；Maker 下单、STOP_LIMIT 和退出意图另有各自必填字段，见第 4 节。
- `MANIFEST` 是静态要求，`context` 是运行输入，返回列表是交易意图；不是在 `MANIFEST` 中声明余额、持仓或实际成交结果。
- 框架负责账户凭证、行情、撮合/风控、账本和调度。策略不得自行连接 Binance、创建后台线程或读写平台数据库。
- 不满足当前执行能力的需求必须明确指出，不能把 `to-do` 规划项当作已实现接口。当前正式实盘执行仍关闭，资金费、逐仓结算和强平模型等限制见第 8 节。
- 实现时处理数据不足、空仓、拒单和异常；按第 9 节验证。空闲返回 `[]`，不能返回 `None`，不能把挂单被接收当作已成交。

## 1. 中性策略代码骨架

以下模板仅提供协议结构，不包含具体策略、指标、仓位算法或交易信号。占位配置必须根据实际策略改写；`on_bar` 默认返回空列表，不下单。

```python
# 中性协议骨架：不包含指标、信号、仓位算法或订单，不会自动交易。
# 标注“占位”的值不是推荐交易配置，必须按实际策略需求修改。
# 合约/双向/Maker 的合法组合与条件必填字段见下方完整协议。
MANIFEST = {
    # 必填：均为非空字符串；每次逻辑变更更新 version。
    "name": "待实现策略",
    "version": "1.0.0",
    "description": "请填写策略目标、适用条件与风险约束",

    # 必填：spot（无借贷现货）或 futures（U 本位永续）。当前为占位。
    "market": "spot",
    # 必填：mode=any/fixed/multi；values 为大写完整交易对允许列表。
    # fixed 必须完整选中 values；multi 至少 2 个；any 也受非空 values 限制。
    "symbols": {"mode": "any", "values": [], "min_count": 1},
    # 必填：账户要求全部声明。现货仅 one_way + cash + 1 倍。
    # 杠杆范围是约束，实际倍数由账户配置或回测配置提供，不是策略参数。
    # min_capital 以 U 计；以下为可通过校验的占位配置。
    "account": {
        "position_modes": ["one_way"],
        "margin_modes": ["cash"],
        "leverage_min": 1,
        "leverage_max": 1,
        "min_capital": 100
    },

    # 可选：省略时为 market_next_open_v1；这里显式写出默认模型。
    # 不可只改模型名称：maker_grid_v1 同时要求 futures + hedge + cross，
    # 且订单需遵守 Maker 字段、关联保护和成交规则。
    "execution": {"model": "market_next_open_v1"},
    # 可选：各周期每币历史上限 1–10000，默认 1000；并非已完成预热。
    "history_window": 200,
    # 可选：默认 1m / []；全部由连续收盘 1m 合成。
    # 主周期控制 on_bar 频率，成交检查始终使用基础 1m。
    "timeframes": {"primary": "1m", "additional": []},
    # 必填：无策略参数时可为空。每个新增参数必须有 type 和 default。
    # 支持 integer/number/boolean/string；可声明 minimum/maximum/enum、
    # title/description；单位写入标题或说明，不支持嵌套对象或数组参数。
    "parameters": {"type": "object", "properties": {}}
}

class Strategy:
    # 如需 __init__，必须支持无参数实例化；self.* 仅保存 JSON 兼容状态。
    def on_bar(self, context):
        # 每根完整收盘的主周期 K 线回调一次，context 是只读输入快照。
        # 使用 context["params"]、["history"]、["positions"]、["account"] 等。
        # 日志：context["log"](level, message, data=None)，level=debug/error。
        # 展示：context["emit"](**display_fields)，字段含 stage/indicators/reason。
        # 自行判断历史预热是否充分；不满足条件时返回 []。
        # 有交易时返回所选执行模型的意图列表，不能自行连接交易所下单。
        return []
```

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
| `execution` | 否 | 执行模型声明；缺省等同 `{"model": "market_next_open_v1"}`，也可明确声明 `{"model": "maker_grid_v1"}`。 |
| `history_window` | 否 | 各周期每币历史上限，整数 1–10000，默认 1000。持续模拟首次仅从共享的最多 1000 根分钟缓存预热，后续完整高周期历史随快照积累。 |
| `timeframes` | 否 | `{primary, additional}`：主周期默认 `1m`，辅助周期默认空列表；全部由连续 1m 合成，详见 7.1。 |

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
| `position_modes` | 非空列表，值为 `one_way` 或 `hedge` | 市价模型必须接受 `one_way`；Maker 模型必须严格为 `["hedge"]`。 |
| `margin_modes` | 非空列表，值为 `cross`、`isolated` 或 `cash` | 市价合约模型必须接受 `cross`；Maker 模型必须严格为 `["cross"]`；现货必须为 `["cash"]`。 |
| `leverage_min` | 大于等于 1 的整数 | 账户实际配置与回测所选杠杆必须在声明范围内；持续模拟优先使用账户保存的杠杆，未配置的旧账户才沿用声明最小值。 |
| `leverage_max` | 大于等于最小杠杆的整数 | 现货最小及最大值都必须为 1。 |
| `min_capital` | 大于等于 0 的有限数字 | 统一以 U 表示最低资金；回测/模拟初始资金仍必须大于 0。 |

账户保存的杠杆是独立执行设置，同一账户内各交易对共用；修改需执行端确认停止且无持仓挂单，实盘设置需经过接入检查和确认调整。策略读取 `context["account"]["leverage"]` 获取实际模型倍数，不能通过改写该字典改变杠杆。

现货还必须声明 `position_modes: ["one_way"]`，不能做空、借贷或加杠杆。`maker_grid_v1` 仅支持合约双向全仓模型；仅声明 `hedge` 而不选择该执行模型不能获得双向撮合。逐仓结算仍未实现，仅支持逐仓的策略不能运行。账户接入检查的真实钱包与保证金要求见 Binance 文档。

`execution.model` 当前只接受 `market_next_open_v1` 和 `maker_grid_v1`。内置网格另声明 `fee_role: "maker"`、`time_in_force: "GTX"` 作为展示元数据；真正的成本解释和订单约束由模型及订单字段决定，这两个元数据字段不是任意切换成交规则的开关。

市价策略可声明 `execution.paper_costs: {"fee_rate": 0.0, "slippage_bps": 0.0}` 指定持续模拟的费用假设。该对象必须且只能包含这两个有限数字字段：费率范围 0–0.1，滑点范围 0–1000 bps，零值有效。明确声明优先于账户中缓存的旧费用展示值，且同时应用于正常成交和管理员平仓；Maker 模型不接受此覆盖。未声明的策略保留原持续模拟成本逻辑（合约默认每次成交 0.0004、现货默认 0.001、滑点 2 bps）。运行指标的 `execution_model.fee_source` 记录来源。历史实验仍使用实验表单/配置中的 `fee_rate`、`slippage_bps`，不会被策略的 `paper_costs` 静默覆盖；创建零成本回测时需显式填写 0。

骨架中的空 `properties` 表示尚未定义策略参数；不要把框架账户杠杆、实验资金或费用重复塞入策略参数。当前支持 `integer`、`number`、`boolean`、`string`，每项必须有 `default`；支持 `minimum`、`maximum`、`enum`，`title` 用于界面标签。未提供的参数使用默认值，未知参数被拒绝；布尔值不作为数字接受，NaN/Infinity 被拒绝。复杂嵌套对象、数组、动态表单、JSON Schema 的其他关键字尚未实现。

参数定义字段（每项写在 `parameters.properties[参数名]` 中）：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `type` | 是 | `integer`、`number`、`boolean` 或 `string`。 |
| `default` | 是 | 与类型、范围和枚举一致的默认值；不能是 NaN/Infinity。 |
| `minimum` / `maximum` | 否 | 数值上下限，包含边界；两者都声明时下限不得大于上限。 |
| `enum` | 否 | 允许值列表，默认值也必须属于此列表。 |
| `title` | 否，建议填写 | 中文字段名，可标明单位。 |
| `description` | 否，建议填写 | 单位、参数作用、边界及修改含义；显示为字段帮助。 |

`required` 仅允许引用已定义的参数。默认值使缺省参数可补齐，不要求用户再次输入。跨字段约束（例如快均线周期必须小于慢均线）应在策略内明确检查并抛出 `ValueError`；这些检查会使任务失败并记录原因，当前表单不能自动从 Python 推断跨字段约束。

## 3. 生命周期函数与上下文

唯一必需方法为 `on_bar(self, context)`。构造函数如有定义，必须支持无参数实例化。当前没有 `on_tick`、`on_fill`、定时器、订单簿订阅或异步回调协议。

每根完整收盘的主周期 K 线调用一次。基础 1m 必须连续、整分钟对齐且所有交易对时间戳一致，否则回测失败，不取交集丢弃缺失分钟。`timestamp` 是本次决策对应的最后一根执行分钟的开盘时间，`decision_time` 是其收盘边界；合成 K 线自身的 `timestamp` 为该周期开盘时间。所有时间统一使用 UTC 毫秒。

| 上下文键 | 类型与内容 |
| --- | --- |
| `params` | 已验证、填充默认值的参数字典。 |
| `timestamp` | 本次决策对应执行分钟的开盘时间（UTC 毫秒）。 |
| `bar_index` | 从 0 开始的主周期回调序号；不是分钟序号或 Unix 时间。 |
| `candles` | `{symbol: {timestamp, open, high, low, close, volume}}`，仅含当前完整收盘的主周期 K 线。 |
| `history` | `{symbol: [bar, ...]}`，主周期完整 K 线，时间升序，包含当前已收盘 K 线，最多 `history_window` 根。 |
| `timeframe_candles` / `timeframe_history` | `{interval: {symbol: bar或bar列表}}`，包含 1m、主周期和辅助周期，只提供完整收盘数据。 |
| `execution_candles` | 当前基础 1m K 线，用于撮合；与主周期 candles 区别。 |
| `base_interval` / `signal_interval` | 固定 `1m` / 策略声明的主周期。 |
| `decision_time` | 当前分钟收盘边界的 UTC 毫秒时间；`timestamp` 保持执行分钟开盘时间。 |
| `positions` | 当前非零持仓；市价模型键为 symbol，Maker 双向模型键为 `symbol:LONG` / `symbol:SHORT`，空仓键不一定存在。 |
| `account` | 统一模拟账户快照，结构见下表。 |
| `open_orders` | 仅 Maker 模型提供：状态为 `NEW` / `PENDING_TRIGGER` 的委托快照；不等于成交列表。 |
| `lots` | 仅 Maker 模型提供：剩余数量大于 0 的逐格仓位快照，包含关联入场单和保护状态。 |
| `execution_events` | 仅 Maker 模型提供：执行事件列表。回测累积自上次主周期回调以来的撮合事件；持续模拟提供自上次策略事件游标以来的事件。不是实时 `on_fill` 回调。 |
| `log` | `log(level, message, data=None)`，`level` 使用 `debug` 或 `error`。 |
| `emit` | `emit(**display_fields)`，输出阶段、指标、原因等扩展展示数据。 |

持仓字段：`symbol` 为交易对，`position_side` 为 `LONG` / `SHORT`；`quantity` 为带符号数量（正数多仓，负数空仓）；`average_price` 为同一持仓键的剩余加权成本；`unrealized_pnl` 为按当前收盘价计算的浮盈亏；`margin_used` 为该持仓的保证金/现货占用额。Maker 同币种多空腿分别计量，不互相抵消。

Maker `lots` 包含 `id` / `lot_id`、`entry_order_id`、`symbol`、`position_side`、`entry_price`、`entry_quantity`、`quantity` / `remaining_quantity`、`entry_fee_remaining`、`opened_at`、`bracket`、`status` 和 `protection_status`。其中数量均为正数，`quantity` 是剩余数量；保护状态包括 `protected`、`stop_triggered`、`stop_rejected`、`unprotected`、`flattening`。一个聚合持仓腿可以包含多个 lot，平仓收益按被退出 lot 的成本和剩余开仓费用计算。

事件包含 `id/type/timestamp` 以及适用时的 `order_id/client_order_id/symbol/lot_id/role/status`。类型包括 `ORDER_ACCEPTED`、`ORDER_REJECTED`、`ORDER_CANCELED`、`ORDER_FILLED`、`LOT_OPENED`、`LOT_CLOSED`、`STOP_TRIGGERED`、`STOP_LIMIT_RESTING`、`AMBIGUOUS_EXIT_DEFERRED`、`ZERO_VOLUME_FILL_SKIPPED`、`IDEMPOTENCY_CONFLICT`。下单/撤单结果、当前订单簿及 lot 快照是判断当前状态的依据；不要假设所有同步提交事件都会在下一根回测上下文重放。回测 `decision.execution_events` 包含当根撮合和随后提交产生的全部事件，按决策拼接对应结果的完整 `events`；持久订单簿也保留完整历史。`context` 没有名为 `events` 的通用键。

账户字段：

| 字段 | 含义 |
| --- | --- |
| `initial_equity` | 本账户初始模拟权益，单位 U。 |
| `balance` | 初始权益 + 已实现毛收益 − 已支付的全部交易费。它是统一账本余额，不能直接当作交易所某个现货币种的钱包现金余额。 |
| `equity` | `balance + unrealized_pnl`。 |
| `available` / `margin_available` | `max(0, equity - margin_used - pending_margin - pending_fee_reserve)`，最终仍由执行器检查。 |
| `margin_used` | 合约为各持仓腿 `abs(quantity) × mark_price / leverage` 的和；双向不净额抵消，现货按 1 倍计算资产占用。 |
| `pending_margin` / `pending_fee_reserve` | Maker 未成交入场委托的保证金及 Maker 费用预留；退出保护单不重复预留开仓保证金。市价模型这两项为 0。 |
| `realized_pnl` | `balance - initial_equity`，包含尚未平仓头寸已支付的开仓费。 |
| `unrealized_pnl` | 各净持仓 `(mark_price - average_price) × quantity` 的和。 |
| `leverage`、`market`、`position_mode`、`margin_mode` | 执行环境的实际模型设置；市价模型单向，Maker 模型双向；合约全仓或现货 cash。 |

上下文是输入快照。不要通过修改 `context`、`positions` 或 `account` 来改变真实账本；输出交易意图才会触发执行器。策略必须自行处理预热不足，通常返回 `[]` 并输出未交易原因。

## 4. 交易意图与成交时序

`on_bar` 必须返回列表，无交易返回 `[]`，每次最多 1000 个字典。两种模型的订单语义不同，不能将 Maker 意图交给市价执行器。

### 4.1 市价模型 `market_next_open_v1`

未声明 `execution` 的旧策略沿用以下字段和时序：

| 字段 | 必需 | 含义 |
| --- | --- | --- |
| `symbol` | 是 | 本实例已选择且行情可用的交易对。 |
| `side` | 是 | 小写 `buy` / `sell`。合约卖出可开空，也可减少多仓。 |
| `quantity` | 是 | 大于 0 的有限数字，以基础资产数量计，不是 U 金额；例：BTC 数量。 |
| `reduce_only` | 否 | 布尔值，默认 false；true 只能减少相反方向现有仓位，数量超出时裁到剩余持仓，不能反手。 |
| `reason` | 否 | 交易原因，保存到成交及决策记录。 |

该模型不会因附加 `price`、`stop_price` 等字段而变成限价/止损执行器；明确声明的非市价订单类型会被拒绝。需要限价语义必须选择 Maker 模型并遵守其订单协议。也不要用返回 `None` 表示空闲。

处理顺序为：上一主周期信号 → 下一分钟开盘价 → 资金/交易规则及分钟范围检查 → 按当前分钟收盘价盯市 → 更新高周期合成 → 仅在主周期完整收盘时调用策略。意图按返回列表顺序执行，因此同根多币种订单会竞争可用资金。最后一根产生的意图标记 `expired`，不使用未来价格补成交。

数量以 Decimal 向下量化，价格按买入向上、卖出向下对齐 tick；滑点对买卖两边都不利。量化/滑点后的价格还须满足该分钟 `low <= price <= high` 且 `volume > 0`，否则记录未成交原因，不截价、不补成交。检查最小数量、最大数量、最小名义金额以及 `LOT_SIZE`/`MARKET_LOT_SIZE` 的共同步长。合约只减仓可按模型豁免最小名义金额；现货卖出仍受最小金额约束，尘埃仓位不会被伪造成已成交。成交并不自动保证所有交易所过滤器都已模拟，例如动态百分比价格限制和盘口流动性尚未实现。

每次成交扣费，增加新风险前按成交滑点和费用后的权益检查保证金；纯减仓仍允许执行。现货卖出后净数量不得小于零。回测结束后保留未平仓头寸，按最后收盘价计权益，不人为创造末尾平仓订单。

### 4.2 Maker 模型 `maker_grid_v1`

该模型使用 `futures`、`hedge`、`cross`。`fee_rate` 为每笔成交的 Maker 费率；`slippage_bps` 必须为 0。所有限价单相对当前参考价做 Post Only 检查；这是 OHLC 模拟，参考价不是实时买一/卖一，不能证明交易所真实 Maker 成交。

| 字段 | 必需 | 含义 |
| --- | --- | --- |
| `action` | 否 | 默认 `place`；也支持 `cancel`、`cancel_entries`。 |
| `client_order_id` | place 必需 | 1–160 字符的稳定模型 ID。同 ID、同意图幂等返回原记录；同 ID、不同意图拒绝，不覆盖原单。它不是可直接发送到 Binance 的订单 ID。 |
| `symbol` / `side` | place 必需 | 所选交易对及小写 `buy` / `sell`。 |
| `position_side` | place 必需 | `LONG` / `SHORT`；开多 `buy+LONG`，开空 `sell+SHORT`，退出方向相反。 |
| `type` / `time_in_force` | place 必需 | `LIMIT` 或 `STOP_LIMIT`，且必须 `GTX`；不接受市价单或 Taker 回退。 |
| `quantity` / `price` | place 必需 | 正的基础资产数量和限价；数量向下量化，买价向下、卖价向上对齐 tick，使用 `LOT_SIZE` 而非市价数量规则。 |
| `stop_price` | STOP_LIMIT 必需 | 条件触发价；卖出触发价向下、买入触发价向上对齐 tick。 |
| `reduce_only` | 否 | 默认 false；true 是框架内部的逐 lot 退出约束，必须关联 `lot_id`，不能超出该 lot 剩余数量，不能反手。不是 Binance Hedge 下可直接发送的 `reduceOnly` 参数。 |
| `lot_id` | 退出必需 | 入场默认等于 `client_order_id`，必须唯一；退出只能关联相同币种和相同持仓方向的已有 lot。 |
| `bracket` | 入场必需 | `{take_profit_price, stop_price, stop_limit_price}`；执行器先量化保护价并检查方向，止损限价也必须仍在入场价亏损一侧，否则拒绝入场。入场仅允许 `LIMIT GTX`。 |
| `parent_id` / `role` | 否 | 关联入场 ID 及用途；框架生成 `entry` / `tp` / `sl` / `flatten`，普通手动减仓默认为 `manual`。 |
| `reason` | 否 | 交易原因，保存到委托、成交和事件。 |

撤单示例为 `{"action": "cancel", "order_id": "稳定订单ID"}`；也可用 `client_order_id` 指定同一 ID。`{"action": "cancel_entries"}` 只撤未成交入场单，保留退出单。撤单不会生成成交记录。

上表定义的是订单结构，不是交易建议；不提供固定币种、价格、仓位或网格信号样例。入场价格、数量及三项保护价格均须由开发者按照用户策略逻辑计算，不能从骨架占位值推导。

处理顺序为：已存在委托处理当前完整共同 1m K 线 → 按分钟收盘价估值 → 更新高周期合成 → 主周期完整收盘时调用 `on_bar` → 提交新意图。新单最早下一分钟有效；买卖单均须满足 `low <= limit <= high` 且该分钟 `volume > 0`，含边界触价，满足时按限价全量成交。跳空后整个分钟范围未触及限价时不成交。零成交量即使触价也不成交，但仍可观察到止损触发；没有盘口队列、按成交量分配或部分成交模拟。最后一根新单保留为未成交，不虚构终局平仓。

入场成交时，执行器同步创建 lot、`LIMIT GTX` 止盈单和 `STOP_LIMIT GTX` 条件止损单，二者最早下一根有效，无需策略再次回调。止损触发的当根只检查并挂出限价单，最早再下一分钟触价区间检查通过后成交；触发参考价为卖单 `min(open, stop_price)`、买单 `max(open, stop_price)`。若该限价会相对参考价立即成交，则拒绝并保留风险，不转市价。无法判断同根止盈/止损先后时优先处理止损，并抑制该 lot 当根的盈利退出；这不等于保证止损成交。

lot 全部退出后，执行器取消该 lot 尚存的所有兄弟退出单；手动退出部分 lot 会同步缩减其他退出单数量。这种内部数量调整不是对真实盘口部分成交的建模。保护缺失或止损拒单会停止新增入场委托。止损触发或拒绝本身不撤止盈，止盈在后续 K 线仍可能退出该 lot；不能将这种结果描述为已完成止损。

`open_orders` 保留 `NEW` 和 `PENDING_TRIGGER`；完整订单记录还包括 `FILLED`、`REJECTED`、`CANCELED`，并提供 `submitted_at`、`eligible_after`、`triggered`、`filled_quantity` 及适用的成交/触发/取消时间和原因。`eligible_after` 是排他时间边界，只有较新的 K 线可撮合；成交单独写入 `orders` 账本。

### 4.3 运行控制

Maker 暂停会撤入场单，保留并继续管理已有 lot 和保护单；策略异常同样暂停新增风险。行情缺失或过期时撤入场，不使用旧行情补成交；缺失期间的保护触发无法靠后续单根 K 线重建，系统保留持仓风险。

Maker “平仓并停止”先撤入场及原退出单，再逐 lot 挂参考价外一个 tick 的被动 `LIMIT GTX` 退出单；缺少参考价时保留原保护。未成交时状态为 `flattening`，命令保持 `running`，可能长期等待，不能显示“已平仓”。只有无 lot 且无挂单才确认 `stopped`。恢复运行解除 flattening 标记；这不是 Taker 紧急退出功能。

## 5. 扩展展示、日志与状态

`context["emit"]` 接受关键字参数：`stage` 为阶段文字、`indicators` 为 JSON 兼容指标字典、`reason` 为最近决策或未开仓原因；这些不是 MANIFEST 必填字段，不预设任何指标或信号。同一次 `on_bar` 内多次调用按键覆盖合并。回测保存完整 `display`，并另外提取 `stage`、`indicators`、`reason`；模拟账户保存最新 `display`。可以加入 JSON 兼容的 `chart_lines` 等自定义键，当前通用前端尚不会自动把任意自定义线绘制成图表。

`context["log"]("debug", message, data)` 用于观察决策；`context["log"]("error", message, data)` 用于策略识别到的问题，`data` 是可省略的结构化详情。记录日志本身不会终止策略，需要中止时抛出异常。历史回测日志存放在对应 decision 内，持续模拟日志写入按策略/账户筛选的日志表。不要写入 API 密钥、口令或其他凭据。

决策记录包含当前输入、账户快照、指标、阶段、原因、交易意图、风控结果和日志。市价模型的 `status="executed"` 只表示意图处理完成，是否真正成交须读每项 `risk.status`；Maker 的 `status="submitted"` 和 `risk.status="accepted"` 仅表示委托处理/接收，须结合订单状态、成交及执行事件判断是否退出。委托接受、止损触发、成交和平仓是不同状态。

策略可以把运行状态保存为 `self.calls`、`self.last_signal` 等实例字段。持续模拟逐根恢复并保存 `strategy_obj.__dict__`，重启后从账本快照恢复。市价模型在策略 ID、源码哈希或配置修订变化时清除旧策略状态和待处理意图，保留金融账本。Maker 模型同时持久保存订单簿、lot、事件、预留资金、flattening 及策略事件游标；存在 lot 或挂单时拒绝以新配置覆盖运行状态，必须先退出并清空。内部成交、快照和命令状态在持有有效租约的数据库事务中保存；这不等于已经验证交易所发送的原子性。

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
    progress=None, cancelled=None, base_interval="1m",
)
```

`candles` 为 `{symbol: [bar, ...]}`，基础周期只允许 1m，开盘时间须对齐整分钟，各币时间戳相同且连续；缺口、粗周期和重复数据会明确失败。价格为正、成交量非负，OHLC 范围须一致。`fee_rate=0.0004` 表示 0.04%，`slippage_bps=2` 表示 0.02%。市价模型按 Taker 假设解释费率及不利滑点；Maker 模型按 Maker 费率解释，非零滑点会拒绝运行。`symbol_filters` 为按 symbol 索引的规则快照，包括 `tick_size`、`step_size`、`min_qty`、`max_qty`、`min_notional` 等。

返回 `metrics`、`equity`、`orders`、`decisions`、`positions`、`account`、`parameters`、`engine_version`、`status`、`assumptions`。`equity` 每点包含 `timestamp/equity/drawdown/margin_used/unrealized_pnl`。`orders` 是实际完成的模拟成交记录，不把被拒绝的意图记为成交。Maker 结果另含 `open_orders`、`lots`、完整 `events` 和可恢复 `order_book`；曲线另含 `pending_margin`，成交带 `lot_id/role/client_order_id/order_type/time_in_force/liquidity`。`liquidity="simulated_maker"` 明确表示模型假设。

`progress(event)` 分批收到 `{processed,total,status,equity}`；中途状态为 `running`，末尾完成事件附最终指标。不要把中途曲线当作完整统计。`cancelled()` 为真会抛出 `BacktestCancelled`；如果策略自身卡在 `on_bar` 内，合作式取消无法立即生效，回测协调器会终止收到取消请求但无响应的子进程，并另设 CPU/墙钟上限。持续模拟在 Unix 上对每次 on_bar 设置 10 秒截止时间，Linux 上对账户进程默认限制 384 MB 地址空间（MK_ACCOUNT_MEMORY_MB）；这些限额不保证任意恶意代码均可安全运行。

`run_split_backtest` 在上述基础上接受 `train_start/train_end/validation_start/validation_end` 和 `seed=42`；要求 `train_start < train_end <= validation_start < validation_end`，区间均为 `[start,end)`。训练与验证使用各自全新策略实例和初始账户，不继承参数搜索中的仓位、状态或未来信息；验证开始阶段自行预热，不自动读取训练末尾历史。

控制服务的实验绑定策略版本/源码哈希、参数哈希、数据 ID/内容哈希、引擎版本、费率/滑点、交易规则快照、种子、区间及 Python/系统环境。直接 `run_split_backtest` 也返回 `reproducibility` 哈希与区间信息。种子用于固定演示数据/执行环境，不意味着可以导入不受控随机模块。`generate_demo_candles` 生成确定性合成数据，必须始终标注为合成数据。

持续模拟使用与回测相同的 1m 区间撮合和高周期合成。聚合状态与完整历史随账户快照持久保存，不随共享分钟缓存滚动而丢失。初次从缓存预热，历史不足时策略继续等待；不会重放缓存中的历史交易。行情轮询得到闭合蜡烛后才补记该根的模拟结果，不是盘口实时撮合。行情断档超过 1 分钟会废弃市价待执行意图或撤销 Maker 入场挂单；Maker 保留退出单并明确记录缺失期间无法重建的风险。行情缓存或最后完整 K 线过期时禁止开仓。未完成的高周期桶在缺口后作废；下一完整桶可以恢复决策，不能用前后分钟拼出虚假连续 K 线。

### 7.1 周期声明与每币归因

`MANIFEST.timeframes = {"primary":"1h", "additional":["4h","1d"]}`；省略时主周期默认为 1m、辅助周期为空。支持 1/3/5/15/30m、1/2/4/6/8/12h、1/3d、1w，最多 8 个不重复辅助周期。按 UTC 桶聚合 open 首值、high 最大、low 最小、close 末值、volume 求和；周线从 UTC 周一开始。开头/末尾不足整桶的数据只参与分钟撮合和估值，不作为策略高周期输入。`bar_index` 是主周期回调序号。

新回测返回 `symbol_results[symbol] = {metrics,equity,account,orders}`，进度另含 `symbol_equity`。API 按训练/验证组织为 `{metrics,validation_metrics,curve,validation_curve,differences,allocation}`；大曲线与汇总一起保存在 curve/validation_curve Parquet 中，使用 symbol 区分。初始资本按交易对等分，单币权益为本币初始归因资本加该币净已实现收益、浮动盈亏；各币权益/成交数可加总核对汇总，PF、胜率、回撤、夏普等比率不能直接相加。共享资金与风控仍按组合执行，不等于独立单币策略绩效。旧实验无此数据时显示未记录，必须重跑才能生成。

页面详情使用 `GET /api/experiments/{id}?view=interactive`：完整统计、有界极值曲线预览及 `detail_pagination`。`GET /api/experiments/{id}/records/{orders|decisions}?offset=0&limit=20` 按需分页，服务端每页最多 100 条。默认详情与完整 JSON 报告保留全量合同，完整 JSON/CSV 以流式读取输出，CSV 包含 `TOTAL` 及各交易对的训练/验证权益；预览不得作为重新计算完整指标的数据源。

## 8. 指标定义与边界

引擎比例用小数：`0.12` 表示 12%。API 的 UI 适配层把收益、回撤、胜率转换为百分数展示；不要重复乘以 100。金额统一按账本 U 计，模型将 USDT 与 USDC 视作等值，不模拟稳定币汇率、真实币种钱包分配或兑换成本。

| 指标 | 当前计算口径 |
| --- | --- |
| `initial_equity` / `final_equity` | 初始权益 / 最后一个估值点的权益，后者包含浮盈亏。 |
| `total_return` | `final_equity / initial_equity - 1`。 |
| `annual_return` | `(final / initial) ** (365 × 86400 / elapsed_seconds) - 1`；不足 1 天、期末权益非正或数值溢出时为 null。 |
| `max_drawdown` | 所有点中 `(历史权益峰值 - 当前权益) / 历史峰值` 的最大值，初始权益参与峰值。 |
| `max_drawdown_amount` | 上述峰值到当前权益的金额差最大值；最大金额回撤与最大比例回撤不必出现在同一时刻。 |
| `win_rate` | 扣除分摊开仓费及本次平仓费后，正收益减仓事件数 / 全部减仓事件数；没有减仓事件为 null。市价模型按净持仓成本，Maker 模型按关联 lot 成本。 |
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
6. Maker 策略另覆盖 GTX 拒单、同根入场后保护不成交、止损触发未退出、同根歧义、兄弟撤单、双腿不互抵、挂单资金预留及重复 ID；暂停仍管理保护、未成交平仓保持 flattening。

仓库回归测试入口：`python -m pytest -q`。测试通过证明已覆盖样例满足预期，不能替代真实部署与交易所验收。
