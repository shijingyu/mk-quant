# 宝塔原生部署指南

使用宝塔 MySQL 保存核心业务数据，使用独立 Parquet 目录保存历史 K 线和回测明细。宝塔 Python 项目分别管理 API、研究任务（回测与历史 K 线下载）和持续模拟进程，Nginx 提供静态页面和同源 HTTPS 入口。本文的域名均为占位符，不包含实际服务器或账户信息。

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

回测角色默认 `MK_BACKTEST_ADDRESS_SPACE_MB=768`；这是地址空间上限，包含 PyArrow 映射空间，不是物理内存预留。资源不足会将任务标记失败并提示调整区间或独立资源预算。只有确认宿主机剩余容量足够时才提高此配置；已有显式设置需要同步更新。

`backtest` 角色还负责历史 K 线下载，需要解析并通过 HTTPS 访问 Binance 公开行情接口；防火墙或代理不能只允许该角色连接 MySQL。下载使用公开接口，不需要交易 API Key。下载与回测在此角色串行调度，各任务运行于独立的低优先级子进程，不占用 `runtime` 角色的任务进程。

只下载 1 分钟基础 K 线，不再限制单次总根数，后台分页并流式落盘；其他策略周期由完整、连续的分钟数据合成。下载按币安动态权重主动延时，429/418 完整等待 Retry-After 并续租，页面显示等待原因与下次尝试时间。`MK_DOWNLOAD_STALL_SECONDS` 默认 900 秒，只限制无进展时间（旧 `MK_DOWNLOAD_TIMEOUT_SECONDS` 为兼容别名）；合法限流等待和文件校验会更新心跳，不计为停滞。`MK_DOWNLOAD_MEMORY_MB` 默认 768 MB（256–2048，且不超过继承的地址空间硬限制），`MK_DOWNLOAD_DISK_RESERVE_MB` 默认 128 MB。下载进程不使用回测的累计 CPU 限额，仍遵守进程优先级和部署资源配额。空间不足时任务明确失败，不发布残缺文件。

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
| `mk-quant-backtest` | 回测、历史 K 线下载队列及独立任务进程 | `services/backtest/launcher.py` |
| `mk-quant-runtime` | 共享行情轮询与逐账户模拟进程 | `services/runtime/launcher.py` |

脚本使用固定项目名和路径；同名项目属于其他路径时会拒绝继续。注册前的面板项目记录保存在已忽略的 `deploy/state/` 私有目录。常规源码更新无需重新注册；修改启动模板后需要重新生成入口并按角色重启。面板升级后先核对 Python 项目接口。

多核机器上，模板将回测与运行角色分配到不同核心；回测采用较低优先级。逐账户进程另有资源与决策超时限制。资源限制需要结合 MySQL 内存、连接数和策略规模验证，不等于容量保证。

这些进程限制不是恶意代码沙箱，策略源码仍只接受可信管理员。无需额外注册下载服务：在宝塔保持 `mk-quant-backtest` 运行即可处理下载队列。下载排队和进度保存在 MySQL，切页、关闭浏览器或单独重启 API 不会撤销已提交任务；已运行的回测结束后才会执行等待中的下载。工作角色停止时任务不会因为页面刷新而变成已完成，应检查工作角色心跳和任务回执。

在宝塔业务库中可查看 `data_download_jobs` 的任务状态、进度、失败原因和数据集编号。下载子进程异常或超时会记为失败；调度进程中断后，未完成任务等待 180 秒租约过期再由新执行者接管，旧执行者不能发布数据集。每个任务最多执行 3 次（含首次，最多 2 次过期接管），之后保留失败记录；恢复会重新下载尚未提交的任务，不是逐根 K 线断点续传。

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

仅从同步下载升级为持久下载队列的历史更新，涉及前端、API 与 `backtest`。新增下载任务表由启动时建表流程补齐，不需要清库；不要只更新 API，否则已入队任务没有新版工作进程执行。保留原来的三个宝塔项目，不需重复注册。

