"""Readable cards and a durable, automatically refreshed inspection dialog."""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from .presentation import label, local_time, number, readable
from .worker import worker_status


def start_inspection(svc, watches, include_ai=None, kind="inspection", start=None, force_repair=False):
    include_ai = svc.inspection_ai_enabled() if include_ai is None else include_ai
    ids = []
    skipped,ai_only,changed = 0,0,0
    for watch in watches:
        if force_repair:
            ids.append(svc.request_data_repair(watch['id'],include_ai))
        elif kind == 'inspection' and not start:
            request = svc.request_inspection(watch['id'],include_ai)
            skipped += request['status'] == 'ALREADY_DONE'
            ai_only += request['status'] == 'AI_ONLY'
            changed += request['status'] == 'UPDATED'
            if request['job_id']: ids.append(request['job_id'])
        else:
            ids.append(svc.submit(watch['id'],refresh=True,include_ai=include_ai,kind=kind,start=start))
    if skipped:
        st.info(f'今天已有 {skipped} 项巡检'+('及AI解读' if include_ai else '')+'生成，已复用结果，无需重复生成。')
    if ai_only:
        st.info(f'今天已有 {ai_only} 项巡检完成，本次只补生成AI解读。')
    if changed:
        st.info(f'今天已有 {changed} 项巡检生成，因行情、持仓或规则等输入更新，需要重新计算。')
    if ids:
        st.session_state.loading_jobs = ids
    elif not watches:
        st.info("还没有监控指数，请先到“监控与数据”添加。")


def data_repair_controls(svc, watch, key):
    records = svc.store.rows('SELECT value FROM settings WHERE key=?',('history_repair:'+watch['code'],))
    if records:
        state = json.loads(records[0]['value'])
        missing = state.get('missing_sessions',0)
        essential = state.get('essential_missing_sessions')
        st.caption(f'历史待补齐：{missing} 个交易日'+(f'；当前规则关键窗口缺 {essential} 个交易日' if essential is not None else ''))
        if 'changed_sessions' in state:
            st.caption(f"上次检查补回或更新 {state['changed_sessions']} 条行情 · "+local_time(state.get('checked_at')))
        if state.get('requests'):
            with st.expander('最近补齐请求明细'):
                st.caption('请求时间：'+local_time(state.get('requests_checked_at')))
                st.dataframe(pd.DataFrame([{'开始日期':r['start'],'结束日期':r['end'],'接口返回条数':r['returned'],
                                            '补回或更新':r['changed'],'接口状态':{'RETURNED':'已返回数据','EMPTY':'本次返回为空','RETRY':'请求失败，稍后重试'}.get(r['status'],'待核实')}
                                           for r in state['requests']]),hide_index=True,width='stretch')
    if st.button('立即补齐缺口',key=key,help='立即重试缺失历史并重新巡检，不删除已有记录；未完成部分由后台分批继续。'):
        try:
            start_inspection(svc,[watch],force_repair=True)
            st.info('补齐请求已提交；本次会重新请求缺口，完成后更新判断。相同待执行请求会合并。')
            # The data card is a fragment; refresh the parent once so the saved
            # request opens the same durable progress dialog as other tasks.
            st.rerun()
        except (ValueError,PermissionError,RuntimeError) as exc:
            st.error(str(exc))


def dismiss_loading():
    st.session_state.pop("loading_jobs",None)


