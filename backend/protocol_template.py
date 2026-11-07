"""Neutral, annotated contract skeleton shared by the protocol API and docs."""

PROTOCOL_VERSION = "1.3.1"

PROTOCOL_TEMPLATE = '''# 中性协议骨架：不包含指标、信号、仓位算法或订单，不会自动交易。
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
'''