**引擎 1.3.0 更新必须预检后重启全部三个 Python 角色：`mk-quant-api`、`mk-quant-backtest`、`mk-quant-runtime`。** 本次修改共用分钟撮合、运行期高周期合成和逐币结果结构，只重启控制与研究服务会让模拟进程继续执行旧语义。

发布前确认所有账户均已取得停止回执，持仓、普通挂单、条件挂单和待处理命令均为空；确认没有排队或运行中的回测、下载。存在未处理风险或任务时，先通过业务流程处理并安排维护窗口，不能直接停止运行角色。完成 MySQL/Parquet 联合备份，独立备份私有配置、主密钥和原源码，检查可用内存与磁盘。预检通过后停止三个角色、发布代码和前端，再通过原有宝塔项目启动三个角色并核对心跳与版本。

启动时仅升级精确匹配已知旧源码、且没有活动账户或未完成实验的内置策略；固定旧、新源码版本，保留管理员编辑与可信/禁用标记。保留旧数据集、实验、账本和 Parquet；不要覆盖旧报告或把小时数据改标签为 `1m`。新回测需要连续、对齐的分钟数据。旧实验没有逐币结果时应明确未记录，重新下载分钟数据后创建新实验才会生成汇总与逐币视图。逐币曲线仍存放在现有曲线 Parquet 中，使用交易对字段分组，无需增加宝塔服务或单独公开文件目录。

更新源码时保留 `data/`、`config/`、主密钥和备份。磁盘清理不能删除 MySQL 数据目录、未完成迁移的回退备份或被数据库清单引用的 Parquet 文件。归档保留策略应根据恢复要求设置。

## 7. 联合备份与恢复

### 执行进程故障恢复

`deploy/bt_register.py` 生成的运行、研究 launcher 保留一个宝塔可跟踪的父进程，由 `deploy/worker_supervisor.py` 管理实际 worker。异常退出后按 5–30 秒退避重启，并先清理旧进程组；角色独占文件锁防止重复启动。Linux 设置父进程死亡信号。宝塔主动停止/重启时先停止整个旧进程组，不会把人工停止当作故障重启。API 仍独立运行。

既有部署升级必须同时发布守护模块并刷新 `services/runtime/launcher.py` 与 `services/backtest/launcher.py`，仅替换 `backend/worker.py` 不会自动增加父进程守护。先保存账户快照、持仓和订单基线，确认无研究任务执行，再由原宝塔项目停止旧 worker，核实所有旧账户子进程均已退出，之后启动新版 launcher 并核对心跳和历史订单。不要额外并行启动第二套 systemd/cron 执行者。

工作进程会重试明确的临时数据库连接/事务错误；凭证、表结构、磁盘写满及策略逻辑错误仍显式失败。数据库断线期间不得产生未持久化的成交，重连后重新加载账本；中断时段不能视为连续正常运行。单实例内存上限不代表整机已预留资源，多应用主机需核对总内存与交换空间，避免操作系统 OOM 杀掉数据库。交换文件可缓冲短时压力，不能代替足够物理内存。

### 备份操作

由有权读取私有环境和 Parquet 的运维用户执行，脚本自动优先选择宝塔的 `mysqldump`：

```bash
cd /www/wwwroot/mk-quant
set -a
. config/api.env
set +a
/www/server/pyporject_evn/mk-quant/bin/python deploy/backup.py --directory /www/backup/mk-quant
```

脚本使用 `mysqldump --single-transaction` 获取 InnoDB 一致性快照，再收集不可变 Parquet。每个权限为 600 的归档包含 `database.sql.gz`、`artifacts/` 和带 SHA-256 的 `manifest.json`，输出归档路径及整体校验和。MySQL 凭据通过临时私有选项文件传递，不放在命令参数中。备份会检查数据库引用及文件校验和，只纳入已发布文件，排除写入临时文件并拒绝符号链接。备份可与交易业务读写并行；文件生命周期锁会暂缓研究文件的最终发布和删除，保障 SQL 快照与文件一致。备份期间不能修改表结构或手工删除文件。

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

