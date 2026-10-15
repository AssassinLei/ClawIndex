import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from clawindex_next.explanations import ExplanationBusy, explain, model_identity
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical, digest, now
from clawindex_next.tasks import enqueue, request_for, reconcile
from clawindex_next.worker import Worker
from conftest import FixtureCalendar, price_rows
from test_ui import button


def response(payload):
    return canonical({'summary':'趋势与估值的程序证据解读','evidence_refs':['close'],
                      'limitations':['未来存在不确定性'],'counterarguments':['可能错过反弹'],'scenarios':[]})


def fresh(seeded, monkeypatch):
    db,a,b,aid,baid = seeded
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    sid,did = Evaluator(db,FixtureCalendar()).evaluate(aid)
    return db,a,b,aid,baid,sid,did


def test_today_finished_inspection_and_ai_are_reused_without_new_job(seeded,monkeypatch):
    db,a,b,aid,baid,sid,did = fresh(seeded,monkeypatch)
    calls = []
    def model(payload): calls.append(1); return response(payload)
    monkeypatch.setattr('clawindex_next.adapters.llm_explanation',model)
    a.explanation(did,True)
    result = a.request_inspection(aid)
    assert result['status'] == 'ALREADY_DONE' and not result['job_id']
    assert '今天已完成' in result['message']
    assert len(calls) == 1 and not db.rows('SELECT * FROM jobs')
    with pytest.raises(PermissionError): b.request_inspection(aid)


def test_only_missing_ai_is_queued_and_no_sync_or_reinspection_runs(seeded,monkeypatch):
    db,a,b,aid,baid,sid,did = fresh(seeded,monkeypatch)
    result = a.request_inspection(aid)
    assert result['status'] == 'AI_ONLY'
    jid = result['job_id']
    assert a.request_inspection(aid)['job_id'] == jid
    class NoSync:
        def sync(self,*args,**kwargs): raise AssertionError('不应重复同步')
    ev = Evaluator(db,FixtureCalendar())
    monkeypatch.setattr(ev,'evaluate',lambda *_:pytest.fail('不应重复巡检'))
    calls=[]
    def model(payload): calls.append(1); return response(payload)
    monkeypatch.setattr('clawindex_next.adapters.llm_explanation',model)
    assert Worker(db,ev,provider=NoSync()).run_job()
    assert a.jobs([jid])[0]['status'] == 'SUCCEEDED'
    assert len(calls) == 1 and len(db.rows('SELECT * FROM signal_runs')) == 1
    assert a.request_inspection(aid)['status'] == 'ALREADY_DONE'


def test_failed_ai_can_retry_without_new_inspection(seeded,monkeypatch):
    db,a,b,aid,baid,sid,did = fresh(seeded,monkeypatch)
    monkeypatch.setattr('clawindex_next.adapters.llm_explanation',lambda _: 'not json')
    first = a.request_inspection(aid)['job_id']
    worker = Worker(db,Evaluator(db,FixtureCalendar()))
    assert worker.run_job()
    job = a.jobs([first])[0]
    assert job['status'] == 'SUCCEEDED' and 'AI未成功' in job['progress']['detail']
    second = a.request_inspection(aid)['job_id']
    assert second != first
    monkeypatch.setattr('clawindex_next.adapters.llm_explanation',response)
    assert worker.run_job()
    assert a.saved_explanation(did)['status'] == 'MODEL_OK'
    assert len(db.rows('SELECT * FROM signal_runs')) == 1


@pytest.mark.parametrize('change',['position','data','expected_session','rule'])
def test_fresh_context_or_market_changes_are_not_hidden_by_today_cache(seeded,monkeypatch,change):
    db,a,b,aid,baid,sid,did = fresh(seeded,monkeypatch)
    assert a.request_inspection(aid,include_ai=False)['status'] == 'ALREADY_DONE'
    if change == 'position':
        a.report_position('HKTECH','HELD')
    elif change == 'data':
        raw = db.rows('SELECT payload FROM observations ORDER BY id DESC LIMIT 1')[0]
        row = {**json.loads(raw['payload']),'pe':99.}
        with db.connect(True) as c:
            c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    elif change == 'expected_session':
        class Tomorrow(FixtureCalendar):
            def expected(self,*args,**kwargs): return '2026-08-13'
        monkeypatch.setattr('clawindex_next.service.MarketCalendar',Tomorrow)
    else:
        aid = a.assign('HKTECH')
    result = a.request_inspection(aid,include_ai=False)
    assert result['status'] in ('UPDATED','QUEUED') and result['job_id']
    assert a.request_inspection(aid,include_ai=False)['job_id'] == result['job_id']
    assert len(db.rows('SELECT * FROM jobs')) == 1


