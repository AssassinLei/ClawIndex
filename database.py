import sqlite3
import pandas as pd
import os
from typing import List, Dict
from logger import setup_logger

logger = setup_logger("database")

DB_NAME = "quant_system.db"

# idx_factor_pro 接口输出的全部数值列（除 ts_code/trade_date 外），共 87 列：
# 行情 9 列 + 技术因子 78 列。建表/写入/读取均以 IDX_FACTOR_COLUMNS 为唯一事实源，
# data_fetcher 拼接接口 fields 参数时也复用该列表，确保字段一致。
# 拆分为行情/技术两段：并入 indicators 时只取技术列，避免 amount 等行情列覆盖既有指标键。
IDX_FACTOR_MARKET_COLUMNS = [
    'open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_change', 'vol', 'amount',
]
IDX_FACTOR_TECH_COLUMNS = [
    # 技术因子（_bfq 表示不复权）
    'asi_bfq', 'asit_bfq', 'atr_bfq', 'bbi_bfq',
    'bias1_bfq', 'bias2_bfq', 'bias3_bfq',
    'boll_lower_bfq', 'boll_mid_bfq', 'boll_upper_bfq',
    'brar_ar_bfq', 'brar_br_bfq', 'cci_bfq', 'cr_bfq',
    'dfma_dif_bfq', 'dfma_difma_bfq',
    'dmi_adx_bfq', 'dmi_adxr_bfq', 'dmi_mdi_bfq', 'dmi_pdi_bfq',
    'downdays', 'updays', 'dpo_bfq', 'madpo_bfq',
    'ema_bfq_5', 'ema_bfq_10', 'ema_bfq_20', 'ema_bfq_30',
    'ema_bfq_60', 'ema_bfq_90', 'ema_bfq_250',
    'emv_bfq', 'maemv_bfq', 'expma_12_bfq', 'expma_50_bfq',
    'kdj_bfq', 'kdj_d_bfq', 'kdj_k_bfq',
    'ktn_down_bfq', 'ktn_mid_bfq', 'ktn_upper_bfq',
    'lowdays', 'topdays',
    'ma_bfq_5', 'ma_bfq_10', 'ma_bfq_20', 'ma_bfq_30',
    'ma_bfq_60', 'ma_bfq_90', 'ma_bfq_250',
    'macd_bfq', 'macd_dea_bfq', 'macd_dif_bfq',
    'mass_bfq', 'ma_mass_bfq', 'mfi_bfq', 'mtm_bfq', 'mtmma_bfq', 'obv_bfq',
    'psy_bfq', 'psyma_bfq', 'roc_bfq', 'maroc_bfq',
    'rsi_bfq_6', 'rsi_bfq_12', 'rsi_bfq_24',
    'taq_down_bfq', 'taq_mid_bfq', 'taq_up_bfq',
    'trix_bfq', 'trma_bfq', 'vr_bfq', 'wr_bfq', 'wr1_bfq',
    'xsii_td1_bfq', 'xsii_td2_bfq', 'xsii_td3_bfq', 'xsii_td4_bfq',
]
IDX_FACTOR_COLUMNS = IDX_FACTOR_MARKET_COLUMNS + IDX_FACTOR_TECH_COLUMNS


def safe_float(val):
    """安全转 float，非数值 / NaN / Inf / 空串 → None

    跨模块公共工具：被 database（写入路径）和 strategy_engine（读取路径）共同依赖。
    修改此函数的清洗规则时，需同步评估对策略引擎 PE/PB 分位数、风险溢价、ROE
    计算及下游 Webhook / AI 报告输出的影响。
    """
    if val is None:
        return None
    s = str(val).strip()
    if s == '' or s.lower() in ('nan', 'inf', '-inf', 'none', 'n/a'):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _norm_date(date_str: str) -> str:
    """将 YYYYMMDD 或 YYYY-MM-DD 统一规范化为 YYYY-MM-DD，异常输入返回原值"""
    if not date_str:
        return date_str
    s = str(date_str).strip()
    # 已是 YYYY-MM-DD
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        return s
    # YYYYMMDD → YYYY-MM-DD
    clean = s.replace('-', '').replace('/', '')
    if len(clean) == 8 and clean.isdigit():
        return f"{clean[:4]}-{clean[4:6]}-{clean[6:8]}"
    return s


