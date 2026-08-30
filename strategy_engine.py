import pandas as pd
from database import get_recent_prices, calculate_percentile, get_latest_market_data, get_latest_idx_factor, IDX_FACTOR_TECH_COLUMNS
from logger import setup_logger

logger = setup_logger("strategy_engine")

def calculate_technical_indicators(df_prices: pd.DataFrame) -> dict:
    """基于近期价格与成交额，计算技术面指标"""
    if df_prices.empty or len(df_prices) < 120:
        return {"current_price": None, "ma60": None, "ma120": None, "amount_ma20": None}
    
    # 计算移动平均线
    df_prices['ma60'] = df_prices['close_price'].rolling(window=60).mean()
    df_prices['ma120'] = df_prices['close_price'].rolling(window=120).mean()
    df_prices['amount_ma20'] = df_prices['amount'].rolling(window=20).mean()
    
    latest_row = df_prices.iloc[-1]
    
    return {
        "current_price": latest_row['close_price'],
        "ma60": latest_row['ma60'],
        "ma120": latest_row['ma120'],
        "amount_ma20": latest_row['amount_ma20'],
    }

def generate_fund_report(fund_code: str, category: str) -> dict:
    """主控函数：拉取数据 -> 计算指标 -> 硬编码判定信号"""
    latest_data = get_latest_market_data(fund_code)
    
    if not latest_data:
        logger.warning(f"generate_fund_report: {fund_code} 无行情数据")
        return {"error": f"数据库中未找到 {fund_code} 的行情数据，请检查：1) 标的是否已添加 2) 历史数据是否同步成功 3) 标的代码格式是否正确（如 000300.SH）"}
        
    # pe/pb/risk_free_rate 已在 get_latest_market_data 中通过 safe_float 转换
    # close_price 在 get_recent_prices 中通过 pd.to_numeric 统一转数值
    pe = latest_data['pe']
    pb = latest_data['pb']
    risk_free = latest_data['risk_free_rate']
    
    # 2. 计算基本面的推导指标和历史分位
    pe_percentile = calculate_percentile(fund_code, 'pe', pe) if pe is not None else None
    pb_percentile = calculate_percentile(fund_code, 'pb', pb) if pb is not None else None
    
    # 推导风险溢价 (Fed模型: 1/PE - 无风险利率)
    risk_premium = (1 / pe - risk_free) if pe is not None and pe > 0 and risk_free is not None else None
    
    # 推导 ROE = PB / PE (需避免除零)
    roe = (pb / pe) if pe is not None and pe > 0 and pb is not None else None

    # 3. 计算技术面指标
    df_prices = get_recent_prices(fund_code, days=150)
    tech_inds = calculate_technical_indicators(df_prices)
    current_price = tech_inds['current_price']
    ma60 = tech_inds['ma60']
    ma120 = tech_inds['ma120']
    amount_ma20 = tech_inds['amount_ma20']

    # 4. 提取当日成交额
    amount = latest_data.get('amount')

    # 国际指数无估值数据：以价格历史分位替代 PE/PB 分位，当日涨跌幅补充动量信息
    # （申万行业指数这两个键置 None，不影响既有逻辑）
    is_global = category == 'global'
    price_percentile = (
        calculate_percentile(fund_code, 'close_price', current_price)
        if is_global and current_price is not None else None
    )
    pct_chg = latest_data.get('pct_change') if is_global else None

    # 国际指数无成交额数据：amount_ma20 无意义（滚动均值为 NaN），显式置 None 避免脏数据入库
    if is_global:
        amount_ma20 = None

    # 组装完整的指标库用于判定
    indicators = {
        "price": current_price,
        "ma60": ma60,
        "ma120": ma120,
        "pe": pe,
        "pb": pb,
        "pe_percentile": pe_percentile,
        "pb_percentile": pb_percentile,
        "price_percentile": price_percentile,
        "pct_chg": pct_chg,
        "roe": roe,
        "risk_premium": risk_premium,
        "amount": amount,
        "amount_ma20": amount_ma20,
    }

    # 非国际指数并入最新一日技术因子（仅 78 项技术列，不含 open/amount 等行情列，
    # 避免覆盖既有指标键），供 AI 按用户勾选引用。无因子数据时返回空 dict，因子键自然缺失
    if not is_global:
        latest_factor = get_latest_idx_factor(fund_code)
        for key in IDX_FACTOR_TECH_COLUMNS:
            indicators[key] = latest_factor.get(key)
    
    logger.info(f"generate_fund_report: {fund_code} (category={category}) PE={pe} PB={pb} PE%={pe_percentile} 价格分位={price_percentile}")
    
    return {
        "fund_code": fund_code,
        "category": category,
        "indicators": indicators
    }

