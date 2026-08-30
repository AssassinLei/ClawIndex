from dotenv import load_dotenv
load_dotenv()

import streamlit as st
import pandas as pd
import re
from pathlib import Path
from database import init_db, add_fund, remove_fund, get_all_funds, get_shared_category, get_all_industries, get_industry_count, get_inspection_history, get_indicator_history, get_market_data_for_date, get_latest_market_data, get_latest_idx_factor, get_latest_trade_date, get_webhook_urls, add_webhook_url, remove_webhook_url, get_setting, set_setting, get_user_setting, set_user_setting, get_custom_prompt, get_selected_indicators, upsert_custom_prompt, delete_custom_prompt, register_user, user_exists
from data_fetcher import sync_all_history, sync_incremental, fetch_industry_classify, fetch_index_members, is_trade_day, is_global_code
from llm_agent import INDICATOR_META, INDICATOR_GROUPS, GLOBAL_INDICATOR_GROUPS, default_indicators_for
from webhook_sender import send_to_all_webhooks, is_action_pushable
from scheduler import start_scheduler, stop_scheduler, get_next_run_time, is_scheduler_running, update_cron_expression
from constants import CATEGORY_NAMES, GLOBAL_INDEX_MAP
from inspection_pipeline import run_single_inspection
from datetime import datetime, timedelta

ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_PATH = ASSETS_DIR / "clawindex_logo.svg"

# 页面基本配置
st.set_page_config(page_title="ClawIndex 爪析", page_icon=str(LOGO_PATH), layout="wide")

# 品牌头部样式 + 隐藏 Streamlit 默认 UI
st.markdown("""
<style>
    /* 隐藏 Streamlit 默认菜单 */
    #MainMenu {visibility: hidden;}
    
    /* 隐藏 Deploy 按钮 */
    .stDeployButton {
        display: none !important;
    }
    button[kind="deploy"] {
        display: none !important;
    }
    
    /* 隐藏标题区域的锚点链接 */
    .clawindex-header a,
    .clawindex-title-block a {
        display: none !important;
    }
    /* 隐藏 Streamlit 自动生成的标题锚点 */
    [data-testid="stHeader"] a,
    h1 a, h2 a, h3 a {
        display: none !important;
    }
    
    .clawindex-header {
        display: flex;
        align-items: center;
        gap: 14px;
        margin: 0 0 0.25rem 0;
    }
    .clawindex-logo-wrap {
        width: 56px;
        height: 56px;
        flex-shrink: 0;
        border-radius: 14px;
        background: linear-gradient(145deg, rgba(6, 182, 212, 0.12), rgba(139, 92, 246, 0.18));
        border: 1px solid rgba(34, 211, 238, 0.35);
        box-shadow:
            0 0 24px rgba(6, 182, 212, 0.25),
            0 0 48px rgba(139, 92, 246, 0.15),
            inset 0 1px 0 rgba(255, 255, 255, 0.08);
        display: flex;
        align-items: center;
        justify-content: center;
        animation: claw-pulse 3s ease-in-out infinite;
    }
    .clawindex-logo-wrap svg {
        width: 40px;
        height: 40px;
    }
    .clawindex-title-block h1 {
        font-size: 2.2rem;
        font-weight: 700;
        line-height: 1.2;
        margin: 0;
        color: #000000;
    }
    .clawindex-title-block .subtitle-en {
        font-size: 0.78rem;
        font-weight: 500;
        letter-spacing: 0.12em;
        color: #000000;
        margin-top: 2px;
    }
    @keyframes claw-pulse {
        0%, 100% {
            box-shadow:
                0 0 20px rgba(6, 182, 212, 0.2),
                0 0 40px rgba(139, 92, 246, 0.12),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
        }
        50% {
            box-shadow:
                0 0 28px rgba(6, 182, 212, 0.45),
                0 0 56px rgba(139, 92, 246, 0.28),
                inset 0 1px 0 rgba(255, 255, 255, 0.12);
        }
    }
</style>
""", unsafe_allow_html=True)

# 初始化数据库
init_db()

# 根据配置决定是否启动定时调度器（关闭后不会因为 rerun 自动重启）
# 注意：必须在登录门之前启动，保证无人登录也能执行定时巡检
auto_enabled_init = get_setting("scheduler_auto_enabled", "true")
if auto_enabled_init == "true":
    start_scheduler()


# --- 登录门：未登录时只渲染登录/注册页，不进入主界面 ---
def _clear_inspection_state():
    """清理巡检相关会话状态，防止同一浏览器换用户后看到上一用户的巡检卡片"""
    for key in ("inspection_active", "inspection_fund_list", "inspection_index",
                "inspection_total", "inspection_cards", "_processed_codes"):
        st.session_state.pop(key, None)


if "username" not in st.session_state:
    st.markdown("## 👤 用户登录")
    st.caption("输入用户名进入系统。各用户拥有独立的监控池、推送配置与提示词，行情数据全局共享，巡检结果按用户隔离。")

    col_login, col_register = st.columns(2)
    with col_login:
        st.subheader("登录", anchor=False)
        # 用 form 包裹，输入框内回车即可提交登录
        with st.form("login_form"):
            login_name = st.text_input("用户名", placeholder="输入已注册的用户名", key="login_input")
            if st.form_submit_button("登录", type="primary", use_container_width=True):
                name = login_name.strip()
                if not name:
                    st.warning("请输入用户名")
                elif user_exists(name):
                    st.session_state.username = name
                    _clear_inspection_state()
                    st.rerun()
                else:
                    st.warning("用户名不存在，请先在右侧注册")
    with col_register:
        st.subheader("注册", anchor=False)
        # 用 form 包裹，输入框内回车即可提交注册
        with st.form("register_form"):
            reg_name = st.text_input("用户名", placeholder="输入名称即可注册", key="register_input")
            if st.form_submit_button("注册并登录", use_container_width=True):
                name = reg_name.strip()
                if not name:
                    st.warning("用户名不能为空")
                elif register_user(name):
                    st.session_state.username = name
                    _clear_inspection_state()
                    st.rerun()
                else:
                    st.warning(f"用户名「{name}」已存在，请直接登录")
    st.stop()

username = st.session_state.username

# 初始化行业分类数据（如果数据库为空则自动拉取；置于登录门后，未登录不触发网络请求）
if get_industry_count() == 0:
    with st.spinner('正在初始化行业分类数据...'):
        success, msg = fetch_industry_classify()
        if success:
            st.success(msg)
        else:
            st.warning(f"行业分类初始化: {msg}")

# 获取行业列表用于前端选择
industries = get_all_industries()
# 构建行业选项: "行业名称 (代码)" 格式，同时保存 index_code 用于提交
industry_options = []
for ind in industries:
    label = f"[{ind['level']}] {ind['industry_name']} ({ind['index_code']})"
    industry_options.append({
        'label': label,
        'index_code': ind['index_code'],
        'industry_name': ind['industry_name'],
        'level': ind['level']
    })

# 操作建议视觉风格映射（巡检页与历史页复用）
ACTION_STYLES = {
    "买入":      {"color": "#16a34a", "bg": "#f0fdf4", "border": "#22c55e", "icon": "🟢", "label": "建议买入"},
    "卖出":      {"color": "#dc2626", "bg": "#fef2f2", "border": "#ef4444", "icon": "🔴", "label": "建议卖出"},
    "持有/观望":  {"color": "#d97706", "bg": "#fffbeb", "border": "#f59e0b", "icon": "🟡", "label": "持有观望"},
    "数据异常":   {"color": "#6b7280", "bg": "#f9fafb", "border": "#9ca3af", "icon": "⚠️", "label": "数据异常"},
}

