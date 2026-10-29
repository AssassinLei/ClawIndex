"""User-scoped evidence for the rule actually in use; never a profit score."""
import json
import math
from datetime import datetime

from .storage import canonical, digest
from .validation import DEFAULT_POLICY


def matching(reports, spec, cost, delay):
    return next((r for r in reports if r.get('spec')==spec and r.get('cost_bps_per_side')==cost
                 and r.get('execution_delay_sessions')==delay),None)


def summary(report):
    strategy,hold = report['strategy'],report['buy_hold']
    count = len(report.get('curve',{}).get('date',[]))
    # Positive sacrifice means less return than holding; negative means more.
    return {'start':report['evaluation_start'],'end':report['evaluation_end'],'sessions':max(0,count-1),
            'return':strategy['return'],'hold_return':hold['return'],
            'return_sacrifice':hold['return']-strategy['return'],
            'max_drawdown':strategy['max_drawdown'],'hold_drawdown':hold['max_drawdown'],
            'drawdown_improvement':abs(hold['max_drawdown'])-abs(strategy['max_drawdown']),
            'exposure':1-report['empty_fraction'],'trades':report['trade_count'],
            'trades_year':report['trade_count']*report['spec']['annual_sessions']/max(1,count-1),
            'cost_bps':report['cost_bps_per_side'],'delay':report['execution_delay_sessions'],
            'data_status':report['comparison_status']}


def historical_source(store, uid, code, spec, policy):
    """Select metadata first; deserialize only the chosen rule's scenarios."""
    args = (canonical(spec),policy['cost_bps'],policy['delay'])
    match = "json_extract(t.value,'$.spec')=? AND json_extract(t.value,'$.cost_bps_per_side')=? AND json_extract(t.value,'$.execution_delay_sessions')=?"
    sources = store.rows('''SELECT r.id,r.created_at,'manual' kind FROM research_runs r
        WHERE r.user_id=? AND r.code=? AND EXISTS (SELECT 1 FROM json_each(r.payload,'$.results') t
        WHERE '''+match+''') ORDER BY r.created_at DESC,r.id DESC LIMIT 1''',(uid,code,*args))
    if store.version>=2:
        # A status/cursor alone is not proof that every planned trial exists or
        # that its rule, fees and delay agree with the reserved experiment.
        sources += store.rows('''SELECT r.id,r.created_at,'evolution' kind FROM evolution_runs r
            JOIN evolution_policies p ON p.id=r.policy_id WHERE p.user_id=? AND p.code=?
            AND r.status IN ('FROZEN','REJECTED') AND r.cursor=json_array_length(r.payload,'$.plan')
            AND r.cursor>0 AND json_extract(r.payload,'$.test_consumed')=1
            AND (SELECT COUNT(*) FROM evolution_trials x WHERE x.run_id=r.id)=r.cursor
            AND NOT EXISTS (SELECT 1 FROM evolution_trials x WHERE x.run_id=r.id AND (
                x.scenario<0 OR x.scenario>=r.cursor OR
                json_extract(x.payload,'$.spec') IS NOT json_extract(r.payload,'$.plan['||x.scenario||'].spec') OR
                json_extract(x.payload,'$.cost_bps_per_side') IS NOT json_extract(r.payload,'$.plan['||x.scenario||'].cost') OR
                json_extract(x.payload,'$.execution_delay_sessions') IS NOT json_extract(r.payload,'$.plan['||x.scenario||'].delay')))
            AND EXISTS (SELECT 1 FROM evolution_trials x WHERE x.run_id=r.id
                AND json_extract(x.payload,'$.spec')=? AND json_extract(x.payload,'$.cost_bps_per_side')=?
                AND json_extract(x.payload,'$.execution_delay_sessions')=?)
            ORDER BY r.created_at DESC,r.id DESC LIMIT 1''',(uid,code,*args))
    if not sources:
        return None
    source = max(sources,key=lambda s:(s['created_at'],s['id']))
    if source['kind']=='manual':
        row = store.rows("SELECT json_extract(payload,'$.dataset_id') dataset_id,json_extract(payload,'$.research_usable') usable FROM research_runs WHERE id=?",(source['id'],))[0]
        reports = store.rows("SELECT t.value payload FROM research_runs r,json_each(r.payload,'$.results') t WHERE r.id=? AND json_extract(t.value,'$.spec')=?",(source['id'],canonical(spec)))
        source.update(row,kind='手动历史研究')
    else:
        row = store.rows('SELECT manifest FROM evolution_runs WHERE id=?',(source['id'],))[0]
        usable = not store.rows("SELECT 1 FROM evolution_trials WHERE run_id=? AND json_extract(payload,'$.comparison_status') IS NOT 'EXPLORATORY' LIMIT 1",(source['id'],))
        reports = store.rows("SELECT payload FROM evolution_trials WHERE run_id=? AND json_extract(payload,'$.spec')=? ORDER BY scenario",(source['id'],canonical(spec)))
        source.update(dataset_id=digest(json.loads(row['manifest'])),usable=usable,kind='后台进化历史试验')
    source['reports'] = [json.loads(row['payload']) for row in reports]
    return source


