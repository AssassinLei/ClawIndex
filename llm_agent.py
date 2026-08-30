import openai
import os
import json
import re
from pathlib import Path
from typing import Dict
from logger import setup_logger
from database import get_custom_prompt, get_selected_indicators

logger = setup_logger("llm_agent")

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL_NAME = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

_client = None

def _get_client():
    """延迟初始化 OpenAI client，避免 import 阶段因缺少 API_KEY 崩溃"""
    global _client
    if _client is None:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量，请检查 .env 文件")
        _client = openai.OpenAI(api_key=api_key, base_url=BASE_URL)
    return _client

# 可提供给 AI 的指标清单：key → (中文标签, 格式化函数)
INDICATOR_META: dict = {
    "price":          ("当前价格",            lambda v: f"{v:.3f}" if v is not None else None),
    "pe":             ("市盈率 (PE)",          lambda v: f"{v:.2f}" if v is not None else None),
    "pb":             ("市净率 (PB)",          lambda v: f"{v:.2f}" if v is not None else None),
    "pe_percentile":  ("PE 历史分位",          lambda v: f"{v*100:.2f}%" if v is not None else None),
    "pb_percentile":  ("PB 历史分位",          lambda v: f"{v*100:.2f}%" if v is not None else None),
    "roe":            ("净资产收益率 (ROE)",     lambda v: f"{v*100:.2f}%" if v is not None else None),
    "risk_premium":   ("风险溢价",             lambda v: f"{v*100:.2f}%" if v is not None else None),
    "ma60":           ("60日均线",             lambda v: f"{v:.3f}" if v is not None else None),
    "ma120":          ("120日均线",            lambda v: f"{v:.3f}" if v is not None else None),
    "amount":         ("成交额 (万元)",         lambda v: f"{v:.0f}" if v is not None else None),
    "amount_ma20":    ("20日均成交额",          lambda v: f"{v:.0f}" if v is not None else None),
    "price_percentile": ("价格历史分位",        lambda v: f"{v*100:.2f}%" if v is not None else None),
    "pct_chg":        ("当日涨跌幅",             lambda v: f"{v:.2f}%" if v is not None else None),
}

# 指标分组（用于 user_prompt 中按类别展示）
INDICATOR_GROUPS = [
    ("基本面数据", ["pe", "pe_percentile", "pb", "pb_percentile", "roe", "risk_premium"]),
    ("行情与技术面", ["price", "ma60", "ma120", "amount", "amount_ma20"]),
]

# 技术因子元数据（idx_factor_pro 78 项，单一事实源）：
# 提示词勾选 UI、user_prompt 渲染、因子面板展示共用；列名对应 idx_factor_data 表
FACTOR_GROUPS: list = [
    ("均线类", [
        "ma_bfq_5", "ma_bfq_10", "ma_bfq_20", "ma_bfq_30",
        "ma_bfq_60", "ma_bfq_90", "ma_bfq_250",
        "ema_bfq_5", "ema_bfq_10", "ema_bfq_20", "ema_bfq_30",
        "ema_bfq_60", "ema_bfq_90", "ema_bfq_250",
        "expma_12_bfq", "expma_50_bfq", "bbi_bfq",
    ]),
    ("趋势类", [
        "macd_dif_bfq", "macd_dea_bfq", "macd_bfq",
        "dmi_pdi_bfq", "dmi_mdi_bfq", "dmi_adx_bfq", "dmi_adxr_bfq",
        "trix_bfq", "trma_bfq", "dpo_bfq", "madpo_bfq",
        "dfma_dif_bfq", "dfma_difma_bfq",
    ]),
    ("摆动类", [
        "kdj_k_bfq", "kdj_d_bfq", "kdj_bfq",
        "rsi_bfq_6", "rsi_bfq_12", "rsi_bfq_24",
        "wr_bfq", "wr1_bfq", "cci_bfq",
        "bias1_bfq", "bias2_bfq", "bias3_bfq",
        "roc_bfq", "maroc_bfq", "mtm_bfq", "mtmma_bfq",
        "psy_bfq", "psyma_bfq",
    ]),
    ("通道类", [
        "boll_upper_bfq", "boll_mid_bfq", "boll_lower_bfq",
        "ktn_upper_bfq", "ktn_mid_bfq", "ktn_down_bfq",
        "taq_up_bfq", "taq_mid_bfq", "taq_down_bfq",
        "xsii_td1_bfq", "xsii_td2_bfq", "xsii_td3_bfq", "xsii_td4_bfq",
    ]),
    ("量能与波动", [
        "obv_bfq", "vr_bfq", "mfi_bfq", "atr_bfq", "emv_bfq", "maemv_bfq",
        "brar_ar_bfq", "brar_br_bfq", "cr_bfq",
        "mass_bfq", "ma_mass_bfq", "asi_bfq", "asit_bfq",
    ]),
    ("涨跌统计", ["updays", "downdays", "topdays", "lowdays"]),
]

