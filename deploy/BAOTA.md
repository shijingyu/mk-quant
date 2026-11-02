# 宝塔原生部署指南

使用宝塔原生 Python 项目管理控制 API、回测与持续模拟进程，Nginx 提供静态页面和同源 HTTPS 入口。本文只包含通用路径和占位地址，不包含实际服务器地址、域名或账户口令。

当前版本提供回测和模拟执行，真实下单保持关闭。注册脚本针对宝塔 Linux 的 Python 项目模型编写，面板版本升级后需先核对接口；它不修改面板代码，也不管理其他项目。

## 1. 准备目录与依赖

准备 Python 3.11、Node.js 22、Nginx 和已启用 Python 项目管理的宝塔 Linux 面板。建议至少 4 GB 内存，并根据账户数量预留资源；两核小内存机器仅适合限制并发的验证环境。

将源码部署到 `/www/wwwroot/mk-quant`，不要上传本地数据库、`.deployment/`、虚拟环境、`node_modules/` 或任何凭据文件。以下命令以服务器管理员执行，Python 命令按实际安装位置替换：

```bash
cd /www/wwwroot/mk-quant
useradd --create-home --shell /sbin/nologin mkquant
python3.11 -m venv /www/server/pyporject_evn/mk-quant
/www/server/pyporject_evn/mk-quant/bin/python -m pip install -r requirements.txt
npm ci --prefix frontend
npm run build --prefix frontend
install -d -m 750 -o mkquant -g mkquant data services
install -d -m 750 -o root -g mkquant config
```

如果 `mkquant` 用户已经存在，不要重复创建。宝塔环境目录 `pyporject_evn` 使用面板本身的命名。Python 依赖范围在 `requirements.txt`，`requirements.deployed.lock` 保存一份已验证 Linux 环境快照；使用锁文件前核对本机 Python 与平台兼容性。

## 2. 设置独立环境文件

在 `config/api.env`、`config/backtest.env`、`config/runtime.env` 中分别填写环境配置。所有角色指向同一数据库，均使用：

```dotenv
MK_DB_PATH=/www/wwwroot/mk-quant/data/mk.sqlite3
MK_DEMO=0
MK_ENABLE_LIVE=0
PYTHONUNBUFFERED=1
```

仅 `api.env` 额外配置以下项目，把示例域名换成自己的 HTTPS 来源：

```dotenv
MK_ALLOWED_ORIGINS=https://quant.your-domain.example
MK_COOKIE_SECURE=1
MK_MASTER_KEY=
```

回测与模拟不需要主密钥。需要录入 Binance 凭据时，用 `cryptography.fernet.Fernet.generate_key()` 生成主密钥，在服务器的私有配置文件中保存；不要将它写入 shell 历史、源码或公开日志。未配置密钥仍可运行研究和模拟，但不能加密保存实盘凭据。

```bash
chown root:mkquant config/*.env
chmod 640 config/*.env
```

密钥需要独立备份。更换主密钥不会自动重加密已有凭据。

## 3. 初始化管理员与注册服务

创建管理员时使用交互输入，并以运行用户写数据库：

```bash
cd /www/wwwroot/mk-quant
sudo -u mkquant env MK_DEMO=0 MK_DB_PATH=/www/wwwroot/mk-quant/data/mk.sqlite3 /www/server/pyporject_evn/mk-quant/bin/python -m backend.worker create-admin
```

`create-admin --username admin --generate-password` 可生成高强度密码，并且只在创建成功时输出一次。不要将命令输出保存到网页目录、共享日志或 Git 仓库。已存在的管理员不会被此命令覆盖。

使用宝塔自带 Python 执行注册脚本：

```bash
/www/server/panel/pyenv/bin/python3 /www/wwwroot/mk-quant/deploy/bt_register.py
```

脚本注册独立命令项目、启用 `auto_run`，并启动以下服务。在 **网站 → Python 项目** 可分别启动、停止、重启与查看日志：

| 项目 | 职责 | 入口 |
| --- | --- | --- |
| `mk-quant-api` | 登录、权限、账户、配置、任务与查询 | `services/api/launcher.py` |
| `mk-quant-backtest` | 回测队列与独立任务进程 | `services/backtest/launcher.py` |
| `mk-quant-runtime` | 共享行情轮询与逐账户模拟进程 | `services/runtime/launcher.py` |

