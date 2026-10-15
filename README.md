# ClawIndex Gen2

全新Next版本发布在 **Gen2** 分支。Linux部署使用根目录的`Dockerfile`、`compose.yaml`和`deploy.sh`；web与worker独立常驻运行，数据与备份持久化，Docker开机启动后容器自动恢复。

首次部署、导入本地新版数据、配置密钥和访问网页，请按 **[Gen2 Linux部署说明](docs/Gen2-Linux部署.md)** 操作。之后更新只需在服务器项目目录运行`sudo bash deploy.sh update`。数据库、密钥与测试运行产物不上传Git。

## VNext 2.4（发现机会）

整体Review修复：延续同一轮信号的已处理状态、刷新未发提醒的判断版本、统一证据有效性校验、补齐投递中断收尾及后台重算；修正并发数据合并、历史与因子重试、来源未知旧价格参与计算、前向模拟成交时点和分段边界收益。升级后同时重启web与worker，自动维护开启时旧计算结果会重算；无须修改表结构。见[整体Review与修复说明](docs/VNext-系统Review与修复-2026-10-15.md)。

新增 **发现机会**：后台共享扫描申万 **L1、L2** 行业，自动补齐历史，按趋势、估值和风险条件筛选，去除重叠方向，最多展示3个研究候选，允许没有机会。支持一键加入我的指数、个人隐藏、独立开关的Webhook机会提醒，以及包含失败案例的20/60交易日后续跟踪。候选来自程序固定规则，尚无持续收益验证，不是个人买入指令。更新后同时重启页面与worker，无需修改表结构。见[机会雷达使用说明](docs/VNext-机会雷达-2026-10-15.md)。

### VNext 2.3（界面与日常使用优化）

2026-10-15更新：今日操作按卡片独立刷新，保存反馈保持原位；添加指数支持名称和代码搜索；执行原因改为预设选项，选“其他”再填写。新增 **进化并应用** 按钮，每天可主动研究并实际应用通过历史检验的新规则，明确区别于保留前向观察的长期自动进化。使用方法与限制见[每日进化与页面交互](docs/VNext-每日进化与页面交互-2026-10-15.md)。

首页先显示待处理、无需操作、待更新或确认的概览，再按优先级展示操作卡片。数据和AI分析按需展开，策略先看状态、再改设置；持仓只需选择状态，金额和预算为可选项。导航与提示改用更直白的中文，统一视觉样式并适配手机。见[新版界面与使用说明](docs/VNext-界面与日常使用-2026-10-14.md)。

AI解读故障修复：延长请求超时，DeepSeek简短解读默认关闭思考；修正风险说明被误拦，并记录具体失败诊断。见[AI排查与重试说明](docs/VNext-AI失败排查与修复-2026-10-14.md)。

Webhook支持每用户、每地址独立选择关闭、全部操作、只买入或买入与卖出；正式卡片仅包含指数名称和操作。配置页可立即发送“你好，世界”测试消息。见[Webhook配置说明](docs/VNext-Webhook推送配置.md)。

### VNext 2.2（巡检与AI体验更新）

当天有效巡检与AI结果直接复用，缺AI只补AI；AI勾选默认开启。“今日操作”新增行情摘要、数据与AI查看入口，自动刷新。修正旧任务长期排队及整批同步阻塞巡检的问题，并明确显示离线、重试和接续状态。见[本次更新说明](docs/VNext-巡检与AI体验更新-2026-10-14.md)。

### VNext 2.1 云端自动维护与因子进化

新增后台自动补历史、基础指标与78项专业技术因子保存、四类固定因子组合研究；未登记持仓默认空仓，已登记状态保持到修改。研究、未来行情模拟观察、授权晋升、回退与快捷成交确认保留。旧授权不自动扩大；收益与未来回撤没有保证。见[云端部署与因子进化手册](docs/VNext-云端自动维护与因子进化-2026-10-13.md)。

新版入口为 `app_next.py`，运行于独立的 `quant_system_next.db`。规则产生可复算的指数买卖信号，AI负责解释；输入账号名即可进入，新账号自动创建；支持手动持仓、预算、版本证据、独立worker、可靠通知与成本/样本外研究。生产源库和旧入口保留。

本地双击 **`一键启动新版.bat`**，页面与后台一起启动，输入账号名即可进入。工作日后台自动维护所有用户指数并生成判断，日常看“今日操作”，无需手动补历史或巡检。需要即时刷新仍可提交巡检，原有 Loading 和中文证据/AI解读保留。通知依然由正式规则和渠道开关控制。

启动、迁移、测试与回滚见 [VNext运行手册](docs/VNext-Runbook.md)。历史研究不证明趋势基线有收益优势；旧版说明中的“AI全权决策”不适用于新版。

Linux 使用根目录 `Dockerfile`、`compose.yaml`，页面与worker分别运行，共享命名卷，提供一致性备份、停止信号和进程恢复；镜像校验实际使用的 SQLite 3.51.3。Linux CI 已添加，当前开发机没有 Docker/Linux，因此实际镜像与云端验收仍待 CI 和服务器确认。部署步骤见上述2.1手册。

## 旧版项目简介

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