FACTOR_LABELS: dict = {
    "ma_bfq_5": "MA5", "ma_bfq_10": "MA10", "ma_bfq_20": "MA20", "ma_bfq_30": "MA30",
    "ma_bfq_60": "MA60", "ma_bfq_90": "MA90", "ma_bfq_250": "MA250",
    "ema_bfq_5": "EMA5", "ema_bfq_10": "EMA10", "ema_bfq_20": "EMA20", "ema_bfq_30": "EMA30",
    "ema_bfq_60": "EMA60", "ema_bfq_90": "EMA90", "ema_bfq_250": "EMA250",
    "expma_12_bfq": "EXPMA12", "expma_50_bfq": "EXPMA50", "bbi_bfq": "BBI",
    "macd_dif_bfq": "MACD DIF", "macd_dea_bfq": "MACD DEA", "macd_bfq": "MACD",
    "dmi_pdi_bfq": "DMI +DI", "dmi_mdi_bfq": "DMI -DI", "dmi_adx_bfq": "DMI ADX", "dmi_adxr_bfq": "DMI ADXR",
    "trix_bfq": "TRIX", "trma_bfq": "TRMA", "dpo_bfq": "DPO", "madpo_bfq": "MADPO",
    "dfma_dif_bfq": "DMA DIF", "dfma_difma_bfq": "DMA DIFMA",
    "kdj_k_bfq": "KDJ K", "kdj_d_bfq": "KDJ D", "kdj_bfq": "KDJ J",
    "rsi_bfq_6": "RSI6", "rsi_bfq_12": "RSI12", "rsi_bfq_24": "RSI24",
    "wr_bfq": "W&R", "wr1_bfq": "W&R1", "cci_bfq": "CCI",
    "bias1_bfq": "BIAS6", "bias2_bfq": "BIAS12", "bias3_bfq": "BIAS24",
    "roc_bfq": "ROC", "maroc_bfq": "MAROC", "mtm_bfq": "MTM", "mtmma_bfq": "MTMMA",
    "psy_bfq": "PSY", "psyma_bfq": "PSYMA",
    "boll_upper_bfq": "BOLL上轨", "boll_mid_bfq": "BOLL中轨", "boll_lower_bfq": "BOLL下轨",
    "ktn_upper_bfq": "KTN上轨", "ktn_mid_bfq": "KTN中轨", "ktn_down_bfq": "KTN下轨",
    "taq_up_bfq": "TAQ上轨", "taq_mid_bfq": "TAQ中轨", "taq_down_bfq": "TAQ下轨",
    "xsii_td1_bfq": "XSII TD1", "xsii_td2_bfq": "XSII TD2", "xsii_td3_bfq": "XSII TD3", "xsii_td4_bfq": "XSII TD4",
    "obv_bfq": "OBV", "vr_bfq": "VR", "mfi_bfq": "MFI", "atr_bfq": "ATR",
    "emv_bfq": "EMV", "maemv_bfq": "MAEMV", "brar_ar_bfq": "BRAR AR", "brar_br_bfq": "BRAR BR",
    "cr_bfq": "CR", "mass_bfq": "MASS", "ma_mass_bfq": "MAMASS",
    "asi_bfq": "ASI", "asit_bfq": "ASIT",
    "updays": "连涨天数", "downdays": "连跌天数", "topdays": "高点周期", "lowdays": "低点周期",
}

