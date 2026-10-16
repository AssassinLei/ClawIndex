"""Explicit gap retries bypass caches, preserve history and remain user-scoped."""
import json
from datetime import timedelta

import pytest
from streamlit.testing.v1 import AppTest

from clawindex_next.data_maintenance import latest_rows
from clawindex_next.service import Evaluator
from clawindex_next.syncing import MarketSync
from clawindex_next.tasks import enqueue, request_for
from clawindex_next.worker import Worker
from conftest import FixtureCalendar
from test_history_maintenance import HistoryProvider, T0
from test_ui_redesign import button, login


def test_force_retry_bypasses_fresh_cache_and_six_hour_old_gap_cooldown(seeded):
    db,*_ = seeded
    with db.connect(True) as c:
        c.execute("UPDATE instruments SET metadata=json_set(metadata,'$.base_date','20260101') WHERE code='HKTECH'")
        c.execute("DELETE FROM observations WHERE trade_date='2026-02-10'")
    provider = HistoryProvider()
    provider.missing.add('2026-02-10')
    syncer = MarketSync(db,FixtureCalendar(),provider)
    syncer.refresh('HKTECH',instant=T0)
    before = len(provider.calls)
    provider.missing.clear()
    syncer.refresh('HKTECH',instant=T0+timedelta(minutes=1))
    assert len(provider.calls)==before and '2026-02-10' not in latest_rows(db,'HKTECH')
    syncer.refresh('HKTECH',instant=T0+timedelta(minutes=1),force_repair=True)
    assert len(provider.calls)>before and '2026-02-10' in latest_rows(db,'HKTECH')
    state = json.loads(db.rows("SELECT value FROM settings WHERE key='history_repair:HKTECH'")[0]['value'])
    assert not state['missing_sessions'] and state['manual_retry'] and state['changed_sessions']>0


def test_manual_repair_queues_even_after_today_complete_and_checks_ownership(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    a.set_inspection_ai(False)
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    assert a.request_inspection(aid,False)['status']=='ALREADY_DONE'
    job = a.request_data_repair(aid)
    request = request_for(db,job)
    assert request['force_repair'] and request['refresh'] and request['kind']=='inspection'
    assert not request['include_ai']
    assert a.request_data_repair(aid)==job
    with pytest.raises(PermissionError):
        b.request_data_repair(aid)
    a.unwatch(aid)
    with pytest.raises(ValueError):
        a.request_data_repair(aid)


def test_pending_normal_job_upgrades_to_force_without_losing_it_on_later_click(seeded):
    db,a,b,aid,_ = seeded
    original = enqueue(db,aid,refresh=False)
    assert a.request_data_repair(aid,False)==original
    assert request_for(db,original)['force_repair']
    assert enqueue(db,aid,refresh=True)==original
    assert request_for(db,original)['force_repair']


def test_running_normal_job_gets_one_forced_followup(seeded):
    db,a,b,aid,_ = seeded
    original = enqueue(db,aid,refresh=True)
    with db.connect(True) as c:
        c.execute("UPDATE jobs SET status='RUNNING' WHERE id=?",(original,))
    forced = a.request_data_repair(aid,False)
    assert forced!=original and a.request_data_repair(aid,False)==forced
    assert len(db.rows('SELECT id FROM jobs'))==2


def test_worker_repairs_then_recomputes_and_keeps_existing_audit_history(seeded):
    db,a,b,aid,_ = seeded
    a.set_inspection_ai(False)
    evaluator = Evaluator(db,FixtureCalendar())
    evaluator.evaluate(aid)
    with db.connect(True) as c:
        c.execute("UPDATE instruments SET metadata=json_set(metadata,'$.base_date','20260101') WHERE code='HKTECH'")
        c.execute("DELETE FROM observations WHERE trade_date='2026-02-10'")
    old_observations = {r['id'] for r in db.rows('SELECT id FROM observations')}
    old_signals = {r['id'] for r in db.rows('SELECT id FROM signal_runs')}
    job = a.request_data_repair(aid,False)
    worker = Worker(db,evaluator,provider=HistoryProvider())
    assert worker.run_job()
    result = a.jobs([job])[0]
    assert result['status']=='SUCCEEDED' and '补回或更新' in result['progress']['detail']
    assert '2026-02-10' in latest_rows(db,'HKTECH')
    assert old_observations<={r['id'] for r in db.rows('SELECT id FROM observations')}
    assert old_signals<{r['id'] for r in db.rows('SELECT id FROM signal_runs')}


def test_my_indices_exposes_repair_button_and_opens_saved_progress(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    app = login(db,monkeypatch)
    app.sidebar.radio[0].set_value('监控与数据').run()
    button(app,'立即补齐缺口').click().run()
    assert not app.exception
    jobs = db.rows('SELECT id FROM jobs')
    assert len(jobs)==1 and request_for(db,jobs[0]['id'])['force_repair']
    assert app.session_state['loading_jobs']==[jobs[0]['id']]


def test_data_card_exposes_same_repair_entry(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    app = login(db,monkeypatch)
    button(app,'查看数据').click().run()
    assert not app.exception and button(app,'立即补齐缺口')
    button(app,'立即补齐缺口').click().run()
    assert not app.exception and app.session_state['loading_jobs']
    job = app.session_state['loading_jobs'][0]
    assert request_for(db,job)['force_repair']
