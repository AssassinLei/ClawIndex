from dotenv import load_dotenv
load_dotenv()

import streamlit as st
import pandas as pd
import re
from pathlib import Path
from database import init_db, add_fund, remove_fund, get_all_funds, get_all_industries, get_industry_count, get_inspection_history, get_market_data_for_date, get_latest_market_data, get_webhook_urls, add_webhook_url, remove_webhook_url, get_setting, set_setting, get_custom_prompt, get_selected_indicators, upsert_custom_prompt, delete_custom_prompt
from data_fetcher import sync_all_history, sync_incremental, fetch_industry_classify, is_trade_day
from llm_agent import INDICATOR_META, INDICATOR_GROUPS
from webhook_sender import send_to_all_webhooks
from scheduler import start_scheduler, stop_scheduler, get_next_run_time, is_scheduler_running
from constants import CATEGORY_NAMES
from inspection_pipeline import run_single_inspection
from datetime import datetime

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
auto_enabled_init = get_setting("scheduler_auto_enabled", "true")
if auto_enabled_init == "true":
    start_scheduler()

# 初始化行业分类数据（如果数据库为空则自动拉取）
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

                # 用嵌套 2 列紧凑排列
                c1, c2 = st.columns(2)
                with c1:
                    st.metric("当前价格",  _fmt(inds.get("price"), "{:.3f}"))
                    st.metric("市盈率 (PE)", _fmt(inds.get("pe")))
                    st.metric("PE 历史分位", f"{inds['pe_percentile'] * 100:.1f}%" if inds.get("pe_percentile") is not None else "N/A")
                    st.metric("市净率 (PB)", _fmt(inds.get("pb")))
                    st.metric("ROE",         f"{inds['roe'] * 100:.2f}%" if inds.get("roe") is not None else "N/A")
                with c2:
                    st.metric("PB 历史分位", f"{inds['pb_percentile'] * 100:.1f}%" if inds.get("pb_percentile") is not None else "N/A")
                    st.metric("风险溢价",    _fmt(inds.get("risk_premium"), "{:.4f}"))
                    st.metric("60日均线",    _fmt(inds.get("ma60"), "{:.3f}"))
                    st.metric("120日均线",   _fmt(inds.get("ma120"), "{:.3f}"))
                    st.metric("成交额 (万元)", _fmt(inds.get("amount"), "{:.0f}"))
                    st.metric("20日均成交额", _fmt(inds.get("amount_ma20"), "{:.0f}"))

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

# --- 侧边栏：巡检进行中提示 ---
if st.session_state.get("inspection_active", False):
    idx = st.session_state.get("inspection_index", 0)
    total = st.session_state.get("inspection_total", 0)
    st.sidebar.warning(f"⏳ 巡检进行中 ({idx}/{total})")

# --- 侧边栏：基金池管理 ---
st.sidebar.header("⚙️ 监控池管理")
st.sidebar.subheader("添加行业指数")
st.sidebar.caption("数据来源：中证申万证券行业指数")

# 选择模式：搜索 vs 分类浏览
select_mode = st.sidebar.radio("选择方式", ["搜索", "分类浏览"], horizontal=True, label_visibility="collapsed")

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
    
    # 策略分类选择（根据行业层级给出默认建议）
    level = selected_industry.get('level', 'L1')
    category_options = [
        ("wide_base", "宽基指数 (沪深300/标普500)"),
        ("tech_growth", "科技成长 (计算机/半导体)"),
        ("cycle_mfg", "周期制造 (新能源/煤炭)"),
        ("dividend", "稳健收息 (红利低波)")
    ]
    new_cat = st.sidebar.selectbox("选择所属领域", category_options, format_func=lambda x: x[1])
    
    # 显示选中的行业信息
    st.sidebar.info(f"已选择: {new_name} ({new_code})\n层级: {level}")
    
    if st.sidebar.button("添加监控", type="primary"):
        added = add_fund(new_code, new_name, new_cat[0])
        if not added:
            st.sidebar.warning(f"{new_name} 已在监控池中，无需重复添加")
            st.rerun()
        else:
            st.sidebar.success(f"已添加 {new_name}")
            # 同步历史数据
            with st.spinner(f'正在拉取 {new_code} 的近10年历史数据...'):
                success, msg = sync_all_history(new_code)
            if success:
                st.sidebar.success(msg)
            else:
                st.sidebar.error(f"数据同步失败：{msg}")
            st.rerun()

