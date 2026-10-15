import json
from datetime import timedelta

import pytest

from clawindex_next.daily_evolution import request, prepare, finish, apply
from clawindex_next.engine import DEFAULT_SPEC, validate_spec
from clawindex_next.evolution import Evolution
from clawindex_next.research import backtest
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical
from test_evolution import Calendar, T0, long_history, add_day
from test_ui_redesign import login, button


def ready(seeded, monkeypatch, config=None):
    db,a,b,aid,baid = seeded
    long_history(db)
    pid = a.set_evolution('HKTECH',config or {})
    ev = Evolution(db,Evaluator(db,Calendar()))
    rid = request(ev,pid,T0)
    prepare(ev,T0)
    run = db.rows('SELECT * FROM evolution_runs WHERE id=?',(rid,))[0]
    data,config = json.loads(run['manifest']),json.loads(run['payload'])
    report = backtest(data['data'],data['sessions'],evaluation_start=config['evaluation_start'])
    with db.connect(True) as c:
        for i,scenario in enumerate(config['plan']):
            c.execute('INSERT INTO evolution_trials VALUES(?,?,?)',(rid,i,canonical({**report,'spec':scenario['spec'],'cost_bps_per_side':scenario['cost'],'execution_delay_sessions':scenario['delay']})))
        c.execute('UPDATE evolution_runs SET cursor=? WHERE id=?',(len(config['plan']),rid))
    monkeypatch.setattr('clawindex_next.daily_evolution.select_candidate',lambda *_:({**DEFAULT_SPEC,'ma':90},{'reasons':[],'historical_only':True}))
    return db,a,b,ev,rid,pid


def complete(db,ev,rid,instant=T0):
    run = db.rows('SELECT * FROM evolution_runs WHERE id=?',(rid,))[0]
    reports = [json.loads(r['payload']) for r in db.rows('SELECT * FROM evolution_trials WHERE run_id=? ORDER BY scenario',(rid,))]
    finish(ev,run,reports,instant)


def test_daily_request_waits_for_data_without_user_sync_and_is_idempotent(seeded):
    db,a,b,aid,baid = seeded
    pid = a.set_evolution('HKTECH',{'enabled':False,'annual_trial_budget':18})
    ev = Evolution(db,Evaluator(db,Calendar()))
    rid = request(ev,pid,T0)
    assert request(ev,pid,T0)==rid
    prepare(ev,T0)
    row = db.rows('SELECT * FROM evolution_runs')[0]
    assert row['status']=='WAITING_DATA' and json.loads(row['payload'])['retry_after']
    assert a.watchlist()[0]['id']==aid
    long_history(db)
    prepare(ev,T0+timedelta(minutes=6))
    assert db.rows('SELECT status FROM evolution_runs')[0]['status']=='PENDING'
    assert a.evolution_policy('HKTECH')['payload']['config']['auto_promote'] is False


def test_daily_evolution_really_updates_only_requesting_user_and_preserves_context(seeded,monkeypatch):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch,{'enabled':False})
    before = a.watchlist()[0]
    a.report_position('HKTECH','HELD',{'shares':12})
    a.set_budget('HKTECH',{'limit':'10000','used':'1000','cash':'1000','currency':'CNY'})
    positions,budgets = db.rows('SELECT * FROM position_reports'),db.rows('SELECT * FROM budgets')
    complete(db,ev,rid)
    current = a.watchlist()[0]
    assert current['strategy_id']==validate_spec({**DEFAULT_SPEC,'ma':90})
    assert current['stage']==before['stage'] and current['catchup']==before['catchup']
    assert b.watchlist()[0]['strategy_id']==validate_spec(DEFAULT_SPEC)
    assert db.rows('SELECT * FROM position_reports')==positions and db.rows('SELECT * FROM budgets')==budgets
    audit = json.loads(db.rows('SELECT payload FROM strategy_promotions')[0]['payload'])
    assert audit['historical_only'] and audit['repeated_history'] and audit['mode']=='DAILY_APPLY'
    assert audit['old_spec']['ma']==120 and audit['new_spec']['ma']==90
    assert db.rows('SELECT status FROM jobs')[0]['status']=='PENDING'
    assert not db.rows('SELECT * FROM evolution_observations')
    assert not apply(ev,rid,T0) and request(ev,pid,T0)==rid
    assert len(db.rows('SELECT * FROM strategy_promotions'))==1
    assert a.evolution_policy('HKTECH')['payload']['config']['auto_promote'] is False


def test_daily_evolution_can_run_next_local_day_despite_auto_interval_or_budget(seeded,monkeypatch):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch,{'annual_trial_budget':18})
    complete(db,ev,rid)
    # T0 is 23:00 HK time. A new calendar day, not 24 hours, permits a new run.
    next_id = request(ev,pid,T0+timedelta(hours=2))
    assert next_id!=rid
    assert request(ev,pid,T0+timedelta(hours=2))==next_id
    assert len(db.rows('SELECT * FROM evolution_runs'))==2


