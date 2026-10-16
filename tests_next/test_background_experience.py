import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from clawindex_next.background import activity
from clawindex_next.migration import insert_instrument, instrument
from clawindex_next.service import Evaluator, UserService
from clawindex_next.storage import canonical, now
from clawindex_next.tasks import request_for
from clawindex_next.worker import Worker
from conftest import FixtureCalendar
from test_ui import button


T0 = datetime(2026,8,12,12,tzinfo=timezone.utc)


def model_response(_):
    return canonical({'summary':'根据程序证据生成的解释','evidence_refs':['close'],
                      'limitations':['未来未知'],'counterarguments':['可能错过反弹'],'scenarios':[]})


def test_ai_preference_is_persistent_per_account_and_defaults_on(seeded):
    db,a,b,aid,baid=seeded
    assert a.inspection_ai_enabled() and b.inspection_ai_enabled()
    a.set_inspection_ai(False)
    assert not UserService(db,a.token).inspection_ai_enabled()
    assert b.inspection_ai_enabled()
    assert not request_for(db,a.request_inspection(aid)['job_id'])['include_ai']
    assert request_for(db,b.request_inspection(baid)['job_id'])['include_ai']


def test_scheduled_inspections_call_ai_only_for_users_who_enabled_it(seeded,monkeypatch):
    db,a,b,aid,baid=seeded
    b.set_inspection_ai(False)
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value='true' WHERE key='daily_sync_enabled'")
    class Cached:
        def sync(self,*args,**kwargs): pass
    calls=[]
    def model(payload):
        calls.append(payload)
        return model_response(payload)
    monkeypatch.setattr('clawindex_next.adapters.llm_explanation',model)
    worker=Worker(db,Evaluator(db,FixtureCalendar()),provider=Cached())
    worker.schedule(T0)
    jobs=db.rows('SELECT * FROM jobs')
    assert len(jobs)==2
    for job in jobs:
        assert request_for(db,job['id'])['include_ai'] == (job['assignment_id']==aid)
    assert worker.run_job() and worker.run_job()
    assert len(calls)==1
    assert a.saved_explanation(a.results()[0]['decision_id'])['status']=='MODEL_OK'
    assert b.saved_explanation(b.results()[0]['decision_id'])['status']=='TEMPLATE'
    worker.schedule(T0)
    assert len(db.rows('SELECT * FROM jobs'))==2


def test_enabling_ai_after_auto_inspection_supplements_same_evidence_without_duplicate_calls(seeded,monkeypatch):
    db,a,b,aid,baid=seeded
    a.set_inspection_ai(False)
    b.set_inspection_ai(False)
    worker=Worker(db,Evaluator(db,FixtureCalendar()))
    assignments=db.rows('SELECT * FROM assignments WHERE active=1')
    worker.enqueue_inspections(assignments,'HKTECH','2026-08-12',T0)
    assert worker.run_job() and worker.run_job()
    calls=[]
    def model(payload):
        calls.append(1)
        return model_response(payload)
    monkeypatch.setattr('clawindex_next.adapters.llm_explanation',model)
    a.set_inspection_ai(True)
    worker.enqueue_inspections(assignments,'HKTECH','2026-08-12',T0)
    assert len(db.rows("SELECT id FROM jobs WHERE status='PENDING'"))==1
    assert worker.run_job()
    assert len(calls)==1 and a.saved_explanation(a.results()[0]['decision_id'])['status']=='MODEL_OK'
    worker.enqueue_inspections(assignments,'HKTECH','2026-08-12',T0)
    assert not worker.run_job() and len(calls)==1


def test_empty_online_backend_shows_receiving_not_a_fictional_queue(seeded):
    db,a,b,aid,baid=seeded
    Worker(db).pulse()
    jid=a.submit(aid)
    job=a.jobs([jid])[0]
    assert job['queue_state']=='STARTING' and job['queue_title']=='准备开始'
    assert job['queue_ahead']==0 and '未发现其他可执行巡检' in job['queue_hint']
    assert '排队' not in job['queue_hint']


@pytest.mark.parametrize('kind,description',[('sync','恒生科技'),('catalog','指数目录'),('evolution','策略研究'),('radar','扫描机会')])
def test_pending_job_explains_actual_background_work(seeded,kind,description):
    db,a,b,aid,baid=seeded
    worker=Worker(db)
    worker.pulse()
    jid=a.submit(aid)
    with activity(db,worker.owner,kind,code='HKTECH'):
        job=a.jobs([jid])[0]
        assert job['queue_state']=='WAITING' and description in job['queue_hint']
        assert description in a.background_status()['activities'][0]['description']
    assert a.jobs([jid])[0]['queue_state']=='STARTING'


def test_other_account_work_is_visible_without_index_or_personal_details(seeded):
    db,a,b,aid,baid=seeded
    with db.connect(True) as c:
        insert_instrument(c,instrument('IXIC','不应泄漏的关注名称'))
    other=b.assign('IXIC')
    worker=Worker(db)
    worker.pulse()
    other_job=b.submit(other)
    jid=a.submit(aid)
    with db.connect(True) as c:
        c.execute("UPDATE jobs SET status='RUNNING',claim_owner=?,claim_until=? WHERE id=?",(worker.owner,(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),other_job))
        c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('progress:'+other_job,canonical({'phase':'explaining','detail':'不应泄漏的账户信息'})))
    with activity(db,worker.owner,'job',user_id=b.user['id'],job_id=other_job):
        job=a.jobs([jid])[0]
        assert job['queue_state']=='WAITING' and '其他账号：生成AI解读' in job['queue_hint']
        public=canonical(a.background_status())
        for secret in ('不应泄漏','IXIC',other_job,'_eligible'):
            assert secret not in public and secret not in job['queue_hint']