# 技术因子简洁中文名（选择界面 checkbox label 展示用；与 LABELS 相同时不重复显示缩写）
FACTOR_CN: dict = {
    "ma_bfq_5": "5日均线", "ma_bfq_10": "10日均线", "ma_bfq_20": "20日均线", "ma_bfq_30": "30日均线",
    "ma_bfq_60": "60日均线", "ma_bfq_90": "90日均线", "ma_bfq_250": "250日均线",
    "ema_bfq_5": "5日指数均线", "ema_bfq_10": "10日指数均线", "ema_bfq_20": "20日指数均线", "ema_bfq_30": "30日指数均线",
    "ema_bfq_60": "60日指数均线", "ema_bfq_90": "90日指数均线", "ema_bfq_250": "250日指数均线",
    "expma_12_bfq": "12日指数平均数", "expma_50_bfq": "50日指数平均数", "bbi_bfq": "多空指标",
    "macd_dif_bfq": "快慢线差", "macd_dea_bfq": "DIF平滑线", "macd_bfq": "MACD柱",
    "dmi_pdi_bfq": "上升方向线", "dmi_mdi_bfq": "下降方向线", "dmi_adx_bfq": "趋势强度", "dmi_adxr_bfq": "趋势评估线",
    "trix_bfq": "三重指数平滑", "trma_bfq": "TRIX均线", "dpo_bfq": "区间震荡线", "madpo_bfq": "DPO平滑线",
    "dfma_dif_bfq": "平行线差", "dfma_difma_bfq": "DMA均线",
    "kdj_k_bfq": "K值", "kdj_d_bfq": "D值", "kdj_bfq": "J值",
    "rsi_bfq_6": "6日相对强弱", "rsi_bfq_12": "12日相对强弱", "rsi_bfq_24": "24日相对强弱",
    "wr_bfq": "威廉指标", "wr1_bfq": "6日威廉指标", "cci_bfq": "顺势指标",
    "bias1_bfq": "6日乖离率", "bias2_bfq": "12日乖离率", "bias3_bfq": "24日乖离率",
    "roc_bfq": "变动率", "maroc_bfq": "ROC均线", "mtm_bfq": "动量指标", "mtmma_bfq": "MTM均线",
    "psy_bfq": "心理线", "psyma_bfq": "PSY均线",
    "boll_upper_bfq": "布林上轨", "boll_mid_bfq": "布林中轨", "boll_lower_bfq": "布林下轨",
    "ktn_upper_bfq": "肯特纳上轨", "ktn_mid_bfq": "肯特纳中轨", "ktn_down_bfq": "肯特纳下轨",
    "taq_up_bfq": "唐安奇上轨", "taq_mid_bfq": "唐安奇中轨", "taq_down_bfq": "唐安奇下轨",
    "xsii_td1_bfq": "薛斯通道一", "xsii_td2_bfq": "薛斯通道二", "xsii_td3_bfq": "薛斯通道三", "xsii_td4_bfq": "薛斯通道四",
    "obv_bfq": "能量潮", "vr_bfq": "容量比率", "mfi_bfq": "资金流量", "atr_bfq": "真实波幅",
    "emv_bfq": "简易波动", "maemv_bfq": "EMV均线", "brar_ar_bfq": "人气指标", "brar_br_bfq": "意愿指标",
    "cr_bfq": "价格动量", "mass_bfq": "梅斯线", "ma_mass_bfq": "梅斯线均线",
    "asi_bfq": "振动升降", "asit_bfq": "ASI均线",
    "updays": "连涨天数", "downdays": "连跌天数", "topdays": "高点周期", "lowdays": "低点周期",
}

