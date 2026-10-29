"""Research evidence cannot be made fresh by renaming settings or changing day."""
import json
from datetime import timedelta

import pytest

from clawindex_next.daily_evolution import request,reuse_key
from clawindex_next.engine import DEFAULT_SPEC
from clawindex_next.evolution import Evolution
from clawindex_next.service import Evaluator
from clawindex_next.validation import DEFAULT_POLICY,select_candidate
from test_daily_evolution import ready,complete
from test_evolution import Calendar,T0,long_history,add_day,freeze


def test_automatic_cooldown_survives_policy_replacement(seeded):
    db,a,*_ = seeded
    long_history(db)
    ev = Evolution(db,Evaluator(db,Calendar()))
    first = a.set_evolution('HKTECH',{})
    rid = ev.request(first,T0)
    with db.connect(True) as c:
        c.execute("UPDATE evolution_runs SET status='REJECTED' WHERE id=?",(rid,))
    updated = a.set_evolution('HKTECH',{'max_drawdown':.25})
    assert updated!=first
    with pytest.raises(ValueError,match='周期'):
        ev.request(updated,T0+timedelta(days=1))
    ev.schedule(T0+timedelta(days=1))
    assert len(db.rows('SELECT id FROM evolution_runs'))==1


def test_uninvestable_neighbour_cannot_prove_parameter_stability(monkeypatch):
    reports = [{'spec':{**DEFAULT_SPEC,'ma':ma},'cost_bps_per_side':10,'execution_delay_sessions':1,
                'curve':{'date':list(range(600))}} for ma in (90,120,150)]
    def segment(report,lo,hi,annual=252):
        ma = report['spec']['ma']
        c = {'max_drawdown':{90:-.10,120:-.15,150:-.11}[ma],
             'return':.1,'exposure':.8,'trades_year':2,'trade_count':2}
        if ma==150:
            c['exposure']=0. # Similar drawdown alone is insufficient evidence.
        return c,{'return':.12},{'return':.1,'max_drawdown':-.2}
    monkeypatch.setattr('clawindex_next.validation.segment',segment)
    chosen,assessment = select_candidate(reports,DEFAULT_SPEC,DEFAULT_POLICY)
    assert chosen is None and '邻近参数稳定性不足' in assessment['reasons']


def test_valid_neighbour_can_support_candidate_without_selecting_on_holdout(monkeypatch):
    reports = [{'spec':{**DEFAULT_SPEC,'ma':ma},'cost_bps_per_side':10,'execution_delay_sessions':1,
                'curve':{'date':list(range(600))}} for ma in (90,120,150)]
    def segment(report,lo,hi,annual=252):
        c = {'max_drawdown':{90:-.10,120:-.15,150:-.11}[report['spec']['ma']],
             'return':.1,'exposure':.8,'trades_year':2,'trade_count':2}
        return c,{'return':.12},{'return':.1,'max_drawdown':-.2}
    monkeypatch.setattr('clawindex_next.validation.segment',segment)
    chosen,assessment = select_candidate(reports,DEFAULT_SPEC,DEFAULT_POLICY)
    assert chosen['ma']==90 and not assessment['reasons']


def rejected_daily(seeded,monkeypatch):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch)
    monkeypatch.setattr('clawindex_next.daily_evolution.select_candidate',lambda *_:(None,{'reasons':['没有通过检验的改进']}))
    complete(db,ev,rid)
    assert db.rows('SELECT status FROM evolution_runs')[0]['status']=='REJECTED'
    return db,a,b,ev,rid,pid


def test_daily_same_evidence_reuses_completed_result_on_next_local_day(seeded,monkeypatch):
    db,a,b,ev,rid,pid = rejected_daily(seeded,monkeypatch)
    later = T0+timedelta(hours=2)
    assert request(ev,pid,later)==rid
    assert len(db.rows('SELECT id FROM evolution_runs'))==1
    row = db.rows('SELECT value FROM settings WHERE key=?',(reuse_key(a.user['id'],'HKTECH','2026-08-13'),))[0]
    receipt = json.loads(row['value'])
    assert receipt['run_id']==rid and receipt['status']=='REUSED'
    assert receipt['as_of']=='2026-08-12' and receipt['original_requested_at']==T0.isoformat()
    assert not db.rows('SELECT * FROM strategy_promotions')


def test_unrelated_observation_revision_does_not_make_daily_evidence_new(seeded,monkeypatch):
    from clawindex_next.migration import insert_instrument,instrument
    db,a,b,ev,rid,pid = rejected_daily(seeded,monkeypatch)
    with db.connect(True) as c:
        insert_instrument(c,instrument('IXIC','纳斯达克'))
        c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) SELECT 'IXIC',trade_date,payload,source,fetched_at,availability_basis,row_hash FROM observations WHERE code='HKTECH'")
    assert request(ev,pid,T0+timedelta(hours=2))==rid


@pytest.mark.parametrize('change',['data','spec','policy','failed','missing_trial'])
def test_daily_changed_inputs_or_failed_experiment_are_not_falsely_reused(seeded,monkeypatch,change):
    db,a,b,ev,rid,pid = rejected_daily(seeded,monkeypatch)
    if change=='data':
        add_day(db,ev.evaluator.calendar,'2026-08-12',310)
    elif change=='spec':
        a.assign('HKTECH',{**DEFAULT_SPEC,'ma':90})
    elif change=='policy':
        pid = a.set_evolution('HKTECH',{'max_drawdown':.25})
    elif change=='failed':
        with db.connect(True) as c:
            c.execute("UPDATE evolution_runs SET status='FAILED' WHERE id=?",(rid,))
    else:
        with db.connect(True) as c:
            c.execute('DELETE FROM evolution_trials WHERE run_id=? AND scenario=0',(rid,))
    new_id = request(ev,pid,T0+timedelta(hours=2))
    assert new_id!=rid
    assert len(db.rows('SELECT id FROM evolution_runs'))==2


def test_history_revision_before_freeze_cannot_become_forward_evidence(seeded,monkeypatch):
    # Create valid historical reports, but delay finalization until data revision.
    original_finish = Evolution.finish
    monkeypatch.setattr(Evolution,'finish',lambda *_:None)
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch)
    monkeypatch.setattr(Evolution,'finish',original_finish)
    add_day(db,calendar,'2026-08-12',310)
    ev.finish(rid,T0)
    run = db.rows('SELECT status,payload FROM evolution_runs WHERE id=?',(rid,))[0]
    assert run['status']=='REJECTED'
    assert any('研究期间发生修订' in r for r in json.loads(run['payload'])['assessment']['reasons'])
    assert not db.rows('SELECT * FROM evolution_candidates')
