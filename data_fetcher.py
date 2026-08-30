import tushare as ts
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
import time
import os
from database import (
    save_daily_data, get_connection, get_latest_trade_date, save_industry_list,
    save_idx_factor_data, get_latest_factor_trade_date, IDX_FACTOR_COLUMNS,
)
from constants import GLOBAL_INDEX_MAP
from logger import setup_logger

logger = setup_logger("data_fetcher")

TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")

# idx_factor_pro 接口请求字段（与 idx_factor_data 表列一一对应）
IDX_FACTOR_FIELDS = 'ts_code,trade_date,' + ','.join(IDX_FACTOR_COLUMNS)

_pro = None

def _get_pro():
    """延迟初始化 tushare pro_api，避免 import 阶段因缺少 TUSHARE_TOKEN 崩溃"""
    global _pro
    if _pro is None:
        token = os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            raise RuntimeError("未设置 TUSHARE_TOKEN 环境变量，请检查 .env 文件")
        ts.set_token(token)
        _pro = ts.pro_api()
    return _pro

def is_global_code(fund_code: str) -> bool:
    """判断 fund_code 是否为国际指数（index_global 接口标的）。

    市场类型识别唯一依据是静态代码表 GLOBAL_INDEX_MAP：
    国际指数无 PE/PB 估值、无技术因子、SSE 交易日历不适用，
    数据抓取/增量同步/因子同步均据此分支。
    """
    return fund_code in GLOBAL_INDEX_MAP

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
        df = _get_pro().trade_cal(exchange='SSE', start_date=clean_date, end_date=clean_date)
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
            df = _get_pro().index_classify(level=level, src='SW2021')
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
    - sw_daily：一次性获取全部行情字段（open、high、low、close、change、pct_change、vol、amount、pe、pb、float_mv、total_mv）
    - akshare：获取中国10年期国债收益率作为无风险利率
    返回: (DataFrame, error_message)
    """
    try:
        # 1. 通过 sw_daily 获取行情 + 估值数据
        logger.info(f"sw_daily 请求: ts_code={fund_code}, start={start_date}, end={end_date}")
        df = _get_pro().sw_daily(
            ts_code=fund_code,
            start_date=start_date,
            end_date=end_date,
            fields='ts_code,trade_date,name,open,high,low,close,change,pct_change,vol,amount,pe,pb,float_mv,total_mv'
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
        df.rename(columns={
            'ts_code': 'fund_code',
            'close': 'close_price',
            'open': 'open_price',
            'high': 'high_price',
            'low': 'low_price',
            'vol': 'volume',
        }, inplace=True)
        df.drop_duplicates(subset=['fund_code', 'trade_date'], inplace=True)

        logger.info(f"fetch_history_data: 最终 {len(df)} 行, 日期 {df['trade_date'].min()}~{df['trade_date'].max()}")
        return df, ""

    except Exception as e:
        error_msg = f"sw_daily 接口调用失败: {type(e).__name__}: {str(e)}"
        logger.error(error_msg)
        return pd.DataFrame(), error_msg

def fetch_global_history_data(fund_code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    """
    获取指定区间的国际指数日线行情（index_global 接口，需 6000 积分）。
    - 无 PE/PB 估值、无市值、无名称字段：名称取静态代码表，估值列不填
    - 无无风险利率（无 PE 无法计算风险溢价）
    返回: (DataFrame, error_message)
    """
    try:
        logger.info(f"index_global 请求: ts_code={fund_code}, start={start_date}, end={end_date}")
        df = _get_pro().index_global(
            ts_code=fund_code,
            start_date=start_date,
            end_date=end_date,
            fields='ts_code,trade_date,open,close,high,low,pre_close,change,pct_chg,swing,vol,amount'
        )

        logger.info(f"index_global 返回: {len(df)} 行")
        if not df.empty:
            logger.debug(f"index_global 前3行:\n{df.head(3)}")

        if df.empty:
            return pd.DataFrame(), (
                f"index_global 接口返回空数据\n"
                f"请求参数: ts_code={fund_code}, start_date={start_date}, end_date={end_date}\n"
                f"可能原因: 1) 标的代码不正确 2) Tushare账户无index_global接口权限（需6000积分） 3) 该区间无行情数据"
            )

        # 列重命名对齐 daily_market_data 表（swing/pre_close 表无对应列，不落库）
        df.rename(columns={
            'ts_code': 'fund_code',
            'close': 'close_price',
            'open': 'open_price',
            'high': 'high_price',
            'low': 'low_price',
            'vol': 'volume',
            'pct_chg': 'pct_change',
        }, inplace=True)
        # 接口不返回名称，从静态代码表填充
        df['name'] = GLOBAL_INDEX_MAP.get(fund_code, fund_code)
        df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
        df.drop_duplicates(subset=['fund_code', 'trade_date'], inplace=True)

        logger.info(f"fetch_global_history_data: 最终 {len(df)} 行, 日期 {df['trade_date'].min()}~{df['trade_date'].max()}")
        return df, ""

    except Exception as e:
        error_msg = f"index_global 接口调用失败: {type(e).__name__}: {str(e)}"
        logger.error(error_msg)
        return pd.DataFrame(), error_msg

def fetch_idx_factor(fund_code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    """
    获取指定区间的指数技术面因子数据（idx_factor_pro 专业版）。
    输出 MACD/KDJ/RSI/BOLL/CCI/DMI 等 78 项技术因子 + 行情 9 项。
    返回: (DataFrame, error_message)
    """
    try:
        logger.info(f"idx_factor_pro 请求: ts_code={fund_code}, start={start_date}, end={end_date}")
        df = _get_pro().idx_factor_pro(
            ts_code=fund_code,
            start_date=start_date,
            end_date=end_date,
            fields=IDX_FACTOR_FIELDS
        )

        logger.info(f"idx_factor_pro 返回: {len(df)} 行")

        if df.empty:
            return pd.DataFrame(), (
                f"idx_factor_pro 接口返回空数据\n"
                f"请求参数: ts_code={fund_code}, start_date={start_date}, end_date={end_date}\n"
                f"可能原因: 1) 标的代码不正确 2) Tushare账户无idx_factor_pro接口权限（需5000积分） 3) 该区间无因子数据"
            )

        df.rename(columns={'ts_code': 'fund_code'}, inplace=True)
        df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
        df.drop_duplicates(subset=['fund_code', 'trade_date'], inplace=True)

        logger.info(f"fetch_idx_factor: 最终 {len(df)} 行, 日期 {df['trade_date'].min()}~{df['trade_date'].max()}")
        return df, ""

    except Exception as e:
        error_msg = f"idx_factor_pro 接口调用失败: {type(e).__name__}: {str(e)}"
        logger.error(error_msg)
        return pd.DataFrame(), error_msg

def fetch_index_members(index_code: str, level: str) -> tuple[pd.DataFrame, str]:
    """
    实时获取申万行业指数的成分股列表（index_member_all 接口）。
    按行业层级传参：L1 → l1_code，L2 → l2_code，L3 → l3_code。
    成分数据变化频繁，纯实时查询，不写入数据库。
    返回: (DataFrame, error_message)
    """
    level_param = {'L1': 'l1_code', 'L2': 'l2_code', 'L3': 'l3_code'}.get(level)
    if level_param is None:
        return pd.DataFrame(), f"未知的行业层级: {level}（仅支持 L1/L2/L3）"

    try:
        logger.info(f"index_member_all 请求: {level_param}={index_code}")
        df = _get_pro().index_member_all(
            **{level_param: index_code},
            is_new='Y',
            fields='l1_name,l2_name,l3_name,ts_code,name,in_date'
        )

        logger.info(f"index_member_all 返回: {len(df)} 行")

        if df.empty:
            return pd.DataFrame(), (
                f"index_member_all 接口返回空数据\n"
                f"请求参数: {level_param}={index_code}, is_new=Y\n"
                f"可能原因: 1) 行业代码不正确 2) Tushare账户无index_member_all接口权限（需2000积分）"
            )

        return df, ""

    except Exception as e:
        error_msg = f"index_member_all 接口调用失败: {type(e).__name__}: {str(e)}"
        logger.error(error_msg)
        return pd.DataFrame(), error_msg

def sync_all_history(fund_code: str) -> tuple[bool, str]:
    """
    初始化函数：当在前端添加新基金时，调用此函数拉取过去 10 年的数据落库。
    按市场类型分支：国际指数走 index_global，申万行业指数走 sw_daily。
    返回: (success: bool, message: str)
    """
    end = datetime.now()
    start = end - timedelta(days=365 * 10) # 10年
    start_str = start.strftime('%Y%m%d')
    end_str = end.strftime('%Y%m%d')

    logger.info(f"sync_all_history: 开始同步 {fund_code}, {start_str}~{end_str}")

    if is_global_code(fund_code):
        df, error = fetch_global_history_data(fund_code, start_str, end_str)
    else:
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

    if is_global_code(fund_code):
        # 国际指数无 idx_factor_pro 技术因子数据，跳过
        logger.info(f"sync_all_history: {fund_code} 为国际指数，无技术因子数据，跳过因子同步")
    else:
        # 同步技术因子（失败不阻断主流程，巡检时会自动重试补齐）
        df_factor, factor_error = fetch_idx_factor(fund_code, start_str, end_str)
        if factor_error:
            logger.warning(f"sync_all_history: 技术因子同步失败（不影响行情数据）- {factor_error}")
            msg += "；技术因子同步失败，将在巡检时自动重试"
        else:
            save_idx_factor_data(df_factor)
            msg += f"；技术因子 {len(df_factor)} 条"
            logger.info(f"sync_all_history: {fund_code} 技术因子同步 {len(df_factor)} 条")

    # 必须休眠防止 Tushare 限流封号
    time.sleep(2)
    return True, msg

def _sync_factor_incremental(fund_code: str, today: str):
    """
    技术因子增量补齐（独立于行情同步判断缺口）。
    失败仅记录日志，不影响巡检主流程。
    国际指数无 idx_factor_pro 数据，直接跳过。
    """
    if is_global_code(fund_code):
        return

    factor_latest = get_latest_factor_trade_date(fund_code)
    
    if factor_latest is None:
        # 添加标的时因子同步失败（如无权限），重试全量拉取近10年
        start = (datetime.now() - timedelta(days=365 * 10)).strftime('%Y%m%d')
    else:
        factor_latest_clean = factor_latest.replace('-', '').replace('/', '')
        if factor_latest_clean >= today:
            return  # 因子数据已是最新
        start = (datetime.strptime(factor_latest_clean, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
    
    logger.info(f"_sync_factor_incremental: {fund_code} 因子增量拉取 {start}~{today}")
    df, error = fetch_idx_factor(fund_code, start, today)
    
    if error:
        if '接口返回空数据' in error:
            logger.info(f"_sync_factor_incremental: {fund_code} 无新因子数据（今日可能尚未发布）")
        else:
            logger.warning(f"_sync_factor_incremental: {fund_code} 因子同步失败（不影响巡检）- {error}")
        return
    
    if df.empty:
        return
    
    save_idx_factor_data(df)
    logger.info(f"_sync_factor_incremental: {fund_code} 新增 {len(df)} 条因子数据")

def sync_incremental(fund_code: str) -> tuple[bool, str]:
    """
    增量同步：行情数据 + 技术因子。
    行情同步成功后，独立判断因子表缺口并补齐（因子失败不影响返回结果）。
    返回: (success: bool, message: str)
    """
    success, msg = _sync_market_incremental(fund_code)
    if success:
        _sync_factor_incremental(fund_code, datetime.now().strftime('%Y%m%d'))
    return success, msg

def _sync_market_incremental(fund_code: str) -> tuple[bool, str]:
    """
    行情增量同步：从数据库最新日期到今天的行情数据。
    用于巡检时确保数据最新。
    返回: (success: bool, message: str)
    """
    now = datetime.now()
    today = now.strftime('%Y%m%d')
    weekday = now.weekday()  # 0=周一, 5=周六, 6=周日
    weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    
    latest_date = get_latest_trade_date(fund_code)
    
    logger.info(f"_sync_market_incremental: {fund_code} 今天={today}({weekday_names[weekday]}), DB最新={latest_date}")
    
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
    
    # 使用交易日历判断今天是否可交易（国际指数不适用 SSE 日历，直接尝试拉取，
    # 接口返回空即视为今日行情尚未发布，由下方空数据分支容错）
    if not is_global_code(fund_code):
        is_open, prev_trade_day = is_trade_day(today)
        if not is_open:
            reason = f"今天({today})非交易日" if weekday < 5 else f"今天是{weekday_names[weekday]}"
            last_date = prev_trade_day if prev_trade_day else latest_date
            return True, f"{reason}，股市休市，数据已是最新（最新日期: {last_date}）"
    
    # 从最新日期的下一天开始拉取
    start = (datetime.strptime(latest_date_clean, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
    logger.info(f"_sync_market_incremental: {fund_code} 增量拉取 {start}~{today}")
    
    df, error = fetch_global_history_data(fund_code, start, today) if is_global_code(fund_code) else fetch_history_data(fund_code, start, today)

    if error:
        # 如果是空数据且不是接口异常，说明今天还没有新行情（如节假日）
        if '接口返回空数据' in error:
            return True, f"无新数据（最新日期: {latest_date}，今日行情可能尚未发布）"
        return False, f"增量同步失败: {error}"
    
    if df.empty:
        return True, f"无新数据（最新日期: {latest_date}）"
    
    save_daily_data(df)
    msg = f"增量同步成功，新增 {len(df)} 条数据（{df['trade_date'].min()} ~ {df['trade_date'].max()}）"
    logger.info(f"_sync_market_incremental: {msg}")
    
    # 防限流
    time.sleep(1)
    return True, msg