def forward_checked(store, row, spec):
    """Recheck immutable observations, not a legacy ACTIVE status label."""
    from .evolution import forward_assessment
    try:
        payload,state = json.loads(row['candidate_payload']),json.loads(row['state'])
        policy = payload['policy']
        frozen = datetime.fromisoformat(payload['frozen_at'])
        assessment = payload['forward_assessment']
        if (row['status']!='ACTIVE' or payload['spec']!=spec or frozen.tzinfo is None
                or not assessment.get('fixed_horizon') or assessment.get('reasons')!=[]
                or state['count']!=policy['shadow_sessions'] or policy['shadow_sessions']<63
                or payload.get('assessment',{}).get('reasons')!=[]):
            return False
        observations = store.rows('SELECT as_of,payload FROM evolution_observations WHERE candidate_id=? ORDER BY as_of',(row['candidate_id'],))
        if (len(observations)!=policy['shadow_sessions'] or observations[-1]['as_of']!=state['last_as_of']
                or observations[0]['as_of']<=payload['frozen_as_of']):
            return False
        computed = forward_assessment([json.loads(o['payload']) for o in observations],policy,spec['annual_sessions'])
        return not computed['reasons']
    except (KeyError,TypeError,ValueError,IndexError,ZeroDivisionError):
        return False


