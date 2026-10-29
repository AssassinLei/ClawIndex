"""Chinese, concise operation and evolution panels; details stay expandable."""
import json
from datetime import datetime,timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from .presentation import label, number, local_time, readable
from .validation import DEFAULT_POLICY
from .factors import PROFILES, TECH_FIELDS, coverage
from .ui_design import card_heading, summary_stats, empty_state, navigate_button
from .today_guidance import display_priority, handling, plan_context, research_reuse_message, summary_counts


def evolution_step(latest, config):
    """Explain what the program is doing and what the user needs to do."""
    if latest and latest['payload'].get('mode')=='DAILY_APPLY':
        status = latest.get('candidate_status') or latest['status']
        messages = {
            'WAITING_DATA':('进化已提交，等待后台准备行情','缺口由后台自动补齐，准备好后继续；无需手动同步。'),
            'PENDING':('每日进化已提交','后台将比较规则，通过历史检验后实际更新；可以关闭网页。'),
            'RUNNING':('每日进化正在比较规则','正在检查回撤、少赚的代价、成本与延迟；完成后显示是否更新。'),
            'READY_TO_APPLY':('新规则等待应用','历史检验已通过，后台会复核最新行情、规则及旧提醒投递状态，再完成更新。'),
            'ACTIVE':('每日进化已更新策略','新规则已经生效，后台正在生成新判断；可回“今日操作”查看。此次更新尚无后续行情验证。'),
            'REJECTED':('今日检查完成，保留原规则','没有找到通过检验的改进；明天仍可进化，无需今天反复点击。'),
            'QUARANTINED':('本次进化已停止','行情、规则或设置已变化，保留当前规则；下次进化使用最新输入。'),
            'CANCELLED':('本次进化已取消','监控或设置发生变化，旧任务不再应用。'),
            'FAILED':('本次进化未完成','当前规则保留，可展开查看失败原因或到“系统状态”排查。'),
            'RETIRED':('已恢复进化前的规则','自动晋升已暂停，需要继续时请重新保存进化设置。'),
        }
        return messages.get(status,('进化状态待确认','请查看下方研究记录。'))
    if not config['enabled']:
        return "自动研究已暂停", "需要继续时，在下方设置中开启自动研究并保存。现行买卖规则不会因此被更换。"
    if not latest:
        return "等待开始研究", "后台会检查数据和研究条件；数据缺口自动补齐。你可以先回“今日操作”，无需反复点击研究。"
    if latest['payload'].get('retry_after'):
        return "等待行情更新", "后台会在数据更新后继续。当前仍使用原规则，无需手动补数据。"
    status = latest.get('candidate_status') or latest['status']
    messages = {
        'PENDING':("等待后台研究", "请求已保存，后台会分批比较规则。可以关闭网页，无需重复提交。"),
        'RUNNING':("正在比较新规则", "系统正在用过去的行情检查回撤、收益代价和交易频率。暂不更换现行规则。"),
        'FORWARD_SHADOW':("新规则正在模拟试用", "系统用之后新出现的行情模拟买卖，不动用真实资金。等观察结束，期间仍使用原规则。"),
        'ELIGIBLE':("新规则已通过观察", "已授权自动更换时，系统还会检查更换间隔等条件；未授权时继续使用原规则。" if config['auto_promote'] else "当前未授权自动更换，因此继续使用原规则。希望自动应用时，请审阅下方范围并明确授权；保存新配置会重新开始检验。"),
        'ACTIVE':("新规则已投入使用", "之后的买卖判断使用更新后的规则。日常回“今日操作”查看即可。"),
        'REJECTED':("本轮未找到合格改进", "继续使用原规则，这是正常筛选结果。下一轮按计划研究，无需为了换规则反复调参数。"),
        'QUARANTINED':("本轮候选已停用", "候选因数据、规则或授权变化停止使用，现行规则保留。具体原因可在下方查看。"),
        'CANCELLED':("本轮研究已结束", "设置或关注状态已变化，旧研究不再继续。以当前配置安排后续研究。"),
        'RETIRED':("已恢复此前的规则", "自动更换已暂停。希望重新启用研究时，请在下方重新确认设置。"),
        'FAILED':("本轮研究未完成", "当前仍使用原规则。查看下方失败原因；如后台持续异常，可到“系统状态”排查。"),
    }
    return messages.get(status,("研究状态待确认", "继续使用现行规则，详细记录可在下方查看。"))


