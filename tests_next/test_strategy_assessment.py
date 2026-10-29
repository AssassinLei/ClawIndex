"""Evidence belongs to the current user's actual rule and cost assumptions."""
import copy
import json
import math

import pytest

from clawindex_next.engine import DEFAULT_SPEC
from clawindex_next.research import backtest
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical,digest,now
from conftest import FixtureCalendar,price_rows
from test_ui_redesign import login
from test_evolution import eligible,freeze


@pytest.fixture
def simulation():
    prices = [100+i*.05+math.sin(i/20)*20 for i in range(800)]
    dates,rows = price_rows(800,prices=prices)
    return backtest(rows,dates)


def save_report(db,a,report,**updates):
    sid,_ = Evaluator(db,FixtureCalendar()).evaluate(a.watchlist()[0]['id'])
    dataset = db.rows('SELECT dataset_id FROM signal_runs WHERE id=?',(sid,))[0]['dataset_id']
    payload = {'strategy_id':digest(report['spec']),'dataset_id':dataset,
               'research_usable':True,'results':[report],**updates}
    rid = digest({'payload':payload,'user':a.user['id']})
    with db.connect(True) as c:
        c.execute('INSERT INTO research_runs VALUES(?,?,?,?,?)',(rid,a.user['id'],'HKTECH',canonical(payload),now()))
    return rid


def test_no_history_is_not_disguised_as_rule_validation(seeded):
    db,a,b,aid,_ = seeded
    before = db.rows('SELECT * FROM jobs')
    report = a.strategy_assessment('HKTECH')
    assert report['level']=='UNTESTED' and report['historical'] is None
    assert report['as_of'] is None and '后台' in report['explanation']
    assert db.rows('SELECT * FROM jobs')==before
    with pytest.raises(PermissionError):a.strategy_assessment('IXIC')


def test_same_rule_cost_and_data_are_explicit_but_not_forward_validated(seeded,simulation):
    db,a,b,aid,_ = seeded
    save_report(db,a,simulation)
    report = a.strategy_assessment('HKTECH')
    h = report['historical']
    assert report['level']=='HISTORICAL' and report['same_dataset']
    assert h['cost_bps']==10 and h['delay']==1
    assert h['return_sacrifice']==simulation['buy_hold']['return']-simulation['strategy']['return']
    assert h['drawdown_improvement']==abs(simulation['buy_hold']['max_drawdown'])-abs(simulation['strategy']['max_drawdown'])
    assert report['candidate'] is None
    assert b.strategy_assessment('HKTECH')['historical'] is None


@pytest.mark.parametrize('change',['rule','free_cost','different_delay'])
def test_unmatched_or_free_scenario_never_substitutes_for_current_assumptions(seeded,simulation,change):
    db,a,*_ = seeded
    simulation = copy.deepcopy(simulation)
    if change=='rule':simulation['spec']={**DEFAULT_SPEC,'ma':90}
    elif change=='free_cost':simulation['cost_bps_per_side']=0
    else:simulation['execution_delay_sessions']=2
    save_report(db,a,simulation)
    assert a.strategy_assessment('HKTECH')['historical'] is None


@pytest.mark.parametrize('status',['SHORT_SAMPLE','INVALID_DATA_GAPS'])
def test_bad_historical_evidence_stays_diagnostic(seeded,simulation,status):
    db,a,*_ = seeded
    simulation['comparison_status']=status
    save_report(db,a,simulation,research_usable=False)
    report = a.strategy_assessment('HKTECH')
    assert report['level']=='UNTESTED' and report['historical']
    assert any('诊断' in reason for reason in report['issues'])


def test_stale_evidence_and_risk_cost_breaches_are_visible(seeded,simulation):
    db,a,*_ = seeded
    simulation['strategy'].update(max_drawdown=-.4,**{'return':-.4})
    simulation['buy_hold']['return']=.2
    simulation['trade_count']=100
    save_report(db,a,simulation,dataset_id='older-data-version')
    report = a.strategy_assessment('HKTECH')
    assert not report['same_dataset']
    text=' '.join(report['issues'])
    assert '数据版本不同' in text and '回撤超过' in text and '少赚' in text and '交易频率' in text