FACTOR_DESC: dict = {
    "ma_bfq_5": "5日简单移动平均", "ma_bfq_10": "10日简单移动平均", "ma_bfq_20": "20日简单移动平均",
    "ma_bfq_30": "30日简单移动平均", "ma_bfq_60": "60日简单移动平均", "ma_bfq_90": "90日简单移动平均",
    "ma_bfq_250": "250日简单移动平均（年线）",
    "ema_bfq_5": "5日指数移动平均", "ema_bfq_10": "10日指数移动平均", "ema_bfq_20": "20日指数移动平均",
    "ema_bfq_30": "30日指数移动平均", "ema_bfq_60": "60日指数移动平均", "ema_bfq_90": "90日指数移动平均",
    "ema_bfq_250": "250日指数移动平均",
    "expma_12_bfq": "EMA指数平均数 N1=12", "expma_50_bfq": "EMA指数平均数 N2=50",
    "bbi_bfq": "多空指标 M=3/6/12/20",
    "macd_dif_bfq": "快慢线差 SHORT=12, LONG=26", "macd_dea_bfq": "DIF的M日平滑 M=9", "macd_bfq": "MACD柱 (DIF-DEA)×2",
    "dmi_pdi_bfq": "上升方向线 M1=14", "dmi_mdi_bfq": "下降方向线 M1=14", "dmi_adx_bfq": "趋势平均线 M2=6", "dmi_adxr_bfq": "ADX评估线",
    "trix_bfq": "三重指数平滑均线 M1=12", "trma_bfq": "TRIX的M日均线 M2=20",
    "dpo_bfq": "区间震荡线 M1=20", "madpo_bfq": "DPO的平滑线 M2=10",
    "dfma_dif_bfq": "平行线差 N1=10, N2=50", "dfma_difma_bfq": "DIF的M日均线 M=10",
    "kdj_k_bfq": "K值 N=9, M1=3", "kdj_d_bfq": "D值 M2=3", "kdj_bfq": "J值 3K-2D",
    "rsi_bfq_6": "6日相对强弱指标", "rsi_bfq_12": "12日相对强弱指标", "rsi_bfq_24": "24日相对强弱指标",
    "wr_bfq": "威廉指标 N=10", "wr1_bfq": "威廉指标 N1=6", "cci_bfq": "顺势指标 N=14",
    "bias1_bfq": "乖离率 L1=6", "bias2_bfq": "乖离率 L2=12", "bias3_bfq": "乖离率 L3=24",
    "roc_bfq": "变动率指标 N=12", "maroc_bfq": "ROC的M日均线 M=6",
    "mtm_bfq": "动量指标 N=12", "mtmma_bfq": "MTM的M日均线 M=6",
    "psy_bfq": "心理线 N=12", "psyma_bfq": "PSY的M日均线 M=6",
    "boll_upper_bfq": "布林带 N=20, P=2", "boll_mid_bfq": "布林带中枢", "boll_lower_bfq": "布林带下轨",
    "ktn_upper_bfq": "肯特纳通道 N=20, ATR=10", "ktn_mid_bfq": "肯特纳通道中枢", "ktn_down_bfq": "肯特纳通道下轨",
    "taq_up_bfq": "唐安奇通道(海龟) N=20", "taq_mid_bfq": "唐安奇通道中枢", "taq_down_bfq": "唐安奇通道下轨",
    "xsii_td1_bfq": "薛斯通道II N=102, M=7", "xsii_td2_bfq": "薛斯通道II", "xsii_td3_bfq": "薛斯通道II", "xsii_td4_bfq": "薛斯通道II",
    "obv_bfq": "能量潮指标", "vr_bfq": "容量比率 M1=26", "mfi_bfq": "资金流量指标 N=14",
    "atr_bfq": "真实波动20日均值 N=20", "emv_bfq": "简易波动指标 N=14", "maemv_bfq": "EMV的M日均线 M=9",
    "brar_ar_bfq": "人气指标 M1=26", "brar_br_bfq": "意愿指标 M1=26", "cr_bfq": "价格动量指标 N=20",
    "mass_bfq": "梅斯线 N1=9, N2=25", "ma_mass_bfq": "梅斯线的M日均线 M=6",
    "asi_bfq": "振动升降指标 M1=26", "asit_bfq": "ASI的M日均线 M2=10",
    "updays": "连续上涨交易日数", "downdays": "连续下跌交易日数", "topdays": "当前最高价为近N周期内最高", "lowdays": "当前最低价为近N周期内最低",
}