def assessment(store, uid, code):
    rows = store.rows('''SELECT a.id,a.strategy_id,a.stage,v.spec,s.dataset_id,s.as_of
        FROM assignments a JOIN strategy_versions v ON v.id=a.strategy_id
        LEFT JOIN current_signals x ON x.assignment_id=a.id LEFT JOIN signal_runs s ON s.id=x.signal_id
        WHERE a.user_id=? AND a.code=? AND a.active=1''',(uid,code))
    if not rows:
        raise PermissionError('只能查看本账号当前监控规则的证据')
    current = rows[0]
    spec = json.loads(current['spec'])
    policy = dict(DEFAULT_POLICY)
    if store.version>=2:
        policies = store.rows('SELECT payload FROM evolution_policies WHERE user_id=? AND code=? AND active=1',(uid,code))
        if policies:
            policy.update(json.loads(policies[0]['payload'])['config'])
    result = {'code':code,'strategy_id':current['strategy_id'],'spec':spec,'stage':current['stage'],
              'as_of':current['as_of'],'level':'UNTESTED','title':'现行规则尚无可用检验证据',
              'explanation':'当前买卖提示来自规则触发，不能据此声称规则已证明有效。后台会按配置继续研究。',
              'historical':None,'stress':[],'issues':[],'source':None,'same_dataset':False,
              'candidate':None,'policy':{k:policy[k] for k in ('max_drawdown','max_return_sacrifice','max_trades_year')},
              'limitations':['以下是指数价格模拟，未代表实际基金申赎、分红、汇率或账户收益。',
                             '最大回撤门槛是研究条件，不能保证未来最大亏损。',
                             '个人持仓权重与账户现金未知时，不能据此证明组合风险可控。']}
    source = historical_source(store,uid,code,spec,policy)
    if source:
        report = matching(source['reports'],spec,policy['cost_bps'],policy['delay'])
        try:
            result['historical'] = summary(report)
            for scenario in source['reports']:
                if scenario.get('evaluation_start')==report['evaluation_start'] and scenario.get('evaluation_end')==report['evaluation_end']:
                    result['stress'].append(summary(scenario))
        except (KeyError,TypeError,ValueError,ZeroDivisionError):
            result['historical'],result['stress'] = None,[]
            result['issues'].append('历史报告字段不完整，未将其标记为通过检验。')
    if result['historical']:
        result['same_dataset'] = source['dataset_id']==current['dataset_id'] and bool(current['dataset_id'])
        result['source'] = {k:v for k,v in source.items() if k!='reports'}
        h = result['historical']
        valid_metrics = all(isinstance(h[k],(int,float)) and math.isfinite(h[k]) for k in
                            ('return','hold_return','max_drawdown','hold_drawdown','exposure','trades_year'))
        valid = source['usable'] and valid_metrics and h['data_status']=='EXPLORATORY' and h['sessions']>=spec['annual_sessions']
        if valid:
            result.update(level='HISTORICAL',title='已有历史模拟，仍需新行情验证',
                          explanation='可以看到控制回撤付出的收益与交易代价；历史结果不等于未来优势。')
        else:
            result['issues'].append('历史缺口、评估期或数值未通过检查，只能作诊断参考。')
        if not result['same_dataset']:
            result['issues'].append('历史试验与当前巡检使用的数据版本不同；以下数值属于注明日期的旧试验，不是最新效果。')
        if valid_metrics:
            if abs(h['max_drawdown'])>policy['max_drawdown']:
                result['issues'].append('该历史试验的回撤超过你设置的研究门槛。')
            if h['return_sacrifice']>policy['max_return_sacrifice']:
                result['issues'].append('该历史试验相对持续持有少赚的幅度超过你的设置。')
            if h['trades_year']>policy['max_trades_year']:
                result['issues'].append('该历史试验的交易频率超过你的设置。')
    if store.version>=2:
        applied = store.rows('''SELECT r.payload,k.status,k.payload candidate_payload,k.state,k.id candidate_id FROM strategy_promotions p
            JOIN evolution_runs r ON r.id=p.candidate_id JOIN evolution_candidates k ON k.id=r.id
            WHERE p.user_id=? AND p.code=? AND p.new_assignment=?''',(uid,code,current['id']))
        if applied:
            payload = json.loads(applied[0]['payload'])
            if payload.get('mode')=='DAILY_APPLY' or payload.get('historical_only'):
                result.update(level='HISTORICAL_APPLIED',title='现行规则依据历史检验更新，尚无前向通过证据',
                              explanation='每日进化已实际改过规则，但未经过后续新行情的固定观察期。不能当作已通过长期自动进化。')
            elif forward_checked(store,applied[0],spec):
                result.update(level='FORWARD_CHECKED',title='现行规则已通过固定前向观察门槛',
                              explanation='已通过当时授权的历史与新行情模拟检查；这仍是指数模型证据，不是未来盈利保证。')
            else:
                result.update(level='APPLIED_UNVERIFIED',title='现行规则曾更新，前向证据待核实',
                              explanation='更新记录仍保留，但固定观察期或逐日证据不完整，不能标记为前向检验通过。')
        candidates = store.rows('''SELECT k.status,k.payload,k.state FROM evolution_candidates k
            JOIN evolution_runs r ON r.id=k.id JOIN evolution_policies p ON p.id=r.policy_id
            WHERE p.user_id=? AND p.code=? AND r.assignment_id=? AND p.active=1
            AND k.status IN ('FORWARD_SHADOW','ELIGIBLE','READY_TO_APPLY') ORDER BY k.created_at DESC LIMIT 1''',(uid,code,current['id']))
        if candidates:
            candidate = candidates[0]
            payload,state = json.loads(candidate['payload']),json.loads(candidate['state'])
            result['candidate'] = {'status':candidate['status'],'observed_sessions':state.get('count',0),
                                   'required_sessions':payload.get('policy',{}).get('shadow_sessions'),
                                   'spec':payload['spec'],'current_rule_unchanged':True}
    return result
