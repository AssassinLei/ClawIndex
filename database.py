import sqlite3
import pandas as pd
import os
from typing import List, Dict
from logger import setup_logger

logger = setup_logger("database")

DB_NAME = "quant_system.db"


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


def get_connection():
    # 确保返回的行是字典格式，方便读取字段
    conn = sqlite3.connect(DB_NAME)
    # 启用 WAL 模式：读写可并发，提升巡检写入时的查询性能
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库表结构"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # 表1：基金池
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fund_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fund_code TEXT UNIQUE NOT NULL,
                fund_name TEXT NOT NULL,
                category TEXT NOT NULL, -- wide_base, tech_growth, cycle_mfg, dividend
                added_date DATE DEFAULT (date('now', 'localtime'))
            )
        ''')
        
        # 表2：每日基础行情缓存
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_market_data (
                fund_code TEXT NOT NULL,
                trade_date DATE NOT NULL,
                close_price REAL,
                pe REAL,
                pb REAL,
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
        
        # 表4：巡检结果记录
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS inspection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fund_code TEXT NOT NULL,
                fund_name TEXT NOT NULL,
                action TEXT NOT NULL,
                ai_report TEXT,
                inspect_date DATE DEFAULT (date('now', 'localtime')),
                UNIQUE(fund_code, inspect_date)
            )
        ''')
        
        # 兼容旧表：如果已有 inspection_log 但缺少 ai_report 列，则追加
        try:
            cursor.execute("ALTER TABLE inspection_log ADD COLUMN ai_report TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略
        
        # 表5：Webhook 地址配置
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS webhook_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        
        conn.commit()
    finally:
        conn.close()
    logger.info("数据库初始化完成")

# --- 基金池管理操作 ---
def add_fund(fund_code: str, fund_name: str, category: str) -> bool:
    """添加基金到监控池，返回 True 表示新增成功，False 表示已存在"""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO fund_pool (fund_code, fund_name, category) VALUES (?, ?, ?)",
            (fund_code, fund_name, category)
        )
        conn.commit()
        logger.info(f"add_fund: {fund_code} {fund_name} (category={category})")
        return True
    except sqlite3.IntegrityError:
        logger.warning(f"add_fund: {fund_code} already exists")
        return False
    finally:
        conn.close()

def remove_fund(fund_code: str):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM fund_pool WHERE fund_code = ?", (fund_code,))
        conn.execute("DELETE FROM daily_market_data WHERE fund_code = ?", (fund_code,))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"remove_fund: {fund_code}，已清理行情数据")

def get_all_funds() -> List[Dict]:
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT * FROM fund_pool")
        return [dict(row) for row in cursor.fetchall()]
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
                str(row.get('trade_date')),
                safe_float(row.get('close_price')),
                safe_float(row.get('pe')),
                safe_float(row.get('pb')),
                safe_float(row.get('risk_free_rate')),
            ))

        cursor.executemany("""
            INSERT OR REPLACE INTO daily_market_data
                (fund_code, trade_date, close_price, pe, pb, risk_free_rate)
            VALUES (?, ?, ?, ?, ?, ?)
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
        cursor.execute(
            "SELECT MAX(trade_date) FROM daily_market_data WHERE fund_code = ?",
            (fund_code,)
        )
        result = cursor.fetchone()[0]
        return str(result) if result is not None else None  # 返回 None 表示无数据
    finally:
        conn.close()

def get_recent_prices(fund_code: str, days: int = 150) -> pd.DataFrame:
    """获取最近N天的价格，用于计算移动平均线(SMA)等技术指标"""
    conn = get_connection()
    try:
        query = """
            SELECT trade_date, close_price 
            FROM daily_market_data 
            WHERE fund_code = ? 
            ORDER BY trade_date DESC LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=(fund_code, days))
    finally:
        conn.close()
    # 确保 close_price 为数值类型（兼容历史 TEXT 数据）
    df['close_price'] = pd.to_numeric(df['close_price'], errors='coerce')
    # 确保 trade_date 为字符串类型，避免 pandas 混合类型导致 sort_values 报错
    df['trade_date'] = df['trade_date'].astype(str)
    # 返回正序排序的数据，方便计算指标
    return df.sort_values(by='trade_date').reset_index(drop=True)