st.sidebar.divider()
st.sidebar.subheader("当前监控列表")
funds = get_all_funds()

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
        remove_fund(del_code)
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
    help="每个交易日 19:30 自动执行巡检并通过 webhook 推送结果",
)
if auto_toggle != auto_current:
    set_setting("scheduler_auto_enabled", "true" if auto_toggle else "false")
    if auto_toggle:
        start_scheduler()
    else:
        stop_scheduler()
    st.rerun()

if is_scheduler_running():
    next_time = get_next_run_time()
    if next_time:
        st.sidebar.caption(f"下一次执行: {next_time}")
    st.sidebar.caption("调度时段: 工作日 19:30")
else:
    st.sidebar.caption("调度器未运行")

# --- 侧边栏：消息推送设置 ---
st.sidebar.divider()
st.sidebar.header("📨 消息推送设置")

# 巡检推送开关
webhook_enabled = get_setting("webhook_inspection_enabled", "false")
current_enabled = webhook_enabled == "true"
new_enabled = st.sidebar.toggle(
    "巡检时发送消息推送",
    value=current_enabled,
    help="开启后，每次巡检将为每个指数向已配置的 webhook 地址发送卡片消息",
)
if new_enabled != current_enabled:
    set_setting("webhook_inspection_enabled", "true" if new_enabled else "false")
    st.rerun()

# Webhook 地址管理
st.sidebar.caption("Webhook 地址管理（飞书自定义机器人）")