提交历史 K 线下载后，确认立即取得任务编号，切换标签页或刷新页面仍显示排队/处理中，并保持下载按钮禁用。单独重启 API 后，工作角色应继续下载；完成后数据集清单和 Parquet 可读，失败后显示原因并允许重试。查看队列状态时同时检查 `mk-quant-backtest` 心跳，`/healthz` 正常并不代表下载工作角色在线。执行清库式集成测试必须使用独立 `_test` 库及单独文件目录，不能连接此生产库。

1.3.0 发布后还需用小范围多币种 1m 数据核对：高周期仅在完整收盘后触发策略；成交价格位于对应分钟的最低价与最高价之间；训练/验证分别提供汇总及各币指标和曲线；逐时刻各币归因权益相加等于组合权益。逐币初始资金按等份作为归因基准，并非独立账户重跑。确认旧实验仍可读取与导出、缺少的逐币结果没有被伪造，最后记录三个角色版本和验收结果。

1.3.0 已有验证目前仅限隔离开发与专用测试库：累计 166 个独立测试经完整与定向回归覆盖；五币 28,800 行公开分钟行情验证网格 215 笔、分钟合成 1 小时双均线 4 笔成交，均落在对应分钟高低价内，训练/验证各 2,880 点且逐币权益可加总。交互详情使用保留极值预览与明细分页，完整报告及权益 CSV 流式读取；有限并发报告内存检查通过。详细范围、分批测试数与内存结果见 [验收清单](../docs/ACCEPTANCE.md)，该记录不能替代生产发布后的检查。

### 数据集删除与长区间下载更新

本次更新前确认没有排队/运行中的研究任务，完成 MySQL/Parquet 联合备份，然后更新前端、API 与研究进程；无需停止现有模拟运行角色。启动时自动添加 `data_download_jobs.waiting_reason`、`retry_at` 两个可空列（`mysql-003-download-waits`），兼容 MySQL 5.7 并串行处理并发启动，保留全部原记录。

数据集删除要求管理员与 CSRF 校验；有任意实验引用时拒绝，未引用时删除目录记录、解除下载日志引用并记审计。删除提交后清理无其他引用的 Parquet；共享文件保留到最后一份引用删除。实验删除只允许已完成、失败、已取消任务，清理结果明细且保留 K 线。清理与发布、联合备份通过共享存储根目录的 flock 互斥，所有角色必须使用相同的存储路径。清理失败写入持久审计待重试，API 启动时自动重试。更新时先确认没有进行中的下载或回测，部署 API、研究进程和新版备份脚本；已有联合归档不受在线文件删除影响。退出或恢复下载进程仍只清理该执行者的临时写入文件。


### 流式回测（引擎 1.4.0）

回测行情按交易对分批读取，权益、决策、成交与历史终态挂单使用任务私有压缩临时文件；内存只缓存有限数据块、策略历史窗口和活动状态。临时文件位于 `MK_ARTIFACT_ROOT` 下的 `.research-<pid>-*` 目录，使用系统自动解除目录链接的临时文件，子进程退出或被取消后由操作系统释放。回测持续检查至少 512 MiB 临时存储余量；最终 Parquet 发布保留至少 128 MiB 余量，空间不足明确失败。

内存预算仍默认 768 MiB。长回测改用进展监测：`MK_BACKTEST_STALL_SECONDS=900` 限制无进展时间，`MK_BACKTEST_CPU_SECONDS=43200` 限制累计 CPU 时间，`MK_BACKTEST_WALL_SECONDS=86400` 限制总运行时间。研究并发仍为一项并保持低优先级；交易运行使用独立角色。配置按宿主机容量调整，限制不是资源预留。长回测不会因关闭网页中断，但占用 CPU 与磁盘的时间可能较长。
