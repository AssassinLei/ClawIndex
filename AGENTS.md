# AGENTS.md

This file provides guidance to Qoder (qoder.com) when working with code in this repository.

## 项目概述

爪析 ClawIndex 是一个 AI 指数基金量化盯盘系统（面向申万行业指数），以单体 Streamlit 应用 + SQLite 持久化的形式构建。代码注释、UI 文案、日志与文档均为中文——新增代码请保持一致。

## 常用命令

```powershell
# 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 启动应用（打开 http://localhost:8501）
streamlit run app.py
```

项目没有测试、没有 lint 配置、没有构建步骤，验证方式是直接运行应用。

项目根目录需要 `.env` 文件（由 `app.py` 顶部的 `load_dotenv()` 加载）：

```ini
TUSHARE_TOKEN=...        # Tushare 行情数据接口
DEEPSEEK_API_KEY=...     # 大模型（OpenAI 兼容 SDK）
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

两个 API 客户端均为延迟初始化（`data_fetcher.py` 的 `_get_pro()`、`llm_agent.py` 的 `_get_client()`），因此缺少密钥时模块仍可正常 import——失败发生在实际调用时。

调试：查看 `logs/clawindex.log`（所有模块通过 `logger.setup_logger(name)` 记录日志；DEBUG 级别写入文件，包含完整的 LLM 请求/响应内容）。要完全重置状态，删除 `quant_system.db` 即可——下次启动时会自动重建空库。

## 架构

### 巡检流水线是核心抽象

`inspection_pipeline.py` 是手动巡检（`app.py`）与定时巡检（`scheduler.py`）共享的唯一流水线：同步数据 → 计算指标 → AI 分析 → 入库 → Webhook 推送。它被刻意拆分为两段：

- `prepare_fund_data(code, name, category)` — 增量同步（`data_fetcher.sync_incremental`）+ 指标计算（`strategy_engine.generate_fund_report`）。**用户无关**：每指数只执行一次。
- `analyze_and_save_for_user(fund_data, username, ai_result=None)` — LLM 调用 + 写入 `inspection_log`。**按用户执行**（读取该用户的定制提示词/指标勾选）。可接收预先算好的 `ai_result`，供调度器去重复用 LLM 调用。
- `run_single_inspection(...)` — 组合以上两段，供 `app.py` 手动巡检调用。

### 三种不同的失败语义（不可混淆）

1. **同步/数据错误**（`fund_data` 含 `"error"`，`sync_error` 有值）— 不入库，UI 展示为「数据异常」。
2. **AI 调用失败**（`ai_result["ai_error"] = True`：限流、API 异常、空响应）— **不入库、不推送 Webhook**，保证失败的巡检永远不会覆盖当日已有的有效记录（`inspection_log` 使用 `INSERT OR REPLACE` + `UNIQUE(username, fund_code, inspect_date)`）。
3. **JSON 解析兜底**（AI 有产出但格式不合规）— 视为有效输出，使用默认值（`advice="持有/观望"`、`confidence=0`）正常入库，不加 `ai_error` 标记。

### 多用户数据归属模型

无密码——用户名即身份。数据分两类归属（9 张表的完整 schema 见 `DATABASE.md`，实现在 `database.py`）：

- **用户维度**（以 `username` 隔离）：`fund_pool`、`inspection_log`、`fund_custom_prompts`、`webhook_urls`、用户级设置（以命名空间键 `key:username` 存入 `app_settings`，通过 `get_user_setting`/`set_user_setting` 读写）。
- **全局共享**：`daily_market_data`、`idx_factor_data`、`industry_list` — 无论多少用户监控同一指数，只存一份。

必须遵守的推论：
- `category` 是**指数级全局属性**：`add_fund` 会强制新监控者沿用已有分类（`get_shared_category`）。
- 删除标的必须走 `remove_fund()` — 它做引用计数，仅在无任何用户监控该指数时才清理共享行情数据（表间没有外键约束）。

### 调度器去重逻辑

`scheduler.py` 在工作日 19:30 执行（Asia/Shanghai 时区，通过 `is_trade_day` 走交易日历二次确认）。每指数流程：只同步/计算一次 → 将监控用户按提示词配置签名 `(custom_prompt, sorted(indicators))` 分组，相同配置共享一次 LLM 调用（以 `LLM_CALL_INTERVAL` 节流）→ 每用户各自入库 → 每指数内跨用户去重 Webhook URL。调度器在 `app.py` 模块顶层、**登录门之前**启动，保证无人登录时也能执行定时巡检。

### LLM 层（`llm_agent.py`）

- System Prompt = `prompt.md`（外部可编辑）+ `CATEGORY_STRATEGIES` 中的分类策略 + JSON Schema 约束。用户定制提示词会**完全替换**基础+策略部分；JSON Schema 始终追加。
- 期望输出为严格 JSON `{analysis, advice, confidence}`；`_parse_ai_json` 做三级容错解析。`advice` 会校验是否属于 `VALID_ADVICE` = {买入, 卖出, 持有/观望}。
- 本模块的 `INDICATOR_META` / `INDICATOR_GROUPS` 同时驱动 AI user prompt 的构建和 `app.py` tab3 的指标勾选框——唯一事实源。

### app.py 中的 Streamlit 状态机

手动巡检是一个**rerun 驱动的状态机**：每次脚本执行只处理一个标的，然后 `st.rerun()`。进度保存在 `st.session_state` 中（`inspection_active`、`inspection_index`、`inspection_cards`，以及作为防重入标记的 `_processed_codes`）。侧边栏交互会在巡检中途触发 rerun，该设计对此有容错——不要将其重构为阻塞式循环。切换用户必须调用 `_clear_inspection_state()`。

## 关键约束

- **无 schema 迁移机制**：`init_db()` 只做 `CREATE TABLE IF NOT EXISTS`。修改表结构需使用全新空库（或手动 `ALTER TABLE`）；旧库缺列会直接报错暴露——这是有意设计。
- `idx_factor_data` 的约 87 个列完全由 `database.py` 模块级常量 `IDX_FACTOR_COLUMNS` 驱动（建表、读写、Tushare `idx_factor_pro` 的 fields 参数的唯一事实源）。改列清单只改这一处。
- SQLite 运行在 WAL 模式 + `busy_timeout=5000`；`database.py` 的 `get_connection()` 是配置连接的唯一位置。
- `calculate_percentile()` 要求 ≥100 条历史记录，否则返回 `None` — 下游代码将 `None` 指标一律显示为 "N/A"；请遵循该模式。
- Tushare 接口支持优雅降级：`idx_factor_pro`（需 5000 积分）和 `index_member_all`（需 2000 积分）为可选——无权限时不得影响核心巡检。
- `risk_free_rate` 通过 akshare 获取（中国 10 年期国债收益率），接口异常时兜底 `0.0172`。
- 数据库中日期为 `YYYY-MM-DD` 字符串；字符串字典序等价于时间序——代码依赖这一点（如趋势图时间范围过滤）。
- `quant_system.db` 与 `logs/` 为运行时产物，已加入 `.gitignore`，切勿提交。
