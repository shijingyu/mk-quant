"""Versioned strategy contract and static validation.

A strategy file contains a literal ``MANIFEST`` dictionary and ``class Strategy``
with ``on_bar(self, context)``. The control API only parses its AST. Loading and
calling a strategy is exclusively a worker responsibility. AST checks and a
restricted namespace are defence in depth, NOT a sandbox for hostile Python.

Every call sees completed bars only. ``context`` contains ``params``, current
``candles`` keyed by symbol, trailing ``history`` keyed by symbol, ``positions``,
``account``, ``timestamp``, ``bar_index``, ``log(level, message, data=None)`` and
``emit(**display_fields)``. Return order intents with symbol, side, quantity and
optional reduce_only/reason. MANIFEST.timeframes declares the primary decision
period and additional periods synthesized from complete minute buckets. Current
candles/history are primary-period views; timeframe_candles/timeframe_history
contain declared periods, execution_candles contains the current closed minute.
The default model executes at the next minute's open within that minute's range.
maker_grid_v1 provides persistent post-only limit orders, hedge
lots and execution_events, with the conservative OHLC rules in the protocol.
Keep persistent instance fields (``self.*``) JSON-compatible: the paper worker
serializes/restores the instance dictionary between bars and after restarts.
Do not keep mutable trading state in class/module globals or mutate inputs.
"""

from __future__ import annotations

import ast
import copy
import importlib
import math
from pathlib import Path
from .timeframes import normalize_timeframes


class StrategyValidationError(ValueError):
    """The uploaded strategy does not satisfy the framework contract."""


_SAFE_IMPORTS = {"math", "statistics", "decimal", "collections"}
_FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "open", "input", "__import__", "globals",
    "locals", "vars", "getattr", "setattr", "delattr", "breakpoint", "help",
}


def _require(condition, message):
    if not condition:
        raise StrategyValidationError(message)


def _number(value, label, minimum=None):
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
             f"{label} 必须为数字")
    _require(math.isfinite(value), f"{label} 必须为有限数字")
    if minimum is not None:
        _require(value >= minimum, f"{label} 不能小于 {minimum}")
    return value


