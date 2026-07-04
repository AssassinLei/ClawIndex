from dotenv import load_dotenv
load_dotenv()

import streamlit as st
import pandas as pd
import re
from pathlib import Path
from database import init_db, add_fund, remove_fund, get_all_funds, get_all_industries, get_industry_count
from data_fetcher import sync_all_history, sync_incremental, fetch_industry_classify
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
    filtered_options = []  # 未搜索时不显示列表

if filtered_options:
    # 构建选择列表
    labels = [opt['label'] for opt in filtered_options]
    
    selected_idx = st.sidebar.selectbox(
        "搜索结果",
        range(len(filtered_options)),
        format_func=lambda x: labels[x]
    )
    selected_industry = filtered_options[selected_idx]
    
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
elif search_keyword:
    st.sidebar.warning("未找到匹配的行业，请尝试其他关键词")
else:
    st.sidebar.caption("请输入行业名称或代码进行搜索")

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
    
    # 删除功能
    del_code = st.sidebar.selectbox("选择要删除的代码", df_funds['fund_code'].tolist())
    if st.sidebar.button("删除所选指数"):
        remove_fund(del_code)
        st.sidebar.warning(f"已删除 {del_code}")
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

if st.button("🚀 运行今日行情抓取与策略巡检", type="primary", use_container_width=True):
    if not funds:
        st.warning("监控池为空，请先在左侧添加。")
    else:
        st.markdown("### 📊 巡检报告")
        
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
                    <span style="font-size:1.2rem; font-weight:700; color:{style['color']};">{style['label']}</span>
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