def get_connection():
    # 确保返回的行是字典格式，方便读取字段
    conn = sqlite3.connect(DB_NAME)
    # 启用 WAL 模式：读写可并发，提升巡检写入时的查询性能
    conn.execute("PRAGMA journal_mode=WAL")
    # 多用户并发写入时等待锁最多 5 秒，避免 database is locked
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库表结构"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # 表1：基金池（多用户：同一指数可被多个用户分别监控）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fund_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                fund_code TEXT NOT NULL,
                fund_name TEXT NOT NULL,
                category TEXT NOT NULL, -- wide_base, tech_growth, cycle_mfg, dividend
                added_date DATE DEFAULT (date('now', 'localtime')),
                UNIQUE(username, fund_code)
            )
        ''')
        
        # 表2：每日基础行情缓存
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_market_data (
                fund_code TEXT NOT NULL,
                trade_date DATE NOT NULL,
                name TEXT,
                open_price REAL,
                high_price REAL,
                low_price REAL,
                close_price REAL,
                change REAL,
                pct_change REAL,
                volume REAL,
                amount REAL,
                pe REAL,
                pb REAL,
                float_mv REAL,
                total_mv REAL,
                risk_free_rate REAL,
                PRIMARY KEY (fund_code, trade_date)
            )
        ''')

        # 表3：行业分类列表（申万行业指数）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS industry_list (
                index_code TEXT PRIMARY KEY,
                industry_name TEXT NOT NULL,
                parent_code TEXT,
                level TEXT NOT NULL,
                industry_code TEXT,
                is_pub TEXT,
                src TEXT
            )
        ''')
        
        # 表4：巡检结果记录（按用户隔离）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS inspection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                fund_code TEXT NOT NULL,
                fund_name TEXT NOT NULL,
                action TEXT NOT NULL,
                ai_report TEXT,
                inspect_date DATE DEFAULT (date('now', 'localtime')),
                confidence INTEGER,
                pe_percentile REAL,
                pb_percentile REAL,
                ma60 REAL,
                ma120 REAL,
                amount REAL,
                amount_ma20 REAL,
                UNIQUE(username, fund_code, inspect_date)
            )
        ''')
        
        # 表5：Webhook 地址配置（按用户隔离）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS webhook_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                url TEXT NOT NULL,
                label TEXT DEFAULT '',
                created_at DATE DEFAULT (date('now', 'localtime'))
            )
        ''')
        
        # 表6：应用设置键值存储
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        
        # 表7：指数定制提示词（用户专属：同一指数不同用户各自配置）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fund_custom_prompts (
                username TEXT NOT NULL,
                fund_code TEXT NOT NULL,
                custom_prompt TEXT NOT NULL DEFAULT '',
                selected_indicators TEXT NOT NULL DEFAULT '',
                updated_at DATE DEFAULT (date('now', 'localtime')),
                PRIMARY KEY (username, fund_code)
            )
        ''')
        
        # 表8：指数技术因子（idx_factor_pro 专业版数据，列结构由 IDX_FACTOR_COLUMNS 驱动）
        factor_cols_sql = ",\n                ".join(f"{c} REAL" for c in IDX_FACTOR_COLUMNS)
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS idx_factor_data (
                fund_code TEXT NOT NULL,
                trade_date DATE NOT NULL,
                {factor_cols_sql},
                PRIMARY KEY (fund_code, trade_date)
            )
        ''')
        
        # 表9：用户表（无密码，仅用户名标识）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                created_at DATE DEFAULT (date('now', 'localtime'))
            )
        ''')
        
        conn.commit()
    finally:
        conn.close()
    logger.info("数据库初始化完成")


# --- 用户管理 ---
def register_user(username: str) -> bool:
    """注册新用户，返回 True 表示成功，False 表示用户名为空或已存在"""
    username = username.strip()
    if not username:
        return False
    conn = get_connection()
    try:
        conn.execute("INSERT INTO users (username) VALUES (?)", (username,))
        conn.commit()
        logger.info(f"register_user: {username}")
        return True
    except sqlite3.IntegrityError:
        logger.warning(f"register_user: {username} 已存在")
        return False
    finally:
        conn.close()