def test_today_means_hong_kong_date_not_the_servers_utc_date(seeded,monkeypatch):
    db,a,b,aid,baid,sid,did = fresh(seeded,monkeypatch)
    before = datetime.now(timezone.utc).replace(hour=15,minute=59,second=0,microsecond=0)
    after = before+timedelta(minutes=2) # 00:01 the following day in Hong Kong
    _,did = Evaluator(db,FixtureCalendar()).evaluate(aid,before)
    with db.connect(True) as c:
        c.execute('UPDATE personal_decisions SET created_at=? WHERE id=?',(before.isoformat(),did))
    assert a.request_inspection(aid,False,before)['status'] == 'ALREADY_DONE'
    assert a.request_inspection(aid,False,after)['status'] == 'QUEUED'


def test_pending_automatic_inspection_is_upgraded_not_duplicated(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    jid = enqueue(db,aid,refresh=False,include_ai=False)
    result = a.request_inspection(aid)
    assert result['job_id'] == jid
    assert request_for(db,jid)['refresh'] and request_for(db,jid)['include_ai']
    assert len(db.rows('SELECT * FROM jobs')) == 1


def test_current_result_with_inspection_in_flight_joins_ai_instead_of_queueing_followup(seeded,monkeypatch):
    db,a,b,aid,baid,sid,did = fresh(seeded,monkeypatch)
    jid = enqueue(db,aid,refresh=False,include_ai=False)
    result = a.request_inspection(aid)
    assert result['status'] == 'AI_JOINED' and result['job_id'] == jid
    assert request_for(db,jid)['include_ai'] and len(db.rows('SELECT * FROM jobs')) == 1


def test_running_inspection_accepts_ai_upgrade_before_atomic_completion(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    jid = enqueue(db,aid,refresh=False,include_ai=False)
    ev = Evaluator(db,FixtureCalendar())
    evaluate = ev.evaluate
    def upgraded(aid):
        result = evaluate(aid)
        assert enqueue(db,aid,refresh=False,include_ai=True) == jid
        return result
    monkeypatch.setattr(ev,'evaluate',upgraded)
    calls=[]
    def model(payload): calls.append(1); return response(payload)
    monkeypatch.setattr('clawindex_next.adapters.llm_explanation',model)
    assert Worker(db,ev).run_job()
    assert a.jobs([jid])[0]['status'] == 'SUCCEEDED'
    assert len(calls) == 1 and len(db.rows('SELECT * FROM jobs')) == 1


def test_concurrent_clicks_queue_only_one_ai_job(seeded,monkeypatch):
    db,a,b,aid,baid,sid,did = fresh(seeded,monkeypatch)
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda _:a.request_inspection(aid)['job_id'],range(8)))
    assert len(set(ids)) == 1 and len(db.rows('SELECT * FROM jobs')) == 1


def test_concurrent_ai_generation_uses_one_provider_call_and_releases_lease(seeded,monkeypatch):
    db,a,b,aid,baid,sid,did = fresh(seeded,monkeypatch)
    started,release = threading.Event(),threading.Event()
    calls=[]
    def model(payload):
        calls.append(1)
        started.set()
        assert release.wait(10)
        return response(payload)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(explain,db,did,model,'test-model')
        try:
            assert started.wait(10)
            with pytest.raises(ExplanationBusy): explain(db,did,model,'test-model')
        finally:
            release.set()
        assert future.result(timeout=10)['summary']
    explain(db,did,model,'test-model')
    assert len(calls) == 1 and not db.rows("SELECT * FROM leases WHERE name LIKE 'explanation:%'")


def test_old_rules_pending_jobs_are_retired_and_other_users_unchanged(seeded):
    db,a,b,aid,baid = seeded
    old = a.submit(aid)
    other = b.submit(baid)
    a.assign('HKTECH')
    assert not a.jobs()
    assert a.jobs([old])[0]['status'] == 'CANCELLED'
    assert b.jobs([other])[0]['status'] == 'PENDING'
    assert not a.jobs([other])


@pytest.mark.parametrize('status',['PENDING','RETRY','RUNNING'])
def test_exhausted_unclaimable_jobs_are_terminal_not_permanently_queued(seeded,status):
    db,a,b,aid,baid = seeded
    jid = a.submit(aid)
    with db.connect(True) as c:
        c.execute('UPDATE jobs SET status=?,attempts=3,claim_until=NULL WHERE id=?',(status,jid))
    assert a.jobs([jid])[0]['status'] == 'FAILED'
    assert not Worker(db,Evaluator(db,FixtureCalendar())).run_job(user_id=a.user['id'])