def rollback_action(svc, history):
    active = next((r for r in history if r["candidate_status"] == "ACTIVE"),None)
    if active and st.button("回退上次策略更新并暂停自动晋升",key="rollback_"+active["id"]):
        try:
            svc.rollback_evolution(active["id"])
        except (ValueError,PermissionError) as exc:
            st.error(str(exc))
        else:
            st.rerun()


@st.fragment(run_every='5s')
def evolution_records_panel(svc, code):
    st.subheader('进化记录')
    st.caption('区分研究发起时间与参数真正生效时间；所有时间为北京时间，最多显示最近250条。')
    records = svc.evolution_records(code)
    if not records:
        st.info('还没有进化记录。手动进化、后台研究及实际参数更新后，会在这里留存。')
        return
    def time_text(value):
        return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Hong_Kong')).strftime('%Y-%m-%d %H:%M:%S') if value else '尚未生效'
    scope = st.selectbox('记录类型',['全部记录','手动发起','自动发起','历史来源未记录'],key='evolution_record_scope_'+code)
    applied_only = st.checkbox('仅看实际参数变更与回退',key='evolution_record_applied_'+code)
    shown = [r for r in records if (not applied_only or r['applied']) and
             (scope=='全部记录' or scope=='手动发起' and r['source'].startswith('MANUAL_') or
              scope=='自动发起' and r['source']=='AUTOMATIC' or scope=='历史来源未记录' and r['source']=='LEGACY')]
    if not shown:
        st.info('当前筛选下没有记录。未通过研究不会被记作参数已调整。')
        return
    st.dataframe(pd.DataFrame([{'发起时间':time_text(r['requested_at']),'生效时间':time_text(r['applied_at']),
                               '发起方式':r['source_label'],'结果':r['outcome'],
                               '实际参数变化':'；'.join(f"{d['parameter']}：{d['before']} → {d['after']}" for d in r['changes']) or '现行参数未调整'} for r in shown]),hide_index=True,width='stretch')
    by_id = {r['id']:r for r in shown}
    with st.expander('查看某次进化的参数与依据',key='evolution_record_detail_'+code,on_change='rerun'):
        rid = st.selectbox('选择进化记录',list(by_id),format_func=lambda key:time_text(by_id[key]['event_at'])+' · '+by_id[key]['source_label']+' · '+by_id[key]['outcome'],key='evolution_record_id_'+code)
        record = by_id[rid]
        st.write('结果：'+record['outcome']+'；'+record['application'])
        st.write('依据：'+record['reason'])
        for title,items in (('实际生效的参数变化',record['changes']),('候选参数变化（尚未生效）',record['candidate_changes'])):
            if items:
                st.markdown('**'+title+'**')
                st.dataframe(pd.DataFrame([{'参数':d['parameter'],'调整前':d['before'],'调整后':d['after']} for d in items]),hide_index=True,width='stretch')