def get_all_users() -> List[str]:
    """获取全部已注册用户名列表（仅供后台调度使用，不得在 UI 展示，避免泄露其他用户名）"""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT username FROM users ORDER BY created_at, username")
        return [row["username"] for row in cursor.fetchall()]
    finally:
        conn.close()


def user_exists(username: str) -> bool:
    """检查用户名是否已注册，供登录校验使用"""
    username = username.strip()  # 与 register_user 防御口径对齐，不依赖调用方预处理
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()

# --- 基金池管理操作 ---
def get_shared_category(fund_code: str) -> str | None:
    """查询指数在任意用户监控池中已有的策略分类，无人监控时返回 None。

    category 是指数级全局属性：巡检结果全局共享，同一指数必须在全系统
    使用同一策略框架，否则不同用户的 AI 分析会互相覆写、推送内容错位。
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT category FROM fund_pool WHERE fund_code = ? LIMIT 1", (fund_code,)
        ).fetchone()
        return row["category"] if row else None
    finally:
        conn.close()


def add_fund(username: str, fund_code: str, fund_name: str, category: str) -> bool:
    """添加基金到指定用户的监控池，返回 True 表示新增成功，False 表示已存在。

    若该指数已被其他用户监控，强制沿用已有 category（指数级全局属性，
    纵深防御：无论调用方是否已做检查，此处都不会产生分类分歧）。
    """
    conn = get_connection()
    try:
        # 强制沿用已有分类，保证同一指数全系统只有一个策略框架
        existing = conn.execute(
            "SELECT category FROM fund_pool WHERE fund_code = ? LIMIT 1", (fund_code,)
        ).fetchone()
        if existing and existing["category"] != category:
            logger.info(
                f"add_fund: [{username}] {fund_code} 所选分类 {category} 被覆盖为已有分类 {existing['category']}（指数级全局属性）"
            )
            category = existing["category"]
        conn.execute(
            "INSERT INTO fund_pool (username, fund_code, fund_name, category) VALUES (?, ?, ?, ?)",
            (username, fund_code, fund_name, category)
        )
        conn.commit()
        logger.info(f"add_fund: [{username}] {fund_code} {fund_name} (category={category})")
        return True
    except sqlite3.IntegrityError:
        logger.warning(f"add_fund: [{username}] {fund_code} already exists")
        return False
    finally:
        conn.close()

def remove_fund(username: str, fund_code: str):
    """将指数移出指定用户的监控池；提示词随本用户删除，行情/因子仅当无人监控时清理"""
    conn = get_connection()
    try:
        # 1. 删除本用户的监控关系与其专属提示词
        conn.execute("DELETE FROM fund_pool WHERE username = ? AND fund_code = ?", (username, fund_code))
        conn.execute("DELETE FROM fund_custom_prompts WHERE username = ? AND fund_code = ?", (username, fund_code))
        # 2. 检查是否仍有其他用户监控该指数
        remaining = conn.execute(
            "SELECT COUNT(*) FROM fund_pool WHERE fund_code = ?", (fund_code,)
        ).fetchone()[0]
        # 3. 无人监控才清理共享的行情/因子数据（inspection_log 保留为历史）
        if remaining == 0:
            conn.execute("DELETE FROM daily_market_data WHERE fund_code = ?", (fund_code,))
            conn.execute("DELETE FROM idx_factor_data WHERE fund_code = ?", (fund_code,))
        conn.commit()
    finally:
        conn.close()
    if remaining == 0:
        logger.info(f"remove_fund: [{username}] {fund_code}，已无用户监控，行情/因子已清理（本用户提示词已删除）")
    else:
        logger.info(f"remove_fund: [{username}] {fund_code}，仍有 {remaining} 个用户监控，共享数据保留（本用户提示词已删除）")

def get_all_funds(username: str) -> List[Dict]:
    """获取指定用户的监控池列表"""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT * FROM fund_pool WHERE username = ?", (username,))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_distinct_funds() -> List[Dict]:
    """获取全体用户基金池的并集（按 fund_code 去重），供定时巡检使用。

    category 由 add_fund 强制沿用已有值（指数级全局属性），同一 fund_code
    各行取值恒一致，MIN 聚合仅为 GROUP BY 语法需要。
    """
    conn = get_connection()
    try:
        cursor = conn.execute("""
            SELECT fund_code, MIN(fund_name) AS fund_name, MIN(category) AS category
            FROM fund_pool GROUP BY fund_code
        """)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_fund_watchers_map() -> Dict[str, List[str]]:
    """构建 fund_code → 监控用户名列表 的映射，供定时巡检推送路由（避免循环内 N+1 查询）"""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT fund_code, username FROM fund_pool")
        result: Dict[str, List[str]] = {}
        for row in cursor.fetchall():
            result.setdefault(row["fund_code"], []).append(row["username"])
        return result
    finally:
        conn.close()

# --- 行情数据写入操作 ---
def save_daily_data(df: pd.DataFrame):
    """将获取到的 Pandas DataFrame 数据批量存入 SQLite，遇重复主键则覆盖"""
    if df.empty:
        return
    conn = get_connection()
    try:
        cursor = conn.cursor()

        rows = []
        for _, row in df.iterrows():
            rows.append((
                row.get('fund_code'),
                _norm_date(str(row.get('trade_date', ''))),
                row.get('name'),
                safe_float(row.get('open_price')),
                safe_float(row.get('high_price')),
                safe_float(row.get('low_price')),
                safe_float(row.get('close_price')),
                safe_float(row.get('change')),
                safe_float(row.get('pct_change')),
                safe_float(row.get('volume')),
                safe_float(row.get('amount')),
                safe_float(row.get('pe')),
                safe_float(row.get('pb')),
                safe_float(row.get('float_mv')),
                safe_float(row.get('total_mv')),
                safe_float(row.get('risk_free_rate')),
            ))

        cursor.executemany("""
            INSERT OR REPLACE INTO daily_market_data
                (fund_code, trade_date, name, open_price, high_price, low_price,
                 close_price, change, pct_change, volume, amount,
                 pe, pb, float_mv, total_mv, risk_free_rate)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)

        conn.commit()
        logger.info(f"save_daily_data: 写入 {len(rows)} 行行情数据")
    finally:
        conn.close()