def validate_manifest(manifest: dict) -> dict:
    """Validate the declarative contract and return a detached normalized copy."""
    _require(isinstance(manifest, dict), "MANIFEST 必须是字典")
    result = copy.deepcopy(manifest)
    for key in ("name", "version", "description"):
        _require(isinstance(result.get(key), str) and bool(result[key].strip()),
                 f"MANIFEST 缺少非空字段 {key}")
    _require(result.get("market") in {"spot", "futures"}, "market 必须为 spot 或 futures")
    symbols = result.get("symbols")
    _require(isinstance(symbols, dict), "必须声明 symbols 币种约束")
    _require(symbols.get("mode") in {"any", "fixed", "multi"}, "symbols.mode 必须为 any、fixed 或 multi")
    values = symbols.get("values", [])
    _require(isinstance(values, list) and all(isinstance(s, str) and s.isalnum() and s.isupper() for s in values),
             "symbols.values 必须是大写交易对字符串列表")
    _require(len(values) == len(set(values)), "symbols.values 不可重复")
    count = symbols.get("min_count", 1)
    _require(type(count) is int and count >= 1, "symbols.min_count 必须为正整数")
    if symbols["mode"] == "fixed":
        _require(bool(values), "固定币种策略必须指定 symbols.values")
        _require(count <= len(values), "固定币种数量小于 min_count")
    if symbols["mode"] == "multi":
        _require(count >= 2, "多币种策略的 min_count 至少为 2")
        _require(not values or len(values) >= count, "允许币种列表小于 min_count")
    symbols.update(values=values, min_count=count)

    account = result.get("account")
    _require(isinstance(account, dict), "必须声明 account 账户要求")
    for key, allowed in (("position_modes", {"one_way", "hedge"}),
                         ("margin_modes", {"cross", "isolated", "cash"})):
        value = account.get(key)
        _require(isinstance(value, list) and value and all(item in allowed for item in value),
                 f"account.{key} 无效或未声明")
    low = _number(account.get("leverage_min"), "account.leverage_min", 1)
    high = _number(account.get("leverage_max"), "account.leverage_max", low)
    _require(float(low).is_integer() and float(high).is_integer(), "杠杆倍数必须为整数")
    _number(account.get("min_capital"), "account.min_capital", 0)
    if result["market"] == "spot":
        _require(low == high == 1, "现货策略不可要求杠杆")
        _require(account["position_modes"] == ["one_way"], "现货策略仅支持单向持仓")
        _require(account["margin_modes"] == ["cash"], "现货策略的保证金模式必须为 cash")
    else:
        _require("cash" not in account["margin_modes"], "合约策略不可使用 cash 保证金模式")

    execution = result.get("execution")
    if execution is not None:
        _require(isinstance(execution, dict), "execution 必须为执行模型声明")
        _require(execution.get("model") in {"market_next_open_v1", "maker_grid_v1"}, "不支持的 execution.model")
        if execution["model"] == "maker_grid_v1":
            _require(result["market"] == "futures", "Maker 网格模型仅支持合约")
            _require(account["position_modes"] == ["hedge"], "Maker 网格必须声明双向持仓 hedge")
            _require(account["margin_modes"] == ["cross"], "Maker 网格当前仅支持全仓权益模型 cross")

    schema = result.get("parameters")
    _require(isinstance(schema, dict) and schema.get("type") == "object", "parameters 必须声明 object 类型参数结构")
    props = schema.get("properties")
    _require(isinstance(props, dict), "parameters.properties 必须为字典")
    for key, spec in props.items():
        _require(isinstance(key, str) and isinstance(spec, dict), "参数定义格式无效")
        _require(spec.get("type") in {"integer", "number", "boolean", "string"}, f"参数 {key} 类型不支持")
        _require("default" in spec, f"参数 {key} 缺少 default")
        for bound in ("minimum", "maximum"):
            if bound in spec:
                _number(spec[bound], f"参数 {key}.{bound}")
        if "minimum" in spec and "maximum" in spec:
            _require(spec["minimum"] <= spec["maximum"], f"参数 {key} 上下限相反")
    required = schema.get("required", [])
    _require(isinstance(required, list) and all(key in props for key in required), "parameters.required 包含未知参数")
    window = result.setdefault("history_window", 1000)
    _require(type(window) is int and 1 <= window <= 10000, "history_window 必须为 1 至 10000 的整数")
    try:
        result["timeframes"] = normalize_timeframes(result.get("timeframes"))
    except ValueError as exc:
        raise StrategyValidationError(str(exc)) from exc
    validate_parameters(result, {})
    return result


def validate_parameters(manifest: dict, params: dict | None) -> dict:
    """Fill defaults, reject unknown fields and validate supported JSON schema."""
    _require(params is None or isinstance(params, dict), "策略参数必须为字典")
    params = params or {}
    props = manifest["parameters"]["properties"]
    unknown = set(params) - set(props)
    _require(not unknown, f"存在未知参数：{', '.join(sorted(unknown))}")
    result = {}
    for key, spec in props.items():
        value = params.get(key, spec["default"])
        kind = spec["type"]
        if kind in {"integer", "number"}:
            _number(value, key)
            if kind == "integer":
                _require(type(value) is int, f"{key} 必须为整数")
            if "minimum" in spec:
                _require(value >= spec["minimum"], f"{key} 小于最小值 {spec['minimum']}")
            if "maximum" in spec:
                _require(value <= spec["maximum"], f"{key} 大于最大值 {spec['maximum']}")
        elif kind == "boolean":
            _require(type(value) is bool, f"{key} 必须为布尔值")
        else:
            _require(isinstance(value, str), f"{key} 必须为字符串")
        if "enum" in spec:
            _require(value in spec["enum"], f"{key} 不在允许的取值中")
        result[key] = copy.deepcopy(value)
    return result


def validate_symbols(manifest: dict, symbols: list[str]) -> None:
    _require(bool(symbols) and len(symbols) == len(set(symbols)), "交易对不能为空或重复")
    constraint = manifest["symbols"]
    _require(len(symbols) >= constraint["min_count"], f"策略至少需要 {constraint['min_count']} 个交易对")
    allowed = constraint["values"]
    if constraint["mode"] == "fixed":
        _require(set(symbols) == set(allowed), f"策略固定交易对：{', '.join(allowed)}")
    elif allowed:
        _require(set(symbols).issubset(allowed), f"允许的交易对：{', '.join(allowed)}")