# 可点击查看历史趋势的指标：key → (DB列列表, 转换函数或None, 中文标题, 公式说明)
TREND_INDICATORS = {
    'close_price':   (['close_price'],                   None,
                      "收盘价",                            "数据来源：Tushare 申万指数日线行情"),
    'pe':            (['pe'],                             None,
                      "市盈率 (PE)",                       "数据来源：Tushare 申万指数日线行情"),
    'pb':            (['pb'],                             None,
                      "市净率 (PB)",                       "数据来源：Tushare 申万指数日线行情"),
    'pb_percentile': (['pb'],                             lambda df: df['pb'].expanding().rank(pct=True),
                      "PB 历史分位",                       "计算方式：PB 从最早至今的累积历史分位（expanding rank）"),
    'roe':           (['pe', 'pb'],                       lambda df: df['pb'] / df['pe'].replace(0, None),
                      "ROE",                              "计算方式：PB ÷ PE（需 PE > 0）"),
    'risk_premium':  (['pe', 'risk_free_rate'],           lambda df: 1/df['pe'].replace(0, None) - df['risk_free_rate'],
                      "风险溢价",                          "计算方式：1 ÷ PE − 无风险利率（Fed 模型）\n无风险利率 = 10 年期国债收益率"),
    'amount':        (['amount'],                         None,
                      "成交额",                            "数据来源：Tushare 申万指数日线行情"),
    'price_percentile': (['close_price'],                 lambda df: df['close_price'].expanding().rank(pct=True),
                      "价格历史分位",                      "计算方式：收盘价从最早至今的累积历史分位（expanding rank）"),
}


# 趋势图时间范围选项：标签 → timedelta 偏移量（None 为全部）
TREND_RANGE_OPTIONS = {
    "全部":  None,
    "近3年": timedelta(days=365 * 3),
    "近1年": timedelta(days=365),
    "近1月": timedelta(days=30),
}


# 技术因子展示元数据：(分组名, [(列名, 指标标签, 参数说明)])，列名对应 idx_factor_data 表
FACTOR_GROUPS = [
    ("均线类", [
        ("ma_bfq_5",     "MA5",      "5日简单移动平均"),
        ("ma_bfq_10",    "MA10",     "10日简单移动平均"),
        ("ma_bfq_20",    "MA20",     "20日简单移动平均"),
        ("ma_bfq_30",    "MA30",     "30日简单移动平均"),
        ("ma_bfq_60",    "MA60",     "60日简单移动平均"),
        ("ma_bfq_90",    "MA90",     "90日简单移动平均"),
        ("ma_bfq_250",   "MA250",    "250日简单移动平均（年线）"),
        ("ema_bfq_5",    "EMA5",     "5日指数移动平均"),
        ("ema_bfq_10",   "EMA10",    "10日指数移动平均"),
        ("ema_bfq_20",   "EMA20",    "20日指数移动平均"),
        ("ema_bfq_30",   "EMA30",    "30日指数移动平均"),
        ("ema_bfq_60",   "EMA60",    "60日指数移动平均"),
        ("ema_bfq_90",   "EMA90",    "90日指数移动平均"),
        ("ema_bfq_250",  "EMA250",   "250日指数移动平均"),
        ("expma_12_bfq", "EXPMA12",  "EMA指数平均数 N1=12"),
        ("expma_50_bfq", "EXPMA50",  "EMA指数平均数 N2=50"),
        ("bbi_bfq",      "BBI",      "多空指标 M=3/6/12/20"),
    ]),
    ("趋势类", [
        ("macd_dif_bfq",   "MACD DIF",  "快慢线差 SHORT=12, LONG=26"),
        ("macd_dea_bfq",   "MACD DEA",  "DIF的M日平滑 M=9"),
        ("macd_bfq",       "MACD",      "MACD柱 (DIF-DEA)×2"),
        ("dmi_pdi_bfq",    "DMI +DI",   "上升方向线 M1=14"),
        ("dmi_mdi_bfq",    "DMI -DI",   "下降方向线 M1=14"),
        ("dmi_adx_bfq",    "DMI ADX",   "趋势平均线 M2=6"),
        ("dmi_adxr_bfq",   "DMI ADXR",  "ADX评估线"),
        ("trix_bfq",       "TRIX",      "三重指数平滑均线 M1=12"),
        ("trma_bfq",       "TRMA",      "TRIX的M日均线 M2=20"),
        ("dpo_bfq",        "DPO",       "区间震荡线 M1=20"),
        ("madpo_bfq",      "MADPO",     "DPO的平滑线 M2=10"),
        ("dfma_dif_bfq",   "DMA DIF",   "平行线差 N1=10, N2=50"),
        ("dfma_difma_bfq", "DMA DIFMA", "DIF的M日均线 M=10"),
    ]),
    ("摆动类", [
        ("kdj_k_bfq",   "KDJ K",   "K值 N=9, M1=3"),
        ("kdj_d_bfq",   "KDJ D",   "D值 M2=3"),
        ("kdj_bfq",     "KDJ J",   "J值 3K-2D"),
        ("rsi_bfq_6",   "RSI6",    "6日相对强弱指标"),
        ("rsi_bfq_12",  "RSI12",   "12日相对强弱指标"),
        ("rsi_bfq_24",  "RSI24",   "24日相对强弱指标"),
        ("wr_bfq",      "W&R",     "威廉指标 N=10"),
        ("wr1_bfq",     "W&R1",    "威廉指标 N1=6"),
        ("cci_bfq",     "CCI",     "顺势指标 N=14"),
        ("bias1_bfq",   "BIAS6",   "乖离率 L1=6"),
        ("bias2_bfq",   "BIAS12",  "乖离率 L2=12"),
        ("bias3_bfq",   "BIAS24",  "乖离率 L3=24"),
        ("roc_bfq",     "ROC",     "变动率指标 N=12"),
        ("maroc_bfq",   "MAROC",   "ROC的M日均线 M=6"),
        ("mtm_bfq",     "MTM",     "动量指标 N=12"),
        ("mtmma_bfq",   "MTMMA",   "MTM的M日均线 M=6"),
        ("psy_bfq",     "PSY",     "心理线 N=12"),
        ("psyma_bfq",   "PSYMA",   "PSY的M日均线 M=6"),
    ]),
    ("通道类", [
        ("boll_upper_bfq", "BOLL上轨", "布林带 N=20, P=2"),
        ("boll_mid_bfq",   "BOLL中轨", "布林带中枢"),
        ("boll_lower_bfq", "BOLL下轨", "布林带下轨"),
        ("ktn_upper_bfq",  "KTN上轨",  "肯特纳通道 N=20, ATR=10"),
        ("ktn_mid_bfq",    "KTN中轨",  "肯特纳通道中枢"),
        ("ktn_down_bfq",   "KTN下轨",  "肯特纳通道下轨"),
        ("taq_up_bfq",     "TAQ上轨",  "唐安奇通道(海龟) N=20"),
        ("taq_mid_bfq",    "TAQ中轨",  "唐安奇通道中枢"),
        ("taq_down_bfq",   "TAQ下轨",  "唐安奇通道下轨"),
        ("xsii_td1_bfq",   "XSII TD1", "薛斯通道II N=102, M=7"),
        ("xsii_td2_bfq",   "XSII TD2", "薛斯通道II"),
        ("xsii_td3_bfq",   "XSII TD3", "薛斯通道II"),
        ("xsii_td4_bfq",   "XSII TD4", "薛斯通道II"),
    ]),
    ("量能与波动", [
        ("obv_bfq",     "OBV",     "能量潮指标"),
        ("vr_bfq",      "VR",      "容量比率 M1=26"),
        ("mfi_bfq",     "MFI",     "资金流量指标 N=14"),
        ("atr_bfq",     "ATR",     "真实波动20日均值 N=20"),
        ("emv_bfq",     "EMV",     "简易波动指标 N=14"),
        ("maemv_bfq",   "MAEMV",   "EMV的M日均线 M=9"),
        ("brar_ar_bfq", "BRAR AR", "人气指标 M1=26"),
        ("brar_br_bfq", "BRAR BR", "意愿指标 M1=26"),
        ("cr_bfq",      "CR",      "价格动量指标 N=20"),
        ("mass_bfq",    "MASS",    "梅斯线 N1=9, N2=25"),
        ("ma_mass_bfq", "MAMASS",  "梅斯线的M日均线 M=6"),
        ("asi_bfq",     "ASI",     "振动升降指标 M1=26"),
        ("asit_bfq",    "ASIT",    "ASI的M日均线 M2=10"),
    ]),
    ("涨跌统计", [
        ("updays",   "连涨天数", "连续上涨交易日数"),
        ("downdays", "连跌天数", "连续下跌交易日数"),
        ("topdays",  "高点周期", "当前最高价为近N周期内最高"),
        ("lowdays",  "低点周期", "当前最低价为近N周期内最低"),
    ]),
]