# --- 核心：策略查询依赖 ---
def get_latest_trade_date(fund_code: str) -> str:
    """获取指定标的在数据库中的最新交易日期，用于增量同步判断"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 使用 REPLACE 去横线再取 MAX，兼容 YYYYMMDD / YYYY-MM-DD 两种格式
        cursor.execute(
            "SELECT MAX(REPLACE(trade_date, '-', '')) FROM daily_market_data WHERE fund_code = ?",
            (fund_code,)
        )
        result = cursor.fetchone()[0]
        return str(result) if result is not None else None  # 返回 None 表示无数据
    finally:
        conn.close()

def get_recent_prices(fund_code: str, days: int = 150) -> pd.DataFrame:
    """获取最近N天的价格与成交额，用于计算移动平均线等技术指标"""
    conn = get_connection()
    try:
        # 使用 REPLACE 去横线后排序，兼容 YYYYMMDD / YYYY-MM-DD 两种格式
        query = """
            SELECT trade_date, close_price, amount
            FROM daily_market_data 
            WHERE fund_code = ? 
            ORDER BY REPLACE(trade_date, '-', '') DESC LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=(fund_code, days))
    finally:
        conn.close()
    # 确保 close_price 为数值类型（兼容历史 TEXT 数据）
    df['close_price'] = pd.to_numeric(df['close_price'], errors='coerce')
    df['amount'] = pd.to_numeric(df['amount'], errors='coerce')
    # 统一日期格式再排序，避免字典序陷阱
    df['trade_date'] = df['trade_date'].astype(str).apply(_norm_date)
    # 返回正序排序的数据，方便计算指标
    return df.sort_values(by='trade_date').reset_index(drop=True)


