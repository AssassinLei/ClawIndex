# 数据库设计说明

本项目使用 **SQLite** 作为本地持久化存储，数据库文件位于项目根目录：

```
quant_system.db
```

所有表结构与访问逻辑集中在 `database.py`。应用启动时由 `app.py` 调用 `init_db()` 确保表存在。

---

## 设计概览

系统支持多用户（无密码，用户名标识），共 **9 张表**，数据分为两类归属：

- **用户维度**（按 `username` 隔离）：监控池 `fund_pool`、Webhook 地址 `webhook_urls`、巡检推送开关（`app_settings` 命名空间键）、定制提示词 `fund_custom_prompts`、巡检结果 `inspection_log`
- **全局共享**：行情 `daily_market_data`、技术因子 `idx_factor_data`、行业分类 `industry_list`、定时巡检总开关

同一指数可被多个用户分别监控；行情/因子数据只存一份，删除监控时仅当无其他用户监控才清理共享数据；提示词与巡检结果按用户隔离。

表之间通过 `fund_code` 在业务逻辑上关联，**未定义外键约束**。

```
users (username PK)
  ▲ 1:N
fund_pool (username, fund_code, UNIQUE(username, fund_code))
        │ N:1 按 fund_code 关联（无外键）
        ▼
daily_market_data / idx_factor_data (fund_code + trade_date 联合主键)
        行情与因子全局共享，多用户复用同一份
```

---

## 表结构

### `users` — 用户表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `username` | TEXT | PRIMARY KEY | 用户名（无密码，注册即创建） |
| `created_at` | DATE | DEFAULT 当天本地日期 | 注册日期 |

### `fund_pool` — 监控池（按用户隔离）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | INTEGER | PRIMARY KEY, AUTOINCREMENT | 自增主键 |
| `username` | TEXT | NOT NULL | 归属用户 |
| `fund_code` | TEXT | NOT NULL | 标的代码，如 `801080.SI` |
| `fund_name` | TEXT | NOT NULL | 显示名称，如「电子」 |
| `category` | TEXT | NOT NULL | 策略分类，决定 AI 分析的策略框架 |
| `added_date` | DATE | DEFAULT 当天本地日期 | 加入监控池的日期 |

表级约束 `UNIQUE(username, fund_code)`：同一用户不可重复监控，不同用户可监控同一指数。

**`category` 是指数级全局属性**：巡检结果全局共享，同一指数必须全系统使用同一策略框架。`add_fund` 会强制沿用已有分类（他人已监控时新用户所选分类被覆盖，UI 会提示），保证同一 `fund_code` 各行的 `category` 恒一致。

**`category` 可选值：**

| 值 | 含义 | 策略侧重 |
|----|------|----------|
| `wide_base` | 宽基指数 | PE 分位 + MA60 趋势 |
| `tech_growth` | 科技成长 | PE 分位 + MA120 趋势过滤 |
| `cycle_mfg` | 周期制造 | PB 分位 |
| `dividend` | 稳健收息 | PB 分位 + PE 绝对值 |

### `daily_market_data` — 每日行情缓存（全局共享）

联合主键 `(fund_code, trade_date)`，数据来源 Tushare `sw_daily` 接口，写入时经 `safe_float` 清洗。

| 字段 | 类型 | 说明 |
|------|------|------|
| `fund_code` / `trade_date` | TEXT / DATE | 联合主键，日期格式 `YYYY-MM-DD` |
| `name` | TEXT | 指数名称 |
| `open_price` / `high_price` / `low_price` / `close_price` | REAL | 开高低收 |
| `change` / `pct_change` | REAL | 涨跌额 / 涨跌幅 |
| `volume` / `amount` | REAL | 成交量 / 成交额 |
| `pe` / `pb` | REAL | 市盈率 / 市净率 |
| `float_mv` / `total_mv` | REAL | 流通市值 / 总市值 |
| `risk_free_rate` | REAL | 无风险利率：akshare 实时拉取中国 10 年期国债收益率（小数形式），接口异常时兜底 `0.0172` |

### `idx_factor_data` — 指数技术因子（全局共享）