def test_live_running_job_is_not_failed_even_on_last_attempt(seeded):
    db,a,b,aid,baid = seeded
    jid = a.submit(aid)
    worker = Worker(db)
    worker.pulse()
    with db.connect(True) as c:
        c.execute("UPDATE jobs SET status='RUNNING',attempts=3,claim_until=? WHERE id=?",((datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),jid))
    assert a.jobs([jid])[0]['status'] == 'RUNNING'


def test_running_job_with_missing_claim_is_recoverable(seeded):
    db,a,b,aid,baid = seeded
    jid = a.submit(aid)
    with db.connect(True) as c:
        c.execute("UPDATE jobs SET status='RUNNING',attempts=1,claim_until=NULL WHERE id=?",(jid,))
    assert Worker(db,Evaluator(db,FixtureCalendar())).run_job(user_id=a.user['id'])
    assert a.jobs([jid])[0]['status'] == 'SUCCEEDED'


def test_specific_job_query_is_not_truncated_by_100_newer_jobs(seeded):
    db,a,b,aid,baid = seeded
    old = a.submit(aid)
    with db.connect(True) as c:
        c.execute("UPDATE jobs SET status='SUCCEEDED' WHERE id=?",(old,))
        for n in range(110):
            stamp = (datetime.now(timezone.utc)+timedelta(seconds=n+1)).isoformat()
            c.execute("INSERT INTO jobs(id,assignment_id,status,created_at,updated_at) VALUES(?,?,'SUCCEEDED',?,?)",(uuid.uuid4().hex,aid,stamp,stamp))
    assert a.jobs([old])[0]['id'] == old


def test_offline_queue_reports_waiting_for_backend_instead_of_processing(seeded):
    db,a,b,aid,baid = seeded
    jid = a.submit(aid)
    job = a.jobs([jid])[0]
    assert job['queue_state'] == 'OFFLINE' and '后台当前离线' in job['queue_hint']


def test_periodic_sync_interleaves_inspections_before_next_index(seeded):
    db,a,b,aid,baid = seeded
    from clawindex_next.migration import insert_instrument,instrument
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value='true' WHERE key='daily_sync_enabled'")
        insert_instrument(c,instrument('IXIC','纳斯达克'))
        for row in price_rows()[1]:
            c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',('IXIC',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    b.assign('IXIC')
    class Probe:
        def sync(self,store,code,**kwargs):
            if code == 'IXIC':
                completed = store.rows("SELECT COUNT(*) n FROM jobs j JOIN assignments a ON a.id=j.assignment_id WHERE a.code='HKTECH' AND j.status='SUCCEEDED'")[0]['n']
                assert completed == 2
    worker = Worker(db,Evaluator(db,FixtureCalendar()),provider=Probe())
    assert worker.schedule(datetime(2026,8,12,12,tzinfo=timezone.utc),interleave=True) == 3


def test_today_buttons_default_ai_and_duplicate_notices_on_hold_wait_cards(seeded,monkeypatch):
    db,a,b,aid,baid,sid,did = fresh(seeded,monkeypatch)
    monkeypatch.setenv('VNEXT_DB_PATH',str(db.path))
    calls=[]
    def model(payload): calls.append(1); return response(payload)
    monkeypatch.setattr('clawindex_next.adapters.llm_explanation',model)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app_next.py'),default_timeout=30).run()
    app.text_input[0].set_value('alice')
    button(app,'登录').click().run()
    assert next(x for x in app.checkbox if x.label=='巡检时同时生成AI解读').value
    assert any('近5日涨跌' in x.value for x in app.caption)
    button(app,'查看数据').click().run()
    assert not app.exception and any(x.label=='指数收盘点位' for x in app.metric)
    button(app,'查看解读').click().run()
    button(app,'生成AI解读').click().run()
    assert not app.exception and a.saved_explanation(did)['status'] == 'MODEL_OK' and len(calls)==1
    button(app,'生成AI解读').click().run()
    assert any('已生成AI解读' in x.value for x in app.info) and len(calls)==1
    button(app,'提交全部巡检').click().run()
    assert not app.exception
    assert any('已复用结果' in x.value for x in app.info) and not db.rows('SELECT * FROM jobs')
    app.sidebar.radio[0].set_value('信号与待执行').run()
    assert next(x for x in app.checkbox if x.label=='巡检时同时生成AI解读').value


def test_cancelled_loading_job_finishes_instead_of_waiting_forever(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    jid = a.submit(aid)
    a.assign('HKTECH')
    assert a.jobs([jid])[0]['status'] == 'CANCELLED'
    monkeypatch.setenv('VNEXT_DB_PATH',str(db.path))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app_next.py'),default_timeout=30).run()
    app.text_input[0].set_value('alice')
    button(app,'登录').click().run()
    app.session_state.loading_jobs = [jid]
    app.run()
    assert not app.exception and not app.session_state.get('loading_jobs',None)