def test_rejected_daily_evolution_keeps_current_rule_and_deduplicates(seeded,monkeypatch):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch)
    monkeypatch.setattr('clawindex_next.daily_evolution.select_candidate',lambda *_:(None,{'reasons':['未达到回撤改善条件']}))
    before = a.watchlist()
    complete(db,ev,rid)
    assert a.watchlist()==before and not db.rows('SELECT * FROM strategy_promotions')
    assert db.rows('SELECT status FROM evolution_runs')[0]['status']=='REJECTED'
    assert request(ev,pid,T0)==rid


@pytest.mark.parametrize('change',['rule','policy','data'])
def test_daily_evolution_does_not_apply_obsolete_inputs(seeded,monkeypatch,change):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch)
    if change=='rule':
        a.assign('HKTECH',{**DEFAULT_SPEC,'ma':150})
    elif change=='policy':
        a.set_evolution('HKTECH',{'max_drawdown':.25})
    else:
        add_day(db,ev.evaluator.calendar,'2026-08-12',310)
    complete(db,ev,rid)
    assert not db.rows('SELECT * FROM strategy_promotions')


def test_daily_promotion_rechecks_authorized_range(seeded,monkeypatch):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch)
    monkeypatch.setattr('clawindex_next.daily_evolution.select_candidate',lambda *_:({**DEFAULT_SPEC,'ma':20},{'reasons':[]}))
    complete(db,ev,rid)
    assert not db.rows('SELECT * FROM strategy_promotions')


def test_daily_rollback_restores_rule_and_leaves_holdings(seeded,monkeypatch):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch)
    complete(db,ev,rid)
    a.report_position('HKTECH','HELD')
    positions = db.rows('SELECT * FROM position_reports')
    a.rollback_evolution(rid)
    assert a.watchlist()[0]['strategy_id']==validate_spec(DEFAULT_SPEC)
    assert db.rows('SELECT * FROM position_reports')==positions
    assert a.evolution_policy('HKTECH') is None
    with pytest.raises(PermissionError):
        b.rollback_evolution(rid)


def test_evolve_button_is_visible_and_creates_daily_request(seeded,monkeypatch):
    db,a,b,*_ = seeded
    app = login(db,monkeypatch)
    app.sidebar.radio[0].set_value('策略与研究').run()
    button(app,'进化并应用').click().run()
    assert not app.exception
    assert any('等待后台准备行情' in x.value for x in app.markdown)
    history = a.evolution_history('HKTECH')
    assert len(history)==1 and history[0]['payload']['mode']=='DAILY_APPLY'
    button(app,'进化并应用').click().run()
    assert not app.exception and len(a.evolution_history('HKTECH'))==1
    assert not b.evolution_history('HKTECH')


def test_cannot_evolve_another_users_unwatched_index(seeded):
    db,a,b,*_ = seeded
    a.unwatch(a.watchlist()[0]['id'])
    with pytest.raises((ValueError,PermissionError)):
        a.evolve_daily('HKTECH')
    assert not db.rows('SELECT * FROM evolution_runs')


def test_real_daily_trials_reach_a_result_without_forcing_a_rule_change(seeded):
    db,a,b,*_ = seeded
    long_history(db)
    pid = a.set_evolution('HKTECH',{})
    ev = Evolution(db,Evaluator(db,Calendar()))
    rid = request(ev,pid,T0)
    prepare(ev,T0)
    plan = json.loads(db.rows('SELECT payload FROM evolution_runs')[0]['payload'])['plan']
    for _ in plan:
        assert ev.run_trial(T0)
    run = db.rows('SELECT * FROM evolution_runs')[0]
    assert run['cursor']==len(plan) and run['status']=='REJECTED'
    assert len(db.rows('SELECT * FROM evolution_trials'))==len(plan)
    assert not db.rows('SELECT * FROM strategy_promotions')
    assert a.watchlist()[0]['strategy_id']==validate_spec(DEFAULT_SPEC)


def test_daily_update_waits_for_inflight_notification_then_expires_old_queue(seeded,monkeypatch):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch)
    aid = a.watchlist()[0]['id']
    did = ev.evaluator.evaluate(aid,T0)[1]
    cid = a.set_channel('https://open.feishu.cn/open-apis/bot/v2/hook/test-only','test',mode='ALL')
    with db.connect(True) as c:
        c.execute("INSERT INTO outbox(user_id,channel_id,decision_id,dedup_key,payload,status,next_at,claim_until) VALUES(?,?,?,'inflight','{}','SENDING',?,?)",(a.user['id'],cid,did,T0.isoformat(),(T0+timedelta(seconds=45)).isoformat()))
        c.execute("INSERT INTO outbox(user_id,channel_id,decision_id,dedup_key,payload,status,next_at) VALUES(?,?,?,'pending','{}','PENDING',?)",(a.user['id'],cid,did,T0.isoformat()))
    complete(db,ev,rid)
    assert not db.rows('SELECT * FROM strategy_promotions')
    assert db.rows('SELECT status FROM evolution_candidates')[0]['status']=='READY_TO_APPLY'
    with db.connect(True) as c:
        c.execute("UPDATE outbox SET status='SENT' WHERE dedup_key='inflight'")
    assert apply(ev,rid,T0)
    assert db.rows("SELECT status FROM outbox WHERE dedup_key='pending'")[0]['status']=='EXPIRED'
    assert db.rows("SELECT status FROM outbox WHERE dedup_key='inflight'")[0]['status']=='SENT'
