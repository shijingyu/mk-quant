"""Closed higher-timeframe bars derived exclusively from contiguous minute bars.

Buckets use UTC exchange boundaries (weekly buckets start Monday). A partial
leading/trailing bucket never becomes a strategy candle. State is JSON-safe so
the same accumulator can be persisted by a paper execution process.
"""
import copy

MINUTE_MS = 60000
INTERVAL_MINUTES = {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,
                    "1h":60,"2h":120,"4h":240,"6h":360,"8h":480,"12h":720,
                    "1d":1440,"3d":4320,"1w":10080}


def normalize_timeframes(value=None):
    value = copy.deepcopy(value if value is not None else {"primary":"1m","additional":[]})
    if not isinstance(value,dict) or set(value)-{"primary","additional"}:
        raise ValueError("timeframes 必须声明 primary 和可选 additional")
    primary, additional = value.get("primary"), value.get("additional",[])
    if not isinstance(primary,str) or primary not in INTERVAL_MINUTES:
        raise ValueError("timeframes.primary 必须是支持的分钟、小时或日周周期")
    if not isinstance(additional,list) or len(additional)>8 or any(not isinstance(item,str) or item not in INTERVAL_MINUTES for item in additional):
        raise ValueError("timeframes.additional 最多声明 8 个支持周期")
    if len(additional)!=len(set(additional)) or primary in additional:
        raise ValueError("timeframes 周期不可重复")
    return {"primary":primary,"additional":additional}


class MinuteFrames:
    def __init__(self, symbols, timeframes=None, history_window=1000):
        self.symbols = sorted(symbols)
        self.timeframes = normalize_timeframes(timeframes)
        self.intervals = list(dict.fromkeys(["1m",self.timeframes["primary"],*self.timeframes["additional"]]))
        self.history_window = history_window
        self.history = {interval:{symbol:[] for symbol in self.symbols} for interval in self.intervals}
        self.pending = {}
        self.last_timestamp = None
        self.primary_index = -1
        self.execution_candles = {}

    def push(self, bars, *, allow_gap=False):
        if set(bars)!=set(self.symbols):
            raise ValueError("1 分钟行情必须覆盖全部交易对，禁止丢弃不对齐分钟")
        stamps={bar["timestamp"] for bar in bars.values()}
        if len(stamps)!=1:
            raise ValueError("多币种 1 分钟时间戳必须一致")
        stamp=stamps.pop()
        if type(stamp) is not int or stamp%MINUTE_MS:
            raise ValueError("基础 K 线必须对齐 1 分钟开盘时间")
        if self.last_timestamp is not None and stamp-self.last_timestamp!=MINUTE_MS:
            if not allow_gap or stamp<=self.last_timestamp:
                raise ValueError("基础 K 线必须为连续 1 分钟数据；检测到缺口或非 1 分钟周期")
            # Runtime may resume after stale data. Never bridge an unfinished
            # bucket over the gap; preserve only previously completed history.
            self.pending={}
        self.last_timestamp=stamp
        self.execution_candles=copy.deepcopy(bars)
        completed=False
        for interval in self.intervals:
            width=INTERVAL_MINUTES[interval]*MINUTE_MS
            offset=4*86400000 if interval=="1w" else 0
            start=(stamp-offset)//width*width+offset
            accumulator=self.pending.get(interval)
            if accumulator is None or accumulator["start"]!=start:
                accumulator={"start":start,"complete":stamp==start,"count":0,"bars":{}}
                self.pending[interval]=accumulator
            accumulator["count"]+=1
            for symbol,bar in bars.items():
                aggregate=accumulator["bars"].get(symbol)
                if aggregate is None:
                    accumulator["bars"][symbol]={"timestamp":start,"open":bar["open"],"high":bar["high"],"low":bar["low"],"close":bar["close"],"volume":bar["volume"]}
                else:
                    aggregate.update(high=max(aggregate["high"],bar["high"]),low=min(aggregate["low"],bar["low"]),close=bar["close"],volume=aggregate["volume"]+bar["volume"])
            if stamp+MINUTE_MS==start+width:
                if accumulator["complete"] and accumulator["count"]==INTERVAL_MINUTES[interval]:
                    for symbol,bar in accumulator["bars"].items():
                        self.history[interval][symbol].append(dict(bar,close_time=start+width))
                        self.history[interval][symbol]=self.history[interval][symbol][-self.history_window:]
                    if interval==self.timeframes["primary"]:
                        self.primary_index+=1
                        completed=True
                self.pending.pop(interval,None)
        return completed

    def context(self):
        primary=self.timeframes["primary"]
        latest={interval:{symbol:copy.deepcopy(rows[-1]) for symbol,rows in symbols.items() if rows} for interval,symbols in self.history.items()}
        return {"candles":latest[primary],"history":copy.deepcopy(self.history[primary]),
                "timeframe_candles":latest,"timeframe_history":copy.deepcopy(self.history),
                "execution_candles":copy.deepcopy(self.execution_candles),
                "base_interval":"1m","signal_interval":primary,"bar_index":self.primary_index,
                "timestamp":self.last_timestamp,"decision_time":self.last_timestamp+MINUTE_MS if self.last_timestamp is not None else None}

    def dump(self):
        return {"version":1,"symbols":self.symbols,"timeframes":copy.deepcopy(self.timeframes),
                "history_window":self.history_window,"history":copy.deepcopy(self.history),
                "pending":copy.deepcopy(self.pending),"last_timestamp":self.last_timestamp,
                "primary_index":self.primary_index,"execution_candles":copy.deepcopy(self.execution_candles)}

    @classmethod
    def load(cls,state):
        if state.get("version")!=1:
            raise ValueError("不支持的周期合成状态版本")
        obj=cls(state["symbols"],state["timeframes"],state["history_window"])
        for key in ("history","pending","last_timestamp","primary_index","execution_candles"):
            setattr(obj,key,copy.deepcopy(state[key]))
        return obj