@st.fragment(run_every="5s")
def evolution_progress(svc, code):
    history = svc.evolution_history(code)
    policy = svc.evolution_policy(code)
    if not policy:
        st.info("当前尚无生效的研究设置。自动维护开启时，新指数可由后台初始化；此前停用的设置需在下方重新保存。")
        rollback_action(svc,history)
        return
    current = [r for r in history if r['policy_id']==policy['id']]
    focus = st.session_state.get('evolution_focus_'+code)
    latest = next((r for r in history if r['id']==focus),current[0] if current else None)
    title,next_step = evolution_step(latest,policy['payload']['config'])
    st.markdown("**现在的进度："+title+"**")
    st.info("你现在要做的："+next_step)
    if latest and latest['status'] in ('PENDING','RUNNING','WAITING_DATA'):
        from .ui_components import background_status_panel
        background_status_panel(svc)
    current_rule = next((item for item in svc.watchlist() if item['code']==code),None)
    if current_rule:
        spec = json.loads(current_rule['spec'])
        st.write(f"当前生效规则：{spec['ma']}日均线 · 连续{spec['confirmation']}日确认 · "+PROFILES.get(spec.get('factor_profile'),{}).get('label','无附加入场因子'))
    if not latest:
        if history:
            with st.expander("此前设置下的研究记录"):
                st.dataframe(pd.DataFrame([{'提交时间':local_time(r['created_at']),'结果':label(r['candidate_status'] or r['status'])} for r in history]),hide_index=True)
        rollback_action(svc,history)
        return
    if latest['payload'].get('retry_after'):
        st.caption(latest['payload'].get('waiting_reason','后台等待接口更新'))
    plan = latest["payload"]["plan"]
    if latest["status"] in ("PENDING","RUNNING") and plan:
        st.progress(min(1.,latest["cursor"]/len(plan)),text=f"已完成 {latest['cursor']} / {len(plan)} 个成本、延迟与参数情景")
    if latest["candidate_state"] and latest['payload'].get('mode')!='DAILY_APPLY':
        count = latest["candidate_state"]["count"]
        horizon = latest["candidate_payload"]["policy"]["shadow_sessions"]
        st.caption(f"新行情模拟试用：已观察 {count} / {horizon} 个交易日。只统计实际遇到的新行情，不会把过去的日期补算成试用。")
    assessment = latest["payload"].get("assessment",{})
    diagnostic = latest['payload'].get('factor_diagnostics')
    if diagnostic:
        with st.expander('全部因子的开发区间探索（不用于选策）'):
            st.caption(f"仅使用 {diagnostic['start']} 至 {diagnostic['end']} 的开发数据，观察之后20日收益与最低收盘变化的关系。")
            st.dataframe(pd.DataFrame([{'因子':label(x['factor']),'成对样本':x['paired_sessions'],
                                      '与后续收益的秩相关':number(x['return_rank_correlation']),
                                      '与后续最低点变化的秩相关':number(x['future_low_rank_correlation'])} for x in diagnostic['factors']]),hide_index=True)
            for limitation in diagnostic['limitations']: st.caption(limitation)
    for reason in assessment.get("reasons",[]):
        st.caption(reason)
    if latest["candidate_payload"]:
        if latest['payload'].get('mode')=='DAILY_APPLY':
            old,new = latest['payload']['base_spec'],latest['candidate_payload']['spec']
            st.write(f"本轮规则对比：{old['ma']}日均线 → {new['ma']}日均线")
            st.caption('入场因子：'+PROFILES.get(old.get('factor_profile'),{}).get('label','无附加因子')+' → '+PROFILES.get(new.get('factor_profile'),{}).get('label','无附加因子'))
        for reason in latest["candidate_payload"].get("forward_assessment",{}).get("reasons",[]):
            st.caption(reason)
        if latest["candidate_payload"].get("quarantine_reason"):
            st.warning(latest["candidate_payload"]["quarantine_reason"])
    if latest["error"]:
        st.warning(latest["error"])
    with st.expander("历次研究进度"):
        st.dataframe(pd.DataFrame([{"研究提交":local_time(r["created_at"]),"状态":label(r["candidate_status"] or r["status"]),"完成情景":f"{r['cursor']}/{len(r['payload']['plan'])}","观察交易日":r["candidate_state"]["count"] if r["candidate_state"] else 0} for r in history]),hide_index=True)
    with st.expander("研究、观察与版本证据"):
        st.json(latest)
    rollback_action(svc,history)


