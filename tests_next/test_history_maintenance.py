"""Real gap repair, unchanged evidence reuse and actionable data diagnostics."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
from streamlit.testing.v1 import AppTest

from clawindex_next.adapters import TushareAdapter
from clawindex_next.data_maintenance import history_start, latest_rows, put_rows, readiness
from clawindex_next.engine import DEFAULT_SPEC, feature_frame
from clawindex_next.explanations import explain
from clawindex_next.migration import instrument
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical, digest, now
from clawindex_next.tasks import request_for
from clawindex_next.worker import Worker
from conftest import FixtureCalendar, price_rows


T0 = datetime(2026,8,12,12,tzinfo=timezone.utc)


class HistoryProvider(TushareAdapter):
    def __init__(self):
        super().__init__(object(),FixtureCalendar(),pause=lambda _:None,minimum_interval=0)
        self.calls = []
        self.missing = set()

    def fetch(self,item,start,end,instant=None):
        self.calls.append((start,end))
        return [{'date':d.date().isoformat(),'open':100.,'high':101.,'low':99.,'close':100.,
                 'quality':'PROVIDER_PRICE','source_endpoint':'index_global'}
                for d in pd.bdate_range(start,end) if d.date().isoformat() not in self.missing]

    def sync_supplements(self,*args):
        return False


def test_hktech_ohlc_history_starts_at_launch_without_losing_stored_closes(seeded):
    db,a,b,aid,_ = seeded
    assert history_start({'code':'HKTECH','metadata':'{}'})=='2020-07-27'
    assert history_start({'code':'IXIC','metadata':'{}'})=='2016-01-01'
    old = {'date':'2020-07-24','close':6862.7,'open':None,'high':None,'low':None}
    with db.connect(True) as c:
        c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',
                  ('HKTECH',old['date'],canonical(old),'fixture',now(),'FIXTURE',digest(old)))
    p = HistoryProvider()
    p.sync(db,'HKTECH',instant=T0)
    assert all(start>='2020-07-27' for start,end in p.calls)
    assert '2020-07-24' in latest_rows(db,'HKTECH')
    manifest,_ = Evaluator(db,FixtureCalendar()).inputs('HKTECH',T0)
    assert manifest['start']>='2020-07-27'
    assert old['date'] not in manifest['sessions']


def test_old_history_batches_do_not_repeat_latest_price_requests(seeded):
    db,*_ = seeded
    with db.connect(True) as c:
        c.execute('DELETE FROM observations')
    p = HistoryProvider()
    p.sync(db,'HKTECH',instant=T0)
    count = len(p.calls)
    assert any(end=='2026-08-12' for start,end in p.calls)
    p.sync(db,'HKTECH',instant=T0+timedelta(minutes=3))
    assert p.calls[count:]
    assert all(end<'2026-07-01' for start,end in p.calls[count:])
    state = json.loads(db.rows("SELECT value FROM settings WHERE key='history_repair:HKTECH'")[0]['value'])
    assert state['pending'] and state['missing_sessions']>0


def test_critical_quarter_does_not_wait_six_hours_and_uses_smaller_requests(seeded):
    db,*_ = seeded
    with db.connect(True) as c:
        c.execute("UPDATE instruments SET metadata=json_set(metadata,'$.base_date','20260101') WHERE code='HKTECH'")
        c.execute("DELETE FROM observations WHERE trade_date BETWEEN '2026-04-01' AND '2026-06-30'")
        c.execute("INSERT INTO settings VALUES('history_repair:HKTECH',?)",(canonical({'expected':'2026-08-12','windows':{'2026Q2':(T0+timedelta(hours=6)).isoformat()}}),))
    class EmptyQuarter(HistoryProvider):
        def fetch(self,item,start,end,instant=None):
            rows = super().fetch(item,start,end,instant)
            return [] if len(rows)>40 else rows
    p = EmptyQuarter()
    p.sync(db,'HKTECH',instant=T0)
    assert ('2026-04-01','2026-06-30') in p.calls
    assert '2026-06-30' in latest_rows(db,'HKTECH')
    state = json.loads(db.rows("SELECT value FROM settings WHERE key='history_repair:HKTECH'")[0]['value'])
    assert state['essential_missing_sessions']>0
    assert state['essential_retry_at']==(T0+timedelta(minutes=2)).isoformat()
    p.sync(db,'HKTECH',instant=T0+timedelta(minutes=3))
    p.sync(db,'HKTECH',instant=T0+timedelta(minutes=6))
    current = latest_rows(db,'HKTECH')
    assert all(d.date().isoformat() in current for d in pd.bdate_range('2026-04-01','2026-06-30'))


def test_unfillable_old_window_backs_off_then_resets_when_repaired(seeded):
    db,*_ = seeded
    with db.connect(True) as c:
        c.execute("UPDATE instruments SET metadata=json_set(metadata,'$.base_date','20260101') WHERE code='HKTECH'")
        c.execute("DELETE FROM observations WHERE trade_date='2026-02-10'")
    p = HistoryProvider()
    p.missing.add('2026-02-10')
    def state():
        return json.loads(db.rows("SELECT value FROM settings WHERE key='history_repair:HKTECH'")[0]['value'])
    p.sync(db,'HKTECH',instant=T0)
    assert state()['stalled_windows']['2026Q1']==1
    p.sync(db,'HKTECH',instant=T0+timedelta(hours=7))
    assert state()['stalled_windows']['2026Q1']==2
    assert datetime.fromisoformat(state()['windows']['2026Q1'])==T0+timedelta(hours=19)
    count = len(p.calls)
    p.sync(db,'HKTECH',instant=T0+timedelta(hours=8))
    assert len(p.calls)==count
    p.missing.clear()
    p.sync(db,'HKTECH',instant=T0+timedelta(hours=20))
    assert state()['missing_sessions']==0 and state()['stalled_windows']['2026Q1']==0


def test_index_daily_absence_does_not_erase_valuation_and_create_fake_revisions(seeded):
    db,*_ = seeded
    item = db.rows("SELECT * FROM instruments WHERE code='HKTECH'")[0]
    p = HistoryProvider()
    price = {'date':'2026-08-12','open':100.,'high':101.,'low':99.,'close':100.,'pe':None,'pb':None,
             'source_endpoint':'index_daily','basic_source':'index_daily','valuation_source':None}
    put_rows(db,p,item,[price],T0)
    put_rows(db,p,item,[{'date':'2026-08-12','pe':20.,'pb':2.,'basic_source':'index_dailybasic','valuation_source':'index_dailybasic'}],T0,True)
    before = len(db.rows("SELECT id FROM observations WHERE code='HKTECH'"))
    assert put_rows(db,p,item,[price],T0+timedelta(minutes=3))==0
    assert len(db.rows("SELECT id FROM observations WHERE code='HKTECH'"))==before
    assert latest_rows(db,'HKTECH')['2026-08-12']['pe']==20.
    # A real correction still changes price evidence, preserving supplemental data.
    put_rows(db,p,item,[{**price,'close':100.5}],T0+timedelta(minutes=4))
    assert latest_rows(db,'HKTECH')['2026-08-12']['close']==100.5
    assert latest_rows(db,'HKTECH')['2026-08-12']['pb']==2.
    # Explicit nulls from the valuation endpoint must still be respected.
    put_rows(db,p,item,[{'date':'2026-08-12','pe':None,'basic_source':'index_dailybasic','valuation_source':'index_dailybasic'}],T0,True)
    assert latest_rows(db,'HKTECH')['2026-08-12']['pe'] is None


def test_same_beijing_day_across_utc_midnight_keeps_one_automatic_receipt(seeded):
    db,a,b,aid,baid = seeded
    a.set_inspection_ai(False)
    b.set_inspection_ai(False)
    worker = Worker(db,Evaluator(db,FixtureCalendar()))
    assignments = db.rows('SELECT * FROM assignments WHERE active=1')
    before = datetime(2026,8,12,23,59,tzinfo=timezone.utc)
    worker.enqueue_inspections(assignments,'HKTECH','2026-08-12',before)
    first = db.rows("SELECT key,value FROM settings WHERE key LIKE 'auto_inspect:%'")
    while worker.run_job():
        pass
    worker.enqueue_inspections(assignments,'HKTECH','2026-08-12',before+timedelta(minutes=2))
    assert db.rows("SELECT key,value FROM settings WHERE key LIKE 'auto_inspect:%'")==first
    assert len(db.rows('SELECT id FROM jobs'))==2


def test_automatic_inspection_reuses_manual_result_but_reacts_to_real_revision(seeded):
    db,a,b,aid,_ = seeded
    a.set_inspection_ai(False)
    Evaluator(db,FixtureCalendar()).evaluate(aid,T0)
    worker = Worker(db,Evaluator(db,FixtureCalendar()))
    assignments = db.rows('SELECT * FROM assignments WHERE id=?',(aid,))
    worker.enqueue_inspections(assignments,'HKTECH','2026-08-12',T0)
    assert not db.rows('SELECT * FROM jobs')
    with db.connect(True) as c:
        row = latest_rows(db,'HKTECH')['2026-08-12']
        row['pe'] = 99.
        c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',
                  ('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    worker.enqueue_inspections(assignments,'HKTECH','2026-08-12',T0)
    assert len(db.rows('SELECT * FROM jobs'))==1


def test_automatic_missing_ai_queues_explanation_without_recomputing_manual_signal(seeded):
    db,a,b,aid,_ = seeded
    _,did = Evaluator(db,FixtureCalendar()).evaluate(aid,T0)
    worker = Worker(db,Evaluator(db,FixtureCalendar()))
    worker.enqueue_inspections(db.rows('SELECT * FROM assignments WHERE id=?',(aid,)),'HKTECH','2026-08-12',T0)
    job = db.rows('SELECT id FROM jobs')[0]
    request = request_for(db,job['id'])
    assert request['kind']=='explanation' and request['decision_id']==did and not request['refresh']


def test_readiness_distinguishes_missing_days_invalid_bars_and_too_short_history():
    days,rows = price_rows()
    missing = rows.pop(-10)['date']
    rows[-20]['open'] = None
    invalid = rows[-20]['date']
    details = readiness(feature_frame(rows,days),rows,DEFAULT_SPEC)
    assert details['required_sessions']==125
    assert details['valid_sessions']==123
    assert details['missing_dates']==[missing] and details['invalid_dates']==[invalid]
    assert details['continuous_valid_sessions']==9
    short = readiness(feature_frame(rows[-20:],days[-20:]),rows[-20:],DEFAULT_SPEC)
    assert short['short_sessions']==105


def test_due_repair_cannot_be_hidden_by_todays_incomplete_result(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    with db.connect(True) as c:
        c.execute("DELETE FROM observations WHERE trade_date='2026-07-10'")
    sid,did = Evaluator(db,FixtureCalendar()).evaluate(aid,T0)
    signal = json.loads(db.rows('SELECT result FROM signal_runs WHERE id=?',(sid,))[0]['result'])
    assert signal['data_readiness']['missing_dates']==['2026-07-10']
    with db.connect(True) as c:
        c.execute('UPDATE personal_decisions SET created_at=? WHERE id=?',(T0.isoformat(),did))
        c.execute("INSERT INTO settings VALUES('history_repair:HKTECH',?)",(canonical({'pending':True,'next_due':(T0+timedelta(hours=1)).isoformat()}),))
    assert a.request_inspection(aid,False,T0)['status']=='ALREADY_DONE'
    result = a.request_inspection(aid,False,T0+timedelta(hours=2))
    assert result['job_id'] and request_for(db,result['job_id'])['refresh']


def test_evidence_panel_displays_exact_gap_and_automatic_repair(seeded):
    db,a,b,aid,_ = seeded
    with db.connect(True) as c:
        c.execute("DELETE FROM observations WHERE trade_date='2026-07-10'")
    sid,_ = Evaluator(db,FixtureCalendar()).evaluate(aid,T0)
    signal = json.loads(db.rows('SELECT result FROM signal_runs WHERE id=?',(sid,))[0]['result'])
    source = ('from clawindex_next.storage import Store\nfrom clawindex_next.ui_components import evidence_panel\n'
              f'evidence_panel(Store({str(db.path)!r}),{signal!r},True)')
    app = AppTest.from_string(source).run()
    assert not app.exception
    assert any('125' in x.value and '连续有效' in x.value for x in app.warning)
    assert any('2026-07-10' in x.value for x in app.caption)
    assert any('自动补齐' in x.value for x in app.caption)


def test_ai_reuses_unchanged_current_evidence_across_old_revisions_and_isolates_accounts(seeded):
    db,a,b,aid,baid = seeded
    ev = Evaluator(db,FixtureCalendar())
    calls = []
    def model(payload):
        calls.append(payload)
        return canonical({'summary':'根据当前指标解释规则','evidence_refs':['close'],
                          'limitations':[],'counterarguments':[],'scenarios':[]})
    _,first = ev.evaluate(aid,T0)
    explain(db,first,model,'test-stable-evidence')
    # An old bar gets a volume correction. The new revision remains auditable,
    # without changing current features or explaining the same action again.
    with db.connect(True) as c:
        raw = c.execute("SELECT payload FROM observations WHERE code='HKTECH' ORDER BY trade_date LIMIT 1").fetchone()
        row = {**json.loads(raw[0])}
        row['amount'] += 1.
        c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',
                  ('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    _,second = ev.evaluate(aid,T0+timedelta(minutes=3))
    assert second!=first
    result = explain(db,second,model,'test-stable-evidence')
    assert len(calls)==1 and result['reuse']['original_generated_at']
    assert 'expires_at' not in calls[0]['decision'] and 'dataset_id' not in calls[0]['signal']
    # Matching evidence from a different user is never borrowed.
    _,other = ev.evaluate(baid,T0+timedelta(minutes=3))
    explain(db,other,model,'test-stable-evidence')
    assert len(calls)==2
    # A genuine change in the current quote must receive a fresh explanation.
    with db.connect(True) as c:
        row = latest_rows(db,'HKTECH')['2026-08-12']
        row['close'] += .2
        c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',
                  ('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    _,third = ev.evaluate(aid,T0+timedelta(minutes=6))
    explain(db,third,model,'test-stable-evidence')
    assert len(calls)==3
