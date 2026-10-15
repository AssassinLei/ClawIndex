# Gen2：Linux部署与更新

发布分支：[Gen2](https://github.com/AssassinLei/ClawIndex/tree/Gen2)。新版页面入口是`app_next.py`。Dockerfile负责构建镜像，Compose负责一起运行页面和worker，`deploy.sh`把首次导入、启动、备份和代码更新串成命令。

这是一台Linux服务器上的部署方案。数据库与备份使用Docker命名卷，更新镜像不会清空数据；密钥只放服务器的`.env.cloud`，不进入Git或镜像。

## 1. 准备服务器

安装Git、Docker Engine和Docker Compose插件。按自己的发行版使用[Docker官方安装说明](https://docs.docker.com/engine/install/)，推荐当前受支持的Ubuntu/Debian版本。脚本不会自行下载脚本并以root安装系统软件。

在使用systemd的服务器启用Docker开机启动：

```bash
sudo systemctl enable --now docker
docker --version
sudo docker compose version
```

下面的部署命令需要Docker权限。如果当前用户不能访问Docker，使用`sudo bash deploy.sh ...`；Git私有仓库凭据也应由执行更新的这个用户配置。脚本使用`flock`防止并发部署，通常由系统的`util-linux`提供。

## 2. 取得Gen2代码并填写一次配置

```bash
git clone --branch Gen2 --single-branch https://github.com/AssassinLei/ClawIndex.git ClawIndex-Gen2
cd ClawIndex-Gen2
cp .env.cloud.example .env.cloud
chmod 600 .env.cloud
nano .env.cloud
```

填写这些变量：

```dotenv
TUSHARE_TOKEN=你的TushareToken
VNEXT_LLM_API_KEY=你的AI密钥
VNEXT_LLM_BASE_URL=https://api.deepseek.com
VNEXT_LLM_MODEL=deepseek-chat
CLAWINDEX_BIND_ADDRESS=127.0.0.1
CLAWINDEX_PORT=8502
```

AI密钥可以留空；行情Token需填写。其余变量保持模板默认值。不要将真实配置提交到Git。

## 3. 首次部署：推荐保留本地新版数据

先停止本地旧worker，避免本地和云端同时按两份数据库推送提醒。把**新版schema 2库的一致性备份**单独上传到服务器，例如`/root/next-export.db`。当前工作区已准备的导出文件在本地`backups/`目录，不在Git中。

需要重新导出时，在Windows项目目录执行，输出文件名必须尚不存在：

```powershell
.\.venv\Scripts\python.exe -m clawindex_next --db quant_system_next.db backup --output backups/next-export.db
scp .\backups\next-export.db 你的服务器用户@服务器IP:/绝对路径/next-export.db
```

这里要上传`quant_system_next.db`的导出，**不是**老版`quant_system.db`或`quant_system20261012.db`。不要直接复制运行中的单个`.db`而漏掉WAL。导出会保存账号、关注、持仓、策略、历史与现有渠道设置，不自动扩大提醒授权。

在服务器的Gen2项目目录执行：

```bash
sudo bash deploy.sh install --import-db /绝对路径/next-export.db
```

脚本构建镜像，检查配置和数据库，将备份导入独立卷，设置运行用户权限，启动web和worker并等待健康检查。服务器已有数据库时拒绝覆盖。首次拉行情/补历史仍会继续在后台运行，不影响以后关闭SSH。

**如果确定从空库开始**，使用下面命令替代导入：

```bash
sudo bash deploy.sh install --empty
sudo bash deploy.sh admin 你的账号名
```

空库没有旧账号、关注和历史；输入账号名会自动注册。`admin`仅设置管理员角色，不开启推送，已有会话会失效一次。导入现有库时，管理员角色随数据库保留。

## 4. 打开网页

默认端口只在服务器本机监听。你在自己电脑上建立SSH隧道：

```bash
ssh -N -L 18502:127.0.0.1:8502 你的服务器用户@服务器IP
```

然后访问`http://127.0.0.1:18502`。使用18502作为电脑本地端口，避免与本地开发的8502冲突。若改了服务器`CLAWINDEX_PORT`，对应调整隧道右侧端口。

朋友共同访问可使用已有的私有网络或带访问控制的反向代理，将代理目标设置为`127.0.0.1:8502`，开启WebSocket支持。账号名登录只是选账号，不能验证访问者身份，因此默认不把页面直接暴露到公网。配置自己的受控网络入口时，可以调整`.env.cloud`中的绑定地址；脚本不修改服务器防火墙或自动申请域名。

## 5. 关闭SSH、服务器重启后怎么办

网页和worker均以后台容器运行，配置`restart: unless-stopped`。Docker服务开机启动后，原本运行的容器会在重启后恢复；无须保持SSH窗口或额外运行Python/BAT。手动执行`stop`表示主动停机，此时服务器重启也保持停机，要用`start`恢复。这与[Docker重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)一致。

```bash
sudo bash deploy.sh status
sudo bash deploy.sh logs
sudo bash deploy.sh stop
sudo bash deploy.sh start
```

健康检查用于判断状态。worker有中断恢复和无进展退出机制，Docker负责进程退出后恢复；不是依靠`unhealthy`自动重启。

## 6. 以后更新：一条命令

确认开发代码已提交并推送到Gen2，在服务器项目目录执行：

```bash
sudo bash deploy.sh update
```

顺序是：检查本地代码没有未保存改动 → 一致性备份 → 获取Gen2并只允许快进合并 → 构建按Git提交编号命名的镜像 → 更新web和worker → 等待健康检查。

更新不需要上传多个Python文件、不需要再次导入数据库、不覆盖`.env.cloud`。构建期间原容器继续运行；切换时会短暂重启页面和后台。本机代码有改动、数据库版本不兼容或构建失败时会报错，不使用`reset --hard`强行覆盖。启动失败保留数据及备份，应先看日志；脚本不声称跨数据库结构变更自动回滚。

## 7. 备份与恢复

```bash
sudo bash deploy.sh backup
```

输出会显示唯一备份路径，例如`/backups/gen2-日期-进程号.db`。备份在`clawindex_clawindex-backups`命名卷，主数据在`clawindex_clawindex-data`卷（默认项目名`clawindex`）。定期将备份另存服务器之外：

```bash
sudo docker compose --env-file .env.cloud cp worker:/backups/替换为备份文件名.db ./
```

不要执行`docker compose down -v`或删除这些卷。需要恢复时，先停止当前两个服务，保留原库和当前卷，再在独立恢复环境导入备份验证；`install --import-db`不会覆盖现有库，防止误恢复丢失新数据。

## 8. 部署失败时检查什么

- 找不到Docker或Compose：完成对应发行版的官方安装。
- 无法连接Docker：确认服务启动并用有权限的用户执行。
- 镜像/依赖下载失败：检查服务器访问Docker Hub、PyPI及SQLite官网的网络；原服务在构建阶段不会被停止。
- 8502端口冲突：停止旧部署或在`.env.cloud`修改主机端口，不要关闭无关进程。
- 初始化拒绝：服务器已经有库；日常使用`start`或`update`。
- 健康检查超时：运行`logs`看具体原因。不要为了让它启动而删除数据卷。

GitHub中的Linux CI会构建镜像、运行回归和离线Compose恢复/备份测试，可在[Actions](https://github.com/AssassinLei/ClawIndex/actions)查看Gen2对应提交。CI使用一次性合成数据库，不使用你的行情密钥或真实Webhook。真实服务器的网络、权限和运行状态，仍以服务器部署结果为准。
