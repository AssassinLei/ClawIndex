## 项目简介

爪析ClawIndex 是一个**AI指数基金量化盯盘系统**，面向指数基金的定投决策辅助。系统提供指数基金的买卖建议以及大语言模型投资简报，帮助投资者高效决策。

**核心理念：**

- **AI 全权决策** — 策略引擎负责计算 PE/PB 历史分位数、均线、风险溢价等指标，AI 大模型基于策略框架自主推演买卖信号，输出操作建议与置信度
- **可定制分析框架** — 支持每个用户为各自监控的指数编写专属提示词与筛选传递指标，AI 将按你的框架解读数据，替换系统默认策略
- **统一巡检流水线** — 同步数据 → 指标计算 → AI 分析 → 入库 → Webhook 推送，手动与定时巡检共享同一条流水线；同步失败或 AI 调用失败时自动跳过入库与推送，保护当日已有的有效巡检记录

## 功能特性

- 👥 **多用户支持** — 用户注册；监控池、提示词、巡检结果、Webhook 配置均按用户隔离，行情/因子数据全局共享只存一份
- 📊 **监控池管理** — 自由添加/删除申万行业指数与国际指数标的；所有指数可一键弹窗查看每日收盘价趋势图
- 📈 **历史数据自动同步** — 添加标的时自动拉取近 10 年日线行情、估值与技术因子数据，巡检前自动增量补齐
- 🧮 **多维指标计算** — PE/PB 历史分位数、MA60/MA120 均线、风险溢价、推导 ROE、成交额 MA20；
- 🤖 **AI 全权决策** — 大模型基于策略框架自主分析，输出操作建议 + 分析 + 置信度；AI 调用失败（限流/异常/空响应）时自动跳过入库与推送，不污染历史记录
- 📝 **指数定制提示词** — 每个用户为自己监控的指数编写专属分析框架，并勾选传递给 AI 的指标
- 🖥️ **卡片化双列布局** — 左列指标数据（每项核心指标可点击 📈 弹窗查看历史趋势，另附 🔬 技术因子面板展示当日全部因子），右列依次展示操作建议 → AI 分析 → 置信度
- 📋 **历史分析页面** — 按用户查看每次巡检记录，支持按指数/操作建议/日期范围筛选，回溯当日行情、计算指标、技术因子与 AI 解读
- 📝 **统一日志系统** — 所有 API 调用、数据库操作自动记录至 `logs/clawindex.log`
- 📨 **Webhook 消息推送** — 巡检时自动向飞书 Bot 发送富文本卡片消息；默认仅推送 AI 建议为「买入/卖出」的强信号（可开关恢复全量推送）；同一地址被多个用户配置时定时巡检自动去重，不重复推送
- ⏰ **定时自动巡检** — 按全局 Cron 表达式触发（默认 `30 19 * * 1-5` = 工作日 19:30，侧边栏可改），交易日历二次确认，可按开关启停；每指数只同步/计算一次，按用户提示词配置分组去重调用 LLM（相同配置共享一次调用，调用间自动节流防限流），每个用户收到自己配置对应的专属分析结果


## 技术栈

| 技术 | 用途 |
|------|------|
| **Python 3.10+** | 开发语言 |
| **Streamlit** | Web 交互界面 |
| **Pandas** | 数据处理与分析 |
| **Tushare** | 指数行情与估值数据（申万行业指数 + 国际指数） |
| **Akshare** | 中国国债收益率数据 |
| **OpenAI SDK** | LLM 投顾简报生成 |
| **SQLite** | 本地持久化存储 |
| **APScheduler** | 定时任务调度 |
| **Requests** | Webhook HTTP 消息推送 |

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/<your-username>/ClawIndex.git
cd ClawIndex
```

### 2. 安装依赖

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. 配置环境变量

在项目根目录创建 `.env` 文件：

```ini
# Tushare 数据接口 Token
TUSHARE_TOKEN=your_tushare_token_here

# DeepSeek AI 大模型
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

> **提示：** 系统实际使用的 Tushare 接口有权限要求。因子/成分股接口无权限时对应功能降级，不影响核心巡检。

### 4. 启动应用

```bash
streamlit run app.py
```

浏览器将自动打开 `http://localhost:8501`。

## 项目结构

```
ClawIndex/
├── assets/                  # 静态资源（Logo 等）
├── .env                     # 环境变量配置（不提交至 Git）
├── app.py                   # Streamlit 前端 & 主工作流
├── inspection_pipeline.py   # 统一巡检流水线模块
├── data_fetcher.py          # Tushare/Akshare 数据采集层
├── database.py              # SQLite 数据层 & 查询 API
├── strategy_engine.py       # 指标计算引擎
├── llm_agent.py             # LLM 决策与报告生成
├── prompt.md                # AI 基础系统提示词（外部可编辑）
├── constants.py             # 分类中文名与 21 个国际指数代码表（市场类型识别唯一依据）
├── logger.py                # 统一日志模块
├── webhook_sender.py        # Webhook 消息推送模块
├── scheduler.py             # 定时调度模块（按 Cron 表达式自动巡检）
├── DATABASE.md              # 数据库表结构与 API 文档
├── requirements.txt         # Python 依赖
├── quant_system.db          # SQLite 数据库文件（运行时生成）
└── logs/                    # 运行日志（按天轮转，保留 30 天）
```

## 使用方式