def evolution_panel(svc, selected, guarded):
    st.header("让系统检查：有没有更合适的买卖规则")
    st.write("**目标：尽量减少大幅回撤，接受可能少赚。** 点击“进化并应用”，系统会比较规则，通过历史分段、成本与延迟检验后实际更新当前策略；未找到合格改进则保留原规则。")
    st.caption("这是程序按固定标准做筛选；AI不决定如何改规则，模拟试用不动用真实资金。")
    if svc.store.version < 2:
        st.warning("数据库需要备份后升级。停止服务后执行：python -m clawindex_next --db 数据库路径 upgrade --backup 新的备份路径")
        return
    saved = svc.evolution_policy(selected["code"])
    spec = json.loads(selected["spec"])
    config = saved["payload"]["config"] if saved else {**DEFAULT_POLICY,"ma_min":max(20,spec["ma"]-30),"ma_max":min(250,spec["ma"]+30)}
    config = {**DEFAULT_POLICY,**config,'factor_research':config.get('factor_research',False) if saved else True}
    if st.button('进化并应用',type='primary',key='daily_evolve_'+selected['code']):
        clicked_at = datetime.now(timezone.utc)
        result = guarded(lambda:svc.evolve_daily(selected['code']))
        if result:
            from .daily_evolution import reuse_key
            day = clicked_at.astimezone(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
            receipt = svc.store.rows('SELECT value FROM settings WHERE key=?',(reuse_key(svc.user['id'],selected['code'],day),))
            try:
                reused = research_reuse_message(json.loads(receipt[0]['value']) if receipt else None,result,clicked_at)
            except (TypeError,ValueError):
                reused = None
            st.session_state['evolution_focus_'+selected['code']] = result
            st.session_state.evolution_notice = {'code':selected['code'],'message':reused or '进化请求已提交；同一天重复点击会展示同一任务，进行中的任务会复用。后台完成后在下方显示是否已更新。'}
            st.rerun()
    st.caption('每天可主动检查一次（香港时间）；相同数据、规则和授权下的已完成研究会复用，不重复搜参数。后台运行，可关闭网页；只调整当前账号的该指数规则，不代替你下单。')
    st.caption('每日进化依据历史检验，尚无后续行情验证；每天反复使用相近历史会增加过拟合风险。下方长期自动进化仍保留新行情模拟观察。')
    notice = st.session_state.get('evolution_notice')
    if notice and notice['code']==selected['code']:
        st.success(st.session_state.pop('evolution_notice')['message'])
    evolution_progress(svc,selected['code'])
    evolution_records_panel(svc,selected['code'])
    st.caption('以下开关属于长期自动进化；上方主动提交的每日进化任务单独运行。')
    cols = st.columns(3)
    cols[0].metric("自动研究", ("已开启" if config["enabled"] else "已暂停") if saved else "待初始化")
    cols[1].metric("更新后生成判断", ("已开启" if config["auto_inspect"] else "已暂停") if saved else "待初始化")
    cols[2].metric("长期自动更新规则", "已授权" if config["auto_promote"] else "未授权")
    with st.expander("我该怎么用？",expanded=not bool(saved)):
        st.write("1. 平时看“今日操作”。这里负责检查规则是否值得改，不需要每天来调参数。")
        st.write("2. 自动研究可以先开着；不允许自动更换时，研究仍会进行，但不会自动替换现行规则。")
        st.write("3. 希望以后达标时自动更换规则，请在开始研究前展开下方设置，审阅范围、勾选允许自动更新并保存。")
        st.caption(f"长期自动研究至少间隔 {config['research_days']} 个自然日；候选需要用 {config['shadow_sessions']} 个新交易日模拟试用。上方每日主动进化不等待这个周期，依据历史检验直接应用，证据较弱。")
        st.caption("更换规则不会替你下单。外部提醒还需要规则已启用提醒、通知渠道已开启。")
    with st.expander("查看正在使用的规则"):
        st.write(f"当前规则：{spec['ma']}日均线，连续{spec['confirmation']}个交易日确认，均线方向参考{spec['slope_window']}个交易日。")
        factor = PROFILES.get(spec.get('factor_profile'),{})
        st.caption("买入附加条件："+factor.get('label','未使用附加因子组合')+"；提醒状态："+label(selected['stage']))
        st.caption("此处是本次打开页面时生效的规则；研究中和模拟试用中的候选不会立即替代它。")
    with st.expander("调整自动进化设置"):
        st.caption("通常只需决定是否允许自动更换。其余参数可保持当前值；真正修改设置会结束旧候选，重新检验。")
        with st.form("evolution_policy"):
            enabled = st.checkbox("开启该指数自动研究与观察",value=config["enabled"])
            inspect = st.checkbox("行情更新后自动生成我的操作判断",value=config["auto_inspect"])
            promote = st.checkbox("允许通过检验后自动更新规则并启用该指数提醒",value=config["auto_promote"],help="只在下方授权均线范围内，保留退出规则和预算。通知渠道仍需另外开启；不会自动交易。")
            factor_research = st.checkbox('将基础指标和专业因子组合纳入自动研究',value=config['factor_research'])
            profiles = st.multiselect('允许研究与晋升的因子组合',list(PROFILES),default=config['factor_profiles'],format_func=lambda k:PROFILES[k]['label'])
            st.caption('因子只过滤新增买入，退出规则保持不变；缺失的组合自动等待或跳过，不用零值填充。旧配置需重新保存才能授权因子规则晋升。')
            with st.expander("优化目标与授权范围",expanded=False):
                minimum = st.number_input("允许均线最短交易日数",20,250,config["ma_min"])
                maximum = st.number_input("允许均线最长交易日数",20,250,config["ma_max"])
                max_dd = st.number_input("研究和观察回撤门槛（%）",1.,60.,float(config["max_drawdown"]*100))
                sacrifice = st.number_input("相对持有最多少赚（百分点）",0.,50.,float(config["max_return_sacrifice"]*100))
                improvement = st.number_input("要求回撤至少改善（百分点）",.1,20.,float(config["min_improvement"]*100))
                exposure = st.number_input("最低模拟持有时间占比（%）",5.,95.,float(config["min_exposure"]*100))
                turnover = st.number_input("最多模拟年化买卖次数",1,60,config["max_trades_year"])
                horizon = st.number_input("固定前向观察交易日数",63,504,config["shadow_sessions"])
                min_trades = st.number_input("观察期至少完成模拟交易次数",2,30,config["min_shadow_trades"])
                interval = st.number_input("研究间隔（自然日）",1,365,config["research_days"])
                cooldown = st.number_input("两次自动更新最少间隔（自然日）",30,730,config["promotion_days"])
                budget = st.number_input("自动研究年度最多历史试验情景数",18,360,config["annual_trial_budget"])
                cost = st.number_input("模拟单边成本（万分之）",0,500,config["cost_bps"])
                delay = st.number_input("模拟成交延迟（交易日）",1,10,config["delay"])
            accepted = st.checkbox("我确认上述范围；回撤门槛不保证实际最大亏损，指数模拟不等于基金成交")
            if st.form_submit_button("保存自动进化配置",type="primary"):
                if not accepted:
                    st.error("请先确认目标、授权范围和模拟限制。")
                else:
                    values = dict(enabled=enabled,auto_inspect=inspect,auto_promote=promote,factor_research=factor_research,factor_profiles=profiles,ma_min=int(minimum),ma_max=int(maximum),max_drawdown=max_dd/100,max_return_sacrifice=sacrifice/100,min_improvement=improvement/100,min_exposure=exposure/100,max_trades_year=int(turnover),shadow_sessions=int(horizon),min_shadow_trades=int(min_trades),research_days=int(interval),promotion_days=int(cooldown),annual_trial_budget=int(budget),cost_bps=int(cost),delay=int(delay))
                    result = guarded(lambda:svc.set_evolution(selected["code"],values))
                    if result:
                        st.session_state.evolution_notice = {'code':selected['code'],'message':"设置未变化，当前研究与观察进度已保留。" if saved and result==saved['id'] else "设置已保存，后台将按新范围研究；旧候选已结束。"}
                        st.rerun()
    with st.expander("需要时：手动检查研究"):
        st.caption("后台原本会定期检查。这个按钮只提交或复用研究任务，不会立即换规则，也不会下单。")
        if st.button("立即提交后台策略研究",disabled=not saved or not config['enabled']):
            if guarded(lambda:svc.request_evolution(selected["code"])):
                st.success("请求已保存或复用进行中的研究，后台分批执行；可以关闭网页。")
    with st.expander('基础指标与专业因子如何参与研究'):
        st.caption("历史筛选 → 固定参数 → 用新行情模拟观察 → 达标后按授权更新。指数模拟不等于基金或账户收益。")
        st.write('专业接口的78项技术因子会完整保存。先用四个固定组合检验，不能把相关指标重复计票当作更多证据。')
        try:
            from .service import Evaluator
            from .engine import feature_frame, features_at
            manifest,_ = Evaluator(svc.store).inputs(selected['code'])
            frame = feature_frame(manifest['data'],manifest['sessions'],spec)
            # Show the common research window rather than demand warm-up factors on day one.
            start = manifest['sessions'][min(len(frame)-1,max(config['ma_max'],120)+spec['slope_window']-1)]
            available = coverage(frame,start)
            st.dataframe(pd.DataFrame([{'组合':v['label'],'完整覆盖':f"{v['coverage']:.1%}",'参与条件':'历史已覆盖' if v['eligible'] else '后台补充，暂不参选','固定规则':v['description']} for v in available.values()]),hide_index=True)
            latest = features_at(frame,manifest['as_of'])
            st.caption(f"行情截至 {manifest['as_of']}；历史完整不代表未来有效，仍需验证与前向观察。")
            st.dataframe(pd.DataFrame([{'指标':label(k),'最新值':number(latest.get(k),k in ('pe_percentile','pb_percentile','volatility20','drawdown120'))} for k in ['pe','pb','pe_percentile','pb_percentile','amount_ratio','volatility20','drawdown120',*TECH_FIELDS] if latest.get(k) is not None]),hide_index=True)
        except (ValueError,RuntimeError):
            st.info('后台正在准备数据；无需手动补历史。')
        endpoints = svc.store.rows('SELECT key,value FROM settings WHERE key LIKE ?',('factor_sync:'+selected['code']+':%',))
        for endpoint in endpoints:
            status = json.loads(endpoint['value']).get('status')
            if status in ('NO_PERMISSION','RETRY','DELAYED'):
                st.caption('专业数据状态：'+label(status)+'；价格规则继续运行，后台会重试。')


def today_items(svc, code=None):
    """Classify current decisions before rendering; old results never become a to-do."""
    items = []
    for row in svc.results():
        if code is not None and row['code']!=code:
            continue
        signal, decision = json.loads(row["signal"]), json.loads(row["decision"])
        try:
            state = svc.decision_state(row['decision_id'])
            current = state['current']
        except (ValueError,RuntimeError):
            state = {'feedback':[]}
            current = False
        feedback = state['feedback']
        handled = bool(feedback and feedback[0]["status"] in ("SUBMITTED","EXECUTED","PARTIAL","DEFERRED","CANCELLED","EXPIRED"))
        category = 'waiting' if not current or decision['status'] != 'AVAILABLE' else 'action' if decision['action'] in ('BUY','SELL') and not handled else 'quiet'
        items.append(dict(row=row,signal=signal,decision=decision,current=current,feedback=feedback,handled=handled,category=category))
    return sorted(items,key=display_priority)


@st.fragment(run_every='5s')
def today_summary(svc):
    from .ui_components import queue_panel
    items = today_items(svc)
    active = [j for j in svc.jobs() if j['status'] in ('PENDING','RUNNING','RETRY')]
    watches = svc.watchlist()
    awaiting = len({w['code'] for w in watches} - {item['row']['code'] for item in items})
    counts = summary_counts(items,awaiting)
    summary_stats([
        ("待你处理", counts['action'], "买卖判断，或已暂缓待决定"),
        ("无需新动作", counts['quiet'], "持有、空仓等待或已确认成交"),
        ("待更新或确认", counts['waiting'], "含待成交、部分成交及数据更新"),
    ])
    if counts['followup']:
        st.caption(f"有 {counts['followup']} 项只是暂缓、已提交或部分成交，尚未确认全部成交。查看对应卡片跟进，不代表需要重新下单。")
    if active:
        queue_panel(active)


def today_panel(svc, guarded):
    # The parent stays fixed. Individual cards refresh without moving a saved
    # card into another group or rerendering the whole page beneath the user.
    today_summary(svc)
    items = today_items(svc)
    watches = svc.watchlist()
    if not items:
        if watches:
            empty_state("指数已加入，正在准备判断", "后台会补齐行情并巡检。你可以关闭网页，稍后回来查看结果。")
        else:
            empty_state("从关注的第一个指数开始", "添加指数后，系统会自动维护数据、生成判断；未登记持仓默认空仓。")
            navigate_button("添加我的第一个指数", "监控与数据", "onboarding_watch")
            return
    st.caption('尚未执行或已暂缓的退出优先展示；报单或部分成交先核对订单。保存后卡片留在原位，重新打开页面时再排序。')
    for item in items:
        today_card(svc,item['row']['code'],guarded)
    for watch in watches:
        if watch['code'] not in {item['row']['code'] for item in items}:
            today_card(svc,watch['code'],guarded)


EXECUTION_REASONS = ['按系统建议操作','我相信后面会涨','再观望下','暂时没有可用资金','已经手动处理','等待基金确认成交','其他']


def execution_outcome_changed(decision_id):
    if st.session_state.get('execution_outcome_'+decision_id) not in ('EXECUTED','PARTIAL'):
        st.session_state['execution_position_'+decision_id] = False


def save_today_execution(svc, code, decision_id, action):
    """Save before the card rerenders: no full-page rerun or list reordering."""
    outcome = st.session_state['execution_outcome_'+decision_id]
    reason = st.session_state['execution_reason_'+decision_id]
    note = st.session_state.get('execution_other_'+decision_id,'').strip() if reason=='其他' else reason
    update = st.session_state.get('execution_position_'+decision_id,False)
    position = ('EMPTY' if action=='SELL' and outcome=='EXECUTED' else 'HELD') if update else None
    try:
        if outcome not in ('SUBMITTED','EXECUTED','PARTIAL','DEFERRED'):
            raise ValueError('请先选择实际处理结果；尚未下单可选择“暂缓”。')
        if update and outcome not in ('EXECUTED','PARTIAL'):
            raise ValueError('尚未确认成交，不能同步成交后的持仓。')
        if reason=='其他' and not note:
            raise ValueError('选择“其他”时，请填写说明。')
        svc.confirm_execution(decision_id,outcome,note,position)
    except (ValueError,PermissionError,RuntimeError,ImportError) as exc:
        st.session_state['execution_saved_'+code] = {'ok':False,'message':str(exc)}
        return
    message = '处理已保存；卡片保留在原位，系统不会替你下单。'
    if update:
        try:
            watch = next(w for w in svc.watchlist() if w['code']==code)
            svc.submit(watch['id'],refresh=True)
        except (ValueError,PermissionError,RuntimeError,ImportError):
            message += ' 新巡检暂未提交，后台会继续自动更新。'
    st.session_state['execution_saved_'+code] = {'ok':True,'message':message}


@st.fragment(run_every='5s')
def today_card(svc, code, guarded):
    from .ui_components import today_details
    item = next(iter(today_items(svc,code)),None)
    if not item:
        watch = next((w for w in svc.watchlist() if w['code']==code),None)
        if watch:
            with st.container(border=True):
                st.write(watch['name']+' · '+code)
                st.caption('后台正在准备判断，完成后此处自动更新。')
        return
    if item:
        row,signal,decision = item['row'],item['signal'],item['decision']
        group = item['category']
        watches = svc.watchlist()
        with st.container(border=True):
            action = decision['action']
            handled = handling(item['feedback'][0]['status'],action) if item['handled'] else None
            title = "等待更新" if not item['current'] else label(decision['status']) if decision['status'] != 'AVAILABLE' else handled['title'] if handled else label(action)
            tone = 'attention' if group=='waiting' or handled and handled['followup'] else action.lower() if group=='action' else 'quiet'
            card_heading(row['name'],row['code'],title,tone)
            notice = st.session_state.get('execution_saved_'+code)
            if notice:
                (st.success if notice['ok'] else st.error)(notice['message'])
            st.caption("判断来源：程序规则 · 规则阶段："+label(signal['research_stage']))
            st.caption('规则触发不等于已验证盈利；证据见“策略与进化”的策略体检。')
            if not item['current']:
                st.warning("这是上次结果，暂不作为当前操作。后台更新完成后会自动替换。")
            elif decision['status'] != 'AVAILABLE':
                st.warning(label(decision['status']))
                for reason in decision['required_context']+decision['reasons']:
                    st.caption(readable(reason))
                if svc.position(row['code']) and st.button("确认期间未交易并重新巡检",key="unchanged_"+row['decision_id']):
                    if guarded(lambda:svc.confirm_unchanged(row['code'])):
                        guarded(lambda:svc.submit(next(w['id'] for w in watches if w['code']==row['code']),refresh=True))
                        st.success('已提交重新巡检，结果会在此处更新。')
            elif item['handled']:
                st.write(handled['text'])
                st.caption("原操作："+label(action))
            elif action in ('BUY','SELL'):
                st.write("**建议操作："+('买入' if action=='BUY' else '卖出')+"** · "+("参考金额 "+number(decision['amount'])+" "+label(decision['currency']) if decision['amount'] else "金额由你决定"))
                st.caption("；".join(readable(x) for x in decision['reasons']) or "按现行规则与持仓生成")
            else:
                st.write(label(action)+"，当前无需操作。")
                if decision['reasons']:
                    st.caption("；".join(readable(x) for x in decision['reasons']))
            if decision.get('position_basis') == 'DEFAULT_EMPTY':
                st.caption('本条按默认空仓判断；如果实际持有，请到“我的持仓”更新。')
            if item['current'] and decision['status']=='AVAILABLE':
                context = plan_context(signal,decision)
                if context['event']:
                    st.caption(context['event'])
                st.caption(context['validity'])
            if item['current'] and decision['status']=='AVAILABLE' and action in ('BUY','SELL') and svc.store.version>=2:
                with st.expander('记录我的处理结果',key='execution_panel_'+code,on_change='rerun'):
                    st.caption("系统不自动下单。成交后可在这里同步确认真实持仓。")
                    outcome = st.selectbox("处理结果",['SUBMITTED','EXECUTED','PARTIAL','DEFERRED'],index=None,placeholder='请选择实际处理结果',format_func=label,key='execution_outcome_'+row['decision_id'],on_change=execution_outcome_changed,args=(row['decision_id'],))
                    reason = st.selectbox('执行说明或暂缓原因',EXECUTION_REASONS,key='execution_reason_'+row['decision_id'])
                    note = st.text_input('其他说明',key='execution_other_'+row['decision_id']) if reason=='其他' else reason
                    update = st.checkbox("同时确认成交后的真实持仓状态",key='execution_position_'+row['decision_id'],disabled=outcome not in ('EXECUTED','PARTIAL'),help="仅已成交/部分成交时可勾选。未填写的旧市值与份额会变为未知，现金需另行确认。")
                    st.button('保存处理结果',key='execution_save_'+row['decision_id'],disabled=outcome is None,on_click=save_today_execution,args=(svc,code,row['decision_id'],action))
            today_details(svc,row,signal)
