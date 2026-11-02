# Binance 适配器与实盘边界

本文件对应 `backend/binance.py`。接口文档核对日期为 2026-11-02。测试通过注入运输层完成，未使用真实密钥，也未执行真实下单。使用 curl 真实读取 BTCUSDT 永续和 BTCUSDC 现货公开 K 线成功；本机独立 Python 的证书链验证失败，适配器会明确报错。部署需通过 `SSL_CERT_FILE` 等方式配置组织认可的 CA 证书链，不得关闭 TLS 验证。

## 连接与行情

`BinanceClient(market='futures'|'spot', api_key='', api_secret='', testnet=False)`；兼容 `perpetual` 别名。USDⓈ-M 主网为 `https://fapi.binance.com`，现货为 `https://api.binance.com`。只列出 USDT、USDC 的现货及永续合约，交割合约不混入永续列表。

| 方法 | 返回值 |
| --- | --- |
| `exchange_info(refresh=False)` | `{market,symbols,rate_limits,fetched_at,source}`；每个 symbol 含 tick_size、step_size、min_qty、min_notional 及原始 filters |
| `klines(symbol, interval, start_ms, end_ms)` | UTC 毫秒区间 `[start,end)`，只返回完整闭合蜡烛；含 timestamp/open/high/low/close/volume/close_time |
| `download_klines` / `fetch_klines` | 同 `klines` |
| `commission(symbol)` | 帐户实际 maker/taker 小数费率，需签名；无密钥时抛出错误，不冒充自动读取 |

行情接口为 `GET /api/v3/exchangeInfo`、`GET /api/v3/klines` 以及 `GET /fapi/v1/exchangeInfo`、`GET /fapi/v1/klines`。按时间分页每页 1000 条，并防止游标停滞。交易数量和价格按 `PRICE_FILTER`、`LOT_SIZE`、`MARKET_LOT_SIZE` 的实际步长使用 Decimal 向下量化，不能拿展示小数位替代交易规则。参见 [USDⓈ-M 行情接口](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data) 和 [Binance 官方现货 REST 文档](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)。

只对 GET 做有界重试。429 遵循 `Retry-After`，长冷却或 418 返回错误并阻止提前请求；写操作不自动重试。API 错误不输出 URL、签名、Key、Secret 或原始服务器回显。币安限额按 IP 统计，因此生产中的行情进程应统一下载，并在部署出口统一管理请求额度；本类的进程内节流不是跨进程限流器。[官方现货 HTTP 与限流说明](https://developers.binance.com/en/docs/products/spot/rest-api)

## 只读账户检查

`inspect_account(manifest,symbols)` / `check_account(...)` 返回：

```python
{
    'ok': False,
    'checks': [{'name': 'permissions', 'status': 'pass|fail|unknown', 'message': '...'}],
    'adjustments': [{'setting': 'leverage', 'symbol': 'BTCUSDT', 'current': 20, 'target': 1}],
    'identity': 'sha256 of exchange account identity',
    'balances': [], 'positions': [], 'orders': [], 'settings': {},
    'available_capital': '1000', 'checked_at': 0
}
```

要求策略声明 `market`、`symbols:{mode,values,min_count}` 和 `account:{position_modes,margin_modes,leverage_min,leverage_max,min_capital}`。`unknown` 与失败一样阻止接入。检查凭证、API 查询和市场交易权限、交易对、账户身份、正确钱包可用资金、账户模式、币种杠杆/保证金、已有持仓、普通和条件挂单。USDC 交易不能用另一钱包中的 USDT 余额冒充已备足资金。多资产保证金、统一账户/PAPI 暂不支持。

合约签名读取：`GET /fapi/v3/account`、`/fapi/v3/balance`、`/fapi/v1/accountConfig`、`/fapi/v1/symbolConfig`、`/fapi/v3/positionRisk`、`/fapi/v1/openOrders`、`/fapi/v1/openAlgoOrders`。空仓时也查询 symbolConfig，不能把 positionRisk 缺少记录解释为默认杠杆。合约身份取交易所 `accountAlias` 的 SHA-256；现货从 `/api/v3/account` 的 uid 构建哈希，外层数据库需要按该身份实施唯一绑定。费率读取 `/fapi/v1/commissionRate`。[官方合约账户接口](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)

普通订单与条件订单必须同时检查。条件订单已迁移至 Algo 服务，只查 openOrders 不足以判断账户是否空闲。明确调用 `apply_adjustments(manifest,symbols,confirmed=True)` 才会修改模式/保证金/杠杆，逐项变更前重新确认全账户无持仓和两类挂单，再调用 `POST /fapi/v1/positionSide/dual`、`/fapi/v1/marginType` 或 `/fapi/v1/leverage`，完成后重新检查。该方法不会取消订单或平仓，也不因部分失败继续尝试后续更改。[官方合约交易接口](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)

权限读取使用主网 `GET /sapi/v1/account/apiRestrictions`：enableReading、enableFutures 或 enableSpotAndMarginTrading。[官方 API 权限接口](https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/account) 测试环境不提供此 SAPI，因此严格接入检查会标记未知，不将测试网权限伪装为已核验。

现货费率来自 `/api/v3/account/commission`。基础 maker/taker 与 buyer/seller、taxCommission、specialCommission、BNB discount 分别返回，调用者应在费用模型中显式应用方向、税费和折扣，不能把基础费率当作所有场景的完整成本。[官方费用说明](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/commission_faq.md)

检查属于瞬时快照，无法证明未来没有外部交易。生产接入必须使用专用账户，运行中持续从账户事件流及 REST 对账检查未管理交易。检查本身不会自动转移资金、取消挂单或接管已有持仓。

## 下单网关

默认禁用。启用需要 `configure_live(enabled=True,journal=MySQLOrderJournal(),execution_guard=callable)`，且此前通过完整账户检查。检查有效期 60 秒。网关只允许 MARKET/LIMIT，市价参考价格不得超过 5 秒，现货 SELL 重新检查可用资产。双向合约的 positionSide 与 reduceOnly 组合严格校验。触发单/止损单下单尚未实现，禁止使用普通 order 路径代替 Algo 接口。

`submit_order(symbol,side,quantity,client_order_id='mk_…',order_type='MARKET',price=None,reference_price=None,reference_timestamp_ms=None,reduce_only=False,position_side='BOTH')` 在发送前将交易意图与 clientOrderId 写入 MySQL InnoDB 的 `binance_order_intents` 表（唯一键与事务）；同一账户/ID 唯一，内容变化会拒绝。网络超时/5xx 等结果不确定时仅用 `origClientOrderId` 查询。查无记录也保持 UNKNOWN，不自动重发；崩溃重启后仍服从该记录。意图已确认时重复调用返回既有结果。

这只是执行边界组件，**不是已经验收的无人值守实盘系统**。运行实例产生持仓后不能重新使用“空账户接入检查”续期；正式运行还需单独实现有账本依据的运行期授权续期、账户事件流、部分成交/手续费/资金费率对账、清算和紧急撤单/平仓。`execution_guard` 能检查租约和停止标记，但仅靠回调无法阻止进程在检查后暂停又恢复发送：生产必须把密钥与出口放在单一、可强制隔离旧持有者的执行网关，或通过基础设施隔离完成真正 fencing。上线前需要演练断网、重复回调、进程恢复和交易所未知订单等故障。

本次实现不持有密钥持久副本；密钥加密、管理员授权、身份唯一约束、命令持久化及运行编排由控制服务负责。实盘 enable 环境开关不应直接向策略暴露。
