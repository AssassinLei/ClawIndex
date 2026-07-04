import sqlite3
import pandas as pd
import os
from typing import List, Dict, Any

DB_NAME = "quant_system.db"

def get_connection():
    # 确保返回的行是字典格式，方便读取字段
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库表结构"""
    conn = get_connection()
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
    
    conn.commit()
    conn.close()

# --- 基金池管理操作 ---
def add_fund(fund_code: str, fund_name: str, category: str):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO fund_pool (fund_code, fund_name, category) VALUES (?, ?, ?)",
            (fund_code, fund_name, category)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        print(f"Fund {fund_code} already exists.")
    finally:
        conn.close()

def remove_fund(fund_code: str):
    conn = get_connection()
    conn.execute("DELETE FROM fund_pool WHERE fund_code = ?", (fund_code,))
    conn.execute("DELETE FROM daily_market_data WHERE fund_code = ?", (fund_code,))
    conn.commit()
    conn.close()

def get_all_funds() -> List[Dict]:
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM fund_pool")
    funds = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return funds

# --- 行情数据写入操作 ---
def save_daily_data(df: pd.DataFrame):
    """将获取到的 Pandas DataFrame 数据批量存入 SQLite，遇重复主键则覆盖"""
    if df.empty:
        return
    conn = get_connection()
    cursor = conn.cursor()

    rows = []
    for _, row in df.iterrows():
        rows.append((
            row.get('fund_code'),
            str(row.get('trade_date')),
            row.get('close_price'),
            row.get('pe'),
            row.get('pb'),
            row.get('risk_free_rate'),
        ))

    cursor.executemany("""
        INSERT OR REPLACE INTO daily_market_data
            (fund_code, trade_date, close_price, pe, pb, risk_free_rate)
        VALUES (?, ?, ?, ?, ?, ?)
    """, rows)

    conn.commit()
    conn.close()

# --- 核心：策略查询依赖 ---
def get_latest_trade_date(fund_code: str) -> str:
    """获取指定标的在数据库中的最新交易日期，用于增量同步判断"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT MAX(trade_date) FROM daily_market_data WHERE fund_code = ?",
        (fund_code,)
    )
    result = cursor.fetchone()[0]
    conn.close()
    return result  # 返回 None 表示无数据

def get_recent_prices(fund_code: str, days: int = 150) -> pd.DataFrame:
    """获取最近N天的价格，用于计算移动平均线(SMA)等技术指标"""
    conn = get_connection()
    query = """
        SELECT trade_date, close_price 
        FROM daily_market_data 
        WHERE fund_code = ? 
        ORDER BY trade_date DESC LIMIT ?
    """
    df = pd.read_sql_query(query, conn, params=(fund_code, days))
    conn.close()
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
    cursor = conn.cursor()
    
    # 1. 检查历史总条数
    cursor.execute(f"SELECT COUNT(*) FROM daily_market_data WHERE fund_code = ? AND {indicator} IS NOT NULL LIMIT ?", (fund_code, lookback_days))
    total_count = cursor.fetchone()[0]
    
    if total_count < 100:
        conn.close()
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
    conn.close()
    
    # 计算百分比并保留四位小数 (例如 0.1542 代表 15.42%)
    return round(lower_count / total_count, 4)

# --- 行业分类管理 ---
def save_industry_list(df: pd.DataFrame):
    """批量保存行业分类数据，遇重复主键则覆盖"""
    if df.empty:
        return
    conn = get_connection()
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
    conn.close()

def get_all_industries() -> List[Dict]:
    """获取所有行业分类数据"""
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM industry_list ORDER BY level, industry_name")
    industries = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return industries

def get_industry_count() -> int:
    """获取行业分类总数"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM industry_list")
    count = cursor.fetchone()[0]
    conn.close()
    return count