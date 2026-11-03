# 部署方式

支持宝塔原生 Python 项目和 Docker Compose 两种单机部署。两种方式均使用 **MySQL / InnoDB + 独立 Parquet 文件**，同一个业务库不要同时启动两套执行服务。宝塔步骤见 [宝塔部署指南](BAOTA.md)；本页介绍容器部署。

当前提供回测和模拟执行，真实下单在应用与编排中均关闭。旧版本数据先按 [一次性迁移说明](../docs/DATABASE.md#既有数据单次迁移) 处理，再启动新版本。

## 1. 准备私有配置

安装 Docker Engine 和 Compose v2，建议至少 4 GB 内存，并为宿主机及数据增长留出空间。数据库镜像固定为 `mysql:5.7.44`，与当前宝塔版本一致；镜像声明 `linux/amd64`，ARM 开发机器需要相应的容器兼容支持。未来升级必须同步验证开发、集成与生产环境的版本和行为。

在仓库根目录创建仅运维用户可读的 `.env`，填写下列值。真实凭据只留在私有部署环境中：

```dotenv
MK_DB_PASSWORD='replace-with-private-application-password'
MK_DB_ROOT_PASSWORD='replace-with-a-different-private-root-password'
MK_MASTER_KEY=
MK_COOKIE_SECURE=0
MK_ALLOWED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080
MK_HTTP_PORT=8080
```

```bash
chmod 600 .env
```

Compose 固定业务库和业务用户为 `mk_quant`；API、回测和运行进程使用业务用户，数据库 root 密码不传给应用。数据库服务不发布宿主机端口。内部数据库网络设为 `internal: true`，因此显式使用 `MK_DB_SSL_MODE=disabled`；跨主机数据库使用受验证的 TLS，参见 [数据库配置](../docs/DATABASE.md#环境与事务规则)。

MySQL 初始化环境变量仅对首次创建空数据卷生效。更换 `.env` 中的密码不会修改现有 MySQL 用户，应通过数据库权限管理同步轮换，再重建应用容器。

需要加密保存 Binance 凭据时，独立生成 Fernet 密钥并设置 `MK_MASTER_KEY`。默认空值支持回测与模拟，但拒绝加密录入实盘凭据。密钥与数据库备份分开保管；丢失密钥无法解密已有凭据。

## 2. 构建与启动

```bash
docker compose build
docker compose up -d
docker compose exec api python -m backend.worker create-admin
```

最后一条命令交互输入管理员用户名与密码。仅新安装需要创建；迁移已有管理员后跳过。默认没有演示管理员，客户由管理员在网页创建。

本机网页地址为 `http://127.0.0.1:8080`，API 为 `http://127.0.0.1:8765`。入口仅绑定回环地址。MySQL 健康检查通过后启动后端，API 健康后启动前端；查看所有容器状态，不能仅依赖 `up -d` 返回成功。

## 3. 进程与资源

| 服务 | 职责 | CPU 上限 | 内存上限 |
| --- | --- | --- | --- |
| mysql | InnoDB 核心业务数据库 | 未单独限定 | 1 GB |
| api | 登录、权限、配置、任务和查询 | 0.75 | 768 MB |
| backtest | 串行调度回测、历史 K 线下载及其独立任务进程 | 1.0 | 768 MB |
| runtime | 共享行情轮询及逐账户模拟进程 | 2.0 | 1 GB |
| frontend | Nginx 静态网页与同源 API 代理 | 0.25 | 128 MB |

`runtime` 不依赖 API 的生命周期，重启 API 不会关闭已有模拟运行进程。`docker compose down` 会停止整套部署；执行前先根据需要完成账户停止指令并检查回执。

`backtest` 同时连接内部 `database` 网络和提供公网出口的 `market` 网络：历史 K 线下载需要访问 Binance 公开 HTTPS 行情接口。该角色不接收凭证主密钥，下载不使用账户交易凭据。`runtime` 也有行情网络出口。所有后端角色挂载同一文件卷，以相同 UID 读写文件。

下载请求先写入 MySQL 任务队列，由 `backtest` 角色在独立、低优先级子进程中执行，完成后发布 Parquet 和数据集清单。下载与回测串行等待，不在实时运行角色执行；已有回测运行时，下载会显示排队。切换页面、关闭浏览器或单独重启 API 不会取消已提交给工作进程的下载。页面重新进入时从服务端恢复任务状态，排队和处理中保持下载按钮禁用；失败任务显示原因，修正后可重新提交。停止 `backtest` 会暂停任务处理，不能用 API 在线状态推断下载服务正常。

`data_download_jobs` 保存任务配置、进度、归属租约、失败原因和完成后的数据集编号。下载子进程异常或超时会记为失败；调度进程中断遗留的任务在 180 秒租约过期后可接管重试，旧执行者不能发布数据集。每个任务最多执行 3 次（含首次，最多 2 次过期接管），之后保留失败记录；恢复可能重新下载未提交的任务，不应理解为逐根 K 线断点续传。

只下载 1 分钟基础 K 线，不再限制单次总根数，后台分页并流式落盘；其他策略周期由完整、连续的分钟数据合成。下载按币安动态权重主动延时，429/418 完整等待 Retry-After 并续租，页面显示等待原因与下次尝试时间。`MK_DOWNLOAD_STALL_SECONDS` 默认 900 秒，只限制无进展时间（旧 `MK_DOWNLOAD_TIMEOUT_SECONDS` 为兼容别名）；合法限流等待和文件校验会更新心跳，不计为停滞。`MK_DOWNLOAD_MEMORY_MB` 默认 768 MB（256–2048，且不超过继承的地址空间硬限制），`MK_DOWNLOAD_DISK_RESERVE_MB` 默认 128 MB。下载进程不使用回测的累计 300 秒 CPU 限额，仍遵守进程优先级和部署资源配额；本次没有放宽回测限制。空间不足时任务明确失败，不发布残缺文件。

回测进程的默认 768 MB 地址空间上限用于限制进程虚拟地址空间，容器物理内存仍受 768 MB 限制。CPU、内存和 PID 上限不是物理资源预留；宿主机需监测 MySQL 连接数、内存、磁盘与整体负载，根据策略规模调整并发。

策略源码只允许可信管理员上传和保存。契约校验与独立进程限额不构成恶意 Python 的安全沙箱。

## 4. 持久化与联合备份

| 命名卷 | 容器位置 | 内容 |
| --- | --- | --- |
| `mysql-data` | MySQL 的 `/var/lib/mysql` | 用户、账户、策略版本、实例、订单、成交、账本、审计、下载任务及文件清单等 |
| `mk-artifacts` | 后端的 `/data` | `/data/artifacts` 下不可变的历史 K 线与回测明细 Parquet |

所有业务表显式使用 InnoDB；连接统一采用 UTC、READ COMMITTED 和严格 SQL 模式。文件先发布再提交数据库引用。备份必须包含 MySQL 一致性快照及其引用的文件，单独保留 SQL 或数据卷其中之一不完整。不要用 `docker compose down -v` 做日常重启，该命令会删除持久卷。

`deploy/backup.py` 需要安装本项目 Python 依赖的 Python 3.11 环境、兼容版本的 `mysqldump`，以及数据库连接和 Parquet 读取权限；应用镜像没有内置备份脚本和 MySQL 命令行客户端。Linux Docker 宿主机可准备独立 Python 环境及 MySQL 客户端，由运维用户在仓库目录执行以下示例。私有 `.env` 必须采用可信、可被 shell 读取的赋值格式：

```bash
set -a
. ./.env
set +a
export MK_DB_NAME=mk_quant
export MK_DB_USER=mk_quant
export MK_DB_PORT=3306
export MK_DB_SSL_MODE=disabled
export MK_DB_HOST="$(docker inspect "$(docker compose ps -q mysql)" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')"
export MK_ARTIFACT_ROOT="$(docker inspect "$(docker compose ps -q api)" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')/artifacts"
.venv/bin/python deploy/backup.py --directory /private-backup/mk-quant --mysqldump /path/to/compatible/mysqldump
```

替换 Python 环境、备份和工具路径，确认两个容器正在运行且查询到的目录正确。这里显式关闭 TLS 仅用于宿主机与隔离 Docker 内网之间的连接；跨主机应设置 `verify_identity` 和 `MK_DB_SSL_CA`，由应用驱动和 `mysqldump` 同时校验证书。宿主机需可访问 Docker 内部数据库地址和卷目录；此方法适用于 Linux 原生 Docker，不直接适用于 Docker Desktop 的虚拟机卷。后者应在具有相同工具、数据库网络和只读文件卷的专用备份环境中运行脚本。

脚本用 `--single-transaction` 导出 InnoDB 快照，再收集不可变 Parquet，输出权限 600 的归档与整体 SHA-256。归档包含 `database.sql.gz`、`artifacts/` 和 `manifest.json`。归档前校验数据库引用与文件哈希，只收集已发布文件，跳过临时写入文件并拒绝符号链接。备份期间可继续业务读写，但不可做 DDL 或删除文件；定期执行恢复演练。环境文件和 Fernet 主密钥不在归档内，分别安全备份。

恢复时停写，将 SQL 导入空的隔离业务库，按清单恢复文件并验证哈希、数据库引用及历史结果。文件目录须允许应用 UID `10001` 读取和写入。先完成权限、回放和独立工作进程验收，再切换正式服务。完整规则见 [联合备份与恢复](../docs/DATABASE.md#联合备份与恢复)。

## 5. 公网入口与日常更新

公网访问使用宿主机 HTTPS 入口，设置 `MK_COOKIE_SECURE=1` 和准确的 `MK_ALLOWED_ORIGINS`，修改后重建 API 容器以加载环境。当前仓库 Nginx 在容器内使用 HTTP；接入 TLS 终止代理时需限定可信代理并正确传递协议，不能接受公网客户端任意伪造的转发头。

```bash
docker compose ps
docker compose logs --tail=100 api runtime backtest
docker compose restart api
```

前端修改后重新构建并启动 `frontend`；后端发布涉及角色代码时按计划重建相应容器。修改环境变量需要重建容器，单纯 `restart` 不会重新读取 Compose 配置。发布前执行联合备份并记录版本；数据库版本升级应先在同版本开发、测试环境验证，再同步生产。

仅从同步下载升级为持久下载队列的历史更新，涉及前端、API 和 `backtest`；该步骤还需重建 `backtest` 容器以应用 `market` 网络配置。启动会创建缺少的下载任务表，已有业务表和 Parquet 不应清空或重建。下载队列同样纳入全库备份。

**升级至引擎 1.3.0 时，必须更新并重启 `api`、`backtest`、`runtime` 三个角色及前端。** 本次同时改变共用撮合引擎、运行期周期合成和结果结构，不能沿用上一轮“只重启 API 和研究进程”的发布方式。先完成以下预检：账户均取得停止回执，确认无持仓、挂单、条件单及待处理命令；回测与下载任务均已结束；数据库、Parquet、私有配置和原发布版本均已备份。有未处理的仓位或任务时，先完成业务处置和维护窗口安排，不直接停止运行角色。

预检通过后先构建镜像，再停止三个后端角色，更新源码和前端并重新创建对应容器；MySQL 和文件卷保留。启动时只会把精确匹配已知旧源码、且没有活动账户或未完成实验的内置策略升级并固定新版本；管理员自定义源码、已有禁用标记和旧策略版本不会被覆盖。上线后核对三个角色的心跳、引擎版本、小范围 1m 下载回测、汇总/逐币结果及历史报告读取，不能只检查 HTTP 首页。

保留所有旧数据集和实验。旧高周期数据不能被改标签当作分钟数据；新实验必须重新下载或导入连续 1m 数据。旧实验没有逐币结果时保持“未记录”的状态，需要以 1m 数据创建新实验，不能重新拆算或覆盖旧报告。新逐币明细沿用现有 `curve` / `validation_curve` Parquet 类型按交易对分组，仍随 MySQL 与文件联合备份。

`/healthz` 查询 MySQL 并报告存储类型，但不会验证所有文件和工作进程。账户运行状态仍以数据库中的执行端心跳、命令回执和账本更新为准。保持证书自动续期、备份任务和磁盘容量监测可观察。

开发和集成库可单独通过 `compose.database.yaml` 启动；开发端口 3307、测试端口 3308 均只绑定回环。测试需要专用 `_test` 数据库和 `MK_TEST_ALLOW_RESET=1`，不得指向生产。测试 fixture 会按 `db.TABLES` 清空全部业务表；新增下载任务表也必须列入该表清单，不可在共享测试库同时运行多组清库测试或连入开发工作进程。详见 [开发与集成验证](../docs/DATABASE.md#开发与集成验证)。

1.3.0 已有验证目前仅限隔离开发与专用测试库：累计 166 个独立测试经完整与定向回归覆盖；五币 28,800 行公开分钟行情验证网格 215 笔、分钟合成 1 小时双均线 4 笔成交，均落在对应分钟高低价内，训练/验证各 2,880 点且逐币权益可加总。交互详情使用保留极值预览与明细分页，完整报告及权益 CSV 流式读取；有限并发报告内存检查通过。详细范围、分批测试数与内存结果见 [验收清单](../docs/ACCEPTANCE.md)，该记录不能替代生产发布后的检查。

### 数据集删除与长区间下载更新

本次更新前确认没有排队/运行中的研究任务，完成 MySQL/Parquet 联合备份，然后更新前端、API 与研究进程；无需停止现有模拟运行角色。启动时自动添加 `data_download_jobs.waiting_reason`、`retry_at` 两个可空列（`mysql-003-download-waits`），兼容 MySQL 5.7 并串行处理并发启动，保留全部原记录。

数据集删除要求管理员与 CSRF 校验；有任意实验引用时拒绝，未引用时删除目录记录、解除下载日志引用并记审计。不可变 Parquet 保留用于共享内容和旧备份恢复，不在 API 请求中做文件垃圾回收。退出或恢复下载进程时只清理该执行者的临时写入文件，已发布文件不会删除。