def calculate_percentile(fund_code: str, indicator: str, current_value: float, lookback_days: int = 2500) -> float:
    """
    利用 SQL 计算指定指标（如 PE, PB）的历史分位数。
    lookback_days 默认 2500，约等于10个交易年。
    """
    if current_value is None or indicator not in ['pe', 'pb']:
        return None
        
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # 1. 检查历史总条数（限定在 lookback_days 窗口内，与分位计算保持一致）
        cursor.execute(f"SELECT COUNT(*) FROM (SELECT {indicator} FROM daily_market_data WHERE fund_code = ? AND {indicator} IS NOT NULL ORDER BY trade_date DESC LIMIT ?)", (fund_code, lookback_days))
        total_count = cursor.fetchone()[0]
        
        if total_count < 100:
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

def save_inspection_result(fund_code: str, fund_name: str, action: str, ai_report: str = ""):
    """保存巡检结果（同一标的同一天覆盖更新）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO inspection_log (fund_code, fund_name, action, ai_report, inspect_date)
            VALUES (?, ?, ?, ?, date('now', 'localtime'))
        """, (fund_code, fund_name, action, ai_report))
        conn.commit()
        logger.info(f"save_inspection_result: {fund_code} {fund_name} action={action}")
    finally:
        conn.close()

def get_inspection_history(fund_code: str = None, action: str = None,
                           start_date: str = None, end_date: str = None) -> List[Dict]:
    """查询巡检历史记录，支持多条件筛选，按日期倒序。"""
    conn = get_connection()
    try:
        query = "SELECT * FROM inspection_log WHERE 1=1"
        params = []
        
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
        logger.info(f"get_inspection_history: 返回 {len(records)} 条记录")
        return records
    finally:
        conn.close()

def get_market_data_for_date(fund_code: str, trade_date: str) -> Dict:
    """查询指定标的在某日的行情数据（close_price, pe, pb, risk_free_rate）"""
    conn = get_connection()
    try:
        cursor = conn.execute("""
            SELECT close_price, pe, pb, risk_free_rate
            FROM daily_market_data
            WHERE fund_code = ? AND trade_date = ?
        """, (fund_code, trade_date))
        row = cursor.fetchone()
        if not row:
            return {}
        # 统一走安全转换，兼容历史 TEXT / 非数值 / NaN 等情况
        return {
            "close_price": safe_float(row["close_price"]),
            "pe": safe_float(row["pe"]),
            "pb": safe_float(row["pb"]),
            "risk_free_rate": safe_float(row["risk_free_rate"]),
        }
    finally:
        conn.close()

# --- Webhook 配置管理 ---
def get_webhook_urls() -> List[Dict]:
    """获取所有已保存的 webhook 地址"""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT * FROM webhook_urls ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

def add_webhook_url(url: str, label: str = "") -> bool:
    """添加一条 webhook 地址，返回 True 表示新增成功，False 表示已存在"""
    url = url.strip()
    label = label.strip()
    conn = get_connection()
    try:
        # 检查是否已存在相同 URL
        existing = conn.execute("SELECT id FROM webhook_urls WHERE url = ?", (url,)).fetchone()
        if existing:
            logger.warning(f"add_webhook_url: URL 已存在, id={existing['id']}")
            return False
        conn.execute(
            "INSERT INTO webhook_urls (url, label) VALUES (?, ?)",
            (url, label)
        )
        conn.commit()
        logger.info(f"add_webhook_url: {url[:50]}...")
        return True
    finally:
        conn.close()

def remove_webhook_url(webhook_id: int):
    """删除指定 webhook 地址"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM webhook_urls WHERE id = ?", (webhook_id,))
        conn.commit()
        logger.info(f"remove_webhook_url: id={webhook_id}")
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