# 技术因子勾选上限（UI 保存校验 + generate_ai_report 防御截断共用）
MAX_FACTOR_COUNT = 10

# 整数型因子（涨跌统计：连续天数/周期数），prompt 中按整数格式化
_INTEGER_FACTOR_KEYS = frozenset({"updays", "downdays", "topdays", "lowdays"})


def _fmt_factor(key: str, v):
    """技术因子格式化：涨跌统计类为整数，其余保留 3 位小数"""
    if v is None:
        return None
    return f"{v:.0f}" if key in _INTEGER_FACTOR_KEYS else f"{v:.3f}"


# 未配置指标筛选时的默认指标（与 UI 默认勾选一致）
DEFAULT_INDICATORS = ["pe", "pb"]

# 国际指数专属：指标分组与默认指标（无 PE/PB 估值，仅价格与趋势类指标）
GLOBAL_INDICATOR_GROUPS = [
    ("价格与趋势", ["price", "price_percentile", "ma60", "ma120"]),
    ("当日表现", ["pct_chg"]),
]
GLOBAL_DEFAULT_INDICATORS = ["price", "price_percentile", "ma60", "ma120", "pct_chg"]


def default_indicators_for(category: str) -> list[str]:
    """按策略分类返回未配置时的默认传递指标。

    scheduler 签名归一化与 app.py 提示词默认勾选共用本函数，
    保证「未配置用户」与「显式勾选默认指标的用户」归一化后合并为同一组（LLM 去重不破）。
    """
    if category == 'global':
        return GLOBAL_DEFAULT_INDICATORS
    return DEFAULT_INDICATORS


def normalize_selected_indicators(selected: list[str] | None, category: str) -> list[str]:
    """归一化并截断用户勾选列表，供 LLM 调用与调度器签名共用。

    - 未配置（None/空）时按分类默认指标；
    - 基础指标与技术因子分别排序后拼接，保证「相同签名 ⟺ 相同最终 prompt」；
    - 技术因子超出 MAX_FACTOR_COUNT 时截断为前 N 个（防御老数据/直调越界）。
    """
    effective = selected or default_indicators_for(category)
    base_keys = sorted(k for k in effective if k in INDICATOR_META)
    factor_keys = sorted(k for k in effective if k in FACTOR_LABELS)
    if len(factor_keys) > MAX_FACTOR_COUNT:
        logger.warning(
            f"normalize_selected_indicators: 技术因子 {len(factor_keys)} 个超过上限 "
            f"{MAX_FACTOR_COUNT}，截断为前 {MAX_FACTOR_COUNT} 个"
        )
        factor_keys = factor_keys[:MAX_FACTOR_COUNT]
    return base_keys + factor_keys


def indicator_groups_for(category: str) -> list:
    """按策略分类返回指标展示分组（提示词勾选 UI 与 user_prompt 生成共用）。

    非国际指数追加技术因子分组（仅勾选项实际渲染）；国际指数无因子数据，不追加。
    """
    if category == 'global':
        return GLOBAL_INDICATOR_GROUPS
    return INDICATOR_GROUPS + FACTOR_GROUPS