@st.fragment(run_every="2s")
def loading_progress(svc):
    ids = st.session_state.get("loading_jobs",[])
    jobs = svc.jobs(ids)
    if not jobs:
        dismiss_loading()
        st.rerun()
    completed = sum(j["status"] in ("SUCCEEDED","FAILED","CANCELLED") for j in jobs)
    receiving = completed < len(jobs) and all(j.get('queue_state') == 'STARTING' or j['status'] in ('SUCCEEDED','FAILED','CANCELLED') for j in jobs)
    st.status("请求已提交，后台准备接收" if receiving else "正在处理，请稍候…" if completed < len(jobs) else "处理结束",state="running" if completed < len(jobs) else "complete",expanded=False)
    st.progress(completed/len(jobs),text=f"已完成 {completed} / {len(jobs)} 项")
    if not worker_status(svc.store):
        st.warning("后台尚未就绪。请求已经保存，请启动页面与后台；恢复后会继续处理。")
    for job in jobs:
        phase = job["progress"]["phase"]
        title = job.get('queue_title') or (label(job["status"]) if job["status"] in ("RETRY","FAILED","CANCELLED","SUCCEEDED") else label(phase))
        with st.container(border=True):
            st.write(f"**{job['name']}** · {title}")
            st.caption(job.get('queue_hint') or job["error"] or job["progress"]["detail"])
    st.caption("更新行情 → 校验与分析 → 可选AI解读。关闭此窗口不会取消任务。")
    if completed == len(jobs):
        st.session_state.inspection_notice = {
            "success":sum(j["status"] == "SUCCEEDED" for j in jobs),
            "errors":[f"{j['name']}：{j['error']}" for j in jobs if j["status"] in ("FAILED","CANCELLED")],
        }
        dismiss_loading()
        st.rerun()
    if st.button("关闭进度窗口，后台继续处理",key="close_loading"):
        dismiss_loading()
        st.rerun()


@st.dialog("正在巡检 / 更新行情",width="large",on_dismiss=dismiss_loading)
def inspection_dialog(svc):
    loading_progress(svc)


def explanation_title(record):
    if not record:
        return "解读尚未生成"
    if record.get('status') in ('MODEL_OK','TEMPLATE','FAILED_TEMPLATE'):
        return label(record['status'])
    return "解读来源待确认"


def explanation_panel(record):
    if not record:
        st.caption("解读尚未生成，可在下方生成AI解读。")
        return
    payload = record["payload"]
    st.markdown("#### "+explanation_title(record))
    if record['status']=='MODEL_OK':
        st.caption("内容来源：AI。下方摘要、反面观点、分析局限和情景说明均由AI根据程序证据生成。")
        if payload.get('reuse'):
            st.caption('当前指标和规则判断未变化，已复用已有AI解读，未再次调用模型。原生成时间：'+local_time(payload['reuse']['original_generated_at']))
    elif record['status'] in ('TEMPLATE','FAILED_TEMPLATE'):
        st.caption("内容来源：程序固定模板。下方摘要和风险说明由程序生成，不是AI生成的内容。")
    else:
        st.caption("该记录的内容来源暂时无法确认。")
    if record["status"] == "FAILED_TEMPLATE":
        diagnostic = payload.get('diagnostic')
        if diagnostic:
            st.warning(diagnostic['message']+" 以下是程序解读。")
            with st.expander('AI失败诊断详情'):
                st.json(diagnostic)
        else:
            st.warning("此次AI解读未成功，旧记录未保存具体原因。以下是程序解读，重试后将记录具体诊断。")
    st.write(readable(payload["summary"]))
    with st.expander("反面观点与分析局限"):
        for title,key in (("需要考虑的反面观点","counterarguments"),("分析局限","limitations")):
            st.markdown("**"+title+"**")
            for text in payload.get(key,[]):
                st.write("• "+readable(text))
        for scenario in payload.get("scenarios",[]):
            st.write("**情景：** "+readable(scenario["assumption"]))
            st.write("可能影响："+readable(scenario["implication"]))
    if payload.get("evidence_refs"):
        st.caption("引用指标："+"、".join(label(key) for key in payload["evidence_refs"]))
    st.caption("生成时间："+local_time(record["created_at"])+"。买卖判断与参考金额由程序规则产生，AI仅负责解释。")


