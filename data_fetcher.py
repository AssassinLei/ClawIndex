import tushare as ts
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
import time
import os
from database import save_daily_data, get_connection, get_latest_trade_date, save_industry_list
from logger import setup_logger

logger = setup_logger("data_fetcher")

TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
if not TUSHARE_TOKEN:
    raise RuntimeError("未设置 TUSHARE_TOKEN 环境变量，请检查 .env 文件")

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

# 交易日历缓存（避免重复查询）
_trade_cal_cache = {}

def is_trade_day(date_str: str) -> tuple[bool, str]:
    """
    判断指定日期是否为交易日。
    参数: date_str - 格式 YYYYMMDD 或 YYYY-MM-DD
    返回: (is_trade_day: bool, prev_trade_day: str 上一个交易日)
    """
    # 统一格式
    clean_date = date_str.replace('-', '')
    
    # 检查缓存
    if clean_date in _trade_cal_cache:
        return _trade_cal_cache[clean_date]
    
    try:
        df = pro.trade_cal(exchange='SSE', start_date=clean_date, end_date=clean_date)
        if df.empty:
            # 接口返回空，回退到周末判断
            dt = datetime.strptime(clean_date, '%Y%m%d')
            is_weekend = dt.weekday() >= 5
            result = (not is_weekend, '')
            _trade_cal_cache[clean_date] = result
            return result
        
        row = df.iloc[0]
        is_open = str(row.get('is_open', '1')) == '1'
        prev_day = str(row.get('pretrade_date', ''))
        result = (is_open, prev_day)
        _trade_cal_cache[clean_date] = result
        return result
    except Exception as e:
        logger.warning(f"trade_cal 查询失败: {e}")
        # 回退到周末判断
        dt = datetime.strptime(clean_date, '%Y%m%d')
        is_weekend = dt.weekday() >= 5
        return (not is_weekend, '')

def fetch_industry_classify() -> tuple[bool, str]:
    """
    拉取申万行业分类数据（L1/L2/L3）并存入数据库。
    用于应用初始化时同步行业列表。
    返回: (success: bool, message: str)
    """
    total_count = 0
    errors = []
    
    for level in ['L1', 'L2', 'L3']:
        try:
            logger.info(f"index_classify {level}: 拉取...")
            df = pro.index_classify(level=level, src='SW2021')
            logger.info(f"index_classify {level}: 返回 {len(df)} 行")
            if not df.empty:
                logger.debug(f"index_classify {level} 前3行:\n{df.head(3)}")
            if df.empty:
                errors.append(f"{level}: 返回空数据")
                continue
            
            save_industry_list(df)
            total_count += len(df)
            logger.info(f"index_classify: 成功同步 {level} 行业 {len(df)} 条")
            
            # 防限流
            time.sleep(0.5)
        except Exception as e:
            error_msg = f"{level}: {type(e).__name__}: {str(e)}"
            logger.error(f"index_classify: {error_msg}")
            errors.append(error_msg)
    
    if errors:
        return False, f"部分同步失败: {'; '.join(errors)}"
    
    return True, f"成功同步 {total_count} 条行业分类数据"

# 中国10年期国债收益率默认值（接口异常时使用）
DEFAULT_RISK_FREE_RATE = 0.0172  # 1.72%

