"""Opportunity discovery UI: evidence first, no fabricated certainty or orders."""
import json

import pandas as pd
import streamlit as st

from .presentation import local_time, number, label
from .radar_rules import KINDS, STATES
from .ui_design import card_heading, empty_state, summary_stats
from .industry_hierarchy import parent_label


def act(svc, action, code):
    try:
        if action=='watch':
            svc.add_radar_watch(code)
            message = '已加入我的指数，后台会更新并生成个人判断；已有策略不会被重置。'
        elif action=='ignore':
            svc.radar_preferences(ignore=code)
            message = '已隐藏该指数，仅影响你的机会页面和机会提醒。'
        else:
            svc.radar_preferences(restore=code)
            message = '已恢复显示该指数。'
        st.session_state.radar_notice = {'ok':True,'message':message}
    except (ValueError,PermissionError,RuntimeError,ImportError) as exc:
        st.session_state.radar_notice = {'ok':False,'message':str(exc)}


@st.fragment(run_every='5s')
def radar_panel(svc, guarded):
    state = guarded(svc.radar)
    if state is None:
        return
    st.write('自动寻找你尚未关注的行业方向。**只扫描申万一级、二级指数，最多展示3个候选，允许没有机会。**')
    st.caption('程序按固定首版规则筛选；尚无长期前向验证。候选不是买入指令，加入监控后由“今日操作”继续判断。')
    notice = st.session_state.pop('radar_notice',None)
    if notice:
        (st.success if notice['ok'] else st.error)(notice['message'])
    with st.container(horizontal=True):
        hide = st.checkbox('隐藏已关注的指数',value=state['preferences']['hide_watched'],key='radar_hide_'+str(svc.user['id']))
        if hide!=state['preferences']['hide_watched']:
            guarded(lambda:svc.radar_preferences(hide_watched=hide))
            state = svc.radar()
        if st.button('刷新机会',key='radar_refresh'):
            result = guarded(svc.request_radar)
            if result:
                st.info('该交易日已有完整扫描，已复用已有结果。行情或规则更新后，后台会重新检查。' if result=='ALREADY_DONE' else '已提交共享扫描；后台分批补齐数据并自动更新此页，可关闭网页。')
    if not state['enabled']:
        st.info('自动机会扫描已暂停；可点击“刷新机会”提交一次后台扫描。')
    report = state['report']
    if not report or state['stale'] or report['status']!='COMPLETE':
        from .ui_components import background_status_panel
        background_status_panel(svc)
    if not report:
        empty_state('机会雷达正在准备', '后台会建立L1、L2目录，分批准备价格与估值历史。首次需要时间，以后主要更新增量数据，无需手动同步。')
        if state['progress'].get('detail'):
            st.caption(state['progress']['detail'])
        return
    summary_stats([('当前新候选',len(state['candidates']),'最多3个，按个人偏好去重'),
                   ('历史已准备',f"{report['available']}/{report['total']}",'共同价格与比较基准'),
                   ('市场观察',{'UP':'向上','CAUTIOUS':'谨慎','UNKNOWN':'准备中'}[report['market']['status']],'描述当前条件，不预测牛熊')],aria_label='机会筛选概览')
    st.caption('行情截至 '+report['as_of']+' · 扫描生成 '+local_time(report['created_at'])+' · 当前范围：申万 L1 + L2')
    if state['stale']:
        st.warning('这是上次扫描的证据，当前候选暂不展示。后台更新至已发布行情后自动替换。')
    elif report['status']!='COMPLETE':
        st.progress(float(report['coverage']),text=f"已具备共同历史 {report['available']} / {report['total']} 个指数")
        st.info('后台正在分批准备数据。覆盖达到80%且基准就绪后才展示候选，避免在小部分数据上仓促排名。')
        if state['progress'].get('detail'):
            st.caption(state['progress']['detail'])
    elif not state['candidates']:
        if any(r['state'] in ('WATCH','READY') for r in report['rows']):
            st.info('当前显示偏好下没有新的候选；已有候选可能已关注或已隐藏。允许保持空缺。')
        else:
            st.success('本轮没有达到条件的新机会。继续跟踪已有指数，等待更清楚的条件。')
    else:
        st.caption(report['market']['reason'])
    for row in state['candidates']:
        with st.container(border=True):
            card_heading(row['name'],row['code'],STATES[row['state']],'quiet' if row['state']=='WATCH' else 'buy')
            st.caption(('一级行业' if row['level']=='L1' else '二级行业')+' · '+KINDS[row['kind']]+' · 来源：程序筛选')
            if row['level']=='L2':
                st.caption(parent_label(row))
            for reason in row['reasons']:
                st.write('• '+reason)
            cols = st.columns(3)
            cols[0].metric('近60日表现',number(row['features']['return60'],True))
            cols[1].metric('相对沪深300',number(row['features']['relative60'],True))
            cols[2].metric('近120日回撤',number(row['features']['drawdown120'],True))
            with st.expander('风险与失效条件',key='radar_risk_'+row['code'],on_change='rerun'):
                for text in row['risks']+row['invalidations']:
                    st.write('• '+text)
                st.caption('加入监控会跟随当前有效条件，以研究观察阶段开始；持仓、预算和正式推送权限由现有流程管理。')
            with st.container(horizontal=True):
                st.button('加入我的指数',key='radar_add_'+row['code'],type='primary',on_click=act,args=(svc,'watch',row['code']))
                st.button('暂不关注',key='radar_ignore_'+row['code'],on_click=act,args=(svc,'ignore',row['code']))
            with st.expander('查看数据依据',key='radar_evidence_'+row['code'],on_change='rerun'):
                st.caption('相对表现是同一交易日窗口的价格收益比，不是胜率。估值只与自身可用历史比较。')
                st.dataframe(pd.DataFrame([{'指标':label(k),'数值':number(v,k in ('return20','return60','return120','relative60','relative120','volatility20','drawdown120','extension20','pe_percentile','pb_percentile'))} for k,v in row['features'].items() if v is not None]),hide_index=True,width='stretch')
                st.caption('规则版本：'+report['rule']['version']+' · 证据版本：'+report['id'][:12])
                st.json(report['sources'].get(row['code'],{}))
    if state['suppressed']:
        st.caption(f"另有 {len(state['suppressed'])} 个候选因行业重叠或走势同向等原因未重复展示。")
    with st.expander('我的隐藏列表'):
        names = {r['code']:r['name'] for r in report['rows']}
        for code in state['preferences']['ignored']:
            with st.container(horizontal=True):
                st.write(names.get(code,code)+' · '+code)
                st.button('恢复显示',key='radar_restore_'+code,on_click=act,args=(svc,'restore',code))
        if not state['preferences']['ignored']:
            st.caption('尚未隐藏任何指数。')
    with st.expander('完整筛选与撤销记录'):
        st.caption('未入选和数据准备中的指数也留存。没有因过去表现好就声称能赚钱。')
        st.dataframe(pd.DataFrame([{'指数':r['name'],'代码':r['code'],'级别':r['level'],'所属一级行业':parent_label(r).removeprefix('所属一级行业：') or '—','状态':STATES[r['state']],
                                  '类型':KINDS.get(r['kind'],'—'),'依据或排除原因':'；'.join(r['reasons'])} for r in state['display_rows']]),hide_index=True,width='stretch')
        if report['events']:
            descriptions = {'NEW':'新进入候选','READY':'研究入场条件满足','WITHDRAWN':'条件失效，撤销','SCOPE_REMOVED':'已移出扫描范围'}
            st.dataframe(pd.DataFrame([{'指数代码':r['code'],'变化':descriptions[r['kind']]} for r in report['events']]),hide_index=True)
        history = svc.radar_history(30)
        st.dataframe(pd.DataFrame([{'行情日期':r['as_of'],'扫描时间':local_time(r['created_at']),'进度':'扫描完成' if r['status']=='COMPLETE' else '数据准备中',
                                  '历史已准备':f"{r['available']}/{r['total']}",'初筛候选':sum(x['state'] in ('READY','WATCH') for x in r['rows']),'版本':r['id'][:12]} for r in history]),hide_index=True)
        st.download_button('下载本次筛选证据',json.dumps(report,ensure_ascii=False,indent=2),file_name='opportunity-radar-'+report['as_of']+'.json',mime='application/json')
    with st.expander('筛选原则与局限'):
        st.write('先检查数据、波动、回撤、追涨幅度及可用估值，再按入场确认、趋势类型与中期相对表现排序；不按涨幅强行凑满名额。')
        for text in report['limitations']:
            st.caption(text)
        st.caption('首版使用价格、估值与少量独立风险指标；专业因子可在加入监控后参与现有策略研究。没有成分股盈利质量和实际基金成交验证。')
    with st.expander('机会后来怎样：20、60个交易日跟踪'):
        st.caption('共享候选首次展示后留证，包含后续下跌与撤销案例。用实际展示时间之后的下一次开盘作为比较起点，模拟双边各10基点成本；不是买入指令或真实账户收益。样本可能重叠，不计算赚钱概率。')
        if st.button('查看展示后的跟踪结果',key='radar_outcomes'):
            records = guarded(svc.radar_outcomes)
            if records:
                st.dataframe(pd.DataFrame([{'指数':r['name'],'首次展示':local_time(r['created_at']),'起算日期':r['entry_date'],
                                          '观察交易日':r['horizon'],'进度':{'MATURED':'观察完成','WAITING':'尚未到期','DATA_WAIT':'行情自动补充中'}[r['status']],
                                          '模拟净表现':number(r['return'],True),'同期沪深300':number(r['benchmark_return'],True),
                                          '相对表现':number(r['relative_return'],True),'期间最大收盘回撤':number(r['max_drawdown'],True)} for r in records]),hide_index=True,width='stretch')
            elif records is not None:
                st.info('尚无首次展示的共享候选；有记录后自动留存，等待观察期结束。')