联合主键 `(fund_code, trade_date)`，数据来源 Tushare `idx_factor_pro` 接口（专业版，需 5000 积分）。

除主键外共 **87 个 REAL 列**（行情 9 列 + 技术因子 78 列，涵盖 MA/EMA/MACD/KDJ/RSI/BOLL/CCI/DMI/BIAS/ATR 等），列清单由 `database.py` 模块级常量 `IDX_FACTOR_COLUMNS` 统一驱动（建表 / 写入 / 读取 / 接口 fields 的唯一事实源）。

### `industry_list` — 申万行业分类（全局共享）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `index_code` | TEXT | PRIMARY KEY | 行业指数代码 |
| `industry_name` | TEXT | NOT NULL | 行业名称 |
| `parent_code` | TEXT | 可空 | 上级行业代码 |
| `level` | TEXT | NOT NULL | 层级：L1 / L2 / L3 |
| `industry_code` / `is_pub` / `src` | TEXT | 可空 | 行业编码 / 是否发布 / 来源（SW2021） |

### `inspection_log` — 巡检结果记录（按用户隔离）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | INTEGER | PRIMARY KEY, AUTOINCREMENT | 自增主键 |
| `username` | TEXT | NOT NULL | 归属用户 |
| `fund_code` / `fund_name` | TEXT | NOT NULL | 标的代码 / 名称 |
| `action` | TEXT | NOT NULL | 操作建议：买入 / 卖出 / 持有观望 / 数据异常 |
| `ai_report` | TEXT | 可空 | AI 分析文本 |
| `inspect_date` | DATE | DEFAULT 当天本地日期 | 巡检日期 |
| `confidence` | INTEGER | 可空 | AI 置信度 0~100 |
| `pe_percentile` / `pb_percentile` | REAL | 可空 | PE / PB 历史分位 |
| `ma60` / `ma120` | REAL | 可空 | 60 / 120 日均线 |
| `amount` / `amount_ma20` | REAL | 可空 | 成交额 / 20 日均成交额 |

`UNIQUE(username, fund_code, inspect_date)` + `INSERT OR REPLACE`：同一用户同一标的同一天巡检结果覆盖更新。定时巡检中同一指数每天只同步与计算一次，但按用户提示词配置分组分别调用 AI 并入库。

### `webhook_urls` — Webhook 地址（按用户隔离）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | INTEGER | PRIMARY KEY, AUTOINCREMENT | 自增主键 |
| `username` | TEXT | NOT NULL | 归属用户 |
| `url` | TEXT | NOT NULL | 飞书机器人 Webhook 地址 |
| `label` | TEXT | DEFAULT '' | 备注标签 |
| `created_at` | DATE | DEFAULT 当天本地日期 | 添加日期 |

同一用户下 URL 查重在 `add_webhook_url` 中以 SELECT 实现（非唯一约束）。

### `app_settings` — 键值设置

| 字段 | 类型 | 约束 |
|------|------|------|
| `key` | TEXT | PRIMARY KEY |
| `value` | TEXT | NOT NULL |

用户级开关以命名空间键存入，格式 `{key}:{username}`：

| 键 | 语义 |
|----|------|
| `scheduler_auto_enabled` | 定时巡检总开关（全局，不带用户后缀） |
| `scheduler_cron_expr` | 定时巡检 cron 表达式（全局，标准 5 字段，默认 `30 19 * * 1-5`；周字段 0=周一…6=周日，可用英文缩写如 tue,thu） |
| `webhook_inspection_enabled:{username}` | 该用户的巡检推送开关 |
| `webhook_strong_signal_only:{username}` | 该用户是否仅推送买入/卖出强信号（默认开启） |

读写通过 `get_user_setting` / `set_user_setting` 封装。

### `fund_custom_prompts` — 定制提示词（用户专属）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `username` | TEXT | PRIMARY KEY (与 fund_code 联合) | 归属用户 |
| `fund_code` | TEXT | PRIMARY KEY (与 username 联合) | 标的代码 |
| `custom_prompt` | TEXT | NOT NULL DEFAULT '' | 定制分析框架（替换 System Prompt 策略段落） |
| `selected_indicators` | TEXT | NOT NULL DEFAULT '' | 传递给 AI 的指标 key，逗号分隔 |
| `updated_at` | DATE | DEFAULT 当天本地日期 | 更新时间 |