脚本使用固定项目名和路径，发现同名项目属于其他路径时会拒绝继续。注册前的宝塔项目记录保存在 `deploy/state/`，此目录已被 Git 忽略，不应公开。常规源码更新无需重复执行注册。

## 4. 域名与 HTTPS

先把自己的域名解析到服务器，在本次 shell 中设置域名，例如：

```bash
export MK_PUBLIC_DOMAIN=quant.your-domain.example
/www/server/panel/pyenv/bin/python3 /www/wwwroot/mk-quant/deploy/bt_web.py
```

示例后缀 `.example` 不能用于真实签发，请替换成自己控制的域名。`bt_web.py` 要求明确提供 `MK_PUBLIC_DOMAIN`，不会内置任何实际地址。脚本只负责登记静态站点，不负责修改 DNS 或自动签发证书。

在宝塔对应站点 SSL 面板申请并部署证书，确认 `/.well-known/acme-challenge/` 可通过 HTTP 访问。证书签发后，渲染 Nginx 模板，再在面板中检查并应用到该站点：

```bash
cd /www/wwwroot/mk-quant
envsubst '${MK_PUBLIC_DOMAIN}' < deploy/bt-nginx.conf > /tmp/mk-quant-nginx.conf
```

只替换指定的 `MK_PUBLIC_DOMAIN`，不要无参数调用 `envsubst`，否则会误替换 Nginx 的 `$host`、`$uri` 等变量。模板假定宝塔使用 `html_<域名>.conf` 和标准证书路径；根据实际站点核对 include 文件，再运行 `nginx -t` 并平滑重载。

Nginx 将 `/api/` 代理到本机 `127.0.0.1:8765`，客户随机路由回退到 `index.html`。公网仅开放必要的 Web 入口；API 不直接暴露公网。确认站点 SSL 续签计划启用，并持续保持解析与验证路径可达。

## 5. 发布与日常管理

| 内容 | 默认路径 |
| --- | --- |
| 源码 | `/www/wwwroot/mk-quant` |
| 前端构建 | `/www/wwwroot/mk-quant/frontend/dist` |
| 数据库 | `/www/wwwroot/mk-quant/data/mk.sqlite3` |
| 角色配置 | `/www/wwwroot/mk-quant/config/{api,backtest,runtime}.env` |
| Python 日志 | `/www/wwwlogs/python/mk-quant-api/error.log`，其他角色替换 `api`。 |
| Nginx 日志 | `/www/wwwlogs/mk-quant.access.log`、`mk-quant.error.log`。 |

前端静态更新不需要重启模拟进程；只重启 API 也不会主动停止其他角色。停止 `runtime` 会停止它管理的模拟进程，应先在业务界面处理停止开仓或平仓停止，并检查执行回执。更新源码时保留 `data/`、`config/`、凭据和备份。

默认启动模板在多核机器上将回测与运行分配到不同核心；回测使用低优先级和 512 MB 地址空间上限。逐账户进程另有资源与决策超时限制。这些限制不等于容量保证，需要根据策略和数据规模压测；调整 `deploy/bt_register.py` 中的启动模板后重新生成对应入口。

## 6. 备份与恢复

SQLite 必须使用同一主机的本地文件系统，不能迁到 NFS 或多机共享目录。使用在线备份脚本生成一致性副本：

```bash
/www/server/pyporject_evn/mk-quant/bin/python /www/wwwroot/mk-quant/deploy/backup.py
```

脚本默认备份到 `/www/backup/mk-quant/`，权限为 `600`，并执行完整性检查。可在宝塔计划任务中登记命令，同时设置保留策略和磁盘监测。数据库备份不包含加密主密钥，主密钥应单独安全保管。

恢复前停止三个 Python 服务，保留当前数据库与 WAL 文件，验证备份后切换数据，确认 `mkquant` 的读写权限，再逐项启动。需要通过实际恢复演练验证流程，不能只检查备份文件存在。

## 7. 验证

部署后分别检查 HTTPS 首页、`/healthz`、登录会话、账户权限、回测任务和模拟命令回执。单独重启 API 后确认模拟进程继续运行。`/healthz` 只证明控制服务存活，执行状态要检查工作进程心跳、账本更新和命令结果。

自动化与人工验收范围见 [实现范围与验收清单](../docs/ACCEPTANCE.md)。正式实盘仍需完成事件对账、保护单、运行授权与故障接管等验收，当前部署不会提交真实交易订单。