def evidence_panel(db, signal, expanded=False, svc=None, signal_id=None, chart_key='evidence'):
    with st.expander("查看数据与判断依据",expanded=expanded):
        st.caption("来源：行情与指标数据，以及程序规则计算的判断依据；此处不是AI生成的解读。")
        st.write("数据状态："+label(signal["data_status"]))
        feature = json.loads(db.rows("SELECT payload FROM feature_snapshots WHERE id=?", (signal["feature_id"],))[0]["payload"])
        spec = json.loads(db.rows("SELECT spec FROM strategy_versions WHERE id=?", (signal["strategy_id"],))[0]["spec"])
        if signal['data_status'] == 'INSUFFICIENT_DATA':
            details = signal.get('data_readiness')
            if not details:
                from .data_maintenance import readiness
                from .engine import feature_frame
                manifest = json.loads(db.rows('SELECT manifest FROM datasets WHERE id=?',(signal['dataset_id'],))[0]['manifest'])
                details = readiness(feature_frame(manifest['data'],manifest['sessions'],spec),manifest['data'],spec)
            st.warning(f"有最新点位，但规则需要最近 {details['required_sessions']} 个连续有效交易日；当前连续有效 {details['continuous_valid_sessions']} 个。")
            if details['short_sessions']:
                st.caption(f"这份分析的历史范围还少 {details['short_sessions']} 个交易日。")
            for field,title in (('missing_dates','缺少行情的交易日'),('invalid_dates','开高低收不完整、数值异常或来源待核实的交易日')):
                if details[field]:
                    st.caption(title+'：'+'、'.join(details[field][:8])+('等' if len(details[field])>8 else ''))
            st.caption('后台会按重试时间自动补齐并重新巡检；不会用前一天价格填补缺口。')
        ma_key = f"ma{spec['ma']}"
        cols = st.columns(3)
        cols[0].metric("指数收盘点位",number(feature.get("close")))
        cols[1].metric(f"{spec['ma']}日平均点位",number(feature.get(ma_key)))
        cols[2].metric(f"均线近{spec['slope_window']}日变化",number(feature.get(ma_key+"_slope"),True))
        st.caption("均线是过去一段交易日收盘点位的平均值；变化为正表示均线向上。")
        st.dataframe(pd.DataFrame([
            {"指标":"连续高于均线","数值":f"{int(feature.get('above') or 0)} 个交易日"},
            {"指标":"连续低于均线","数值":f"{int(feature.get('below') or 0)} 个交易日"},
            {"指标":"触发所需连续天数","数值":f"{spec['confirmation']} 个交易日"},
            {"指标":"市盈率历史分位","数值":number(feature.get("pe_percentile"),True)},
            {"指标":"估值有效样本数","数值":str(int(feature.get("pe_samples") or 0))},
            {"指标":"已观察到的市场特征","数值":label(feature.get("regime"))},
        ]),hide_index=True,width="stretch")
        for trace in signal["rule_trace"]:
            st.write("• "+readable(trace))
        st.caption("市场特征描述已发生的行情，不预测牛熊。估值不会临时取消退出条件。")
        if svc and signal_id:
            from .ui_trends import trend_expander
            trend_expander(svc,signal['code'],chart_key,signal_id)
        with st.expander("技术版本详情（供排查和复核）"):
            st.json({"数据版本":signal["dataset_id"],"规则版本":signal["strategy_id"],"模型持仓状态":label(signal["model_state"])})


