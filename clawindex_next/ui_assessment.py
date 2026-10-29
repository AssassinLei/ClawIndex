"""A compact assessment of actual evidence, without model confidence scores."""
import pandas as pd
import streamlit as st

from .presentation import number,local_time


def assessment_panel(svc, code):
    with st.expander('策略体检：现行规则的证据与代价',key='assessment_'+str(svc.user['id'])+'_'+code,on_change='rerun') as panel:
        if not panel.open:
            return
        report = svc.strategy_assessment(code)
        st.markdown('**'+report['title']+'**')
        st.write(report['explanation'])
        st.caption('检查对象：当前正在使用的规则；研究中的候选不会冒充现行规则。')
        h = report['historical']
        if h:
            st.caption(f"历史试验区间 {h['start']} 至 {h['end']} · {h['sessions']}个收益交易时段 · 单边成本{h['cost_bps']}基点 · 延迟{h['delay']}个交易日")
            cols = st.columns(3)
            cols[0].metric('历史最大收盘回撤',number(h['max_drawdown'],True))
            cols[1].metric('相对持续持有少赚',number(h['return_sacrifice'],True))
            cols[2].metric('模拟持有时间',number(h['exposure'],True))
            st.caption('“少赚”为负表示此段历史多赚；不是未来预期收益。回撤改善：'+number(h['drawdown_improvement'],True)+'；模拟年化交易次数：'+number(h['trades_year']))
        for issue in report['issues']:
            st.warning(issue)
        if report['candidate']:
            candidate = report['candidate']
            st.info('另有候选正在研究或观察；不代表当前规则已经通过这些检查。'+
                    (f" 新行情已观察{candidate['observed_sessions']}/{candidate['required_sessions']}个交易日。" if candidate['required_sessions'] else ''))
        if report['stress']:
            st.dataframe(pd.DataFrame([{'单边成本（基点）':s['cost_bps'],'成交延迟（交易日）':s['delay'],
                                      '规则收益':number(s['return'],True),'持续持有收益':number(s['hold_return'],True),
                                      '规则最大回撤':number(s['max_drawdown'],True),'相对持有少赚':number(s['return_sacrifice'],True)} for s in report['stress']]),hide_index=True,width='stretch')
        if report['source']:
            st.caption('证据来源：'+report['source']['kind']+' · 生成时间：'+local_time(report['source']['created_at'])+' · '+('与当前巡检数据一致' if report['same_dataset'] else '历史数据版本'))
        for text in report['limitations']:
            st.caption(text)