def get_indicator_history(fund_code: str, columns: list[str]) -> pd.DataFrame:
    """获取指定标的的历史指标时序数据，供趋势图使用。

    白名单校验 columns 防止 SQL 注入，仅允许 daily_market_data 中的数值列。
    返回 DataFrame 含 trade_date + 请求列，按日期正序排列。
    """
    ALLOWED = {'close_price', 'pe', 'pb', 'amount', 'risk_free_rate'}
    safe_cols = [c for c in columns if c in ALLOWED]
    if not safe_cols:
        return pd.DataFrame()
    cols_str = ', '.join(safe_cols)
    conn = get_connection()
    try:
        query = (
            f"SELECT trade_date, {cols_str} FROM daily_market_data "
            f"WHERE fund_code = ? "
            f"ORDER BY REPLACE(trade_date, '-', '') ASC"
        )
        df = pd.read_sql_query(query, conn, params=(fund_code,))
    finally:
        conn.close()
    if df.empty:
        return df
    df['trade_date'] = df['trade_date'].astype(str).apply(_norm_date)
    for col in safe_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df

def calculate_percentile(fund_code: str, indicator: str, current_value: float, lookback_days: int = 2500) -> float:
    """
    利用 SQL 计算指定指标（如 PE, PB）的历史分位数。
    lookback_days 默认 2500，约等于10个交易年。
    """
    if current_value is None or indicator not in ['pe', 'pb', 'close_price']:
        return None
                
    conn = get_connection()
    try:
        cursor = conn.cursor()
                
        # 1. 检查历史总条数（限定在 lookback_days 窗口内，与分位计算保持一致）
        cursor.execute(f"SELECT COUNT(*) FROM (SELECT {indicator} FROM daily_market_data WHERE fund_code = ? AND {indicator} IS NOT NULL ORDER BY trade_date DESC LIMIT ?)", (fund_code, lookback_days))
        total_count = cursor.fetchone()[0]
                
        logger.info(
            "[%s] %s 分位计算：请求窗口 %d 天，实际可用数据 %d 条",
            fund_code, indicator.upper(), lookback_days, total_count
        )
    
        if total_count < 100:
            logger.warning(
                "[%s] %s 分位计算数据不足（实际 %d 条 < 100），跳过",
                fund_code, indicator.upper(), total_count
            )
            return None # 数据不足，不计算分位
                    
        # 2. 计算有多少天的数值低于当前数值
        cursor.execute(f"""
            SELECT COUNT(*) FROM (
                SELECT {indicator} FROM daily_market_data 
                WHERE fund_code = ? AND {indicator} IS NOT NULL 
                ORDER BY trade_date DESC LIMIT ?
            ) WHERE {indicator} < ?
        """, (fund_code, lookback_days, current_value))
                
        lower_count = cursor.fetchone()[0]
                
        # 计算百分比并保留四位小数 (例如 0.1542 代表 15.42%)
        return round(lower_count / total_count, 4)
    finally:
        conn.close()

# --- 技术因子数据操作 ---
def save_idx_factor_data(df: pd.DataFrame):
    """将 idx_factor_pro 拉取的因子数据批量存入 SQLite，遇重复主键则覆盖"""
    if df.empty:
        return
    conn = get_connection()
    try:
        cursor = conn.cursor()

        rows = []
        for _, row in df.iterrows():
            rows.append(
                (row.get('fund_code'), _norm_date(str(row.get('trade_date', ''))))
                + tuple(safe_float(row.get(c)) for c in IDX_FACTOR_COLUMNS)
            )

        cols_str = ', '.join(['fund_code', 'trade_date'] + IDX_FACTOR_COLUMNS)
        placeholders = ', '.join(['?'] * (len(IDX_FACTOR_COLUMNS) + 2))
        cursor.executemany(
            f"INSERT OR REPLACE INTO idx_factor_data ({cols_str}) VALUES ({placeholders})",
            rows
        )

        conn.commit()
        logger.info(f"save_idx_factor_data: 写入 {len(rows)} 行技术因子数据")
    finally:
        conn.close()