@st.fragment(run_every="5s")
def signal_dashboard(svc):
    jobs = svc.jobs()
    active = [j for j in jobs if j["status"] in ("PENDING","RUNNING","RETRY")]
    if active:
        queue_panel(active)
    with st.expander("筛选指数状态"):
        filter_by = st.multiselect("显示指数状态",["BUY","SELL","HOLD","WAIT","数据未通过"],default=["BUY","SELL","HOLD","WAIT","数据未通过"],format_func=label,key="signal_filter")
    results = svc.results()
    st.header("逐项查看")
    if not results:
        st.info("还没有分析结果。添加指数后后台会自动更新行情并巡检；需要立即检查时，可点击上方巡检按钮。")
    for row in results:
        signal,decision = json.loads(row["signal"]),json.loads(row["decision"])
        if (signal["state_label"] or "数据未通过") not in filter_by:
            continue
        with st.container(border=True):
            st.subheader(row["name"])
            st.caption(f"{row['code']} · 行情截至 {row['as_of']} · {label(signal['research_stage'])}")
            try:
                market = svc.market_status(row["code"])
                state = svc.decision_state(row['decision_id'])
                stale = not state['current']
            except (ValueError,RuntimeError):
                stale = True
                market = {"latest":None}
            if stale:
                st.warning("这是较早行情的分析结果，请重新巡检后再阅读当前判断。"+(f"共享行情已更新至 {market['latest']}。" if market["latest"] else ""))
            if any(j["code"] == row["code"] for j in active):
                st.caption("本卡片是上次完成的结果，新巡检完成后自动更新。")
            cols = st.columns(3)
            cols[0].metric("上次指数信号" if stale else "指数信号",label(signal["state_label"]) if signal["state_label"] else "数据未通过")
            handled = not stale and bool(state['feedback'] and state['feedback'][0]['status'] in ('SUBMITTED','EXECUTED','PARTIAL','DEFERRED','CANCELLED','EXPIRED'))
            cols[1].metric("针对你的判断","需要重新巡检" if stale else '已处理' if handled else label(decision["action"] or decision["status"]))
            cols[2].metric("参考金额",number(decision["amount"]) if decision["amount"] and not stale and not handled else "由你决定")
            st.caption("以上指数信号、个人判断与参考金额由程序规则产生。")
            if decision.get("currency"):
                st.caption("金额币种："+label(decision["currency"]))
            st.caption("最近一次规则触发："+(signal["triggered_at"] or "暂无")+"。持有/等待状态不代表今天又出现新买卖信号。")
            if decision["status"] != "AVAILABLE":
                st.warning("需要先处理："+label(decision["status"]))
            for text in decision["reasons"]+decision["required_context"]:
                st.write("• "+readable(text))
            evidence_panel(svc.store,signal,svc=svc,signal_id=row['signal_id'],chart_key='signals')
            record = svc.saved_explanation(row['decision_id'])
            with st.expander(explanation_title(record)):
                explanation_panel(record)
                ai_action(svc,row['decision_id'],'ai_','生成AI解释')


def queue_panel(active):
    if any(j.get('queue_state') == 'OFFLINE' for j in active):
        st.warning(f'有 {len(active)} 项请求等待后台恢复；当前没有后台服务在执行它们。')
    elif any(j.get('queue_state') in ('RECOVERING','RETRY','DELAYED') for j in active):
        st.warning(f'有 {len(active)} 项请求等待执行、重试或接续，可展开进度查看原因。')
    elif all(j.get('queue_state') == 'STARTING' for j in active):
        st.info(f'已提交 {len(active)} 项请求，后台准备接收；无需重复点击。')
    elif not any(j['status']=='RUNNING' for j in active) and any(j.get('queue_state')=='WAITING' for j in active):
        st.info(next(j['queue_hint'] for j in active if j.get('queue_state')=='WAITING'))
    else:
        running = sum(j['status']=='RUNNING' for j in active)
        st.info(f'正在处理 {running} 项，等待执行 {len(active)-running} 项；每5秒刷新状态。')
    with st.expander('查看任务进度与原因'):
        st.dataframe(pd.DataFrame([{'指数':j['name'],'状态':j.get('queue_title') or label(j['status']),
                                  '说明':j.get('queue_hint') or j['error'] or j['progress']['detail']} for j in active]),hide_index=True,width="stretch")


def background_status_panel(svc):
    state = svc.background_status()
    if not state['ready']:
        st.caption('后台状态：未检测到运行心跳，请求会保留，恢复后继续。')
    elif state['activities']:
        st.caption('后台当前：'+'；'.join(item['description'] for item in state['activities']))
    else:
        st.caption('后台当前没有已登记的执行步骤；新请求等待接收，不代表有其他任务在排队。')
    if state['own_waiting'] or state['other_waiting']:
        st.caption(f"待接收巡检或同步：本账号 {state['own_waiting']} 项，其他账号 {state['other_waiting']} 项。其他账号的指数与账户信息不展示。")


