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

每次下载跨全部交易对合计最多 100000 根 K 线，较大范围需分段提交。下载默认总时限为 900 秒，可在 `backtest` 容器环境中设置 `MK_DOWNLOAD_TIMEOUT_SECONDS`（有效范围 30–3600）；Linux 子进程默认地址空间上限为 768 MB，可设置 `MK_DOWNLOAD_MEMORY_MB`（有效范围 256–2048，且不超过继承的回测硬上限）。地址空间上限不代表可用物理内存，仍受容器 768 MB 内存配额约束；调整前应验证 Parquet 发布及宿主机余量。

回测进程的 2048 MB 地址空间上限用于限制进程虚拟地址空间，容器物理内存仍受 768 MB 限制。CPU、内存和 PID 上限不是物理资源预留；宿主机需监测 MySQL 连接数、内存、磁盘与整体负载，根据策略规模调整并发。

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

从同步下载版本升级时，需同时更新前端、API 和 `backtest`，并重建 `backtest` 容器以应用 `market` 网络配置。启动会创建缺少的下载任务表，已有业务表和 Parquet 不应清空或重建。发布前确认旧版同步下载请求、回测均已结束；本轮更新不需要重启 `runtime`。验证下载请求快速返回任务编号、切页和刷新后仍能恢复进度、完成后数据集可读取，再确认失败原因可见。下载队列同样纳入全库备份。

`/healthz` 查询 MySQL 并报告存储类型，但不会验证所有文件和工作进程。账户运行状态仍以数据库中的执行端心跳、命令回执和账本更新为准。保持证书自动续期、备份任务和磁盘容量监测可观察。

开发和集成库可单独通过 `compose.database.yaml` 启动；开发端口 3307、测试端口 3308 均只绑定回环。测试需要专用 `_test` 数据库和 `MK_TEST_ALLOW_RESET=1`，不得指向生产。测试 fixture 会按 `db.TABLES` 清空全部业务表；新增下载任务表也必须列入该表清单，不可在共享测试库同时运行多组清库测试或连入开发工作进程。详见 [开发与集成验证](../docs/DATABASE.md#开发与集成验证)。