def get_latest_factor_trade_date(fund_code: str) -> str:
    """获取指定标的在因子表中的最新交易日期，用于增量同步判断"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 使用 REPLACE 去横线再取 MAX，兼容 YYYYMMDD / YYYY-MM-DD 两种格式
        cursor.execute(
            "SELECT MAX(REPLACE(trade_date, '-', '')) FROM idx_factor_data WHERE fund_code = ?",
            (fund_code,)
        )
        result = cursor.fetchone()[0]
        return str(result) if result is not None else None  # 返回 None 表示无数据
    finally:
        conn.close()


def get_latest_idx_factor(fund_code: str) -> Dict:
    """查询指定标的最新一日的全部技术因子数据，供巡检卡片展示"""
    conn = get_connection()
    try:
        cursor = conn.execute("""
            SELECT *
            FROM idx_factor_data
            WHERE fund_code = ?
            ORDER BY REPLACE(trade_date, '-', '') DESC LIMIT 1
        """, (fund_code,))
        row = cursor.fetchone()
        if not row:
            return {}
        result = {c: safe_float(row[c]) for c in IDX_FACTOR_COLUMNS}
        result['trade_date'] = _norm_date(row['trade_date'])
        return result
    finally:
        conn.close()


def get_idx_factor_for_date(fund_code: str, trade_date: str) -> Dict:
    """查询指定标的在某日的全部技术因子数据，供历史页回溯当日因子。

    兼容数据库中 YYYYMMDD 和 YYYY-MM-DD 两种日期格式；无匹配时返回空 dict，
    由调用方决定是否回退到最新可用数据。
    """
    conn = get_connection()
    try:
        normalized = _norm_date(trade_date)
        cursor = conn.execute("""
            SELECT *
            FROM idx_factor_data
            WHERE fund_code = ? AND REPLACE(trade_date, '-', '') = REPLACE(?, '-', '')
        """, (fund_code, normalized))
        row = cursor.fetchone()
        if not row:
            return {}
        result = {c: safe_float(row[c]) for c in IDX_FACTOR_COLUMNS}
        result['trade_date'] = _norm_date(row['trade_date'])
        return result
    finally:
        conn.close()


# --- 行业分类管理 ---
def save_industry_list(df: pd.DataFrame):
    """批量保存行业分类数据，遇重复主键则覆盖"""
    if df.empty:
        return
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        rows = []
        for _, row in df.iterrows():
            rows.append((
                row.get('index_code'),
                row.get('industry_name'),
                row.get('parent_code'),
                row.get('level'),
                row.get('industry_code'),
                row.get('is_pub'),
                row.get('src'),
            ))
        
        cursor.executemany("""
            INSERT OR REPLACE INTO industry_list
                (index_code, industry_name, parent_code, level, industry_code, is_pub, src)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, rows)
        
        conn.commit()
        logger.info(f"save_industry_list: 写入 {len(rows)} 条行业分类")
    finally:
        conn.close()

def get_all_industries() -> List[Dict]:
    """获取所有行业分类数据"""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT * FROM industry_list ORDER BY level, industry_name")
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