def validate_strategy_source(source: str) -> dict:
    """Inspect source without importing or executing any uploaded Python."""
    _require(isinstance(source, str) and 0 < len(source.encode("utf-8")) <= 256000,
             "策略源码必须为非空文本，且不超过 256 KB")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise StrategyValidationError(f"Python 语法错误：{exc}") from exc
    manifests = []
    classes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MANIFEST" for t in node.targets):
            manifests.append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "MANIFEST":
            manifests.append(node.value)
        elif isinstance(node, ast.ClassDef) and node.name == "Strategy":
            classes.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
            pass
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            pass
        else:
            raise StrategyValidationError("顶层仅允许 MANIFEST 常量、文档、白名单导入和函数/类声明")
    _require(len(manifests) == 1, "必须且只能声明一个 MANIFEST 字典常量")
    try:
        manifest = ast.literal_eval(manifests[0])
    except (ValueError, TypeError, SyntaxError, RecursionError) as exc:
        raise StrategyValidationError("MANIFEST 必须是可静态读取的字典常量") from exc
    _require(len(classes) == 1, "必须且只能声明一个 Strategy 类")
    methods = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == "on_bar"]
    _require(len(methods) == 1, "Strategy 必须实现 on_bar(self, context)")
    method = methods[0]
    _require(len(method.args.args) == 2 and not method.args.vararg and not method.args.kwarg,
             "on_bar 方法签名必须为 on_bar(self, context)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            _require(all(alias.name in _SAFE_IMPORTS for alias in node.names), "仅允许导入 math、statistics、decimal、collections")
        elif isinstance(node, ast.ImportFrom):
            _require(node.level == 0 and node.module in _SAFE_IMPORTS and all(n.name != "*" and not n.name.startswith("_") for n in node.names),
                     "不支持该导入或通配符导入")
        elif isinstance(node, ast.Attribute):
            _require(not node.attr.startswith("_"), "不可访问私有或双下划线属性")
        elif isinstance(node, ast.Name):
            _require(node.id not in _FORBIDDEN_CALLS and not node.id.startswith("__"), f"不允许使用 {node.id}")
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            _require(not node.decorator_list, "策略不支持装饰器")
            if isinstance(node, ast.ClassDef):
                _require(not node.bases and not node.keywords, "策略类不可声明继承或元类")
    return validate_manifest(manifest)


def load_strategy(source: str):
    """Create a fresh strategy in the worker; never call from a control API."""
    validate_strategy_source(source)
    import builtins

    allowed = (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int",
        "isinstance", "len", "list", "map", "max", "min", "next", "range", "reversed",
        "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip", "Exception",
        "ValueError", "TypeError", "ZeroDivisionError", "RuntimeError", "object",
    )
    safe_builtins = {name: getattr(builtins, name) for name in allowed}
    safe_builtins["__build_class__"] = builtins.__build_class__

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name not in _SAFE_IMPORTS:
            raise ImportError("策略导入不在白名单内")
        return importlib.import_module(name)

    safe_builtins["__import__"] = safe_import
    namespace = {"__builtins__": safe_builtins, "__name__": "isolated_strategy"}
    exec(compile(source, "<strategy>", "exec"), namespace, namespace)
    return namespace["Strategy"]()


def builtin_strategies() -> list[dict]:
    strategies = []
    for filename in ("momentum.py", "spot_dca.py", "usdc_hedge_grid.py"):
        source = (Path(__file__).parent / "strategies" / filename).read_text(encoding="utf-8")
        manifest = validate_strategy_source(source)
        manifest["source"] = source
        strategies.append(manifest)
    return strategies


list_strategies = builtin_strategies


def get_strategy(strategy_id: str) -> dict:
    for strategy in builtin_strategies():
        if strategy["id"] == strategy_id:
            return strategy
    raise KeyError(f"未知策略：{strategy_id}")
