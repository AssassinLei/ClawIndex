"""ClawIndex 全局常量定义"""

# 分类中文映射
CATEGORY_NAMES = {
    "wide_base": "宽基指数",
    "tech_growth": "科技成长",
    "cycle_mfg": "周期制造",
    "dividend": "稳健收息",
    "global": "国际指数",
}

# 国际指数代码表（Tushare index_global 接口，需 6000 积分）
# 同时作为市场类型识别依据：fund_code 在此表内即按国际指数处理
# （无 PE/PB 估值、无技术因子、SSE 交易日历不适用）
GLOBAL_INDEX_MAP = {
    "XIN9": "富时中国A50指数",
    "HSI": "恒生指数",
    "HKTECH": "恒生科技指数",
    "HKAH": "恒生AH股H指数",
    "DJI": "道琼斯工业指数",
    "SPX": "标普500指数",
    "IXIC": "纳斯达克指数",
    "FTSE": "富时100指数",
    "FCHI": "法国CAC40指数",
    "GDAXI": "德国DAX指数",
    "N225": "日经225指数",
    "KS11": "韩国综合指数",
    "AS51": "澳大利亚标普200指数",
    "SENSEX": "印度孟买SENSEX指数",
    "IBOVESPA": "巴西IBOVESPA指数",
    "RTS": "俄罗斯RTS指数",
    "TWII": "台湾加权指数",
    "CKLSE": "马来西亚指数",
    "SPTSX": "加拿大S&P/TSX指数",
    "CSX5P": "STOXX欧洲50指数",
    "RUT": "罗素2000指数",
}
