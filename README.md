## 项目简介

爪析ClawIndex 是一个玩具级的**AI指数基金量化盯盘系统**，面向指数基金的定投决策辅助。系统提供指数基金的买卖建议以及大语言模型投资简报，帮助投资者高效决策。

**核心理念：**

- **AI 全权决策** — 策略引擎负责计算 PE/PB 历史分位数、均线、风险溢价等指标，AI 大模型基于策略框架自主推演买卖信号，输出操作建议与置信度
- **可定制分析框架** — 支持为每个指数编写专属提示词与筛选传递指标，AI 将按你的框架解读数据，替换系统默认策略
- **统一巡检流水线** — 同步数据 → 指标计算 → AI 分析 → 入库 → Webhook 推送，手动与定时巡检共享同一条流水线

## 功能特性

- 📊 **监控池管理** — 自由添加/删除行业指数标的，支持四种策略分类
- 📈 **历史数据自动同步** — 添加标的时自动拉取近 10 年日线行情与估值数据
- 🧮 **多维指标计算** — PE/PB 历史分位数、MA60/MA120 均线、风险溢价、推导 ROE
- 🤖 **AI 全权决策** — 大模型基于策略框架自主分析，输出操作建议 + 分析 + 置信度
- 📝 **指数定制提示词** — 为每个指数编写专属分析框架并勾选传递指标，替换默认策略
- 🖥️ **卡片化双列布局** — 左列纯指标，右列依次展示操作建议 → AI 分析 → 置信度
- 📋 **历史分析页面** — 查看每次巡检记录，回溯当日指标与 AI 解读
- 📝 **统一日志系统** — 所有 API 调用、数据库操作自动记录至 `logs/clawindex.log`
- 📨 **Webhook 消息推送** — 巡检时自动向飞书 Bot 发送富文本卡片消息
- ⏰ **定时自动巡检** — 每个交易日 19:30 自动触发，交易日历二次确认，可按开关启停

## 策略框架概览

以下为系统内置的四套默认策略框架（AI 分析时的参考依据，可在「提示词配置」中为每个指数替换为定制框架）：

| 分类 | 核心指标 | 买入信号 | 卖出信号 |
|------|----------|----------|----------|
| **宽基指数** | PE 分位 + MA60 | PE 分位 <20% 且价格>MA60 → 强买；<50% → 定投 | PE 分位 >80% 或风险溢价 <3% |
| **科技类** | PE 分位 + MA120 | PE 分位 <30% 且站上 MA120 → 买入 | 跌破 MA120 → 暂停加仓 |
| **制造业** | PB 分位 | PB 分位 <15% → 左侧建仓 | PB 分位 >85% → 清仓 |
| **红利低波** | PB 分位 + PE 绝对值 | PB 分位 <50% 且 PE<15 → 买入 | 跌破 MA120 → 仅定投 |

## 技术栈

| 技术 | 用途 |
|------|------|
| **Python 3.10+** | 开发语言 |
| **Streamlit** | Web 交互界面 |
| **Pandas** | 数据处理与分析 |
| **Tushare** | 指数行情与估值数据 |
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

> **提示：** Tushare 的 `index_dailybasic` 接口需要 Level 2 权限。

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
├── constants.py             # 分类名称等常量
├── logger.py                # 统一日志模块
├── webhook_sender.py        # Webhook 消息推送模块
├── scheduler.py             # 定时调度模块（交易日 19:30 自动巡检）
├── requirements.txt         # Python 依赖
├── quant_system.db          # SQLite 数据库文件（运行时生成）
└── logs/                    # 运行日志（按天轮转，保留 30 天）
```

## 使用方式

1. **登录/注册** — 首次进入输入用户名注册即可（无密码）；各用户拥有独立的监控池与推送配置，行情数据与巡检结果全局共享
2. **添加监控标的** — 在左侧边栏搜索或按分类浏览行业指数，选择策略领域，点击添加
3. **等待数据同步** — 系统自动拉取近 10 年历史数据（首次约需数秒；若其他用户已同步过该指数则直接复用）
4. **（可选）配置提示词** — 在「提示词配置」标签页为指数编写专属分析框架、勾选要传递给 AI 的指标（提示词全局共享）
5. **运行巡检** — 点击主页「运行今日行情研判」按钮
6. **阅读报告** — 每个标的以卡片形式展示：左列指标数据，右列操作建议 + AI 分析 + 置信度
7. **消息推送（可选）** — 在侧边栏「消息推送设置」中添加你的 Webhook 地址并开启开关；定时巡检仅推送你监控的指数

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
- **Tushare 权限错误**：确认账户已充值积分，`index_dailybasic` 接口需要 Level 2 权限
- **更新代码**：`git pull && sudo systemctl restart clawindex`
- **查看日志**：`journalctl -u clawindex -f`（systemd）或 `tail -f streamlit.log`（nohup）

## 注意事项

- `risk_free_rate`（无风险利率）通过 akshare 获取中国10年期国债收益率，接口异常时默认值 1.72%
- 每日增量数据同步已实现，支持交易日历自动识别（周末/节假日跳过）
- 巡检结果自动存入 `inspection_log` 表，支持历史追溯查看
- 系统日志按天写入 `logs/clawindex.log`，自动轮转保留最近 30 天，已加入 `.gitignore`
- SQLite 适合单用户本地使用，多用户并发场景建议迁移至 PostgreSQL
- 数据库文件 `quant_system.db` 为运行时生成，已加入 `.gitignore`

## License

MIT