主键 `(username, fund_code)`：同一指数不同用户各自配置，互不影响。

---

## 数据流

| 操作 | 触发位置 | 涉及表 | 说明 |
|------|----------|--------|------|
| 初始化表结构 | `app.py` → `init_db()` | 全部 | 应用每次加载时执行 |
| 注册用户 | `app.py` 登录门 → `register_user()` | `users` | 重名返回 False |
| 添加标的 | `app.py` → `add_fund(username, ...)` | `fund_pool` | 同用户重复添加触发唯一约束，静默忽略；他人已监控时强制沿用已有 category |
| 同步历史数据 | `data_fetcher.py` → `save_daily_data()` / `save_idx_factor_data()` | `daily_market_data` / `idx_factor_data` | 添加标的时拉近 10 年；行情已存在（他人已同步）则跳过全量拉取，改走 `sync_incremental` 补齐增量 |
| 增量同步 | 巡检流水线 → `sync_incremental()` | 同上 | 每次巡检前补齐 DB 最新日期到今天的缺口（行情与因子独立判断） |
| 删除标的 | `app.py` → `remove_fund(username, code)` | 多表 | 删本用户监控关系与其专属提示词；无其他用户监控才清理共享行情/因子（巡检记录保留） |
| 巡检入库 | `inspection_pipeline.py` → `save_inspection_result()` | `inspection_log` | 同用户同标的同日覆盖更新 |
| 定时巡检 | `scheduler.py` → `get_distinct_funds()` | `fund_pool` | 全用户并集去重，同一指数每天只同步/计算一次；按用户提示词配置分组去重调用 AI，每用户各自入库与推送 |
| 策略计算 | `strategy_engine.py` | `daily_market_data` | 读最新基本面、近 150 日价格、历史分位 |
| 提示词配置 | `app.py` tab3 → `upsert_custom_prompt()` | `fund_custom_prompts` | 按用户生效，AI 调用时读取当前用户配置 |

---

## 核心 API（`database.py`）

### 连接与用户

| 函数 | 作用 |
|------|------|
| `get_connection()` | 获取 SQLite 连接（WAL + busy_timeout=5000 + 外键开启），行结果以字典形式返回 |
| `init_db()` | 创建 9 张表（若不存在） |
| `register_user(username)` / `get_all_users()` | 用户注册与列表 |

### 监控池

| 函数 | 作用 |
|------|------|
| `add_fund(username, code, name, category)` | 向指定用户监控池插入一条记录；他人已监控时强制沿用已有 category |
| `get_shared_category(code)` | 查询指数已有的全局策略分类（无人监控返回 None） |
| `remove_fund(username, code)` | 删除用户监控关系；无人监控才清理共享数据 |
| `get_all_funds(username)` | 返回指定用户监控池条目 |
| `get_distinct_funds()` | 全用户基金池并集去重（供定时巡检） |
| `get_fund_watchers_map()` | fund_code → 监控用户列表（供推送路由） |

### 行情与因子

| 函数 | 作用 |
|------|------|
| `save_daily_data(df)` | 批量写入行情（INSERT OR REPLACE，`safe_float` 清洗） |
| `save_idx_factor_data(df)` | 批量写入技术因子（列由 `IDX_FACTOR_COLUMNS` 驱动） |
| `get_latest_trade_date(code)` / `get_latest_factor_trade_date(code)` | 行情 / 因子表最新日期（增量同步判断） |
| `get_latest_market_data(code)` / `get_market_data_for_date(code, date)` | 最新 / 指定日行情 |
| `get_latest_idx_factor(code)` | 最新一日全部技术因子（卡片弹窗展示） |
| `get_recent_prices(code, days=150)` | 最近 N 个交易日收盘价与成交额，供 MA 计算 |
| `get_indicator_history(code, columns)` | 历史指标时序（白名单列，供趋势图） |
| `calculate_percentile(code, indicator, value, lookback_days=2500)` | PE/PB 历史分位数（要求 ≥ 100 条数据） |

### 巡检、Webhook 与设置

