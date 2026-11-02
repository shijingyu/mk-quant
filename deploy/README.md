# 部署方式

支持宝塔原生 Python 项目和 Docker Compose 两种单机部署方式。宝塔安装步骤见 [宝塔部署指南](BAOTA.md)；本文介绍容器方案。同一数据库不要同时启动两套执行服务。

## 可选：单机容器部署

该配置用于可审查的回测与模拟部署。实盘执行在应用和 Compose 中均关闭；容器编排不能替代真实交易验收。

## 启动

安装 Docker Engine 与 Compose v2。需要约 4 GB 以上可用内存，并预留宿主机资源。默认仅向本机开放：网页 `http://127.0.0.1:8080`，API `http://127.0.0.1:8765`。

```sh
docker compose build
docker compose up -d
docker compose exec api python -m backend.worker create-admin
```

第三条命令会交互输入管理员账户与密码；部署不会自动生成演示管理员。模拟客户由管理员在网页创建。

需要加密保存币安凭证时，先独立生成并保管 Fernet 密钥，然后通过运行环境提供 `MK_MASTER_KEY`。不要将真实密钥写进 Dockerfile、Compose、策略源码或版本库。默认为空，界面仍可运行回测和模拟，但拒绝加密录入实盘凭证。备份该密钥与数据需要分别保管；丢失密钥无法解密已有凭证。

## 进程与资源

| 服务 | 任务 | CPU 上限 | 内存上限 |
| --- | --- | --- | --- |
| api | 登录、权限、配置与任务控制 | 0.75 | 768 MB |
| backtest | 队列与独立回测子进程 | 1.0 | 1 GB |
| runtime | 共享行情轮询及逐账户模拟进程 | 2.0 | 2 GB |
| frontend | nginx 静态网页与同源 API 代理 | 0.25 | 128 MB |

`runtime` 不依赖 `api` 的生命周期，重启 API 不会关闭已有模拟运行进程；`docker compose down` 会停止整个部署。回测容器禁止网络访问，也不接收主密钥；历史数据从共享数据库读取。策略上传只向已登录管理员开放，管理员保存通过契约检查的源码即可运行；管理员是代码的信任边界，进程和容器限额不是恶意 Python 的安全沙箱。

CPU 限额防止单个服务无限占用，但不是物理资源预留。宿主机仍须监测 CPU、内存、磁盘空间和连接容量。进程 PID 上限也限制了单机最大并发账户数，需要容量测试后调整。

## 持久化和备份

三个 Python 服务共享本机命名卷 `mk-data`，数据库路径固定为 `/data/mk.sqlite3`，采用 SQLite WAL 和文件锁。此方案只面向单宿主机，不允许把数据库文件放入 NFS 或多个宿主机共享目录，也不提供数据库高可用。需要横向扩展时应迁移 PostgreSQL、统一任务协调和独立执行网关。

运行时不要只复制 `.sqlite3` 主文件而遗漏 WAL。使用 SQLite 在线 backup API 生成一致性备份，再导出并核对恢复流程。不要用 `docker compose down -v` 做日常重启；该命令删除持久卷。

## 公网部署和运维

默认只绑定回环地址。公网访问需要在宿主机入口设置 HTTPS，保持单一可信代理，配置 `MK_COOKIE_SECURE=1` 以及准确的 `MK_ALLOWED_ORIGINS`。本仓库 nginx 使用本地 HTTP；接在 TLS 终止代理后应让可信入口传递正确协议，并调整 nginx 的 `X-Forwarded-Proto` 设置，不能接受任意公网客户端伪造代理头。不要把未加密的管理端口直接开放到互联网。

查看状态与日志：

```sh
docker compose ps
docker compose logs --tail=100 api runtime backtest
docker compose restart api
```

API `/healthz` 只能证明控制服务存活；工作进程的真实状态取自数据库中的心跳和命令结果。实盘上线还需要账户事件流/成交对账、保护单、运行授权续期、旧执行者强制隔离、故障恢复演练及受保护的交易出口，详见 `docs/BINANCE.md`。本部署不会执行真实下单。

基础镜像使用维护分支标签，Python 使用明确版本范围，前端通过已提交 lockfile 和 `npm ci` 构建。正式发布应另外固定镜像摘要与 Python 完整锁文件，并记录构建产物和依赖审计结果。
