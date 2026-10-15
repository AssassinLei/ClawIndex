"""Shared visual language and navigation for the Streamlit application."""
from html import escape

import streamlit as st


# Keep route identifiers stable for sessions and links; use plain language on screen.
PAGES = {
    "今日操作": ("今日操作", "先看今天是否需要处理，再决定是否查看依据。"),
    '发现机会':('发现机会','自动扫描一级、二级行业，寻找少量有依据的研究候选。'),
    "监控与数据": ("我的指数", "添加想关注的指数，行情和指标由后台持续维护。"),
    "持仓与预算": ("我的持仓", "只在实际持仓发生变化时更新；未登记时默认空仓。"),
    "策略与研究": ("策略与进化", "让系统定期检查买卖规则是否值得改进；平时只需看进度。"),
    "历史与反馈": ("操作记录", "查找过去的判断，补记你的实际处理结果。"),
    "信号与待执行": ("信号详情", "对照指数信号、个人判断与数据依据，理解每一次结论。"),
    "通知与运行": ("通知与设置", "管理提醒渠道和后台自动更新。"),
    "健康": ("系统状态", "查看后台是否正常，以及任务停在哪一步。"),
}


def install_design():
    # Native controls retain their labels, keyboard behavior and Streamlit theme.
    # Colors use Streamlit theme variables so dark mode remains readable.
    st.html("""<style>
    :root { --ci-accent: #0f766e; --ci-muted: #64748b; }
    .stApp { font-family: Inter, "Microsoft YaHei", "PingFang SC", sans-serif; }
    .stMainBlockContainer { max-width: 1180px; padding-top: 2.4rem; padding-bottom: 4rem; }
    h1 { font-size: 2rem !important; letter-spacing: -.04em; padding-bottom: .35rem !important; }
    h2 { font-size: 1.35rem !important; letter-spacing: -.02em; }
    h3 { font-size: 1.1rem !important; }
    [data-testid="stMetricValue"] { font-size:1.6rem !important; }
    p, li { line-height: 1.7; }
    [data-testid="stCaptionContainer"] { opacity: 1; }
    [data-testid="stVerticalBlockBorderWrapper"] { border-radius: 14px; }
    [data-testid="stForm"] { border-radius: 14px; padding: 1.2rem; }
    button { min-height: 2.6rem; }
    button:focus-visible, input:focus-visible, summary:focus-visible { outline: 3px solid #0d9488 !important; outline-offset: 3px; }
    [data-testid="stSidebar"] [role="radiogroup"] { gap: .35rem; }
    [data-testid="stSidebar"] [role="radiogroup"] label { padding: .55rem .65rem; border-radius: 9px; }
    [data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) { background: rgba(13,148,136,.12); font-weight: 650; }
    .ci-brand { display:flex; align-items:center; gap:.65rem; margin:.25rem 0 1.35rem; font-size:1.3rem; font-weight:750; letter-spacing:-.04em; }
    .ci-mark { display:grid; place-items:center; width:34px; height:34px; border-radius:10px; background:#0f766e; color:white; font-size:1rem; letter-spacing:0; }
    .ci-eyebrow { color:var(--text-color); opacity:.65; font-size:.75rem; font-weight:650; letter-spacing:.12em; margin-bottom:.4rem; }
    .ci-summary { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin:.25rem 0 1.2rem; }
    .ci-stat { padding:1rem 1.2rem; border:1px solid var(--border-color,rgba(128,128,128,.24)); border-radius:14px; background:var(--secondary-background-color); }
    .ci-stat strong { display:block; font-size:1.8rem; line-height:1.25; margin:.3rem 0; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
    .ci-stat span, .ci-stat small { display:block; font-size:.8rem; }
    .ci-stat small { opacity:.72; }
    .ci-card-heading { display:flex; gap:1rem; justify-content:space-between; align-items:flex-start; margin-bottom:.4rem; }
    .ci-card-heading h3 { margin:0; padding:0 !important; overflow-wrap:anywhere; }
    .ci-code { font-size:.8rem; opacity:.65; margin-top:.25rem; }
    .ci-badge { display:inline-block; border-radius:7px; padding:.3rem .65rem; font-weight:650; font-size:.8rem; white-space:nowrap; background:var(--secondary-background-color); border:1px solid var(--border-color,rgba(128,128,128,.24)); }
    .ci-badge.buy { background:#e0f2fe; color:#075985; border-color:#bae6fd; }
    .ci-badge.sell { background:#fff1f2; color:#9f1239; border-color:#fecdd3; }
    .ci-badge.attention { background:#fffbeb; color:#92400e; border-color:#fde68a; }
    .ci-badge.quiet { background:#f1f5f9; color:#334155; border-color:#e2e8f0; }
    .ci-status { font-size:.83rem; display:flex; gap:.45rem; align-items:center; margin:.7rem 0; }
    .ci-dot { width:7px; height:7px; background:#0d9488; border-radius:50%; display:inline-block; }
    .ci-dot.offline { background:#b45309; }
    .ci-empty { border:1px dashed var(--border-color,rgba(128,128,128,.35)); padding:2rem; border-radius:14px; text-align:center; margin:1rem 0; }
    .ci-empty strong { display:block; font-size:1.1rem; margin-bottom:.5rem; }
    @media(max-width:640px) {
      .stMainBlockContainer { padding:1.25rem 1rem 3rem; }
      h1 { font-size:1.65rem !important; }
      .ci-summary { gap:7px; }
      .ci-stat { padding:.8rem .6rem; }
      .ci-stat strong { font-size:1.45rem; }
      .ci-stat small { font-size:.7rem; }
      .ci-card-heading { gap:.5rem; }
      [data-testid="stForm"] { padding:.8rem; }
    }
    @media(prefers-reduced-motion:reduce) { *, *::before, *::after { scroll-behavior:auto !important; transition:none !important; } }
    </style>""")


def brand():
    st.html('<div class="ci-brand"><span class="ci-mark" aria-hidden="true">C</span>ClawIndex</div>')


def page_header(page):
    title, description = PAGES[page]
    st.html('<div class="ci-eyebrow">行业指数 · 投资助手</div>')
    st.title(title)
    st.caption(description)


def status_line(online, text):
    st.html(f'<div class="ci-status"><span class="ci-dot {"" if online else "offline"}" aria-hidden="true"></span>{escape(text)}</div>')


def summary_stats(items, aria_label='今日判断概览'):
    parts = ''.join(f'<div class="ci-stat"><span>{escape(title)}</span><strong>{escape(str(count))}</strong><small>{escape(note)}</small></div>' for title,count,note in items)
    st.html('<section class="ci-summary" aria-label="'+escape(aria_label)+'">'+parts+'</section>')


def card_heading(name, code, status, tone="quiet"):
    tone = tone if tone in ("buy","sell","attention","quiet") else "quiet"
    st.html(f'<div class="ci-card-heading"><div><h3>{escape(name)}</h3><div class="ci-code">{escape(code)}</div></div><span class="ci-badge {tone}">{escape(status)}</span></div>')


def empty_state(title, description):
    st.html(f'<div class="ci-empty"><strong>{escape(title)}</strong>{escape(description)}</div>')


def go_to(page):
    st.session_state["main_page"] = page


def navigate_button(title, page, key):
    st.button(title, key=key, on_click=go_to, args=(page,))