with st.sidebar.form("add_webhook_form", clear_on_submit=True):
    new_url = st.text_input("Webhook URL", placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/...", key="wh_url")
    new_label = st.text_input("备注标签（选填）", placeholder="例如：公司群机器人", key="wh_label")
    submitted = st.form_submit_button("添加 Webhook", type="primary")
    if submitted:
        if new_url.strip():
            added = add_webhook_url(new_url.strip(), new_label.strip())
            if added:
                st.sidebar.success("已添加 Webhook 地址")
            else:
                st.sidebar.warning("该 Webhook 地址已存在，无需重复添加")
            st.rerun()
        else:
            st.sidebar.warning("请输入 Webhook URL")

# 已保存的 webhook 列表
webhook_urls = get_webhook_urls()
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
                remove_webhook_url(wh["id"])
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

            result = run_single_inspection(code, name, cat)
            fund_data = result["fund_data"]
            ai_result = result["ai_report"]  # dict: {analysis, advice, confidence}

            # 确定视觉风格
            has_error = 'error' in fund_data
            action = fund_data.get('decision', {}).get('action', '持有/观望')
            confidence = ai_result.get('confidence', 0) if isinstance(ai_result, dict) else 0
            if has_error:
                style = ACTION_STYLES["数据异常"]
            else:
                style = ACTION_STYLES.get(action, ACTION_STYLES["持有/观望"])

            # Webhook 推送
            if new_enabled:
                wh_urls = get_webhook_urls()
                if wh_urls:
                    send_to_all_webhooks(wh_urls, fund_data)

            # 构建渲染数据 & 渲染当前卡片
            today_str = datetime.now().strftime('%Y-%m-%d')
            current_card = {
                'code': code,
                'name': name,
                'date': today_str,
                'icon': style['icon'],
                'color': style['color'],
                'border': style['border'],
                'bg': style['bg'],
                'label': style['label'],
                'has_error': has_error,
                'error_msg': fund_data.get('error', '') if has_error else '',
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
    
    # 获取所有历史记录
    all_records = get_inspection_history()
    
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
                        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
                        col_m1.metric("收盘价", f"{market_data.get('close_price', 'N/A')}")
                        col_m2.metric("PE", f"{market_data.get('pe', 'N/A')}")
                        col_m3.metric("PB", f"{market_data.get('pb', 'N/A')}")
                        col_m4.metric("无风险利率", f"{market_data.get('risk_free_rate', 'N/A')}")
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
                        col_c1, col_c2, col_c3, col_c4 = st.columns(4)
                        pe_pct_str = f"{pe_pct * 100:.1f}%" if pe_pct is not None else "N/A"
                        pb_pct_str = f"{pb_pct * 100:.1f}%" if pb_pct is not None else "N/A"
                        ma60_str = f"{ma60:.3f}" if ma60 is not None else "N/A"
                        ma120_str = f"{ma120:.3f}" if ma120 is not None else "N/A"
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
    st.caption("为每个监控指数编写专属分析框架，AI 将按你的提示词解读估值数据。留空则使用系统默认策略。")

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

            # 当前配置状态
            current_prompt = get_custom_prompt(code)
            if current_prompt:
                st.success(f"✅ **{name}** 已配置定制提示词")
            else:
                st.info(f"⚪ **{name}** 使用系统默认策略（分类: {CATEGORY_NAMES.get(cat, cat)}）")

            # 提示词编辑器
            prompt_text = st.text_area(
                "提示词内容",
                value=current_prompt or "",
                height=280,
                max_chars=2000,
                placeholder="在此输入专属分析框架，例如：\n该指数属于消费行业，侧重分析 ROE 稳定性和现金流质量...\n\n留空则使用系统默认策略。",
                key=f"prompt_editor_{code}",
                help="提示词将替换 AI 分析中的「估值解读框架」段落。系统红线（只能基于数据解读、不做预测、不改变决策）始终生效。"
            )

            # 指标勾选
            st.markdown("**📊 传递给 AI 的指标**")
            st.caption("勾选需要的指标，AI 将只看到选中项。默认仅选 PE、PB。")

            saved_indicators = get_selected_indicators(code)
            default_checked = saved_indicators if saved_indicators else ["pe", "pb"]

            if st.button("☑️ 全选", key=f"select_all_{code}", use_container_width=True):
                for key in INDICATOR_META:
                    st.session_state[f"ind_{code}_{key}"] = True
                st.rerun()

            # 按分组展示指标 checkboxes
            selected_keys = []
            for group_label, group_keys in INDICATOR_GROUPS:
                cols = st.columns(len(group_keys))
                for i, key in enumerate(group_keys):
                    label = INDICATOR_META[key][0]
                    checked = cols[i].checkbox(
                        label,
                        value=(key in default_checked),
                        key=f"ind_{code}_{key}",
                    )
                    if checked:
                        selected_keys.append(key)

            # 如果没有勾选任何指标，默认全部
            if not selected_keys:
                selected_keys = list(INDICATOR_META.keys())

            col_btn1, col_btn2 = st.columns([1, 1])
            with col_btn1:
                if st.button("💾 保存提示词", key=f"save_prompt_{code}", type="primary", use_container_width=True):
                    if prompt_text.strip():
                        upsert_custom_prompt(code, prompt_text.strip(), indicators=selected_keys)
                        st.session_state.prompt_saved_msg = f"已保存 {name} 的定制提示词（{len(selected_keys)} 项指标）"
                        st.rerun()
                    else:
                        st.warning("提示词内容为空，请先编写后再保存。如需恢复默认，请点击「重置为默认」。")

            with col_btn2:
                if current_prompt:
                    if st.button("🗑️ 重置为默认", key=f"reset_prompt_{code}", use_container_width=True):
                        delete_custom_prompt(code)
                        st.warning(f"已清除 {name} 的定制提示词，恢复系统默认策略")
                        st.rerun()

            # 使用说明
            st.divider()
            st.markdown("""
            **📋 使用提示**
            - 提示词将**替换**系统默认的「第二步：分类定价逻辑」段落，第一/三/四步保持不变
            - 系统**绝对红线**（只能基于量化数据解读、语气冷静客观、不改变决策动作）始终生效，不可覆盖
            - 建议关注：该指数的估值锚点（PE/PB/ROE）、行业特性、需要特别注意的风险维度
            - 最多 2000 字符，保存后下次巡检自动生效
            """)