from dotenv import load_dotenv
load_dotenv()

import streamlit as st
import pandas as pd
import re
from pathlib import Path
from database import init_db, add_fund, remove_fund, get_all_funds, get_all_industries, get_industry_count, save_inspection_result, get_inspection_history, get_market_data_for_date
from data_fetcher import sync_all_history, sync_incremental, fetch_industry_classify, is_trade_day
from strategy_engine import generate_fund_report
from llm_agent import generate_ai_report
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
        add_fund(new_code, new_name, new_cat[0])
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

# 分类中文映射
CATEGORY_NAMES = {
    "wide_base": "宽基指数",
    "tech_growth": "科技成长",
    "cycle_mfg": "周期制造",
    "dividend": "稳健收息"
}

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

tab1, tab2 = st.tabs(["巡检", "历史分析"])

with tab1:
    if st.button("🚀 运行今日行情抓取与策略巡检", type="primary", use_container_width=True):
        if not funds:
            st.warning("监控池为空，请先在左侧添加。")
        else:
            st.header("📊 巡检报告", anchor=False)
            
            # 进度展示
            total_funds = len(funds)
            progress_bar = st.progress(0)
            progress_text = st.caption(f"准备分析 {total_funds} 个指数...")
            
            for i, fund in enumerate(funds):
                code = fund['fund_code']
                name = fund['fund_name']
                cat = fund['category']
                
                # 1. 增量同步：从数据库最新日期到今天的行情数据
                sync_error = None
                with st.spinner(f'正在同步 {name} ({code}) 最新数据...'):
                    sync_success, sync_msg = sync_incremental(code)
                    if not sync_success:
                        sync_error = sync_msg
                
                # 如果增量同步失败，跳过策略引擎，直接显示错误
                if sync_error:
                    fund_data = {"error": f"数据同步失败: {sync_error}"}
                else:
                    # 2. 策略引擎介入 (硬逻辑计算)
                    fund_data = generate_fund_report(code, cat)
                
                # 3. AI 报告生成
                ai_report = generate_ai_report(fund_data)
                
                # 4. 前端渲染展示
                has_error = 'error' in fund_data
                action = fund_data.get('decision', {}).get('action', 'ERROR')
                
                # 根据 Action 确定视觉风格
                ACTION_STYLES = {
                    "STRONG_BUY": {"color": "#16a34a", "bg": "#f0fdf4", "border": "#22c55e", "icon": "🟢", "label": "强烈加仓"},
                    "BUY_PLAN":   {"color": "#15803d", "bg": "#f0fdf4", "border": "#4ade80", "icon": "🟩", "label": "定投买入"},
                    "HOLD":       {"color": "#d97706", "bg": "#fffbeb", "border": "#f59e0b", "icon": "🟡", "label": "持有观望"},
                    "SELL_PLAN":  {"color": "#dc2626", "bg": "#fef2f2", "border": "#ef4444", "icon": "🔴", "label": "止盈卖出"},
                    "ERROR":      {"color": "#6b7280", "bg": "#f9fafb", "border": "#9ca3af", "icon": "⚠️", "label": "数据异常"},
                }
                
                if has_error:
                    style = ACTION_STYLES["ERROR"]
                else:
                    style = ACTION_STYLES.get(action, ACTION_STYLES["HOLD"])
                
                # 保存巡检结果到数据库（含 AI 报告）
                save_inspection_result(code, name, action if not has_error else "ERROR", ai_report)
                
                # 渲染卡片
                st.markdown(f"""
                <div style="
                    border-left: 5px solid {style['border']};
                    background: {style['bg']};
                    border-radius: 0 12px 12px 0;
                    padding: 16px 20px;
                    margin: 12px 0;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.04);
                ">
                    <div style="display:flex; align-items:center; gap:10px; margin-bottom:4px;">
                        <span style="font-size:1.6rem;">{style['icon']}</span>
                        <span style="font-size:1.2rem; font-weight:700; color:{style['color']};">{name} · {style['label']}</span>
                        <span style="font-size:0.85rem; color:#6b7280; margin-left:auto;">{code} · {datetime.now().strftime('%Y-%m-%d')}</span>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                
                with st.expander(f"{style['icon']} {name}", expanded=True):
                    if has_error:
                        st.error(f"⚠️ {fund_data.get('error', '未知错误')}")
                    
                    col1, col2 = st.columns([1, 2])
                    
                    with col1:
                        st.subheader(name, anchor=False)
                        inds = fund_data.get('indicators', {})
                        st.metric(label="当前价格", value=round(inds.get('price', 0), 3) if inds.get('price') else "N/A")
                        st.metric(label="当前市盈率 (PE)", value=round(inds.get('pe', 0), 2) if inds.get('pe') else "N/A")
                        st.metric(label="历史PE分位数", value=f"{round(inds.get('pe_percentile', 0)*100, 2)}%" if inds.get('pe_percentile') else "N/A")
                        
                        st.subheader("韬略", anchor=False)
                        details = fund_data.get('decision', {}).get('details', [])
                        if details:
                            for detail in details:
                                st.markdown(f"<div style='color:#1a1a1a; font-weight:500; font-size:0.92rem; margin:4px 0;'>- {detail}</div>", unsafe_allow_html=True)
                        else:
                            st.markdown("<div style='color:#1a1a1a; font-weight:500; font-size:0.92rem;'>- 无（数据异常）</div>", unsafe_allow_html=True)
                            
                    with col2:
                        st.subheader("AI 投顾解读", anchor=False)
                        # 将 AI 报告中的标题语法转为粗体，避免 Streamlit 渲染锚点链接
                        clean_report = re.sub(r'^#{1,6}\s+', '**', ai_report, flags=re.MULTILINE)
                        clean_report = re.sub(r'\*\*([^*]+)\*\*\s*$', '**\1**', clean_report, flags=re.MULTILINE)
                        if has_error:
                            st.warning(clean_report)
                        else:
                            st.info(clean_report)
                
                # 更新进度
                progress_bar.progress((i + 1) / total_funds)
                progress_text.text(f"已分析 {i + 1}/{total_funds}")
                
            st.success("🎉 所有指数巡检完毕！")

with tab2:
    st.subheader("📋 历史巡检记录", anchor=False)
    
    # 获取所有历史记录
    all_records = get_inspection_history()
    
    if not all_records:
        st.info("暂无巡检记录，请先在「巡检」标签页运行一次分析。")
    else:
        # 提取筛选选项
        fund_names = sorted(set(r['fund_name'] for r in all_records))
        actions_list = ["STRONG_BUY", "BUY_PLAN", "HOLD", "SELL_PLAN", "ERROR"]
        action_labels = {"STRONG_BUY": "强烈加仓", "BUY_PLAN": "定投买入", "HOLD": "持有观望", "SELL_PLAN": "止盈卖出", "ERROR": "数据异常"}
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
            # 复用巡检页的颜色配置
            HISTORY_STYLES = {
                "STRONG_BUY": {"color": "#16a34a", "bg": "#f0fdf4", "border": "#22c55e", "icon": "🟢", "label": "强烈加仓"},
                "BUY_PLAN":   {"color": "#15803d", "bg": "#f0fdf4", "border": "#4ade80", "icon": "🟩", "label": "定投买入"},
                "HOLD":       {"color": "#d97706", "bg": "#fffbeb", "border": "#f59e0b", "icon": "🟡", "label": "持有观望"},
                "SELL_PLAN":  {"color": "#dc2626", "bg": "#fef2f2", "border": "#ef4444", "icon": "🔴", "label": "止盈卖出"},
                "ERROR":      {"color": "#6b7280", "bg": "#f9fafb", "border": "#9ca3af", "icon": "⚠️", "label": "数据异常"},
            }
            
            for record in filtered_records:
                act = record.get('action', 'HOLD')
                s = HISTORY_STYLES.get(act, HISTORY_STYLES["HOLD"])
                
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
                    # 尝试获取当日行情数据
                    market_data = get_market_data_for_date(record['fund_code'], record['inspect_date'])
                    
                    if market_data:
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
                            st.caption("当日行情数据暂不可用（可能尚未入库）")
                    
                    # AI 报告
                    st.markdown("**AI 投顾解读**")
                    ai_text = record.get('ai_report', '')
                    if ai_text:
                        clean_report = re.sub(r'^#{1,6}\s+', '**', ai_text, flags=re.MULTILINE)
                        clean_report = re.sub(r'\*\*([^*]+)\*\*\s*$', '**\1**', clean_report, flags=re.MULTILINE)
                        st.info(clean_report)
                    else:
                        st.caption("- 无 AI 报告")