| 函数 | 作用 |
|------|------|
| `save_inspection_result(...)` / `get_inspection_history(...)` | 巡检结果写入 / 多条件查询 |
| `get_webhook_urls(username)` / `add_webhook_url(username, url, label)` / `remove_webhook_url(username, id)` | 用户维度 Webhook 管理（删除校验归属） |
| `get_setting` / `set_setting` | 全局键值设置 |
| `get_user_setting` / `set_user_setting` | 用户级设置（命名空间键 `key:username`） |
| `get_custom_prompt` / `get_selected_indicators` / `upsert_custom_prompt` / `delete_custom_prompt` | 定制提示词管理（用户专属，均以 username + fund_code 定位） |
| `get_prompt_configs_for_users(fund_code, usernames)` | 批量读取多用户对同一指数的提示词配置，供定时巡检分组去重 |
| `save_industry_list(df)` / `get_all_industries()` / `get_industry_count()` | 行业分类管理 |

---

## `init_db()` 行为说明

`init_db()` 在 `app.py` 模块顶层被调用。Streamlit 会在用户交互时反复重跑脚本，因此该函数**会被多次执行**。

由于使用 `CREATE TABLE IF NOT EXISTS`：

- 表不存在 → 自动创建
- 表已存在 → 跳过，**不会清空或修改已有数据**

该设计是**幂等**的，重复执行安全，但每次交互会产生一次 SQLite 连接开销（通常可忽略）。

---

## 注意事项

### 1. 无 schema 迁移机制

`init_db()` 只负责「建表」（`CREATE TABLE IF NOT EXISTS`），**不会**自动添加新字段或修改列类型。若调整表结构，需以空库重新部署（或手动 `ALTER TABLE`）。带入旧版库时，缺失列会直接报错暴露，属有意设计。

### 2. 行情写入为 upsert

`save_daily_data()` / `save_idx_factor_data()` 使用 `executemany` + `INSERT OR REPLACE`，重复的 `(fund_code, trade_date)` 自动覆盖，配合 `sync_incremental()` 实现每日增量同步（巡检前自动补齐数据缺口）。

### 3. 删除需走 `remove_fund()`

表之间没有外键，共享数据的清理（含引用计数判断）集中实现在 `remove_fund()` 内。若绕过该函数直接删 `fund_pool` 行，会留下孤儿行情数据。

### 4. 分位数计算有最低数据量要求

`calculate_percentile()` 要求该标的有效历史记录 **≥ 100 条**，否则返回 `None`，AI 输入中将缺少分位指标。默认回溯窗口 `lookback_days=2500`（约 10 个交易年）。

### 5. `risk_free_rate` 为真实宏观数据

由 `data_fetcher.py` 通过 akshare `bond_china_yield` 接口拉取中国 10 年期国债收益率（百分比转小数逐日合并入行情），接口异常时使用默认值 `0.0172`（1.72%）。

### 6. 数据库文件不宜提交到 Git

`quant_system.db` 为运行时生成的本地文件，体积随历史数据增长。建议在 `.gitignore` 中忽略该文件，仅保留 `database.py` 中的 schema 定义作为结构来源。

### 7. 并发与备份

多用户场景下已启用 WAL 模式（读写并发）+ `busy_timeout=5000`（写锁等待 5 秒），可支撑十级用户量的 Streamlit 多会话并发。重要数据变更前建议复制 `quant_system.db` 备份。

---

## 常用维护命令

查看表结构（需安装 sqlite3 CLI）：

```bash
sqlite3 quant_system.db ".schema"
```

查看某用户的监控池：

```sql
SELECT * FROM fund_pool WHERE username = '你的用户名';
```

查看某标的最近 5 条行情：

```sql
SELECT trade_date, close_price, pe, pb FROM daily_market_data
WHERE fund_code = '801080.SI'
ORDER BY trade_date DESC
LIMIT 5;
```

查看今日巡检结果：

```sql
SELECT fund_name, action, confidence FROM inspection_log
WHERE inspect_date = date('now', 'localtime');
```

直接删除数据库文件可完全重置，下次启动应用时会自动重建空库：

```bash
rm quant_system.db   # Linux / macOS
del quant_system.db  # Windows
```