def render_factor_panel(fund_code: str):
    """在 popover 内渲染最新一日的技术因子数据，按类别分组展示。"""
    factor = get_latest_idx_factor(fund_code)
    if not factor:
        st.caption("暂无技术因子数据（可能接口无权限或尚未同步）")
        return

    st.caption(f"交易日期：{factor.get('trade_date', 'N/A')} · 数据来源：Tushare idx_factor_pro（不复权）")
    for group_label, items in FACTOR_GROUPS:
        rows = []
        for col, label, desc in items:
            val = factor.get(col)
            rows.append({
                "指标": label,
                "数值": f"{val:.3f}" if val is not None else "N/A",
                "说明": desc,
            })
        st.markdown(f"**{group_label}**")
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


@st.dialog("指数成分", width="large")
def show_index_members(index_code: str, industry_name: str, level: str):
    """弹窗实时展示指数的成分股列表（每次实时获取，不落库）。"""
    with st.spinner(f"正在获取 {industry_name} 的成分股..."):
        df, error = fetch_index_members(index_code, level)

    if error:
        st.error(f"成分获取失败：{error}")
        return
    if df.empty:
        st.info("该指数暂无成分股数据")
        return

    st.caption(f"{industry_name} ({index_code}) · 共 {len(df)} 只成分股 · 数据实时获取自 Tushare")
    display_df = df[['ts_code', 'name', 'in_date', 'l3_name']].copy()
    display_df['in_date'] = pd.to_datetime(
        display_df['in_date'], errors='coerce'
    ).dt.strftime('%Y-%m-%d').fillna('N/A')
    display_df.columns = ['股票代码', '股票名称', '纳入日期', '三级行业']
    st.dataframe(display_df, hide_index=True, use_container_width=True)


@st.cache_data(ttl=3600, show_spinner=False)
def _load_history_for_trend(fund_code: str) -> pd.DataFrame:
    """加载某标的的全部历史数据（缓存1小时），供趋势图复用。"""
    from database import get_indicator_history
    return get_indicator_history(fund_code, ['close_price', 'pe', 'pb', 'amount', 'risk_free_rate'])


def render_trend_chart(fund_code: str, indicator_key: str, title: str):
    """在 popover 内部渲染单个指标的历史趋势折线图（支持时间范围筛选）。"""
    meta = TREND_INDICATORS.get(indicator_key)
    if not meta:
        st.caption("不支持的指标类型")
        return

    df = _load_history_for_trend(fund_code)
    if df.empty or len(df) < 5:
        st.caption("暂无足够历史数据")
        return

    # 阶段1: 基于全量数据计算 Y 轴（保证 expanding.rank 等派生指标正确）
    transform = meta[1]
    if transform:
        df['_value'] = transform(df)
    else:
        df['_value'] = df[meta[0][0]]

    chart_df = df[['trade_date', '_value']].dropna().set_index('trade_date')
    if chart_df.empty:
        st.caption("暂无有效数据")
        return

    # 阶段2: 时间范围选择器（label_visibility="collapsed" 复用 app.py:327 现有模式）
    time_range = st.radio(
        "时间范围",
        list(TREND_RANGE_OPTIONS.keys()),
        horizontal=True,
        key=f"trend_range_{fund_code}_{indicator_key}",
        label_visibility="collapsed",
    )

    # 阶段3: 过滤后渲染（YYYY-MM-DD 字符串字典序 ≡ 时间序，无需 pd.to_datetime）
    offset = TREND_RANGE_OPTIONS[time_range]
    if offset is not None:
        cutoff = (datetime.now() - offset).strftime("%Y-%m-%d")
        chart_df = chart_df[chart_df.index >= cutoff]
        if chart_df.empty:
            st.caption(f"所选时间范围「{time_range}」内暂无数据")
            return

    st.line_chart(chart_df, use_container_width=True)
    st.caption(f"共 {len(chart_df)} 个交易日 · {chart_df.index[0]} ~ {chart_df.index[-1]}")


