# 宝塔原生部署指南

使用宝塔 MySQL 保存核心业务数据，使用独立 Parquet 目录保存历史 K 线和回测明细。宝塔 Python 项目分别管理 API、回测与持续模拟进程，Nginx 提供静态页面和同源 HTTPS 入口。本文的域名均为占位符，不包含实际服务器或账户信息。

当前版本提供回测和模拟执行，真实下单保持关闭。部署流程不替代 [实盘验收](../docs/BINANCE.md)。从旧版本升级时，先完成 [一次性数据迁移](../docs/DATABASE.md#既有数据单次迁移)，不要在迁移目标中提前创建管理员或启动服务。

## 1. 准备环境与目录

准备 Python 3.11、Node.js 22、Nginx，以及启用 Python 项目管理的宝塔 Linux 面板。数据库基线为 **MySQL 5.7.44**，与当前宝塔部署一致；开发、集成验证和生产必须保持数据库版本及事务行为一致。未来升级时，应先在隔离环境验证，再同步更新三个环境及两份 Compose 配置。

建议至少 4 GB 内存，并根据账户数量预留资源；小内存机器需要降低回测及账户并发，不能只根据页面可访问判断容量。

将源码部署到 `/www/wwwroot/mk-quant`，不要上传 `.deployment/`、本地环境文件、虚拟环境、`node_modules/` 或凭据。以下命令由服务器管理员执行，Python 路径按实际安装位置替换：

```bash
cd /www/wwwroot/mk-quant
useradd --create-home --shell /sbin/nologin mkquant
python3.11 -m venv /www/server/pyporject_evn/mk-quant
/www/server/pyporject_evn/mk-quant/bin/python -m pip install -r requirements.txt
npm ci --prefix frontend
npm run build --prefix frontend
install -d -m 750 -o mkquant -g mkquant data data/artifacts services
install -d -m 750 -o root -g mkquant config
```

如果 `mkquant` 用户已存在，不要重复创建。`pyporject_evn` 是宝塔环境目录的原有命名。Parquet 根目录应位于支持硬链接和 fsync 的本地文件系统；不放入前端静态目录。

## 2. 在宝塔创建业务数据库

在 **数据库 → MySQL → 添加数据库** 创建专用业务库，例如 `mk_quant`，使用 `utf8mb4`，访问权限限本机，并设置独立数据库用户和随机密码。不要给应用 MySQL root 凭据。应用用户只需要自身业务库的权限，包括首次建表所需权限。

通过面板创建的数据库会显示在宝塔数据库列表中，可查看表、备份和管理权限。Parquet 是独立文件，不会显示为 MySQL 表；它们的路径、校验和与版本清单保存在业务表中。

建表由应用执行，所有业务表显式使用 **InnoDB**、`utf8mb4_bin`。每次连接设置 UTC 会话时区、READ COMMITTED 隔离和严格 SQL 模式，不依赖面板全局默认值。开发库和测试库使用各自的数据库、用户和文件目录，不共用生产账户；3306 不对公网开放。

## 3. 配置三个角色

在 `config/api.env`、`config/backtest.env`、`config/runtime.env` 中分别填写以下配置。三个角色连接同一业务库并访问同一 Parquet 根目录；将密码占位符替换为私有值：

```dotenv
export MK_DB_HOST=127.0.0.1
export MK_DB_PORT=3306
export MK_DB_NAME=mk_quant
export MK_DB_USER=mk_quant
export MK_DB_PASSWORD='replace-with-private-database-password'
export MK_ARTIFACT_ROOT=/www/wwwroot/mk-quant/data/artifacts
export MK_DEMO=0
export MK_ENABLE_LIVE=0
export PYTHONUNBUFFERED=1
```

本机或 SSH 隧道的回环连接默认不另加 MySQL TLS。数据库在其他主机时，使用 `MK_DB_SSL_MODE=verify_identity` 并提供 `MK_DB_SSL_CA`，校验服务器证书和主机身份，参见 [数据库环境规则](../docs/DATABASE.md#环境与事务规则)。

仅 `api.env` 额外配置以下内容，HTTPS 来源必须匹配实际站点：

```dotenv
export MK_ALLOWED_ORIGINS=https://quant.your-domain.example
export MK_COOKIE_SECURE=1
export MK_MASTER_KEY=
```

回测角色可设置 `MK_BACKTEST_ADDRESS_SPACE_MB=2048`；这是地址空间上限，包含 PyArrow 映射空间，不是物理内存预留。

需要录入 Binance 凭据时，独立生成 Fernet 主密钥并保存到 API 私有环境文件。未配置主密钥仍可运行研究和模拟，但不能加密保存实盘凭据。回测与模拟不接收主密钥。主密钥单独备份；更换密钥不会自动重加密已有凭据。

```bash
chown root:mkquant config/*.env
chmod 640 config/*.env
```

环境文件不要进入版本库、共享日志或公开网页目录。使用 `export KEY=VALUE` 格式，避免宝塔仅执行 shell source 时变量未传给子进程。当前生成的 launcher 每次启动都会显式读取本角色的 `.env`，兼容有无 `export` 的赋值；修改环境后重启对应角色即可，无需重新注册项目。手动执行 CLI 时也必须显式加载环境。

## 4. 新安装初始化与服务注册

以下管理员初始化只适用于新安装，迁移已有用户时跳过。环境文件采用上面可被 shell 读取的赋值格式，并仅允许可信管理员编辑：

```bash
cd /www/wwwroot/mk-quant
sudo -u mkquant bash -c 'set -a; . config/api.env; set +a; exec /www/server/pyporject_evn/mk-quant/bin/python -m backend.worker create-admin'
```

命令交互输入管理员用户名和密码，不会覆盖已有管理员。也可使用 `create-admin --username admin --generate-password` 生成密码，仅在创建成功时输出一次；不要保存到公共日志。

依赖、数据库和环境文件准备完成后，使用宝塔自带 Python 注册：

```bash
/www/server/panel/pyenv/bin/python3 /www/wwwroot/mk-quant/deploy/bt_register.py
```

脚本注册独立命令项目，启用 `auto_run` 并启动服务。在 **网站 → Python 项目** 分别启动、停止、重启和查看日志：

| 项目 | 职责 | 入口 |
| --- | --- | --- |
| `mk-quant-api` | 登录、权限、账户、配置、任务与查询 | `services/api/launcher.py` |
| `mk-quant-backtest` | 回测队列与独立任务进程 | `services/backtest/launcher.py` |
| `mk-quant-runtime` | 共享行情轮询与逐账户模拟进程 | `services/runtime/launcher.py` |

脚本使用固定项目名和路径；同名项目属于其他路径时会拒绝继续。注册前的面板项目记录保存在已忽略的 `deploy/state/` 私有目录。常规源码更新无需重新注册；修改启动模板后需要重新生成入口并按角色重启。面板升级后先核对 Python 项目接口。

多核机器上，模板将回测与运行角色分配到不同核心；回测采用较低优先级。逐账户进程另有资源与决策超时限制。资源限制需要结合 MySQL 内存、连接数和策略规模验证，不等于容量保证。

## 5. 域名与 HTTPS

先将自有域名解析到服务器，再显式设置站点名称：

```bash
export MK_PUBLIC_DOMAIN=quant.your-domain.example
/www/server/panel/pyenv/bin/python3 /www/wwwroot/mk-quant/deploy/bt_web.py
```

`.example` 不能用于真实签发，请替换为自己控制的域名。`bt_web.py` 只登记静态站点，不修改 DNS，也不自动签发证书。

在宝塔对应站点的 SSL 面板申请并部署证书，确认 `/.well-known/acme-challenge/` 可通过 HTTP 访问。签发后渲染模板，在面板检查并应用到该站点：

```bash
cd /www/wwwroot/mk-quant
envsubst '${MK_PUBLIC_DOMAIN}' < deploy/bt-nginx.conf > /tmp/mk-quant-nginx.conf
```

仅替换 `MK_PUBLIC_DOMAIN`，不要无参数调用 `envsubst`，以免替换 Nginx 的 `$host`、`$uri`。核对模板中的 include 和证书路径，运行 `nginx -t` 后平滑重载。

Nginx 将 `/api/` 代理到 `127.0.0.1:8765`，客户随机路由回退到 `index.html`。公网只开放必要的 Web 入口，API 和数据库不直接暴露。SSL 自动续期由宝塔证书管理及计划任务负责；必须确认对应任务已启用，检查近期执行记录，并在续期后确认 Nginx 已加载新证书。仓库不会假定注册站点就已经启用续期。

## 6. 发布与日常管理

| 内容 | 默认位置 |
| --- | --- |
| 源码 | `/www/wwwroot/mk-quant` |
| 前端构建 | `/www/wwwroot/mk-quant/frontend/dist` |
| 核心业务数据库 | 宝塔 MySQL 数据库列表中的 `mk_quant` |
| Parquet 文件 | `/www/wwwroot/mk-quant/data/artifacts` |
| 角色配置 | `/www/wwwroot/mk-quant/config/{api,backtest,runtime}.env` |
| Python 日志 | `/www/wwwlogs/python/mk-quant-api/error.log`，其他角色替换 `api` |
| Nginx 日志 | `/www/wwwlogs/mk-quant.access.log`、`mk-quant.error.log` |

发布前备份数据库、Parquet 和私有配置，记录源码版本。前端静态更新无需重启模拟进程；单独重启 API 不会主动停止其他角色。停止 `runtime` 会影响它管理的模拟进程，应先在业务界面执行停止开仓或平仓停止，并核对执行端回执。数据库切换、表结构变更和完整恢复时停止所有写入进程。

更新源码时保留 `data/`、`config/`、主密钥和备份。磁盘清理不能删除 MySQL 数据目录、未完成迁移的回退备份或被数据库清单引用的 Parquet 文件。归档保留策略应根据恢复要求设置。

## 7. 联合备份与恢复

由有权读取私有环境和 Parquet 的运维用户执行，脚本自动优先选择宝塔的 `mysqldump`：

```bash
cd /www/wwwroot/mk-quant
set -a
. config/api.env
set +a
/www/server/pyporject_evn/mk-quant/bin/python deploy/backup.py --directory /www/backup/mk-quant
```

脚本使用 `mysqldump --single-transaction` 获取 InnoDB 一致性快照，再收集不可变 Parquet。每个权限为 600 的归档包含 `database.sql.gz`、`artifacts/` 和带 SHA-256 的 `manifest.json`，输出归档路径及整体校验和。MySQL 凭据通过临时私有选项文件传递，不放在命令参数中。备份会检查数据库引用及文件校验和，只纳入已发布文件，排除写入临时文件并拒绝符号链接。备份可以与业务读写并行，但备份期间不能修改表结构或删除历史文件。

在宝塔计划任务中使用同样的环境加载步骤，检查执行结果、磁盘容量并设置保留期限。备份与应用采用相同的 `MK_DB_SSL_MODE` / `MK_DB_SSL_CA` 规则，远程连接必须通过 `VERIFY_IDENTITY` 校验；所选 `mysqldump` 必须支持这些选项。宝塔单库备份可额外启用，但不能代替包含 Parquet 的联合备份。归档不包含环境文件或主密钥，它们应分别安全保管。

恢复前停止三个 Python 服务，在空的隔离业务库导入 SQL，恢复 `artifacts/` 到配置的根目录，并逐项校验清单及数据库引用。目录和文件分别使用 750/640，归 `mkquant` 可读写。先验证登录、历史实验导出、回放与权限，再切换服务环境并启动。详细流程见 [联合备份与恢复](../docs/DATABASE.md#联合备份与恢复)。

## 8. 验证

检查 HTTPS 首页、登录会话、客户权限、历史行情、回测与模拟命令回执。`/healthz` 会查询 MySQL，并报告 `database=mysql`、`table_engine=InnoDB`、`artifact_storage=parquet`；它不替代逐表引擎检查、文件校验或工作进程验收。

在数据库管理工具执行以下只读查询，确认业务表均为 InnoDB：

```sql
SELECT TABLE_NAME, ENGINE
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE();
```

单独重启 API，确认模拟进程心跳和账本仍更新；页面状态应来自执行端回执。验收范围见 [实现范围与验收清单](../docs/ACCEPTANCE.md)。
