import pandas as pd
from database import get_recent_prices, calculate_percentile, get_connection
from logger import setup_logger

logger = setup_logger("strategy_engine")

def calculate_technical_indicators(df_prices: pd.DataFrame) -> dict:
    """基于近期价格，计算技术面指标"""
    if df_prices.empty or len(df_prices) < 120:
        return {"current_price": None, "ma60": None, "ma120": None}
    
    # 计算移动平均线
    df_prices['ma60'] = df_prices['close_price'].rolling(window=60).mean()
    df_prices['ma120'] = df_prices['close_price'].rolling(window=120).mean()
    
    latest_row = df_prices.iloc[-1]
    
    return {
        "current_price": latest_row['close_price'],
        "ma60": latest_row['ma60'],
        "ma120": latest_row['ma120']
    }

def generate_fund_report(fund_code: str, category: str) -> dict:
    """主控函数：拉取数据 -> 计算指标 -> 硬编码判定信号"""
    conn = get_connection()
    # 1. 获取最新一天的基本面数据
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM daily_market_data WHERE fund_code = ? ORDER BY trade_date DESC LIMIT 1", (fund_code,))
    latest_data = cursor.fetchone()
    conn.close()
    
    if not latest_data:
        logger.warning(f"generate_fund_report: {fund_code} 无行情数据")
        return {"error": f"数据库中未找到 {fund_code} 的行情数据，请检查：1) 标的是否已添加 2) 历史数据是否同步成功 3) 标的代码格式是否正确（如 000300.SH）"}
        
    latest_data = dict(latest_data)
    pe = latest_data['pe']
    pb = latest_data['pb']
    risk_free = latest_data['risk_free_rate']
    
    # 2. 计算基本面的推导指标和历史分位
    pe_percentile = calculate_percentile(fund_code, 'pe', pe) if pe else None
    pb_percentile = calculate_percentile(fund_code, 'pb', pb) if pb else None
    
    # 推导风险溢价 (Fed模型: 1/PE - 无风险利率)
    risk_premium = (1 / pe - risk_free) if pe and pe > 0 and risk_free is not None else None
    
    # 推导 ROE = PB / PE (需避免除零)
    roe = (pb / pe) if pe and pe > 0 and pb else None

    # 3. 计算技术面指标
    df_prices = get_recent_prices(fund_code, days=150)
    tech_inds = calculate_technical_indicators(df_prices)
    current_price = tech_inds['current_price']
    ma60 = tech_inds['ma60']
    ma120 = tech_inds['ma120']

    # 组装完整的指标库用于判定
    indicators = {
        "price": current_price,
        "ma60": ma60,
        "ma120": ma120,
        "pe": pe,
        "pb": pb,
        "pe_percentile": pe_percentile,
        "pb_percentile": pb_percentile,
        "roe": roe,
        "risk_premium": risk_premium
    }
    
    # 4. 【核心硬逻辑】分支判定，输出强结构化信号
    signal = _apply_hard_rules(category, indicators)
    
    logger.info(f"generate_fund_report: {fund_code} (category={category}) PE={pe} PB={pb} PE%={pe_percentile} action={signal['action']}")
    
    return {
        "fund_code": fund_code,
        "category": category,
        "indicators": indicators,
        "decision": signal
    }

def _apply_hard_rules(category: str, inds: dict) -> dict:
    """根据分类，执行硬编码的 If-Else 规则树"""
    action = "HOLD"
    logic_details = []

    pe_pct = inds.get("pe_percentile")
    pb_pct = inds.get("pb_percentile")
    price = inds.get("price")
    ma60 = inds.get("ma60")
    ma120 = inds.get("ma120")
    rp = inds.get("risk_premium")

    if category == 'wide_base':
        # 宽基指数逻辑
        if pe_pct is not None:
            if pe_pct < 0.20:
                if price and ma60 and price > ma60:
                    action = "STRONG_BUY"
                    logic_details.append("极度低估 (PE分位<20%) 且 价格突破60日趋势线，触发强烈加仓。")
                else:
                    action = "BUY_PLAN"
                    logic_details.append("极度低估 (PE分位<20%) 但趋势未确立，仅执行左侧大额定投。")
            elif pe_pct < 0.50:
                action = "BUY_PLAN"
                logic_details.append("估值适中偏低 (PE分位<50%)，维持常规额度定投。")
            elif pe_pct > 0.80 or (rp is not None and rp < 0.03):
                action = "SELL_PLAN"
                logic_details.append("高估值或风险溢价过低，建议分批止盈并停止定投。")
            else:
                action = "HOLD"
                logic_details.append("估值在合理中枢内，持有观望。")
        else:
            logic_details.append(f"PE分位数无法计算（历史数据不足100条），当前PE={inds.get('pe')}，暂维持持有观望。")
                
    elif category == 'tech_growth':
        # 科技成长逻辑（强调用趋势过滤，防止左侧接飞刀）
        if pe_pct is not None and price and ma120:
            if pe_pct < 0.30 and price > ma120:
                action = "STRONG_BUY"
                logic_details.append("估值便宜 (PE分位<30%) 且站上120日牛熊线，右侧信号确立，执行买入。")
            elif pe_pct < 0.10:
                action = "BUY_PLAN"
                logic_details.append("估值极度压缩 (PE分位<10%)，但均线处于空头，只进行小资金试探定投。")
            elif price < ma120:
                action = "HOLD"
                logic_details.append("趋势破位 (跌破120日线)，暂停加仓，保护本金。")
            else:
                logic_details.append(f"PE分位{pe_pct*100:.1f}%处于中性区间，价格站上MA120，暂无明确信号，持有观望。")
        else:
            missing = []
            if pe_pct is None: missing.append("PE分位数")
            if not price: missing.append("当前价格")
            if not ma120: missing.append("MA120")
            logic_details.append(f"指标缺失({','.join(missing)})，无法判定信号，维持持有。")

    elif category == 'cycle_mfg':
        # 周期制造逻辑
        if pb_pct is not None:
            if pb_pct < 0.15:
                action = "BUY_PLAN"
                logic_details.append("周期底部确认 (PB分位<15%)，全行业破净，开启左侧建仓。")
            elif pb_pct > 0.85:
                action = "SELL_PLAN"
                logic_details.append("景气度见顶 (PB分位>85%)，执行清仓卖出。")
            else:
                logic_details.append(f"PB分位{pb_pct*100:.1f}%处于中枢区间(15%~85%)，无明确买卖信号，持有观望。")
        else:
            logic_details.append(f"PB分位数无法计算（历史数据不足100条），当前PB={inds.get('pb')}，暂维持持有。")

    elif category == 'dividend':
        # 红利稳健逻辑
        pe_val = inds.get("pe")
        if pb_pct is not None and pe_val:
             if pb_pct < 0.50 and pe_val < 15:
                 action = "BUY_PLAN"
                 logic_details.append("估值合理，提供充分安全垫，适合稳健吃息买入。")
             elif price and ma120 and price < ma120:
                 logic_details.append("趋势向下，仅定投不单笔大额加仓。")
             else:
                 logic_details.append(f"PB分位{pb_pct*100:.1f}%，PE={pe_val:.1f}，不满足买入条件，持有观望。")
        else:
            missing = []
            if pb_pct is None: missing.append("PB分位数")
            if not pe_val: missing.append("PE")
            logic_details.append(f"指标缺失({','.join(missing)})，无法判定信号，维持持有。")

    return {
        "action": action, 
        "details": logic_details
    }