"""User-scoped evolution audit: submitted research is distinct from applied changes."""
import json

from .factors import PROFILES


SOURCES = {'MANUAL_DAILY':'手动进化并应用','MANUAL_RESEARCH':'手动提交研究',
           'AUTOMATIC':'后台自动研究','MANUAL_ROLLBACK':'手动回退','LEGACY':'历史记录（发起方式未记录）'}
PARAMETERS = {'ma':'均线交易日数','confirmation':'连续确认交易日数','slope_window':'均线变化观察日数',
              'factor_profile':'入场因子组合','valuation_window':'估值观察交易日数',
              'annual_sessions':'年化计算交易日数','name':'规则类型'}


def display(key,value):
    if key=='factor_profile':
        return PROFILES.get(value,{}).get('label',value or '无附加因子')
    if key=='name':
        return {'trend_confirm_v1':'均线趋势规则','trend_factor_v2':'趋势与因子规则'}.get(value,str(value))
    return str(value) if value is not None else '未设置'


def changes(before,after):
    return [{'parameter':PARAMETERS.get(k,k),'before':display(k,before.get(k)),'after':display(k,after.get(k))}
            for k in PARAMETERS if before.get(k)!=after.get(k)]


def source(payload):
    return 'MANUAL_DAILY' if payload.get('mode')=='DAILY_APPLY' else payload.get('trigger') if payload.get('trigger') in SOURCES else 'LEGACY'


def records(store, uid, code, limit=250):
    limit = min(max(int(limit),1),250)
    rows = store.rows('''SELECT r.id,r.created_at,r.updated_at,r.status,r.error,r.payload,
        k.status candidate_status,k.payload candidate_payload,
        p.created_at applied_at,p.payload promotion_payload,p.new_assignment,
        bs.spec before_spec,ns.spec after_spec,na.active new_active,base.spec base_spec
        FROM evolution_runs r JOIN evolution_policies policy ON policy.id=r.policy_id
        JOIN assignments a ON a.id=r.assignment_id JOIN strategy_versions base ON base.id=a.strategy_id
        LEFT JOIN evolution_candidates k ON k.id=r.id
        LEFT JOIN strategy_promotions p ON p.candidate_id=r.id AND p.user_id=policy.user_id AND p.code=policy.code
        LEFT JOIN assignments ba ON ba.id=p.old_assignment
        LEFT JOIN strategy_versions bs ON bs.id=ba.strategy_id
        LEFT JOIN assignments na ON na.id=p.new_assignment
        LEFT JOIN strategy_versions ns ON ns.id=na.strategy_id
        WHERE policy.user_id=? AND policy.code=?
        ORDER BY COALESCE(p.created_at,r.created_at) DESC,r.id DESC LIMIT ?''',(uid,code,limit))
    result = []
    statuses = {'WAITING_DATA':'等待行情，参数未变','PENDING':'等待研究，参数未变','RUNNING':'研究中，参数未变',
                'FROZEN':'历史研究完成，参数未变','FORWARD_SHADOW':'新行情观察中，参数未变',
                'ELIGIBLE':'观察通过，尚未应用','READY_TO_APPLY':'等待应用新参数',
                'REJECTED':'未通过检验，保留原规则','QUARANTINED':'已隔离，保留现行规则',
                'CANCELLED':'任务已结束，参数未变','FAILED':'研究失败，参数未变'}
    for row in rows:
        payload = json.loads(row['payload'])
        candidate = json.loads(row['candidate_payload']) if row['candidate_payload'] else {}
        applied = bool(row['applied_at'])
        before = json.loads(row['before_spec']) if applied else payload.get('base_spec') or json.loads(row['base_spec'])
        after = json.loads(row['after_spec']) if applied else before
        origin = source(payload)
        status = row['candidate_status'] or row['status']
        outcome = ('已生效' if row['new_active'] else '曾生效，现已回退' if status=='RETIRED' else '曾生效，现已被替换') if applied else statuses.get(status,'未应用，参数未变')
        reasons = payload.get('assessment',{}).get('reasons',[])
        reason = json.loads(row['promotion_payload']).get('reason','') if applied else candidate.get('quarantine_reason') or row['error'] or '；'.join(reasons) or statuses.get(status,'保留现行规则，详细依据见研究进度')
        result.append({'id':'run:'+row['id'],'run_id':row['id'],'requested_at':row['created_at'],
                       'applied_at':row['applied_at'],'event_at':row['applied_at'] or row['created_at'],
                       'source':origin,'source_label':SOURCES[origin],'applied':applied,'outcome':outcome,
                       'application':'手动进化应用' if applied and origin=='MANUAL_DAILY' else '长期观察通过后自动应用' if applied else '尚未应用',
                       'changes':changes(before,after) if applied else [],
                       'candidate_changes':changes(before,candidate['spec']) if not applied and candidate.get('spec') else [],
                       'reason':reason,'before_spec':before,'after_spec':after})
    events = store.rows('''SELECT e.id,e.candidate_id,e.created_at,e.payload,bs.spec before_spec,ns.spec after_spec
        FROM strategy_events e
        JOIN assignments ba ON ba.id=json_extract(e.payload,'$.from_assignment') AND ba.user_id=e.user_id AND ba.code=e.code
        JOIN assignments na ON na.id=json_extract(e.payload,'$.to_assignment') AND na.user_id=e.user_id AND na.code=e.code
        JOIN strategy_versions bs ON bs.id=ba.strategy_id JOIN strategy_versions ns ON ns.id=na.strategy_id
        WHERE e.user_id=? AND e.code=? AND e.kind='ROLLBACK'
        ORDER BY e.created_at DESC,e.id DESC LIMIT ?''',(uid,code,limit))
    for event in events:
        before,after = json.loads(event['before_spec']),json.loads(event['after_spec'])
        result.append({'id':'rollback:'+str(event['id']),'run_id':event['candidate_id'],'requested_at':event['created_at'],
                       'applied_at':event['created_at'],'event_at':event['created_at'],'source':'MANUAL_ROLLBACK',
                       'source_label':SOURCES['MANUAL_ROLLBACK'],'applied':True,'outcome':'回退已执行','application':'手动回退',
                       'changes':changes(before,after),'candidate_changes':[],
                       'reason':json.loads(event['payload']).get('reason','用户明确回退'),'before_spec':before,'after_spec':after})
    return sorted(result,key=lambda r:(r['event_at'],r['id']),reverse=True)[:limit]