def _build_indicators_text(inds: dict, selected: list[str] | None, groups: list | None = None) -> str:
    """按用户勾选的指标和分组，构建 user_prompt 中的指标展示文本"""
    keys = selected if selected else DEFAULT_INDICATORS
    groups = groups if groups is not None else INDICATOR_GROUPS
    lines = []
    for group_label, group_keys in groups:
        parts = []
        for key in group_keys:
            if key not in keys:
                continue
            val = inds.get(key)
            if val is None:
                continue
            meta = INDICATOR_META.get(key)
            if meta is None:
                # 技术因子：回退到 FACTOR_LABELS（未知 key 静默跳过，避免 KeyError）
                if key not in FACTOR_LABELS:
                    continue
                label = FACTOR_LABELS[key]
                fmt = lambda v, k=key: _fmt_factor(k, v)
            else:
                label, fmt = meta
            formatted = fmt(val)
            if formatted is not None:
                parts.append(f"{label} {formatted}")
        if parts:
            lines.append(f"- **{group_label}**：{'，'.join(parts)}")
    if not lines:
        lines.append("- **数据**：暂无可用指标")
    return "\n    ".join(lines)


# 合法的 AI 操作建议值
VALID_ADVICE = frozenset({"买入", "卖出", "持有/观望"})

# 预编译 JSON 代码块提取正则
_JSON_BLOCK_RE = re.compile(r'```(?:json)?\s*\n?([\s\S]*?)\n?```')

# 四类策略的自然语言描述（从旧硬编码规则树翻译而来，供 AI 参考）
CATEGORY_STRATEGIES = {
    "wide_base": (
        "宽基指数策略：侧重PE历史分位与无风险利率的对比，基于均值回归逻辑。"
        "PE分位低于20%为极度低估区间，高于80%为高估区间。"
        "结合60日均线判断趋势方向，价格站上MA60视为趋势确认信号。"
    ),
    "tech_growth": (
        "科技成长策略：侧重PE分位与120日牛熊线的关系，强调趋势确认以防左侧接飞刀。"
        "PE分位低于30%且站上MA120为右侧买入信号。"
        "跌破MA120应警惕趋势破位风险，暂停加仓保护本金。"
    ),
    "cycle_mfg": (
        "周期制造策略：侧重PB历史分位，周期行业以资产安全垫为核心判断依据。"
        "PB分位低于15%视为周期底部，高于85%视为景气度见顶信号。"
        "PB分位在15%~85%区间的中枢区域无明确买卖信号。"
    ),
    "dividend": (
        "稳健收息策略：侧重PB分位与PE绝对值，兼顾股息率与国债收益率利差。"
        "PB分位低于50%且PE低于15为合理估值区间，提供充分安全垫。"
        "价格跌破MA120时趋势向下，仅适合定投不宜单笔大额加仓。"
    ),
    "global": (
        "国际指数趋势跟踪策略：以双均线趋势为核心，不依赖估值指标。"
        "价格站上MA60且MA60高于MA120（多头排列）为买入/持有信号。"
        "价格跌破MA60或MA60下穿MA120为减仓/卖出信号。"
        "价格历史分位辅助判断当前价格在近10年区间的高低位置，分位越高追高风险越大。"
    ),
}

# prompt.md 文件路径
_PROMPT_MD_PATH = Path(__file__).parent / "prompt.md"


def _load_prompt_md() -> str:
    """从 prompt.md 读取基础提示词内容"""
    if _PROMPT_MD_PATH.exists():
        return _PROMPT_MD_PATH.read_text(encoding="utf-8").strip()
    logger.warning(f"prompt.md 不存在: {_PROMPT_MD_PATH}，使用最小化默认提示词")
    return "你是一名专业的指数基金投资分析助手。基于第一性原理进行逻辑推演，仅依据输入数据分析。"


