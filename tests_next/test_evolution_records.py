"""Evolution history reports actual application times and parameter changes."""
import json

import pytest

from clawindex_next.evolution import Evolution
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical
from test_daily_evolution import ready, complete
from test_evolution import Calendar, T0, eligible, freeze, long_history
from test_ui_redesign import login


def test_manual_daily_record_distinguishes_request_from_application(seeded,monkeypatch):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch)
    initial = a.evolution_records('HKTECH')[0]
    assert initial['source']=='MANUAL_DAILY' and not initial['applied']
    assert initial['applied_at'] is None and initial['changes']==[]
    complete(db,ev,rid)
    record = a.evolution_records('HKTECH')[0]
    assert record['applied'] and record['outcome']=='已生效'
    assert record['changes']==[{'parameter':'均线交易日数','before':'120','after':'90'}]
    assert record['applied_at']==db.rows('SELECT created_at FROM strategy_promotions')[0]['created_at']
    assert b.evolution_records('HKTECH')==[]
    with pytest.raises(PermissionError):
        b.evolution_records('IXIC')


def test_rejected_research_never_claims_parameters_changed(seeded,monkeypatch):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch)
    monkeypatch.setattr('clawindex_next.daily_evolution.select_candidate',lambda *_:(None,{'reasons':['未达到回撤改善条件']}))
    complete(db,ev,rid)
    record = a.evolution_records('HKTECH')[0]
    assert not record['applied'] and not record['changes'] and record['applied_at'] is None
    assert '保留原规则' in record['outcome'] and '未达到回撤改善条件' in record['reason']


def test_automatic_promotion_is_read_from_actual_audit_not_candidate_status(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch,True)
    pending = a.evolution_records('HKTECH')[0]
    assert pending['source']=='AUTOMATIC' and not pending['changes']
    assert pending['candidate_changes'][0]['after']=='90'
    instant = eligible(db,rid,calendar)
    assert ev.promote(rid,instant)
    applied = a.evolution_records('HKTECH')[0]
    assert applied['applied'] and applied['source']=='AUTOMATIC'
    assert applied['changes'][0]['before']=='120' and applied['changes'][0]['after']=='90'
    assert applied['applied_at']==instant.isoformat()


def test_manual_research_records_its_trigger_and_reuses_outstanding_origin(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    long_history(db)
    pid = a.set_evolution('HKTECH',{})
    monkeypatch.setattr('clawindex_next.service.Evaluator',lambda store:Evaluator(store,Calendar()))
    rid = a.request_evolution('HKTECH')
    assert a.evolution_records('HKTECH')[0]['source']=='MANUAL_RESEARCH'
    assert Evolution(db,Evaluator(db,Calendar())).request(pid,T0)==rid
    assert a.evolution_records('HKTECH')[0]['source']=='MANUAL_RESEARCH'


def test_unrecorded_legacy_source_is_not_guessed_as_automatic(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch)
    row = db.rows('SELECT payload FROM evolution_runs WHERE id=?',(rid,))[0]
    payload = json.loads(row['payload']);payload.pop('trigger')
    with db.connect(True) as c:
        c.execute('UPDATE evolution_runs SET payload=? WHERE id=?',(canonical(payload),rid))
    record = a.evolution_records('HKTECH')[0]
    assert record['source']=='LEGACY' and '未记录' in record['source_label']


def test_rollback_keeps_both_change_directions_and_history_without_active_policy(seeded,monkeypatch):
    db,a,b,ev,rid,pid = ready(seeded,monkeypatch)
    complete(db,ev,rid)
    a.rollback_evolution(rid)
    records = a.evolution_records('HKTECH')
    assert len(records)==2 and a.evolution_policy('HKTECH') is None
    rollback = next(r for r in records if r['source']=='MANUAL_ROLLBACK')
    original = next(r for r in records if r['source']=='MANUAL_DAILY')
    assert rollback['changes'][0]['before']=='90' and rollback['changes'][0]['after']=='120'
    assert original['changes'][0]['after']=='90' and '回退' in original['outcome']
    assert b.evolution_records('HKTECH')==[]
    app = login(db,monkeypatch)
    app.sidebar.radio[0].set_value('策略与研究').run()
    assert not app.exception
    assert any(x.value=='进化记录' for x in app.subheader)
    table = next(x.value for x in app.dataframe if '实际参数变化' in x.value.columns)
    assert any('120 → 90' in v for v in table['实际参数变化'])
    assert any('90 → 120' in v for v in table['实际参数变化'])
    next(x for x in app.checkbox if x.label=='仅看实际参数变更与回退').check().run()
    assert not app.exception