# --- 巡检卡片渲染辅助函数 ---
def render_card_header(card: dict):
    """渲染巡检卡片的彩色标题栏"""
    st.markdown(f"""
    <div style="
        border-left: 5px solid {card['border']};
        background: {card['bg']};
        border-radius: 0 12px 12px 0;
        padding: 16px 20px;
        margin: 12px 0;
        box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    ">
        <div style="display:flex; align-items:center; gap:10px; margin-bottom:4px;">
            <span style="font-size:1.6rem;">{card['icon']}</span>
            <span style="font-size:1.2rem; font-weight:700; color:{card['color']};">{card['name']} · {card['label']}</span>
            <span style="font-size:0.85rem; color:#6b7280; margin-left:auto;">{card['code']} · {card['date']}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)


def render_card_expander(card: dict, expanded: bool = False):
    """渲染巡检卡片的展开详情"""
    with st.expander(f"{card['icon']} {card['name']}", expanded=expanded):
        if card.get('has_error'):
            st.error(f"⚠️ {card.get('error_msg', '未知错误')}")
        else:
            col_left, col_right = st.columns([1, 1.5])

            # ===== 左列：纯指标数据 =====
            with col_left:
                st.subheader("📊 指标数据", anchor=False)
                inds = card.get('indicators', {})

                def _fmt(v, fmt_spec="{:.2f}", post=""):
                    if v is None:
                        return "N/A"
                    try:
                        return f"{fmt_spec.format(v)}{post}"
                    except (ValueError, TypeError):
                        return str(v)

                # 每行一对指标，用 4 列布局（指标A | 按钮A | 指标B | 按钮B）
                # key=None 表示该指标不可点击，按钮列留空
                code = card['code']
                if card.get('category') == 'global':
                    # 国际指数无估值数据：展示价格、价格历史分位、涨跌幅与均线
                    metric_rows = [
                        ("当前价格",  _fmt(inds.get("price"), "{:.3f}"), 'close_price',
                         "价格历史分位", f"{inds['price_percentile'] * 100:.1f}%" if inds.get("price_percentile") is not None else "N/A", 'price_percentile'),
                        ("当日涨跌幅", _fmt(inds.get("pct_chg"), "{:.2f}%"), None,
                         "60日均线",  _fmt(inds.get("ma60"), "{:.3f}"), None),
                    ]
                    # 最后一行只有左侧指标（120日均线不可点击）
                    last_row = ("120日均线", _fmt(inds.get("ma120"), "{:.3f}"), None, None, None, None)
                else:
                    metric_rows = [
                        ("当前价格",  _fmt(inds.get("price"), "{:.3f}"), 'close_price',
                         "PB 历史分位", f"{inds['pb_percentile'] * 100:.1f}%" if inds.get("pb_percentile") is not None else "N/A", 'pb_percentile'),
                        ("市盈率 (PE)", _fmt(inds.get("pe")), 'pe',
                         "风险溢价",    _fmt(inds.get("risk_premium"), "{:.4f}"), 'risk_premium'),
                        ("PE 历史分位", f"{inds['pe_percentile'] * 100:.1f}%" if inds.get("pe_percentile") is not None else "N/A", None,
                         "60日均线",    _fmt(inds.get("ma60"), "{:.3f}"), None),
                        ("市净率 (PB)", _fmt(inds.get("pb")), 'pb',
                         "120日均线",   _fmt(inds.get("ma120"), "{:.3f}"), None),
                        ("ROE",         f"{inds['roe'] * 100:.2f}%" if inds.get("roe") is not None else "N/A", 'roe',
                         "成交额", _fmt(inds.get("amount"), "{:.0f}"), 'amount'),
                    ]
                    # 最后一行只有右侧指标（20日均成交额不可点击）
                    last_row = (None, None, None, "20日均成交额", _fmt(inds.get("amount_ma20"), "{:.0f}"), None)

                for label_a, val_a, key_a, label_b, val_b, key_b in metric_rows + [last_row]:
                    mc_a, mb_a, mc_b, mb_b = st.columns([0.37, 0.13, 0.37, 0.13])
                    if label_a:
                        with mc_a:
                            st.metric(label_a, val_a)
                        with mb_a:
                            if key_a:
                                formula = TREND_INDICATORS.get(key_a, (None,None,None,""))[3]
                                with st.popover("📈", help=f"查看 {label_a} 历史趋势\n\n{formula}"):
                                    render_trend_chart(code, key_a, label_a)
                    if label_b:
                        with mc_b:
                            st.metric(label_b, val_b)
                        with mb_b:
                            if key_b:
                                formula = TREND_INDICATORS.get(key_b, (None,None,None,""))[3]
                                with st.popover("📈", help=f"查看 {label_b} 历史趋势\n\n{formula}"):
                                    render_trend_chart(code, key_b, label_b)

                # 技术因子入口（idx_factor_pro 专业版数据，弹窗展示最新交易日全部因子）
                # 国际指数无技术因子数据，不展示入口
                if card.get('category') != 'global':
                    with st.popover("🔬 技术因子", use_container_width=True,
                                    help="查看该指数最新交易日的全部技术面因子（MACD/KDJ/RSI/BOLL 等）"):
                        render_factor_panel(code)

            # ===== 右列：操作建议 → 分析 → 置信度 =====
            with col_right:
                ai_data = card.get('ai_report', {})
                advice = ai_data.get('advice', '持有/观望') if isinstance(ai_data, dict) else '持有/观望'
                confidence_val = card.get('confidence', 0)
                analysis_text = ai_data.get('analysis', '') if isinstance(ai_data, dict) else str(ai_data) if ai_data else ''

                # 操作建议
                st.subheader("🎯 操作建议", anchor=False)
                style = ACTION_STYLES.get(advice, ACTION_STYLES["持有/观望"])
                st.markdown(f"""
                <div style="
                    display:inline-block;
                    padding:6px 20px;
                    border-radius:20px;
                    font-size:1.1rem;
                    font-weight:700;
                    color:{style['color']};
                    background:{style['bg']};
                    border:1.5px solid {style['border']};
                    margin-bottom:12px;
                ">{style['icon']} {style['label']}</div>
                """, unsafe_allow_html=True)

                # AI 分析
                st.subheader("📝 AI 分析", anchor=False)
                if analysis_text:
                    clean = re.sub(r'^#{1,6}\s+(.+)$', r'**\1**', analysis_text, flags=re.MULTILINE)
                    st.info(clean)
                else:
                    st.caption("- 无 AI 报告")

                # 置信度（小字，不占主视觉）
                st.html("<h5 style='color:#6b7280; margin:16px 0 4px 0;'>置信度</h5>")
                if confidence_val is not None:
                    st.progress(confidence_val / 100.0, text=f"{confidence_val}%")
                else:
                    st.caption("N/A")

# --- 侧边栏：当前用户 ---
st.sidebar.markdown(f"👤 当前用户：**{username}**")
if st.sidebar.button("退出登录", use_container_width=True):
    _clear_inspection_state()
    st.session_state.pop("username", None)
    st.rerun()
st.sidebar.divider()

# --- 侧边栏：巡检进行中提示 ---
if st.session_state.get("inspection_active", False):
    idx = st.session_state.get("inspection_index", 0)
    total = st.session_state.get("inspection_total", 0)
    st.sidebar.warning(f"⏳ 巡检进行中 ({idx}/{total})")

# --- 侧边栏：基金池管理 ---
st.sidebar.header("⚙️ 监控池管理")
st.sidebar.subheader("添加监控标的")
st.sidebar.caption("数据来源：Tushare（申万行业指数 + 国际指数）")

# 选择模式：搜索 / 分类浏览 / 国际指数
select_mode = st.sidebar.radio("选择方式", ["搜索", "分类浏览", "国际指数"], horizontal=True, label_visibility="collapsed")

selected_industry = None

if select_mode == "搜索":
    # 模糊搜索输入
    search_keyword = st.sidebar.text_input("搜索行业", placeholder="输入行业名称或代码...")
    
    # 根据搜索词过滤行业列表
    if search_keyword:
        filtered_options = [
            opt for opt in industry_options 
            if search_keyword.lower() in opt['industry_name'].lower() 
            or search_keyword.lower() in opt['index_code'].lower()
        ]
    else:
        filtered_options = []
    
    if filtered_options:
        labels = [opt['label'] for opt in filtered_options]
        selected_idx = st.sidebar.selectbox(
            "搜索结果",
            range(len(filtered_options)),
            format_func=lambda x: labels[x]
        )
        selected_industry = filtered_options[selected_idx]
    elif search_keyword:
        st.sidebar.warning("未找到匹配的行业，请尝试其他关键词")
    else:
        st.sidebar.caption("请输入行业名称或代码进行搜索")

elif select_mode == "国际指数":
    # 静态 21 个国际指数（GLOBAL_INDEX_MAP），支持关键词过滤
    search_keyword = st.sidebar.text_input("搜索国际指数", placeholder="输入名称或代码，如 恒生 / HSI...")

    if search_keyword:
        filtered_global = [
            (c, n) for c, n in GLOBAL_INDEX_MAP.items()
            if search_keyword.lower() in n.lower() or search_keyword.lower() in c.lower()
        ]
    else:
        filtered_global = list(GLOBAL_INDEX_MAP.items())

    if filtered_global:
        global_labels = [f"{n} ({c})" for c, n in filtered_global]
        selected_idx = st.sidebar.selectbox(
            "国际指数列表",
            range(len(filtered_global)),
            format_func=lambda x: global_labels[x]
        )
        g_code, g_name = filtered_global[selected_idx]
        selected_industry = {
            'index_code': g_code,
            'industry_name': g_name,
            'level': 'GLOBAL',
            'is_global': True,
        }
    else:
        st.sidebar.warning("未找到匹配的国际指数，请尝试其他关键词")

else:
    # 分类浏览：L1 → L2 → L3 级联选择
    # 构建层级树
    l1_list = [ind for ind in industries if ind['level'] == 'L1']
    
    if l1_list:
        l1_options = {ind['industry_name']: ind for ind in l1_list}
        selected_l1_name = st.sidebar.selectbox("一级行业", list(l1_options.keys()))
        selected_l1 = l1_options[selected_l1_name]
        
        # 查找该 L1 下的 L2 行业
        l2_list = [
            ind for ind in industries 
            if ind['level'] == 'L2' 
            and (ind.get('parent_code') == selected_l1.get('industry_code') 
                 or ind.get('parent_code') == selected_l1.get('index_code'))
        ]
        
        if l2_list:
            l2_options = {ind['industry_name']: ind for ind in l2_list}
            selected_l2_name = st.sidebar.selectbox("二级行业", list(l2_options.keys()))
            selected_l2 = l2_options[selected_l2_name]
            
            # 查找该 L2 下的 L3 行业
            l3_list = [
                ind for ind in industries 
                if ind['level'] == 'L3' 
                and (ind.get('parent_code') == selected_l2.get('industry_code') 
                     or ind.get('parent_code') == selected_l2.get('index_code'))
            ]
            
            if l3_list:
                l3_options = {ind['industry_name']: ind for ind in l3_list}
                selected_l3_name = st.sidebar.selectbox("三级行业", list(l3_options.keys()))
                selected_industry = {
                    'index_code': l3_options[selected_l3_name]['index_code'],
                    'industry_name': l3_options[selected_l3_name]['industry_name'],
                    'level': 'L3'
                }
            else:
                # L2 下面没有 L3，直接使用 L2
                selected_industry = {
                    'index_code': selected_l2['index_code'],
                    'industry_name': selected_l2['industry_name'],
                    'level': 'L2'
                }
        else:
            # L1 下面没有 L2，直接使用 L1
            selected_industry = {
                'index_code': selected_l1['index_code'],
                'industry_name': selected_l1['industry_name'],
                'level': 'L1'
            }
    else:
        st.sidebar.warning("行业分类数据为空")

if selected_industry:
    new_code = selected_industry['index_code']
    new_name = selected_industry['industry_name']
    level = selected_industry.get('level', 'L1')
    is_global_sel = selected_industry.get('is_global', False)

    if is_global_sel:
        # 国际指数：固定 global 分类（无估值数据，现有 4 类策略不适用）；
        # 无成分查询（index_member_all 仅支持申万行业指数）
        new_cat_key = 'global'
        st.sidebar.info(f"已选择: {new_name} ({new_code})\n类型: 国际指数")
        add_clicked = st.sidebar.button("添加监控", type="primary", use_container_width=True)
    else:
        # 策略分类选择（根据行业层级给出默认建议）
        category_options = [
            ("wide_base", "宽基指数 (沪深300/标普500)"),
            ("tech_growth", "科技成长 (计算机/半导体)"),
            ("cycle_mfg", "周期制造 (新能源/煤炭)"),
            ("dividend", "稳健收息 (红利低波)")
        ]
        new_cat_key = st.sidebar.selectbox("选择所属领域", category_options, format_func=lambda x: x[1])[0]

        # 显示选中的行业信息
        st.sidebar.info(f"已选择: {new_name} ({new_code})\n层级: {level}")

        col_add, col_members = st.sidebar.columns(2)
        with col_add:
            add_clicked = st.sidebar.button("添加监控", type="primary", use_container_width=True)
        with col_members:
            # 成分数据变化频繁，点击后弹窗实时拉取，不落库
            if st.sidebar.button("成分查询", use_container_width=True):
                show_index_members(new_code, new_name, level)

    if add_clicked:
        # category 为指数级全局属性：他人已监控时强制沿用已有分类，
        # 避免同一指数多策略框架导致共享巡检结果互相覆写
        chosen_cat = new_cat_key
        shared_cat = get_shared_category(new_code)
        if shared_cat is not None and shared_cat != chosen_cat:
            st.sidebar.info(
                f"该指数已被其他用户监控，策略分类沿用现有配置："
                f"{CATEGORY_NAMES.get(shared_cat, shared_cat)}"
            )
            chosen_cat = shared_cat
        added = add_fund(username, new_code, new_name, chosen_cat)
        if not added:
            st.sidebar.warning(f"{new_name} 已在监控池中，无需重复添加")
            st.rerun()
        else:
            st.sidebar.success(f"已添加 {new_name}")
            # 同步历史数据（行情全局共享：他人已同步过则复用，并补齐增量防止陈旧）
            if get_latest_trade_date(new_code) is not None:
                with st.spinner('正在校验共享行情数据完整性...'):
                    inc_ok, inc_msg = sync_incremental(new_code)
                if inc_ok:
                    st.sidebar.success(f"行情数据已就绪（共享）：{inc_msg}")
                else:
                    st.sidebar.warning(f"行情数据已就绪（共享），但增量补齐失败：{inc_msg}")
            else:
                with st.spinner(f'正在拉取 {new_code} 的近10年历史数据...'):
                    success, msg = sync_all_history(new_code)
                if success:
                    st.sidebar.success(msg)
                else:
                    st.sidebar.error(f"数据同步失败：{msg}")
            st.rerun()

st.sidebar.divider()
st.sidebar.subheader("当前监控列表")
funds = get_all_funds(username)

if funds:
    df_funds = pd.DataFrame(funds)
    # 转换为中文表头和分类名称
    df_display = df_funds[['fund_code', 'fund_name', 'category']].copy()
    df_display.columns = ['代码', '名称', '领域']
    df_display['领域'] = df_display['领域'].map(CATEGORY_NAMES)
    st.sidebar.dataframe(df_display, hide_index=True)
    
    # 删除功能：显示名称，但用代码删除
    fund_name_to_code = {f['fund_name']: f['fund_code'] for f in funds}
    selected_name = st.sidebar.selectbox("选择要删除的指数", list(fund_name_to_code.keys()))
    if st.sidebar.button("删除所选指数"):
        del_code = fund_name_to_code[selected_name]
        remove_fund(username, del_code)
        st.sidebar.warning(f"已删除 {selected_name}")
        st.rerun()
else:
    st.sidebar.info("监控池为空，请先添加基金。")

# --- 侧边栏：定时巡检自动执行 ---
st.sidebar.divider()
st.sidebar.header("⏰ 定时巡检")

auto_enabled = get_setting("scheduler_auto_enabled", "true")
auto_current = auto_enabled == "true"
auto_toggle = st.sidebar.toggle(
    "启用定时自动巡检",
    value=auto_current,
    help="按配置的 Cron 表达式自动执行巡检并通过 webhook 推送结果",
)
if auto_toggle != auto_current:
    set_setting("scheduler_auto_enabled", "true" if auto_toggle else "false")
    if auto_toggle:
        start_scheduler()
    else:
        stop_scheduler()
    st.rerun()

# Cron 调度表达式配置（全局）
cron_current = get_setting("scheduler_cron_expr", "30 19 * * 1-5")
with st.sidebar.form("cron_form", clear_on_submit=False):
    cron_input = st.text_input(
        "调度时间（标准 5 字段 Cron）",
        value=cron_current,
        key="cron_expr_input",
        help="格式：分 时 日 月 周。默认 30 19 * * 1-5 = 工作日 19:30；周字段 0=周一…6=周日（如周二、周四=1,3），也可用英文缩写 tue,thu。执行前仍会二次校验交易日",
    )
    if st.form_submit_button("保存调度时间"):
        ok, msg = update_cron_expression(cron_input)
        if ok:
            st.sidebar.success(msg)
        else:
            st.sidebar.error(msg)
            st.session_state["cron_expr_input"] = cron_current   # 非法时不生效，回显旧值
        st.rerun()

if is_scheduler_running():
    next_time = get_next_run_time()
    if next_time:
        st.sidebar.caption(f"下一次执行: {next_time}")
    st.sidebar.caption(f"调度表达式: {cron_current}")
else:
    st.sidebar.caption("调度器未运行")

# --- 侧边栏：消息推送设置 ---
st.sidebar.divider()
st.sidebar.header("📨 消息推送设置")

# 巡检推送开关（按用户隔离）
webhook_enabled = get_user_setting(username, "webhook_inspection_enabled", "false")
current_enabled = webhook_enabled == "true"
new_enabled = st.sidebar.toggle(
    "巡检时发送消息推送",
    value=current_enabled,
    help="开启后，每次巡检将为每个指数向你配置的 webhook 地址发送卡片消息",
)
if new_enabled != current_enabled:
    set_user_setting(username, "webhook_inspection_enabled", "true" if new_enabled else "false")
    st.rerun()

# 强信号过滤开关（按用户隔离；仅影响推送，巡检结果一律照常入库）
strong_only = get_user_setting(username, "webhook_strong_signal_only", "true")
current_strong = strong_only == "true"
new_strong = st.sidebar.toggle(
    "仅推送买入/卖出建议",
    value=current_strong,
    help="开启后，仅当 AI 建议为「买入」或「卖出」时推送卡片；「持有/观望」「数据异常」仍照常入库但不推送",
)
if new_strong != current_strong:
    set_user_setting(username, "webhook_strong_signal_only", "true" if new_strong else "false")
    st.rerun()

# Webhook 地址管理
st.sidebar.caption("Webhook 地址管理（飞书自定义机器人）")

with st.sidebar.form("add_webhook_form", clear_on_submit=True):
    new_url = st.text_input("Webhook URL", placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/...", key="wh_url")
    new_label = st.text_input("备注标签（选填）", placeholder="例如：公司群机器人", key="wh_label")
    submitted = st.form_submit_button("添加 Webhook", type="primary")
    if submitted:
        if new_url.strip():
            added = add_webhook_url(username, new_url.strip(), new_label.strip())
            if added:
                st.sidebar.success("已添加 Webhook 地址")
            else:
                st.sidebar.warning("该 Webhook 地址已存在，无需重复添加")
            st.rerun()
        else:
            st.sidebar.warning("请输入 Webhook URL")

# 已保存的 webhook 列表（仅当前用户）
webhook_urls = get_webhook_urls(username)
if webhook_urls:
    for wh in webhook_urls:
        wh_label = wh.get("label", "") or "未命名"
        wh_url = wh.get("url", "")
        # 截断显示 URL
        display_url = wh_url[:50] + "..." if len(wh_url) > 50 else wh_url
        col1, col2 = st.sidebar.columns([5, 1])
        with col1:
            st.caption(f"📎 {wh_label}")
            st.caption(f"{display_url}")
        with col2:
            if st.button("🗑️", key=f"del_wh_{wh['id']}", help="删除此 Webhook"):
                remove_webhook_url(username, wh["id"])
                st.rerun()
else:
    st.sidebar.caption("暂无 Webhook 地址，请添加")

st.sidebar.caption("当前版本 V0.3")

# --- 主页面：巡检核心工作流 ---
logo_svg = LOGO_PATH.read_text(encoding="utf-8")

st.markdown(f"""
<div class="clawindex-header">
    <div class="clawindex-logo-wrap">{logo_svg}</div>
    <div class="clawindex-title-block">
        <h1>ClawIndex 爪析</h1>
        <div class="subtitle-en">Quant · Claw · Insight</div>
    </div>
</div>
""", unsafe_allow_html=True)
st.markdown("策略不动摇，AI助决断。")

tab1, tab2, tab3 = st.tabs(["研判", "历史分析", "提示词配置"])

with tab1:
    # --- 巡检按钮（仅非活跃状态显示）---
    if not st.session_state.get("inspection_active", False):
        if st.button("🚀 运行今日行情研判", type="primary", use_container_width=True):
            if not funds:
                st.warning("监控池为空，请先在左侧添加。")
            else:
                st.session_state.inspection_active = True
                st.session_state.inspection_fund_list = list(funds)  # 快照当前监控列表
                st.session_state.inspection_index = 0
                st.session_state.inspection_total = len(funds)
                st.session_state.inspection_cards = []
                st.rerun()

    # ============================================================
    # 巡检状态机：利用 session_state 跨 rerun 持久化进度
    # 每次脚本执行只处理一个标的，然后 st.rerun() 推进到下一个
    # 这样即使侧边栏交互触发 rerun，巡检也不会丢失
    # ============================================================
    if st.session_state.get("inspection_active", False):
        funds_list = st.session_state.inspection_fund_list
        idx = st.session_state.inspection_index
        total = st.session_state.inspection_total
        processed_cards = st.session_state.get("inspection_cards", [])
        # 防重入：独立集合在「处理开始前」就写入，防止侧边栏交互触发 rerun 导致重复
        processed_codes = st.session_state.get("_processed_codes", set())

        st.header("📊 巡检报告", anchor=False)
        progress_bar = st.progress(idx / max(total, 1))
        progress_text = st.caption(f"已分析 {idx}/{total}")

        # 重渲染已完成的卡片
        for card in processed_cards:
            render_card_header(card)
            render_card_expander(card, expanded=True)

        # 处理当前标的
        if idx < total:
            fund = funds_list[idx]
            code = fund['fund_code']
            name = fund['fund_name']
            cat = fund['category']

            # 防重入：若标记 + 卡片均已写入，说明本轮完整结束，跳过
            if code in processed_codes:
                if any(c['code'] == code for c in processed_cards):
                    # 卡片已存 → 确实已完成，推进到下一标的
                    st.session_state.inspection_index = idx + 1
                    if idx + 1 >= total:
                        st.session_state.inspection_active = False
                        st.success("🎉 所有指数巡检完毕！")
                        st.rerun()
                    else:
                        st.rerun()
                else:
                    # 标记在但卡片不在 → 上一轮被打断，清标记重新处理
                    processed_codes.discard(code)
                    st.session_state._processed_codes = processed_codes

            # 立刻打标记，后续任何 rerun 都不会再进入本轮
            processed_codes.add(code)
            st.session_state._processed_codes = processed_codes

            result = run_single_inspection(code, name, cat, username)
            fund_data = result["fund_data"]
            ai_result = result["ai_report"]  # dict: {analysis, advice, confidence[, ai_error]}

            # 确定视觉风格（AI 调用失败时同样降级为异常样式，且不推送）
            has_error = 'error' in fund_data
            ai_error = ai_result.get('ai_error', False) if isinstance(ai_result, dict) else False
            action = fund_data.get('decision', {}).get('action', '持有/观望')
            confidence = ai_result.get('confidence', 0) if isinstance(ai_result, dict) else 0
            if has_error or ai_error:
                style = ACTION_STYLES["数据异常"]
            else:
                style = ACTION_STYLES.get(action, ACTION_STYLES["持有/观望"])

            # Webhook 推送（仅当前用户的开关与地址；AI 调用失败不推送；强信号过滤由用户开关控制）
            if new_enabled and not ai_error and (not new_strong or is_action_pushable(fund_data)):
                wh_urls = get_webhook_urls(username)
                if wh_urls:
                    send_to_all_webhooks(wh_urls, fund_data)

            # 构建渲染数据 & 渲染当前卡片
            today_str = datetime.now().strftime('%Y-%m-%d')
            current_card = {
                'code': code,
                'name': name,
                'date': today_str,
                'category': cat,
                'icon': style['icon'],
                'color': style['color'],
                'border': style['border'],
                'bg': style['bg'],
                'label': style['label'],
                'has_error': has_error or ai_error,
                'error_msg': fund_data.get('error', '') if has_error else (ai_result.get('analysis', 'AI 调用失败') if ai_error else ''),
                'indicators': fund_data.get('indicators', {}),
                'details': fund_data.get('decision', {}).get('details', []),
                'ai_report': ai_result,
                'confidence': confidence,
            }
            render_card_header(current_card)
            render_card_expander(current_card, expanded=True)

            # 8. 存入已处理列表
            processed_cards.append(current_card)
            st.session_state.inspection_cards = processed_cards
            st.session_state.inspection_index = idx + 1

            # 刷新进度条，反映刚刚完成的标的
            progress_bar.progress((idx + 1) / max(total, 1))
            progress_text.caption(f"已分析 {idx + 1}/{total}")

            if idx + 1 >= total:
                # 全部完成
                st.session_state.inspection_active = False
                if "_processed_codes" in st.session_state:
                    del st.session_state._processed_codes
                st.success("🎉 所有指数巡检完毕！")
                st.rerun()
            else:
                st.rerun()

    # 巡检完成后展示结果（非活跃状态但 inspection_cards 有数据）
    else:
        completed_cards = st.session_state.get("inspection_cards", [])
        if completed_cards:
            st.header("📊 巡检报告", anchor=False)
            st.success("🎉 所有指数巡检完毕！")
            for card in completed_cards:
                render_card_header(card)
                render_card_expander(card, expanded=True)

with tab2:
    st.subheader("📋 历史巡检记录", anchor=False)
    
    # 获取当前用户的历史记录
    all_records = get_inspection_history(username=username)
    
    if not all_records:
        st.info("暂无巡检记录，请先在「巡检」标签页运行一次分析。")
    else:
        # 提取筛选选项
        fund_names = sorted(set(r['fund_name'] for r in all_records))
        actions_list = ["买入", "卖出", "持有/观望", "数据异常"]
        action_labels = {"买入": "建议买入", "卖出": "建议卖出", "持有/观望": "持有观望", "数据异常": "数据异常"}
        date_range = [r['inspect_date'] for r in all_records]
        min_date = min(date_range) if date_range else datetime.now().strftime('%Y-%m-%d')
        max_date = max(date_range) if date_range else datetime.now().strftime('%Y-%m-%d')
        
        # --- 筛选栏 ---
        st.markdown("##### 筛选条件")
        col_f1, col_f2, col_f3 = st.columns([2, 2, 2])
        
        with col_f1:
            selected_fund = st.selectbox("指数", ["全部"] + fund_names)
        with col_f2:
            selected_actions = st.multiselect(
                "操作建议",
                actions_list,
                default=actions_list,
                format_func=lambda x: action_labels.get(x, x)
            )
        with col_f3:
            date_from, date_to = st.columns(2)
            with date_from:
                start_date = st.text_input("开始日期", value=min_date, placeholder="YYYY-MM-DD")
            with date_to:
                end_date = st.text_input("结束日期", value=max_date, placeholder="YYYY-MM-DD")
        
        # --- 根据筛选条件过滤 ---
        filtered_records = []
        for r in all_records:
            if selected_fund != "全部" and r['fund_name'] != selected_fund:
                continue
            if selected_actions and r['action'] not in selected_actions:
                continue
            if start_date and r['inspect_date'] < start_date:
                continue
            if end_date and r['inspect_date'] > end_date:
                continue
            filtered_records.append(r)
        
        st.caption(f"共 {len(filtered_records)} 条记录")
        
        # --- 卡片列表 ---
        if not filtered_records:
            st.info("筛选条件无匹配记录。")
        else:
            for record in filtered_records:
                act = record.get('action', '持有/观望')
                s = ACTION_STYLES.get(act, ACTION_STYLES["持有/观望"])
                
                # 标题卡
                st.markdown(f"""
                <div style="
                    border-left: 5px solid {s['border']};
                    background: {s['bg']};
                    border-radius: 0 12px 12px 0;
                    padding: 12px 20px;
                    margin: 10px 0 4px 0;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.04);
                ">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span style="font-size:1.4rem;">{s['icon']}</span>
                        <span style="font-size:1.1rem; font-weight:700; color:{s['color']};">{record['fund_name']} · {s['label']}</span>
                        <span style="font-size:0.85rem; color:#6b7280; margin-left:auto;">{record['inspect_date']}</span>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                
                # 展开详情
                with st.expander(f"查看详情 - {record['fund_name']} ({record['inspect_date']})", expanded=True):
                    # 国际指数按代码表判断市场类型（巡检记录表无 category 列，兼容旧记录）
                    is_global_rec = is_global_code(record['fund_code'])

                    # 展示行情数据（来自 daily_market_data，优先匹配巡检日期，失败回退到最新可用数据）
                    market_data = get_market_data_for_date(record['fund_code'], record['inspect_date'])
                    fallback_used = False
                    if not market_data:
                        market_data = get_latest_market_data(record['fund_code'])
                        fallback_used = True
                    
                    if market_data:
                        if fallback_used:
                            actual_date = market_data.get('trade_date', '?')
                            st.caption(f"⚠️ 巡检日期 ({record['inspect_date']}) 无行情数据，展示最新可用数据 (交易日: {actual_date})")
                        if is_global_rec:
                            # 国际指数无估值数据：展示价格与涨跌幅
                            pct = market_data.get('pct_change')
                            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
                            col_m1.metric("收盘价", f"{market_data.get('close_price', 'N/A')}")
                            col_m2.metric("当日涨跌幅", f"{pct:.2f}%" if pct is not None else "N/A")
                            col_m3.metric("最高价", f"{market_data.get('high_price', 'N/A')}")
                            col_m4.metric("最低价", f"{market_data.get('low_price', 'N/A')}")
                        else:
                            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
                            col_m1.metric("收盘价", f"{market_data.get('close_price', 'N/A')}")
                            col_m2.metric("PE", f"{market_data.get('pe', 'N/A')}")
                            col_m3.metric("PB", f"{market_data.get('pb', 'N/A')}")
                            col_m4.metric("无风险利率", f"{market_data.get('risk_free_rate', 'N/A')}")
                    else:
                        if is_global_rec:
                            st.caption("该指数暂无任何行情数据（可能尚未同步）")
                        else:
                            is_open, _ = is_trade_day(record['inspect_date'])
                            if not is_open:
                                st.caption("当日为非交易日（周末或节假日），无行情数据")
                            else:
                                st.caption("该指数暂无任何行情数据（可能尚未同步）")

                    # 展示计算指标（来自 inspection_log，巡检时实时计算并入库）
                    st.markdown("**📐 计算指标**")
                    pe_pct = record.get('pe_percentile')
                    pb_pct = record.get('pb_percentile')
                    ma60 = record.get('ma60')
                    ma120 = record.get('ma120')
                    has_calc = any(v is not None for v in [pe_pct, pb_pct, ma60, ma120])
                    if has_calc:
                        ma60_str = f"{ma60:.3f}" if ma60 is not None else "N/A"
                        ma120_str = f"{ma120:.3f}" if ma120 is not None else "N/A"
                        if is_global_rec:
                            # 国际指数无 PE/PB 分位，仅展示均线
                            col_c1, col_c2 = st.columns(2)
                            col_c1.metric("60日均线", ma60_str)
                            col_c2.metric("120日均线", ma120_str)
                        else:
                            col_c1, col_c2, col_c3, col_c4 = st.columns(4)
                            pe_pct_str = f"{pe_pct * 100:.1f}%" if pe_pct is not None else "N/A"
                            pb_pct_str = f"{pb_pct * 100:.1f}%" if pb_pct is not None else "N/A"
                            col_c1.metric("PE 历史分位", pe_pct_str)
                            col_c2.metric("PB 历史分位", pb_pct_str)
                            col_c3.metric("60日均线", ma60_str)
                            col_c4.metric("120日均线", ma120_str)
                    else:
                        st.caption("该记录为旧版数据，无计算指标（请重新巡检以生成）")
                    
                    # AI 报告
                    st.markdown("**AI 投顾解读**")
                    ai_text = record.get('ai_report', '')
                    if ai_text:
                        clean_report = re.sub(r'^#{1,6}\s+(.+)$', r'**\1**', ai_text, flags=re.MULTILINE)
                        st.info(clean_report)
                    else:
                        st.caption("- 无 AI 报告")