def _parse_ai_json(raw: str) -> dict:
    """三级容错提取 AI 输出的 JSON，返回 {analysis, advice, confidence}"""
    parsed = None

    # L1: 直接解析全文
    try:
        parsed = json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    # L2: 正则提取 markdown 代码块后解析
    if parsed is None:
        match = _JSON_BLOCK_RE.search(raw)
        if match:
            try:
                parsed = json.loads(match.group(1).strip())
            except (json.JSONDecodeError, ValueError):
                pass

    # 校验字段
    if parsed and isinstance(parsed, dict):
        advice = str(parsed.get("advice", "")).strip()
        if advice not in VALID_ADVICE:
            advice = "持有/观望"
        analysis = str(parsed.get("analysis", raw))
        try:
            confidence = int(parsed.get("confidence", 50))
        except (ValueError, TypeError):
            confidence = 50
        confidence = max(0, min(100, confidence))  # 钳位 0~100
        return {"analysis": analysis, "advice": advice, "confidence": confidence}

    # L3: 兜底默认值（AI 有实质产出仅格式不合规，不属于 AI 失败，不加 ai_error 标记）
    logger.warning(f"_parse_ai_json: JSON 解析失败，使用兜底值。原始输出前200字符: {raw[:200]}")
    return {"analysis": raw, "advice": "持有/观望", "confidence": 0}


def _build_system_prompt(category: str, custom_prompt: str | None) -> str:
    """构建 System Prompt：prompt.md 基础 + 策略描述 + JSON Schema 约束"""

    json_schema = (
        "# 输出格式（严格遵守）\n"
        "你必须**仅**输出以下 JSON 结构，不要包含任何其他文字、解释或 Markdown 标记：\n"
        '{"analysis": "简洁清晰的分析说明，解释触发策略的关键原因", '
        '"advice": "买入|卖出|持有/观望", '
        '"confidence": 0-100}\n\n'
        "字段说明：\n"
        '- analysis: 基于第一性原理的推导分析，简洁易理解\n'
        '- advice: 唯一操作建议，必须是 "买入"、"卖出" 或 "持有/观望" 之一\n'
        '- confidence: 当前数据与策略的匹配程度（0~100），不是未来上涨概率\n\n'
        "直接输出 JSON，不要加 ```json 标记！"
    )

    if custom_prompt and custom_prompt.strip():
        # 用户定制提示词：用户已含 Role/Task，不叠加系统基础提示词，仅追加输出格式
        prompt = custom_prompt.strip()
    else:
        base = _load_prompt_md()
        strategy_desc = CATEGORY_STRATEGIES.get(category, "根据指数分类标签，参考通用估值分析逻辑。")
        prompt = f"{base}\n\n# 投资策略框架\n{strategy_desc}"

    prompt += f"\n\n{json_schema}"
    return prompt