def get_industry_count() -> int:
    """获取行业分类总数"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM industry_list")
        return cursor.fetchone()[0]
    finally:
        conn.close()

def save_inspection_result(username: str, fund_code: str, fund_name: str, action: str, ai_report: str = "",
                           pe_percentile: float = None, pb_percentile: float = None,
                           ma60: float = None, ma120: float = None,
                           amount: float = None, amount_ma20: float = None,
                           confidence: int = None):
    """保存巡检结果（同一用户同一标的同一天覆盖更新），含计算指标与AI置信度"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO inspection_log
                (username, fund_code, fund_name, action, ai_report, inspect_date,
                 pe_percentile, pb_percentile, ma60, ma120, amount, amount_ma20, confidence)
            VALUES (?, ?, ?, ?, ?, date('now', 'localtime'), ?, ?, ?, ?, ?, ?, ?)
        """, (username, fund_code, fund_name, action, ai_report,
              pe_percentile, pb_percentile, ma60, ma120, amount, amount_ma20, confidence))
        conn.commit()
        logger.info(
            f"save_inspection_result: [{username}] {fund_code} {fund_name} action={action} "
            f"PE%={pe_percentile} PB%={pb_percentile} MA60={ma60} MA120={ma120} confidence={confidence}"
        )
    finally:
        conn.close()

def get_inspection_history(username: str, fund_code: str = None, action: str = None,
                           start_date: str = None, end_date: str = None) -> List[Dict]:
    """按用户查询巡检历史记录，支持多条件筛选，按日期倒序。"""
    conn = get_connection()
    try:
        query = "SELECT * FROM inspection_log WHERE username = ?"
        params = [username]

        if fund_code:
            query += " AND fund_code = ?"
            params.append(fund_code)
        if action:
            query += " AND action = ?"
            params.append(action)
        if start_date:
            query += " AND inspect_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND inspect_date <= ?"
            params.append(end_date)
        
        query += " ORDER BY inspect_date DESC, id DESC"
        
        cursor = conn.execute(query, params)
        records = [dict(row) for row in cursor.fetchall()]
        logger.info(f"get_inspection_history: [{username}] 返回 {len(records)} 条记录")
        return records
    finally:
        conn.close()

def get_market_data_for_date(fund_code: str, trade_date: str) -> Dict:
    """查询指定标的在某日的全部行情数据。
    兼容数据库中 YYYYMMDD 和 YYYY-MM-DD 两种格式。"""
    conn = get_connection()
    try:
        normalized = _norm_date(trade_date)
        cursor = conn.execute("""
            SELECT *
            FROM daily_market_data
            WHERE fund_code = ? AND REPLACE(trade_date, '-', '') = REPLACE(?, '-', '')
        """, (fund_code, normalized))
        row = cursor.fetchone()
        if not row:
            return {}
        return {
            "close_price": safe_float(row["close_price"]),
            "open_price": safe_float(row["open_price"]),
            "high_price": safe_float(row["high_price"]),
            "low_price": safe_float(row["low_price"]),
            "change": safe_float(row["change"]),
            "pct_change": safe_float(row["pct_change"]),
            "volume": safe_float(row["volume"]),
            "amount": safe_float(row["amount"]),
            "pe": safe_float(row["pe"]),
            "pb": safe_float(row["pb"]),
            "float_mv": safe_float(row["float_mv"]),
            "total_mv": safe_float(row["total_mv"]),
            "risk_free_rate": safe_float(row["risk_free_rate"]),
        }
    finally:
        conn.close()


def get_latest_market_data(fund_code: str) -> Dict:
    """查询指定指数的最新行情数据（不限日期），作为 get_market_data_for_date 的回退"""
    conn = get_connection()
    try:
        cursor = conn.execute("""
            SELECT *
            FROM daily_market_data
            WHERE fund_code = ?
            ORDER BY trade_date DESC LIMIT 1
        """, (fund_code,))
        row = cursor.fetchone()
        if not row:
            return {}
        return {
            "close_price": safe_float(row["close_price"]),
            "open_price": safe_float(row["open_price"]),
            "high_price": safe_float(row["high_price"]),
            "low_price": safe_float(row["low_price"]),
            "change": safe_float(row["change"]),
            "pct_change": safe_float(row["pct_change"]),
            "volume": safe_float(row["volume"]),
            "amount": safe_float(row["amount"]),
            "pe": safe_float(row["pe"]),
            "pb": safe_float(row["pb"]),
            "float_mv": safe_float(row["float_mv"]),
            "total_mv": safe_float(row["total_mv"]),
            "risk_free_rate": safe_float(row["risk_free_rate"]),
            "trade_date": _norm_date(row["trade_date"]),
        }
    finally:
        conn.close()

# --- Webhook 配置管理 ---
def get_webhook_urls(username: str) -> List[Dict]:
    """获取指定用户已保存的 webhook 地址"""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT * FROM webhook_urls WHERE username = ? ORDER BY id", (username,))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

def add_webhook_url(username: str, url: str, label: str = "") -> bool:
    """为指定用户添加一条 webhook 地址，返回 True 表示新增成功，False 表示已存在"""
    url = url.strip()
    label = label.strip()
    conn = get_connection()
    try:
        # 检查同一用户下是否已存在相同 URL
        existing = conn.execute(
            "SELECT id FROM webhook_urls WHERE username = ? AND url = ?", (username, url)
        ).fetchone()
        if existing:
            logger.warning(f"add_webhook_url: [{username}] URL 已存在, id={existing['id']}")
            return False
        conn.execute(
            "INSERT INTO webhook_urls (username, url, label) VALUES (?, ?, ?)",
            (username, url, label)
        )
        conn.commit()
        logger.info(f"add_webhook_url: [{username}] {url[:50]}...")
        return True
    finally:
        conn.close()

def remove_webhook_url(username: str, webhook_id: int):
    """删除指定用户的 webhook 地址（校验归属，防止误删他人配置）"""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM webhook_urls WHERE id = ? AND username = ?",
            (webhook_id, username)
        )
        conn.commit()
        logger.info(f"remove_webhook_url: [{username}] id={webhook_id}")
    finally:
        conn.close()

# --- 应用设置管理 ---
def get_setting(key: str, default: str = "") -> str:
    """读取应用设置，不存在时返回默认值"""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default
    finally:
        conn.close()

def set_setting(key: str, value: str):
    """写入或更新应用设置"""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        conn.commit()
        logger.info(f"set_setting: {key} = {value}")
    finally:
        conn.close()


# --- 用户级设置（命名空间键 key:username，复用 app_settings 表） ---
def get_user_setting(username: str, key: str, default: str = "") -> str:
    """读取指定用户的设置，不存在时返回默认值"""
    return get_setting(f"{key}:{username}", default)


def set_user_setting(username: str, key: str, value: str):
    """写入或更新指定用户的设置"""
    set_setting(f"{key}:{username}", value)


# --- 指数定制提示词管理（用户专属） ---
def get_custom_prompt(username: str, fund_code: str) -> str | None:
    """获取指定用户对某个指数的定制提示词，未配置时返回 None"""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT custom_prompt FROM fund_custom_prompts WHERE username = ? AND fund_code = ?",
            (username, fund_code)
        )
        row = cursor.fetchone()
        if row and row["custom_prompt"].strip():
            return row["custom_prompt"].strip()
        return None
    finally:
        conn.close()


def get_selected_indicators(username: str, fund_code: str) -> list[str]:
    """获取指定用户对某个指数已勾选的指标列表，未配置时返回空列表"""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT selected_indicators FROM fund_custom_prompts WHERE username = ? AND fund_code = ?",
            (username, fund_code)
        )
        row = cursor.fetchone()
        if row and row["selected_indicators"].strip():
            return [k.strip() for k in row["selected_indicators"].split(",") if k.strip()]
        return []
    finally:
        conn.close()


def get_prompt_configs_for_users(fund_code: str, usernames: list[str]) -> dict[str, tuple[str | None, list[str]]]:
    """批量获取多个用户对同一指数的提示词配置，供定时巡检分组去重。

    返回 {username: (custom_prompt | None, selected_indicators)}，
    未配置的用户返回 (None, [])。
    """
    result: dict[str, tuple[str | None, list[str]]] = {u: (None, []) for u in usernames}
    if not usernames:
        return result
    conn = get_connection()
    try:
        placeholders = ",".join("?" for _ in usernames)
        cursor = conn.execute(
            f"SELECT username, custom_prompt, selected_indicators FROM fund_custom_prompts "
            f"WHERE fund_code = ? AND username IN ({placeholders})",
            (fund_code, *usernames)
        )
        for row in cursor.fetchall():
            prompt = row["custom_prompt"].strip() or None
            indicators = [k.strip() for k in row["selected_indicators"].split(",") if k.strip()]
            result[row["username"]] = (prompt, indicators)
        return result
    finally:
        conn.close()


def upsert_custom_prompt(username: str, fund_code: str, prompt_text: str, indicators: list[str] | None = None) -> None:
    """插入或更新指定用户对指数的定制提示词及选中的指标"""
    indicators_str = ",".join(indicators) if indicators else ""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO fund_custom_prompts (username, fund_code, custom_prompt, selected_indicators, updated_at) VALUES (?, ?, ?, ?, date('now', 'localtime'))",
            (username, fund_code, prompt_text, indicators_str)
        )
        conn.commit()
        logger.info(f"upsert_custom_prompt: [{username}] {fund_code} prompt 已更新 ({len(prompt_text)} 字符, 指标 {len(indicators or [])} 项)")
    finally:
        conn.close()


def delete_custom_prompt(username: str, fund_code: str) -> None:
    """删除指定用户对指数的定制提示词，恢复使用默认逻辑"""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM fund_custom_prompts WHERE username = ? AND fund_code = ?",
            (username, fund_code)
        )
        conn.commit()
        logger.info(f"delete_custom_prompt: [{username}] {fund_code} prompt 已删除")
    finally:
        conn.close()
