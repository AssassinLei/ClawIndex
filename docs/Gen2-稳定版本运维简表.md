# Gen2 稳定版本运维简表

日常只需让服务器上的网页与后台两个容器持续运行。Linux更新、首次安装、数据库权限及网络配置详见[部署说明](Gen2-Linux部署.md)。以下命令均在 `~/ClawIndex-Gen2` 中执行。

| 目的 | 命令 | 应看到什么 |
| --- | --- | --- |
| 更新并重启 | `sudo bash deploy.sh update` | 先备份，再快进拉取、构建并等待网页与后台健康检查 |
| 查看状态 | `sudo bash deploy.sh status` | worker、web运行，数据库状态为 `READY` |
| 查看日志 | `sudo bash deploy.sh logs` | 最近150行网页和后台日志 |
| 导出可下载数据库 | `sudo bash deploy.sh export-db` | 打印项目目录下 `.db` 文件路径；可直接用SFTP下载 |
| 只在备份卷保存副本 | `sudo bash deploy.sh backup` | 打印 `/backups/` 下的新备份路径 |
| 主动停机 | `sudo bash deploy.sh stop` | 网页与后台停止，重启服务器也保持停止 |
| 恢复已有服务 | `sudo bash deploy.sh start` | 继续使用当前镜像与现有数据库，恢复两项服务 |

服务器首次安装时启用Docker开机启动：`sudo systemctl enable --now docker`。运行中的容器配置了 `unless-stopped`，服务器重启后会恢复；主动停机后需执行 `start`。不要重复运行 `install --empty` 来更新已有系统。

## 一次更新的验收

```bash
cd ~/ClawIndex-Gen2 &&
sudo bash deploy.sh update &&
sudo bash deploy.sh status
```

然后打开网页，检查本账号关注指数仍在、今日操作能展示、数据日期与相应市场交易日一致。若AI服务没有可用额度，程序判断和数据页面仍应可以使用，具体状态以网页说明为准。

构建失败发生在替换容器之前，原服务继续使用旧镜像。健康检查失败时先查看日志，保留数据与备份；不要删除数据卷、不要执行 `docker compose down -v`。`unhealthy`只是健康状态，Docker依靠进程退出和重启策略恢复，不会仅因标签变成不健康就重启。

## 备份与隔离恢复演练

备份通过SQLite接口生成，包含已提交的WAL记录。直接复制运行中的单个主库可能漏数据。`export-db`可在服务运行时执行，不需要访问 `/var/lib/docker/`；务必将导出文件再存一份到服务器之外。

下面的恢复演练只启动一次性Python管理进程，在 `reports/` 下生成独立数据库，不启动网页、后台或通知，也不挂载生产数据卷。先导出一次数据库，再将第一行文件名换成刚得到的实际名称。

```bash
EXPORT_DATABASE="$PWD/clawindex-export-替换为实际文件名.db"
test -f "$EXPORT_DATABASE" &&
EXPORT_DATABASE="$(realpath -- "$EXPORT_DATABASE")" &&
WORKER_CONTAINER="$(sudo docker compose --env-file .env.cloud ps -aq worker)" &&
test -n "$WORKER_CONTAINER" &&
GEN2_IMAGE="$(sudo docker inspect --format '{{.Config.Image}}' "$WORKER_CONTAINER")" &&
mkdir -p reports &&
VERIFY_DIR="$(mktemp -d "$PWD/reports/restore-check-XXXXXX")" &&
sudo docker run --rm --user 0:0 \
  --mount "type=bind,src=$EXPORT_DATABASE,dst=/import/source.db,readonly" \
  --mount "type=bind,src=$VERIFY_DIR,dst=/restore" \
  --env VNEXT_DB_PATH=/restore/verified.db \
  --entrypoint python "$GEN2_IMAGE" \
  -m clawindex_next.deployment import --source /import/source.db &&
sudo docker run --rm --user 0:0 \
  --mount "type=bind,src=$VERIFY_DIR,dst=/restore" \
  --env VNEXT_DB_PATH=/restore/verified.db \
  --entrypoint python "$GEN2_IMAGE" \
  -m clawindex_next.deployment status
```

应依次输出 `IMPORTED` 和 `READY`。目标文件已存在、备份损坏或所需表不完整时会拒绝，不覆盖原文件。此命令需要服务器已有当前镜像；本地Windows没有Docker时，不能把离线测试当成这项云端演练已经完成。

真正恢复服务时，先停止原后台，避免两份库同时推送。保留原数据卷和导出，再在空白恢复环境按部署说明执行 `install --import-db`；恢复库包含原账号、渠道与授权配置。现有数据库不会被该命令覆盖。不要把隔离演练文件直接复制到正在运行的生产库上。

## 已核实的边界

本次代码检查覆盖空库初始化拒绝误覆盖、更新前备份、构建失败保留旧服务、sudo下Git权限、只读导入、完整的34张应用数据表检查，以及真实WAL在线备份后隔离导入的回归。测试不使用真实密钥或Webhook。真实服务器的Docker镜像、网络、磁盘空间、开机恢复和通知送达，仍需以该服务器的 `status`、日志和实际使用结果确认。
