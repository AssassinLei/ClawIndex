# 数据库设计说明

本项目使用 **SQLite** 作为本地持久化存储，数据库文件位于项目根目录：

```
quant_system.db
```

所有表结构与访问逻辑集中在 `database.py`。应用启动时由 `app.py` 调用 `init_db()` 确保表存在。

---

## 设计概览

数据库承担两类职责：

1. **监控池管理**（`fund_pool`）：记录用户要盯盘的标的及其策略分类。
2. **行情数据缓存**（`daily_market_data`）：存储从 Tushare 拉取的历史日线与估值数据，供策略引擎计算分位数、均线等指标。

两表通过 `fund_code` 在业务逻辑上关联，**未定义外键约束**。

```
┌─────────────────────┐         ┌──────────────────────────────┐
│      fund_pool      │         │     daily_market_data        │
├─────────────────────┤         ├──────────────────────────────┤
│ id (PK)             │         │ fund_code (PK) ──────────────┼──┐
│ fund_code (UNIQUE)  │────────▶│ trade_date (PK)              │  │
│ fund_name           │  1 : N  │ close_price                  │  │
│ category            │         │ pe                           │  │
│ added_date          │         │ pb                           │  │
└─────────────────────┘         │ risk_free_rate               │  │
                                └──────────────────────────────┘  │
                                         同一 fund_code 对应多行日线  │
```

---

## 表结构

### `fund_pool` — 监控池

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | INTEGER | PRIMARY KEY, AUTOINCREMENT | 自增主键 |
| `fund_code` | TEXT | UNIQUE, NOT NULL | 标的代码，如 `000300.SH` |
| `fund_name` | TEXT | NOT NULL | 显示名称，如「沪深300」 |
| `category` | TEXT | NOT NULL | 策略分类，决定硬逻辑规则分支 |
| `added_date` | DATE | DEFAULT 当天本地日期 | 加入监控池的日期 |

**`category` 可选值：**

| 值 | 含义 | 策略侧重 |
|----|------|----------|
| `wide_base` | 宽基指数 | PE 分位 + MA60 趋势 |
| `tech_growth` | 科技成长 | PE 分位 + MA120 趋势过滤 |
| `cycle_mfg` | 周期制造 | PB 分位 |
| `dividend` | 稳健收息 | PB 分位 + PE 绝对值 |

### `daily_market_data` — 每日行情缓存

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `fund_code` | TEXT | PRIMARY KEY (联合) | 标的代码 |
| `trade_date` | DATE | PRIMARY KEY (联合) | 交易日，格式 `YYYY-MM-DD` |
| `close_price` | REAL | 可空 | 收盘价 |
| `pe` | REAL | 可空 | 市盈率 |
| `pb` | REAL | 可空 | 市净率 |
| `risk_free_rate` | REAL | 可空 | 无风险利率（当前由数据采集层写入固定值） |

联合主键 `(fund_code, trade_date)` 保证同一标的、同一交易日只有一条记录。

---

## 数据流

| 操作 | 触发位置 | 涉及表 | 说明 |
|------|----------|--------|------|
| 初始化表结构 | `app.py` → `init_db()` | 两表 | 应用每次加载时执行 |
| 添加标的 | `app.py` → `add_fund()` | `fund_pool` | 重复代码会触发唯一约束，静默忽略 |
| 同步历史数据 | `data_fetcher.py` → `save_daily_data()` | `daily_market_data` | 添加标的时拉取近 10 年数据 |
| 删除标的 | `app.py` → `remove_fund()` | 两表 | 同时删除监控池条目及全部历史行情 |
| 策略计算 | `strategy_engine.py` | `daily_market_data` | 读最新基本面、近 150 日价格、历史分位 |

---

## 核心 API（`database.py`）

| 函数 | 作用 |
|------|------|
| `init_db()` | 创建表（若不存在） |
| `get_connection()` | 获取 SQLite 连接，行结果以字典形式返回 |
| `add_fund(code, name, category)` | 向监控池插入一条记录 |
| `remove_fund(code)` | 删除监控池记录及该标的全部行情 |
| `get_all_funds()` | 返回监控池全部条目 |
| `save_daily_data(df)` | 批量追加写入行情 DataFrame |
| `get_recent_prices(code, days=150)` | 取最近 N 个交易日收盘价，供 MA 计算 |
| `calculate_percentile(code, indicator, value, lookback_days=2500)` | 计算 PE/PB 历史分位数 |

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

`init_db()` 只负责「建表」，**不会**自动添加新字段或修改列类型。若未来调整表结构，需手动执行 `ALTER TABLE` 或备份后重建数据库。

### 2. 写入方式为 append，非 upsert

`save_daily_data()` 使用 `pandas.to_sql(..., if_exists='append')`。对已有 `(fund_code, trade_date)` 重复写入会触发主键冲突报错，**不会自动覆盖旧数据**。

增量更新场景应改为 `INSERT OR REPLACE` 或先查后写，目前项目尚未实现每日增量同步。

### 3. 删除需手动级联

两表之间没有外键，`remove_fund()` 中手动删除了 `daily_market_data` 中对应记录。若绕过该函数直接删 `fund_pool`，会留下孤儿行情数据。

### 4. 分位数计算有最低数据量要求

`calculate_percentile()` 要求该标的有效历史记录 **≥ 100 条**，否则返回 `None`，策略引擎将跳过依赖分位数的判定分支。

默认回溯窗口 `lookback_days=2500`（约 10 个交易年）。

### 5. `risk_free_rate` 当前为占位数据

`data_fetcher.py` 中将无风险利率写死为 `0.023`（2.3%），并逐行存入 `daily_market_data`。接入真实宏观数据前，该字段不代表实际历史利率。

### 6. 数据库文件不宜提交到 Git

`quant_system.db` 为运行时生成的本地文件，体积随历史数据增长。建议在 `.gitignore` 中忽略该文件，仅保留 `database.py` 中的 schema 定义作为结构来源。

### 7. 并发与备份

SQLite 适合单用户本地场景。多进程同时写入可能遇到锁冲突；重要数据变更前建议复制 `quant_system.db` 备份。

---

## 常用维护命令

查看表结构（需安装 sqlite3 CLI）：

```bash
sqlite3 quant_system.db ".schema"
```

查看监控池：

```sql
SELECT * FROM fund_pool;
```

查看某标的最近 5 条行情：

```sql
SELECT * FROM daily_market_data
WHERE fund_code = '000300.SH'
ORDER BY trade_date DESC
LIMIT 5;
```

清空全部数据（谨慎操作）：

```sql
DELETE FROM daily_market_data;
DELETE FROM fund_pool;
```

或直接删除数据库文件，下次启动应用时会自动重建空库：

```bash
rm quant_system.db   # Linux / macOS
del quant_system.db  # Windows
```