with tab3:
    st.subheader("📝 定制 AI 分析提示词", anchor=False)
    st.caption("为每个监控指数编写你的专属分析框架（仅对当前账号生效），AI 将按你的提示词解读估值数据。留空则使用系统默认策略。")

    if not funds:
        st.info("监控池为空，请先在左侧「监控池管理」添加指数。")
    else:
        # 显示保存成功提示（跨 rerun 持久化）
        saved_msg = st.session_state.pop("prompt_saved_msg", None)
        if saved_msg:
            st.success(f"💾 {saved_msg}")

        # 构建指数选择列表
        fund_options = {f"{f['fund_name']} ({f['fund_code']})": f for f in funds}
        selected_label = st.selectbox(
            "选择要配置的指数",
            list(fund_options.keys()),
            key="prompt_fund_selector"
        )

        if selected_label:
            fund = fund_options[selected_label]
            code = fund['fund_code']
            name = fund['fund_name']
            cat = fund['category']

            # 按分类选择指标分组与默认勾选（国际指数无估值指标，仅价格与趋势类）
            is_global_fund = cat == 'global'
            prompt_groups = GLOBAL_INDICATOR_GROUPS if is_global_fund else INDICATOR_GROUPS
            all_meta_keys = [key for _, keys in prompt_groups for key in keys]

            # 当前配置状态（仅当前用户）
            current_prompt = get_custom_prompt(username, code)
            if current_prompt:
                st.success(f"✅ **{name}** 已配置定制提示词（仅本账号）")
            else:
                st.info(f"⚪ **{name}** 使用系统默认策略（分类: {CATEGORY_NAMES.get(cat, cat)}）")

            # 提示词编辑器
            prompt_text = st.text_area(
                "提示词内容",
                value=current_prompt or "",
                height=280,
                max_chars=8000,
                placeholder="在此输入专属分析框架，例如：\n该指数属于消费行业，侧重分析 ROE 稳定性和现金流质量...\n\n留空则使用系统默认策略。",
                key=f"prompt_editor_{username}_{code}",
                help="提示词将替换 AI 分析中的「估值解读框架」段落。系统红线（只能基于数据解读、不做预测、不改变决策）始终生效。"
            )

            # 指标勾选
            st.markdown("**📊 传递给 AI 的指标**")
            if is_global_fund:
                st.caption("勾选需要的指标，AI 将只看到选中项。国际指数默认传递价格、价格历史分位、均线与当日涨跌幅。")
            else:
                st.caption("勾选需要的指标，AI 将只看到选中项。默认仅选 PE、PB。")

            saved_indicators = get_selected_indicators(username, code)
            default_checked = saved_indicators if saved_indicators else default_indicators_for(cat)

            if st.button("☑️ 全选", key=f"select_all_{username}_{code}", use_container_width=True):
                for key in all_meta_keys:
                    st.session_state[f"ind_{username}_{code}_{key}"] = True
                st.rerun()

            # 按分组展示指标 checkboxes
            selected_keys = []
            for group_label, group_keys in prompt_groups:
                cols = st.columns(len(group_keys))
                for i, key in enumerate(group_keys):
                    label = INDICATOR_META[key][0]
                    checked = cols[i].checkbox(
                        label,
                        value=(key in default_checked),
                        key=f"ind_{username}_{code}_{key}",
                    )
                    if checked:
                        selected_keys.append(key)

            # 如果没有勾选任何指标，默认全部（按分类清单）
            if not selected_keys:
                selected_keys = all_meta_keys

            col_btn1, col_btn2 = st.columns([1, 1])
            with col_btn1:
                if st.button("💾 保存提示词", key=f"save_prompt_{username}_{code}", type="primary", use_container_width=True):
                    if prompt_text.strip():
                        upsert_custom_prompt(username, code, prompt_text.strip(), indicators=selected_keys)
                        st.session_state.prompt_saved_msg = f"已保存 {name} 的定制提示词（{len(selected_keys)} 项指标）"
                        st.rerun()
                    else:
                        st.warning("提示词内容为空，请先编写后再保存。如需恢复默认，请点击「重置为默认」。")

            with col_btn2:
                if current_prompt:
                    if st.button("🗑️ 重置为默认", key=f"reset_prompt_{username}_{code}", use_container_width=True):
                        delete_custom_prompt(username, code)
                        st.warning(f"已清除 {name} 的定制提示词，恢复系统默认策略")
                        st.rerun()

            # 使用说明
            st.divider()
            st.markdown("""
            **📋 使用提示**
            - 提示词将**替换**系统默认的「第二步：分类定价逻辑」段落，第一/三/四步保持不变
            - 系统**绝对红线**（只能基于量化数据解读、语气冷静客观、不改变决策动作）始终生效，不可覆盖
            - 建议关注：该指数的估值锚点（PE/PB/ROE）、行业特性、需要特别注意的风险维度
            - 最多 8000 字符，保存后下次巡检自动生效
            """)