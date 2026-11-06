"""Seed only market history for a pristine, stopped pair paper account.

Uses public Binance minute candles and the normal MinuteFrames accumulator.
Never loads exchange credentials, calls a strategy, replays orders, starts an
account, initializes a database, or overwrites an existing ledger snapshot.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.strategy import validate_parameters, validate_strategy_source, validate_symbols
from backend.timeframes import INTERVAL_MINUTES, MINUTE_MS, MinuteFrames, normalize_timeframes


PAIR = {"BTCUSDC", "ETHUSDC"}
ACCOUNT_COLUMNS = "id,mode,strategy_id,config,symbols,initial_equity,metrics,source,created_at"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def warmup_requirements(manifest, params):
    periods = normalize_timeframes(manifest.get("timeframes"))
    if periods["primary"] not in {"15m", "1h", "4h", "1d"} or periods["additional"]:
        raise ValueError("预热仅支持无辅助周期的15m、1h、4h、1d配对策略")
    if manifest.get("market") != "futures" or manifest.get("execution", {}).get("model") != "market_next_open_v1":
        raise ValueError("预热仅支持永续市价配对执行模型")
    values = manifest.get("symbols", {}).get("values", [])
    if manifest.get("symbols", {}).get("mode") != "fixed" or set(values) != PAIR or len(values) != 2:
        raise ValueError("预热仅支持固定BTCUSDC/ETHUSDC配对")
    params = validate_parameters(manifest, params)
    lookback = params.get("lookback")
    if type(lookback) is not int or lookback < 2 or manifest["history_window"] < lookback:
        raise ValueError("lookback须至少2且不得超过history_window")
    if params.get("exit_z", 0) >= params.get("entry_z", 0):
        raise ValueError("离场阈值必须小于入场阈值")
    return (lookback + 1) * INTERVAL_MINUTES[periods["primary"]]


def build_warmup_frames(manifest, params, candles, end_ms):
    """Pure validation/aggregation; end_ms is the exclusive closed-minute end.

    An extra primary period accommodates a partial leading bucket. Missing or
    duplicate minutes, unequal leg timestamps and invalid OHLCV fail closed.
    No synthetic prices, strategy callbacks, orders or PnL are generated.
    """
    required = warmup_requirements(manifest, params)
    params = validate_parameters(manifest, params)
    if type(end_ms) is not int or end_ms % MINUTE_MS:
        raise ValueError("预热截止时间必须是整分钟UTC时间戳")
    start_ms = end_ms - required * MINUTE_MS
    if start_ms < 0 or set(candles) != PAIR:
        raise ValueError("预热范围或双腿行情无效")
    if any(len(rows) != required for rows in candles.values()):
        raise ValueError("预热行情缺失或重复：每条腿必须覆盖全部所需分钟")
    frames = MinuteFrames(sorted(PAIR), manifest.get("timeframes"), manifest["history_window"])
    for index in range(required):
        stamp = start_ms + index * MINUTE_MS
        bars = {}
        for symbol in sorted(PAIR):
            bar = candles[symbol][index]
            if type(bar.get("timestamp")) is not int or bar["timestamp"] != stamp:
                raise ValueError("预热分钟必须连续、无重复且双腿对齐")
            values = [bar.get(key) for key in ("open", "high", "low", "close", "volume")]
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
                raise ValueError("预热OHLCV必须是有限数字")
            opening, high, low, close, volume = values
            if min(opening, high, low, close) <= 0 or volume < 0 or low > min(opening, close) or high < max(opening, close):
                raise ValueError("预热OHLCV区间或成交量无效")
            if "close_time" in bar and bar["close_time"] not in {stamp + MINUTE_MS - 1, stamp + MINUTE_MS}:
                raise ValueError("预热行情不是完整原生1分钟K线")
            bars[symbol] = dict(bar)
        frames.push(bars)
    primary = frames.timeframes["primary"]
    if any(len(rows) < params["lookback"] for rows in frames.history[primary].values()):
        raise ValueError("完整主周期数量不足，不能标记预热完成")
    return frames.dump()


def assert_pristine(account, instance, now_s):
    """Validate non-secret financial/runtime metadata; SQL history checks follow."""
    if not account or not instance:
        raise ValueError("模拟账户或实例不存在")
    if account["mode"] != "paper" or account["source"] == "demo":
        raise ValueError("只能预热独立新建的paper账户，不支持实盘或演示账户")
    if instance["desired_state"] != "stopped" or instance["observed_state"] != "stopped":
        raise ValueError("账户必须已停止且收到停止回执")
    if instance["lease_until"] > now_s:
        raise ValueError("账户仍有活跃执行租约，请等待租约过期")
    initial = Decimal(str(account["initial_equity"]))
    if not initial.is_finite() or initial <= 0:
        raise ValueError("初始资金无效")
    metrics = json.loads(account["metrics"])
    if not isinstance(metrics, dict) or any(metrics.get(key) for key in ("positions", "orders", "curve", "open_orders", "lots", "position_count", "open_order_count")):
        raise ValueError("账户已有持仓、订单或运行曲线，请新建模拟账户")
    equity = Decimal(str(metrics.get("equity", account["initial_equity"])))
    if not equity.is_finite() or equity != initial or any(metrics.get(key, 0) != 0 for key in ("fees", "fees_paid", "realized_pnl", "unrealized_pnl")):
        raise ValueError("账户已存在资金变化，不能重置为初始状态")


def read_target(con, database, account_id, now_s, *, lock=False):
    # The same instance lock guards worker commits, config changes and commands.
    instance = (database.lock_instance(con, account_id) if lock else
                con.execute("SELECT * FROM instances WHERE account_id=?", (account_id,)).fetchone())
    suffix = " FOR UPDATE" if lock else ""
    account = con.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id=?" + suffix, (account_id,)).fetchone()
    assert_pristine(account, instance, now_s)
    for table in ("orders", "fills", "ledger"):
        if con.execute(f"SELECT 1 FROM {table} WHERE account_id=? LIMIT 1", (account_id,)).fetchone():
            raise ValueError("账户已有订单、成交或账本；预热不覆盖任何历史，请新建模拟账户")
    if con.execute("SELECT 1 FROM commands WHERE account_id=? AND status IN ('pending','running') LIMIT 1", (account_id,)).fetchone():
        raise ValueError("账户存在待处理命令，不能预热")
    strategy = con.execute("SELECT id,manifest,source,source_hash,trusted FROM strategies WHERE id=?" + suffix, (account["strategy_id"],)).fetchone()
    if not strategy or not strategy["trusted"]:
        raise ValueError("账户策略不存在或未启用")
    manifest = validate_strategy_source(strategy["source"])
    # Normal administrator upload assigns a database UUID while keeping the
    # uploaded source literal unchanged. Match that documented registration.
    manifest["id"] = strategy["id"]
    if hashlib.sha256(strategy["source"].encode()).hexdigest() != strategy["source_hash"] or manifest != json.loads(strategy["manifest"]):
        raise ValueError("策略源码、manifest及哈希不一致")
    symbols = json.loads(account["symbols"])
    validate_symbols(manifest, symbols)
    if set(symbols) != PAIR or len(symbols) != 2:
        raise ValueError("账户必须同时选择BTCUSDC和ETHUSDC")
    params = validate_parameters(manifest, json.loads(account["config"]))
    required = warmup_requirements(manifest, params)
    revision = {"strategy_id": account["strategy_id"], "source_hash": strategy["source_hash"], "config_revision": instance["config_revision"]}
    # Exclude credentials at the SELECT boundary; include all selected metadata
    # so even an intervening start/stop or balance/config change invalidates work.
    identity = canonical({"account": dict(account), "instance": dict(instance), "strategy": dict(strategy)})
    return dict(account=dict(account), manifest=manifest, params=params, revision=revision, required_minutes=required, identity=identity)


def fetch_recent_minutes(client, required_minutes, clock=time.time):
    offset = client.sync_time()
    end_ms = (int(clock() * 1000) + offset) // MINUTE_MS * MINUTE_MS
    start_ms = end_ms - required_minutes * MINUTE_MS
    candles = {symbol: client.klines(symbol, "1m", start_ms, end_ms, max_rows=required_minutes) for symbol in sorted(PAIR)}
    # Larger daily windows can take several minutes. Catch up both legs before
    # building state, then trim exactly the requested moving window.
    for _ in range(4):
        current_end = (int(clock() * 1000) + offset) // MINUTE_MS * MINUTE_MS
        if current_end == end_ms:
            return candles, end_ms, offset
        if current_end < end_ms:
            raise ValueError("系统时间在下载期间回退，请校时后重试")
        for symbol in sorted(PAIR):
            candles[symbol].extend(client.klines(symbol, "1m", end_ms, current_end))
        end_ms = current_end
        start_ms = end_ms - required_minutes * MINUTE_MS
        candles = {symbol: [bar for bar in rows if bar["timestamp"] >= start_ms] for symbol, rows in candles.items()}
    raise ValueError("行情持续落后，未写入预热状态，请稍后重试")


def seed_account(account_id, *, dry_run=False, database=None, client=None, clock=time.time):
    if database is None:
        from backend import db as database
    if client is None:
        from backend.binance import BinanceClient
        client = BinanceClient(market="futures")  # Public data only; no keys.
    with database.connect() as con:
        prepared = read_target(con, database, account_id, clock())
    candles, end_ms, offset = fetch_recent_minutes(client, prepared["required_minutes"], clock)
    frames = build_warmup_frames(prepared["manifest"], prepared["params"], candles, end_ms)
    source_hash = hashlib.sha256(canonical(candles).encode()).hexdigest()
    primary = prepared["manifest"]["timeframes"]["primary"]
    audit = {"source": "Binance public USD-M native 1m klines", "source_sha256": source_hash,
             "symbols": sorted(PAIR), "interval": "1m", "primary": primary,
             "minutes_per_symbol": prepared["required_minutes"],
             "start_ms": end_ms - prepared["required_minutes"] * MINUTE_MS, "end_ms_exclusive": end_ms,
             "complete_primary_bars": min(len(rows) for rows in frames["history"][primary].values()),
             "revision": prepared["revision"], "orders_replayed": 0, "strategy_calls": 0}
    snapshot = {"model": "market_next_open_v1", "balance": str(prepared["account"]["initial_equity"]),
                "fees": "0", "positions": {}, "orders": [], "closed_pnls": [], "pending": [],
                "strategy_state": {}, "revision": prepared["revision"], "last_timestamp": end_ms - MINUTE_MS,
                "bar_index": 0, "frames": frames, "curve": [], "warmup": audit}
    with database.connect() as con:
        current = read_target(con, database, account_id, clock(), lock=True)
        if current["identity"] != prepared["identity"]:
            raise ValueError("下载期间账户、策略或实例状态已改变，预热未写入")
        if int(clock() * 1000) + offset - end_ms > 120000:
            raise ValueError("下载后的行情已过期，预热未写入，请重试")
        if not dry_run:
            # Plain INSERT plus unique snapshot reference: never replace or merge.
            con.execute("INSERT INTO ledger(id,account_id,kind,reference,payload,created_at) VALUES(?,?,?,?,?,?)",
                        (database.uid(), account_id, "snapshot", f"snapshot:{account_id}", database.dump(snapshot), database.now()))
            database.audit(con, None, "account.paper_warmup", "account", account_id, audit)
    return {"account_id": account_id, "applied": not dry_run, **audit}


def main():
    parser = argparse.ArgumentParser(description="给全新且停止的BTC/ETH模拟账户预热行情，不执行策略、不启动账户。")
    parser.add_argument("account_id", help="管理员创建的模拟账户ID；不是客户链接route")
    parser.add_argument("--dry-run", action="store_true", help="执行读取、下载、验证和最终重检，但不写入账本")
    args = parser.parse_args()
    try:
        result = seed_account(args.account_id, dry_run=args.dry_run)
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"预热未完成：{exc}\n")
    except Exception as exc:
        # Database/transport exception strings may contain private connection
        # information. Do not print them or a credentials-bearing traceback.
        parser.exit(1, f"预热未完成（{type(exc).__name__}），请检查私有数据库配置及公开行情连接。\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