def generate_ai_report(fund_data: Dict, custom_prompt: str = None, selected_indicators: list[str] | None = None) -> dict:
    """
    根据指标数据，由 AI 自主分析并输出结构化决策。

    参数 custom_prompt: 用户为指数定制的分析框架，注入 System Prompt 作为补充策略。
    参数 selected_indicators: 用户勾选的指标列表，None 表示全部。

    返回: {"analysis": str, "advice": str, "confidence": int}；
    AI 调用失败（API 异常/空响应）时额外含 "ai_error": True，下游据此跳过入库与推送。
    注意三类语义：数据异常（fund_data 含 error，仍入库）、AI 调用失败（ai_error，不入库）、
    JSON 解析兜底（AI 有产出仅格式不合规，照常入库）。
    """
    if "error" in fund_data:
        # 数据异常短路：属于"数据异常"语义（仍入库），不属于 AI 失败，不加 ai_error 标记
        logger.warning(f"generate_ai_report: 数据异常, 跳过AI调用 - {fund_data['error']}")
        return {
            "analysis": f"数据获取异常：{fund_data['error']}",
            "advice": "持有/观望",
            "confidence": 0,
        }

    code = fund_data['fund_code']
    name = fund_data.get('fund_name', code)
    cat = fund_data['category']
    inds = fund_data['indicators']

    # 构建 System Prompt
    system_prompt = _build_system_prompt(cat, custom_prompt)

    # 构建 User Prompt（归一化与 scheduler 签名共用同一函数，保证缓存键与实际 prompt 一致）
    effective_selected = normalize_selected_indicators(selected_indicators, cat)
    indicators_text = _build_indicators_text(inds, effective_selected, groups=indicator_groups_for(cat))
    if selected_indicators:
        logger.info(f"generate_ai_report: {code} 已筛选指标 {selected_indicators}")

    user_prompt = f"""请根据以下指数数据进行分析：

- **监控指数**：{name}（{code}）(分类标签: {cat})
{indicators_text}

请输出你的 JSON 分析结果。"""

    try:
        logger.info(f"generate_ai_report: {code} (category={cat}), 调用 {MODEL_NAME}")

        # 记录发送给 AI 的完整内容（DEBUG 级别，生产环境可关闭控制台输出）
        logger.debug(
            f"generate_ai_report: {code} → AI 请求内容\n"
            f"--- System Prompt ---\n{system_prompt}\n"
            f"--- User Prompt ---\n{user_prompt}\n"
            f"--- 结束 ---"
        )

        response = _get_client().chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=16384
        )
        choice = response.choices[0] if response.choices else None
        raw = choice.message.content if choice else None
        finish_reason = choice.finish_reason if choice else "N/A（无 choices）"

        # 记录 token 用量
        usage = response.usage
        if usage:
            logger.debug(
                f"generate_ai_report: {code} token 用量 "
                f"prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
                f"total={usage.total_tokens}"
            )

        # 记录 AI 返回的原始内容（DEBUG 级别）
        raw_len = len(raw) if raw else 0
        logger.debug(
            f"generate_ai_report: {code} ← AI 返回内容 ({raw_len} 字符) "
            f"finish_reason={finish_reason}\n"
            f"--- 原始输出 ---\n{raw or '(空)'}\n"
            f"--- 结束 ---"
        )

        # 空响应：属于 AI 调用失败，标记 ai_error 供下游跳过入库/推送
        if not raw:
            logger.warning(
                f"generate_ai_report: {code} AI 返回空内容！"
                f" finish_reason={finish_reason}"
                f" (若为 content_filter 则说明 prompt 被安全过滤拦截)"
            )
            return {
                "analysis": f"AI 返回空内容（finish_reason={finish_reason}，可能被安全过滤拦截）",
                "advice": "持有/观望",
                "confidence": 0,
                "ai_error": True,
            }

        logger.info(f"generate_ai_report: {code} 返回 {raw_len} 字符, finish_reason={finish_reason}")
        return _parse_ai_json(raw)
    except openai.RateLimitError as e:
        # 注意：RateLimitError 是 APIError 子类，必须在前否则永远不可达
        logger.error(f"generate_ai_report: RateLimitError - {e}")
        return {"analysis": f"AI 接口限流: {str(e)}", "advice": "持有/观望", "confidence": 0, "ai_error": True}
    except openai.APIError as e:
        logger.error(f"generate_ai_report: APIError - {e}")
        return {"analysis": f"AI 接口调用失败 [{type(e).__name__}]: {str(e)}", "advice": "持有/观望", "confidence": 0, "ai_error": True}
    except Exception as e:
        logger.error(f"generate_ai_report: 未知异常 - {type(e).__name__}: {e}")
        return {"analysis": f"AI 报告生成失败 [{type(e).__name__}]: {str(e)}", "advice": "持有/观望", "confidence": 0, "ai_error": True}


def generate_ai_report_with_config(fund_data: Dict, username: str) -> dict:
    """
    高层封装：自动从数据库读取该用户对该指数的定制提示词与指标勾选配置，
    再调用 generate_ai_report。调用方无需关心数据库查询细节。
    """
    code = fund_data.get('fund_code', '')
    custom_prompt = get_custom_prompt(username, code)
    selected_indicators = get_selected_indicators(username, code) or None
    return generate_ai_report(
        fund_data,
        custom_prompt=custom_prompt,
        selected_indicators=selected_indicators,
    )