def test_actual_prior_requests_are_counted_and_retry_cooldown_is_not_a_blocker(seeded):
    db,a,b,aid,baid=seeded
    Worker(db).pulse()
    prior=b.submit(baid)
    jid=a.submit(aid)
    job=a.jobs([jid])[0]
    assert job['queue_state']=='WAITING' and job['queue_ahead']==1
    with db.connect(True) as c:
        c.execute("UPDATE jobs SET status='RETRY',updated_at=? WHERE id=?",(now(),prior))
    assert a.jobs([jid])[0]['queue_state']=='STARTING'


def test_expired_worker_activity_does_not_claim_backend_is_busy(seeded):
    db,a,b,aid,baid=seeded
    worker=Worker(db)
    worker.pulse()
    jid=a.submit(aid)
    with activity(db,worker.owner,'radar'):
        with db.connect(True) as c:
            c.execute("UPDATE leases SET expires_at='2000-01-01' WHERE name=?",('worker:'+worker.owner,))
        assert not a.background_status()['activities']
        assert a.jobs([jid])[0]['queue_state']=='OFFLINE'


def test_long_unclaimed_request_is_reported_as_delayed_instead_of_fake_queue(seeded):
    db,a,b,aid,baid=seeded
    Worker(db).pulse()
    jid=a.submit(aid,instant=datetime.now(timezone.utc)-timedelta(minutes=2))
    job=a.jobs([jid])[0]
    assert job['queue_state']=='DELAYED' and '尚未接收' in job['queue_hint']
    assert job['queue_ahead']==0


def test_retry_shows_reason_and_cooldown(seeded):
    db,a,b,aid,baid=seeded
    Worker(db).pulse()
    jid=a.submit(aid)
    with db.connect(True) as c:
        c.execute("UPDATE jobs SET status='RETRY',error='行情接口网络暂不可达',updated_at=? WHERE id=?",(now(),jid))
    job=a.jobs([jid])[0]
    assert job['queue_state']=='RETRY'
    assert '网络暂不可达' in job['queue_hint'] and '秒后可自动重试' in job['queue_hint']


def test_nested_task_activity_restores_automatic_stage_and_cleans_up_on_error(seeded):
    db,a,*_=seeded
    worker=Worker(db)
    worker.pulse()
    with activity(db,worker.owner,'radar'):
        with pytest.raises(RuntimeError):
            with activity(db,worker.owner,'evolution'):
                assert '策略研究' in a.background_status()['activities'][0]['description']
                raise RuntimeError('test interruption')
        assert '扫描机会' in a.background_status()['activities'][0]['description']
    assert not a.background_status()['activities']


def test_job_claim_updates_progress_atomically_before_execution(seeded,monkeypatch):
    db,a,b,aid,baid=seeded
    jid=a.submit(aid)
    worker=Worker(db)
    def execute(job):
        record=db.rows('SELECT * FROM jobs WHERE id=?',(jid,))[0]
        progress=json.loads(db.rows('SELECT value FROM settings WHERE key=?',('progress:'+jid,))[0]['value'])
        assert record['status']=='RUNNING' and progress['phase']=='starting'
        return True
    monkeypatch.setattr(worker,'execute_job',execute)
    assert worker.run_job()


def test_idle_worker_detects_new_request_after_two_second_poll(seeded,monkeypatch):
    db,a,b,aid,baid=seeded
    worker=Worker(db)
    waits=[]
    def wait(seconds):
        waits.append(seconds)
        a.submit(aid)
        return False
    monkeypatch.setattr(worker.stopping,'wait',wait)
    worker.wait_for_work(30)
    assert waits==[2]


def test_worker_loop_error_retains_backoff_instead_of_spinning_on_pending_request(seeded,monkeypatch):
    db,a,b,aid,baid=seeded
    a.submit(aid)
    worker=Worker(db)
    waits=[]
    def fail():
        raise RuntimeError('test worker failure')
    def wait(seconds):
        waits.append(seconds)
        worker.stopping.set()
        return True
    monkeypatch.setattr(worker,'once',fail)
    monkeypatch.setattr(worker.stopping,'wait',wait)
    worker.run(interval=30)
    assert waits==[30]


def test_checkbox_persists_across_sessions_and_pending_dialog_has_clear_status(seeded,monkeypatch):
    db,a,b,aid,baid=seeded
    monkeypatch.setenv('VNEXT_DB_PATH',str(db.path))
    def login():
        app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app_next.py'),default_timeout=30).run()
        app.text_input[0].set_value('alice')
        button(app,'登录').click().run()
        assert not app.exception
        return app
    app=login()
    next(x for x in app.checkbox if x.label=='巡检时同时生成AI解读').uncheck().run()
    assert not a.inspection_ai_enabled() and b.inspection_ai_enabled()
    app=login()
    assert not next(x for x in app.checkbox if x.label=='巡检时同时生成AI解读').value
    Worker(db).pulse()
    button(app,'提交全部巡检').click().run()
    assert not app.exception
    assert any('准备开始' in x.value for x in app.markdown)
    assert any('未发现其他可执行巡检' in x.value for x in app.caption)