def test_ui_is_lazy_reads_owned_history_and_never_submits_research(seeded,simulation,monkeypatch):
    db,a,*_ = seeded
    save_report(db,a,simulation)
    app = login(db,monkeypatch)
    app.sidebar.radio[0].set_value('策略与研究').run()
    panel = next(x for x in app.expander if x.label=='策略体检：现行规则的证据与代价')
    assert not any(x.label=='历史最大收盘回撤' for x in app.metric)
    app.session_state[panel.key]=True
    app.run()
    assert not app.exception
    assert any(x.label=='历史最大收盘回撤' for x in app.metric)
    assert any('仍需新行情验证' in x.value for x in app.markdown)
    assert not db.rows('SELECT * FROM jobs')
    assert not db.rows('SELECT * FROM evolution_runs')


@pytest.mark.parametrize('damage',[None,'missing_trial','wrong_index','wrong_cost','wrong_delay','wrong_spec','unfinished'])
def test_evolution_evidence_requires_every_reserved_scenario(seeded,simulation,damage):
    db,a,*_ = seeded
    pid = a.set_evolution('HKTECH',{})
    reports = [copy.deepcopy(simulation),copy.deepcopy(simulation)]
    reports[1]['cost_bps_per_side']=30
    plan = [{'spec':r['spec'],'cost':r['cost_bps_per_side'],'delay':r['execution_delay_sessions']} for r in reports]
    payload = {'plan':plan,'test_consumed':damage!='unfinished'}
    rid = 'assessment-evolution'
    if damage=='wrong_cost':reports[1]['cost_bps_per_side']=40
    if damage=='wrong_delay':reports[1]['execution_delay_sessions']=2
    if damage=='wrong_spec':reports[1]['spec']={**DEFAULT_SPEC,'ma':90}
    with db.connect(True) as c:
        c.execute('''INSERT INTO evolution_runs(id,policy_id,assignment_id,status,manifest,payload,cursor,created_at,updated_at)
            VALUES(?,?,?,'REJECTED','{}',?,2,?,?)''',(rid,pid,a.watchlist()[0]['id'],canonical(payload),now(),now()))
        for i,r in enumerate(reports):
            if damage=='missing_trial' and i==1:continue
            c.execute('INSERT INTO evolution_trials VALUES(?,?,?)',(rid,i+1 if damage=='wrong_index' else i,canonical(r)))
    report = a.strategy_assessment('HKTECH')
    assert (report['level']=='HISTORICAL')==(damage is None)
    assert (report['historical'] is not None)==(damage is None)


@pytest.mark.parametrize('damage',[None,'missing_frozen_at','missing_observation','not_fixed','failed_actual_gate'])
def test_active_promotion_is_forward_checked_only_with_reproducible_observations(seeded,monkeypatch,damage):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch,True)
    instant = eligible(db,rid,calendar)
    assert ev.promote(rid,instant)
    with db.connect(True) as c:
        payload = json.loads(c.execute('SELECT payload FROM evolution_candidates WHERE id=?',(rid,)).fetchone()[0])
        if damage=='missing_frozen_at':payload.pop('frozen_at')
        if damage=='not_fixed':payload['forward_assessment']['fixed_horizon']=False
        c.execute('UPDATE evolution_candidates SET payload=? WHERE id=?',(canonical(payload),rid))
        if damage=='missing_observation':
            c.execute('DELETE FROM evolution_observations WHERE candidate_id=? AND as_of=(SELECT MIN(as_of) FROM evolution_observations WHERE candidate_id=?)',(rid,rid))
        if damage=='failed_actual_gate':
            c.execute("UPDATE evolution_observations SET payload=json_set(payload,'$.candidate',0.5) WHERE candidate_id=?",(rid,))
    report = a.strategy_assessment('HKTECH')
    assert report['level']==('FORWARD_CHECKED' if damage is None else 'APPLIED_UNVERIFIED')
    assert b.strategy_assessment('HKTECH')['level']!='FORWARD_CHECKED'


def test_manual_historical_application_never_borrows_forward_pass_label(seeded,monkeypatch):
    from test_daily_evolution import ready,complete
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch)
    complete(db,ev,rid)
    report = a.strategy_assessment('HKTECH')
    assert report['level']=='HISTORICAL_APPLIED' and report['historical']
    assert not db.rows('SELECT * FROM evolution_observations')


def test_incomplete_legacy_report_does_not_break_strategy_page(seeded,simulation):
    db,a,*_ = seeded
    simulation.pop('strategy')
    save_report(db,a,simulation)
    report = a.strategy_assessment('HKTECH')
    assert report['level']=='UNTESTED' and report['historical'] is None
    assert any('字段不完整' in issue for issue in report['issues'])