1. **登录/注册** — 首次进入输入用户名注册；各用户拥有独立的监控池、推送配置与提示词，行情数据全局共享，巡检结果按用户隔离
2. **添加监控标的** — 在左侧边栏搜索或按分类浏览行业指数（选择策略领域），或切换至「国际指数」选择全球主要指数（恒生/标普500/纳指等），点击添加
3. **管理监控池** — 在侧边栏「当前监控列表」中选中指数后，可执行：删除所选指数（红色按钮）、成分查询（实时拉取申万成分股）、查看指数价格趋势（弹窗展示每日收盘价，支持时间范围筛选）
4. **等待数据同步** — 系统自动拉取近 10 年历史数据（首次约需数秒；若其他用户已同步过该指数则直接复用）
5. **（可选）配置提示词** — 在「提示词配置」标签页为指数编写专属分析框架、勾选要传递给 AI 的基础指标与技术因子（提示词仅对当前账号生效）
6. **运行巡检** — 点击主页「运行今日行情研判」按钮
7. **阅读报告** — 每个标的以卡片形式展示：左列指标数据（点击 📈 查看单项指标历史趋势，🔬 查看当日技术因子），右列操作建议 + AI 分析 + 置信度
8. **消息推送（可选）** — 在侧边栏「消息推送设置」中添加你的 Webhook 地址并开启开关；默认仅推送 AI 建议为「买入/卖出」的强信号（可在设置中关闭过滤恢复全量推送）；定时巡检仅推送你监控的指数，且推送内容为你的提示词配置对应的专属分析结果

## 部署

支持 Linux 服务器部署（Ubuntu 20.04+ / CentOS 8+，Python 3.10+，默认端口 8501）。以下为生产环境推荐方案。

### 1. 基础启动

```bash
# 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 配置 .env（参考「快速开始」第 3 步）

# 前台运行（首次验证）
streamlit run app.py --server.port 8501

# 后台运行（生产环境）
nohup streamlit run app.py --server.port 8501 > streamlit.log 2>&1 &
```

### 2. systemd 守护进程（推荐）

创建 `/etc/systemd/system/clawindex.service`：

```ini
[Unit]
Description=ClawIndex 量化盯盘引擎
After=network.target

[Service]
Type=simple
User=admin
WorkingDirectory=/home/admin/clawindex
EnvironmentFile=/home/admin/clawindex/.env
ExecStart=/home/admin/clawindex/.venv/bin/streamlit run app.py --server.port 8501 --server.headless true
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动并设为开机自启：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now clawindex
```

> `EnvironmentFile` 会自动加载 `.env`，与 `app.py` 中的 `load_dotenv()` 两层加载不冲突。

### 3. Nginx 反向代理（可选）

有域名时通过 Nginx 反代更规范：

```nginx
server {
    listen 80;
    server_name clawindex.yourdomain.com;
    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4. 密钥管理

| 方式 | 适用场景 | 说明 |
|------|----------|------|
| `.env` 文件 | 单机部署 | 项目根目录创建即可，已被 `.gitignore` 忽略 |
| systemd `EnvironmentFile` | systemd 托管 | 密钥由 systemd 注入，与项目文件分离 |
| 环境变量 `export` | Docker / CI | 写入 `~/.bashrc` 或 Dockerfile |

项目优先从环境变量读取（`os.environ.get()`），三种方式互不冲突。

### 5. 常见问题

- **外网无法访问**：检查防火墙 `sudo ufw allow 8501`，云服务器还需在安全组放行 8501 端口
- **Tushare 权限错误**：确认账户积分满足接口要求（`idx_factor_pro` 需 5000 积分、`index_member_all` 需 2000 积分，行情接口 `sw_daily` 需开通对应权限）
- **AI 分析失败**：检查 `.env` 中 `DEEPSEEK_API_KEY` 是否配置、API 额度是否充足；AI 调用失败不会写入巡检记录，修复后重新巡检即可补录
- **更新代码**：`git pull && sudo systemctl restart clawindex`
- **查看日志**：`journalctl -u clawindex -f`（systemd）或 `tail -f streamlit.log`（nohup）

## 注意事项

- 国际指数（index_global 接口）无 PE/PB 估值与技术因子数据，固定使用「国际指数」分类的趋势跟踪策略（价格 + MA60/MA120 + 价格历史分位 + 当日涨跌幅）；其增量同步不依赖 A 股交易日历，接口返回空即视为无新数据
- `risk_free_rate`（无风险利率）通过 akshare 获取中国10年期国债收益率，接口异常时默认值 1.72%
- 每日增量数据同步已实现，支持交易日历自动识别（周末/节假日跳过）
- 巡检结果按用户存入 `inspection_log` 表（同一用户同一标的同一天覆盖更新），支持历史追溯查看；数据同步失败或 AI 调用失败时不入库，不会覆盖当日已有的有效记录
- 删除标的的级联规则：仅删除当前用户的监控关系与该用户的定制提示词；行情/因子数据仅当该指数无任何用户监控时才清理；历史巡检记录永久保留
- 系统日志按天写入 `logs/clawindex.log`，自动轮转保留最近 30 天，已加入 `.gitignore`
- SQLite 已启用 WAL 模式 + 写锁等待，可支撑十级用户量的多用户并发；更大规模场景可另行评估
- 数据库文件 `quant_system.db` 为运行时生成，已加入 `.gitignore`；系统无 schema 迁移机制，部署时请使用全新空库（携带旧版库会因缺列直接报错）
- 数据库表结构与 API 详见 `DATABASE.md`

## License

MIT