def fetch_risk_free_rate(start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    """
    获取中国10年期国债收益率作为无风险利率。
    使用 akshare bond_china_yield 接口。
    返回值为百分比形式（如 2.35），转换为小数形式（如 0.0235）。
    返回: (DataFrame, error_message)
    """
    try:
        # 若截止日为非交易日，自动调整为最近交易日，确保获取最新可用收益率
        query_end = end_date
        is_open, prev_trade_day = is_trade_day(end_date)
        if not is_open and prev_trade_day:
            query_end = prev_trade_day
            logger.info(f"bond_china_yield: 截止日 {end_date} 为非交易日，调整至 {query_end}")
        
        df = ak.bond_china_yield(start_date=start_date, end_date=query_end)
        logger.info(f"bond_china_yield: 返回 {len(df)} 行")
        
        if df.empty:
            return pd.DataFrame(), "bond_china_yield 接口返回空数据"
        
        # 筛选中债国债收益率曲线
        df_cn = df[df["曲线名称"] == "中债国债收益率曲线"].copy()
        if df_cn.empty:
            return pd.DataFrame(), "未找到中债国债收益率曲线数据"
        
        # 提取需要的列
        try:
            rate_series = df_cn['10年'].astype(float) / 100  # 百分比转小数
        except KeyError:
            # 兼容 akshare 列名变更，查找包含 '10' 和 '年' 的备选列
            logger.warning(f"bond_china_yield: 未找到 '10年' 列，可用列: {list(df_cn.columns)}")
            alt_cols = [c for c in df_cn.columns if '10' in c and '年' in c]
            if alt_cols:
                logger.info(f"bond_china_yield: 使用备选列 '{alt_cols[0]}'")
                rate_series = df_cn[alt_cols[0]].astype(float) / 100
            else:
                return pd.DataFrame(), f"未找到10年期国债收益率列，可用列: {list(df_cn.columns)}"

        result = pd.DataFrame({
            'trade_date': pd.to_datetime(df_cn['日期']).dt.strftime('%Y-%m-%d'),
            'risk_free_rate': rate_series
        })
        
        logger.info(f"bond_china_yield: 提取10年期国债 {len(result)} 行")
        
        # 检查最新数据日期是否落后于请求范围，结合交易日历给出说明
        if not result.empty:
            max_date = result['trade_date'].max()
            if end_date and max_date < end_date:
                is_open, _ = is_trade_day(end_date)
                if not is_open:
                    logger.info(f"bond_china_yield: 最新数据 {max_date}, 请求截止日 {end_date} 为非交易日")
                else:
                    logger.info(f"bond_china_yield: 最新数据 {max_date}, 请求截止日 {end_date} 数据尚未发布")
        
        return result, ""
        
    except Exception as e:
        error_msg = f"bond_china_yield 接口调用失败: {type(e).__name__}: {str(e)}"
        logger.warning(f"{error_msg}，将使用默认值 {DEFAULT_RISK_FREE_RATE}")
        return pd.DataFrame(), error_msg

def fetch_history_data(fund_code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    """
    获取指定区间的历史日线数据（含行情与估值）。
    - sw_daily：一次性获取 close、pe、pb
    - akshare：获取中国10年期国债收益率作为无风险利率
    返回: (DataFrame, error_message)
    """
    try:
        # 1. 通过 sw_daily 获取行情 + 估值数据
        logger.info(f"sw_daily 请求: ts_code={fund_code}, start={start_date}, end={end_date}")
        df = pro.sw_daily(
            ts_code=fund_code,
            start_date=start_date,
            end_date=end_date,
            fields='ts_code,trade_date,close,pe,pb'
        )
        
        logger.info(f"sw_daily 返回: {len(df)} 行, 列={list(df.columns)}")
        if not df.empty:
            logger.debug(f"sw_daily 前3行:\n{df.head(3)}")

        if df.empty:
            return pd.DataFrame(), (
                f"sw_daily 接口返回空数据\n"
                f"请求参数: ts_code={fund_code}, start_date={start_date}, end_date={end_date}\n"
                f"可能原因: 1) 标的代码不正确 2) Tushare账户无sw_daily接口权限 3) 该标的无行情数据"
            )

        # 2. 获取无风险利率并合并
        df_rf, rf_error = fetch_risk_free_rate(start_date, end_date)
        if not df_rf.empty:
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
            df = pd.merge(df, df_rf, on='trade_date', how='left')
        else:
            df['risk_free_rate'] = DEFAULT_RISK_FREE_RATE  # 兜底默认值
            if rf_error:
                logger.warning(f"无风险利率获取失败，使用默认值 {DEFAULT_RISK_FREE_RATE}: {rf_error}")

        # 3. 数据清洗
        df.rename(columns={'ts_code': 'fund_code', 'close': 'close_price'}, inplace=True)
        df.drop_duplicates(subset=['fund_code', 'trade_date'], inplace=True)

        logger.info(f"fetch_history_data: 最终 {len(df)} 行, 日期 {df['trade_date'].min()}~{df['trade_date'].max()}")
        return df, ""

    except Exception as e:
        error_msg = f"sw_daily 接口调用失败: {type(e).__name__}: {str(e)}"
        logger.error(error_msg)
        return pd.DataFrame(), error_msg

def sync_all_history(fund_code: str) -> tuple[bool, str]:
    """
    初始化函数：当在前端添加新基金时，调用此函数拉取过去 10 年的数据落库。
    返回: (success: bool, message: str)
    """
    end = datetime.now()
    start = end - timedelta(days=365 * 10) # 10年
    start_str = start.strftime('%Y%m%d')
    end_str = end.strftime('%Y%m%d')
    
    logger.info(f"sync_all_history: 开始同步 {fund_code}, {start_str}~{end_str}")
    
    df, error = fetch_history_data(fund_code, start_str, end_str)
    
    if error:
        logger.error(f"sync_all_history: 失败 - {error}")
        return False, error
    
    if df.empty:
        logger.error(f"sync_all_history: 失败 - 未获取到任何数据")
        return False, f"标的 {fund_code} 未获取到任何数据"
    
    save_daily_data(df)
    msg = f"成功同步 {fund_code} 共 {len(df)} 条历史数据（{df['trade_date'].min()} ~ {df['trade_date'].max()}）"
    logger.info(f"sync_all_history: {msg}")
    
    # 必须休眠防止 Tushare 限流封号
    time.sleep(2)
    return True, msg

def sync_incremental(fund_code: str) -> tuple[bool, str]:
    """
    增量同步：从数据库最新日期到今天的行情数据。
    用于巡检时确保数据最新。
    返回: (success: bool, message: str)
    """
    now = datetime.now()
    today = now.strftime('%Y%m%d')
    weekday = now.weekday()  # 0=周一, 5=周六, 6=周日
    weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    
    latest_date = get_latest_trade_date(fund_code)
    
    logger.info(f"sync_incremental: {fund_code} 今天={today}({weekday_names[weekday]}), DB最新={latest_date}")
    
    if latest_date is None:
        return False, (
            f"数据库中无 {fund_code} 的历史数据\n"
            f"可能原因: 添加标的时数据同步失败\n"
            f"建议: 删除该标的后重新添加"
        )
    
    # 统一日期格式为 YYYYMMDD 进行比较
    latest_date_clean = latest_date.replace('-', '').replace('/', '')
    if latest_date_clean >= today:
        return True, f"数据已是最新（最新日期: {latest_date}）"
    
    # 使用交易日历判断今天是否可交易
    is_open, prev_trade_day = is_trade_day(today)
    if not is_open:
        reason = f"今天({today})非交易日" if weekday < 5 else f"今天是{weekday_names[weekday]}"
        last_date = prev_trade_day if prev_trade_day else latest_date
        return True, f"{reason}，股市休市，数据已是最新（最新日期: {last_date}）"
    
    # 从最新日期的下一天开始拉取
    start = (datetime.strptime(latest_date_clean, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
    logger.info(f"sync_incremental: {fund_code} 增量拉取 {start}~{today}")
    
    df, error = fetch_history_data(fund_code, start, today)
    
    if error:
        # 如果是空数据且不是接口异常，说明今天还没有新行情（如节假日）
        if '接口返回空数据' in error:
            return True, f"无新数据（最新日期: {latest_date}，今日行情可能尚未发布）"
        return False, f"增量同步失败: {error}"
    
    if df.empty:
        return True, f"无新数据（最新日期: {latest_date}）"
    
    save_daily_data(df)
    msg = f"增量同步成功，新增 {len(df)} 条数据（{df['trade_date'].min()} ~ {df['trade_date'].max()}）"
    logger.info(f"sync_incremental: {msg}")
    
    # 防限流
    time.sleep(1)
    return True, msg