def ai_action(svc, decision_id, key_prefix, title='生成AI解读', refresh=True):
    ai_pause = svc.ai_availability()
    if ai_pause:
        st.caption(ai_pause['message']+' 自动AI请求暂停至 '+local_time(ai_pause['retry_at'])+'；程序判断继续运行。下方按钮可立即尝试恢复。')
    if st.button(title,key=key_prefix+decision_id,help='已有成功解读会直接复用；失败后可重试。'):
        saved = svc.saved_explanation(decision_id)
        if saved and saved['status'] == 'MODEL_OK':
            today = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Hong_Kong')).date()
            generated = datetime.fromisoformat(saved['created_at']).astimezone(ZoneInfo('Asia/Hong_Kong')).date()
            st.info(('今天' if generated == today else '此前')+'已生成AI解读，已展示已有结果。生成时间：'+local_time(saved['created_at']))
            return
        with st.spinner('正在生成AI解读，请稍候…'):
            try:
                svc.explanation(decision_id,True)
                latest = svc.saved_explanation(decision_id)
                reason = latest['payload'].get('diagnostic',{}).get('message') if latest else None
                st.session_state.ai_notice = {'ok':bool(latest and latest['status']=='MODEL_OK'),'message':'AI解读已保存。' if latest and latest['status']=='MODEL_OK' else (reason or 'AI未成功，可稍后重试。')+' 已保留程序解读。'}
            except (ValueError,PermissionError,RuntimeError):
                st.session_state.ai_notice = {'ok':False,'message':'AI正在生成或暂时不可用，已保留原结果，请稍后查看。'}
        if refresh:
            st.rerun()
        notice = st.session_state.pop('ai_notice')
        (st.success if notice['ok'] else st.warning)(notice['message'])


def today_details(svc, row, signal):
    """Available for HOLD/WAIT, blocked and historical cards alike."""
    feature = json.loads(svc.store.rows('SELECT payload FROM feature_snapshots WHERE id=?',(signal['feature_id'],))[0]['payload'])
    st.caption('指数行情截至 '+row['as_of']+' · 收盘点位 '+number(feature.get('close'))+' · 近5日涨跌 '+number(feature.get('return5'),True))
    record = svc.saved_explanation(row['decision_id'])
    st.caption('当前解读：'+explanation_title(record))
    # Decision IDs change after new evidence or daily expiry. Keep the user's
    # selected view while rendering the replacement decision's own evidence.
    selection = 'today_detail_'+str(svc.user['id'])+'_'+row['code']
    with st.container(horizontal=True):
        if st.button('查看数据',key='today_data_'+row['decision_id']):
            st.session_state[selection] = 'data'
        if st.button('查看曲线',key='today_chart_'+row['decision_id']):
            st.session_state[selection] = 'chart'
        if st.button('查看解读',key='today_ai_'+row['decision_id'],help='可能是AI解读或程序解读，具体来源以内容开头的标识为准。'):
            st.session_state[selection] = 'ai'
        if st.session_state.get(selection) and st.button('收起详情',key='today_close_'+row['decision_id']):
            st.session_state.pop(selection,None)
    if st.session_state.get(selection) == 'data':
        st.caption('行情截至 '+row['as_of'])
        evidence_panel(svc.store,signal,expanded=True,svc=svc,signal_id=row['signal_id'],chart_key='today_evidence')
        watch = next((w for w in svc.watchlist() if w['code']==row['code']),None)
        if watch:
            data_repair_controls(svc,watch,'today_repair_'+row['code'])
    elif st.session_state.get(selection) == 'chart':
        from .ui_trends import trend_panel
        trend_panel(svc,row['code'],'today',row['signal_id'])
    elif st.session_state.get(selection) == 'ai':
        explanation_panel(record)
        ai_action(svc,row['decision_id'],'today_generate_',refresh=False)
