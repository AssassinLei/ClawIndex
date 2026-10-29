"""Quota/auth failures must not stall rules or repeatedly call the provider."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from openai import APIStatusError, APITimeoutError

from clawindex_next.ai_availability import config_key,paused,record_failure,record_success
from clawindex_next.explanations import explain,model_identity
from clawindex_next.llm_diagnostics import failure_diagnostic
from clawindex_next.service import Evaluator
from clawindex_next.storage import Store,canonical
from clawindex_next.tasks import enqueue
from clawindex_next.worker import Worker
from conftest import FixtureCalendar
from test_ui_redesign import button,login


def error(status=402,code=None):
    return APIStatusError('private-api-key',response=httpx.Response(status,
        request=httpx.Request('POST','https://example.invalid')),body={'code':code,'message':'private-api-key'})


def valid(_):
    return canonical({'summary':'规则仍可运行','evidence_refs':['close'],
                      'limitations':[],'counterarguments':[],'scenarios':[]})


def decisions(seeded):
    db,a,b,aid,baid = seeded
    ev = Evaluator(db,FixtureCalendar())
    _,adid=ev.evaluate(aid)
    _,bdid=ev.evaluate(baid)
    return db,a,b,adid,bdid


@pytest.mark.parametrize('status',[401,402,403,429])
def test_permanent_or_throttled_provider_failure_pauses_shared_auto_calls_without_changing_decisions(seeded,status):
    db,a,b,adid,bdid=decisions(seeded)
    before=db.rows('SELECT id,result FROM personal_decisions ORDER BY id')
    calls=[]
    def fail(_):
        calls.append(1)
        raise error(status)
    explain(db,adid,fail,model_identity())
    result=explain(Store(db.path),bdid,fail,model_identity()) # Simulate another process/restart.
    assert calls==[1] and result['diagnostic']['automatic_paused']
    assert db.rows('SELECT id,result FROM personal_decisions ORDER BY id')==before
    assert len(db.rows("SELECT * FROM health WHERE component='explanation'"))==1
    assert not db.rows("SELECT * FROM leases WHERE name LIKE 'explanation:%'")
    assert 'private-api-key' not in canonical(db.rows('SELECT * FROM settings'))


@pytest.mark.parametrize('code',['insufficient_quota','insufficient_balance','billing_hard_limit_reached'])
def test_explicit_quota_exhaustion_is_not_confused_with_short_rate_limit(code):
    result=failure_diagnostic(error(429,code),'provider')
    assert result['code']=='INSUFFICIENT_BALANCE' and not result['retryable']


def test_manual_retry_recovers_quota_immediately_and_clears_pause(seeded,monkeypatch):
    db,a,b,adid,bdid=decisions(seeded)
    def fail(_):raise error()
    explain(db,adid,fail,model_identity())
    assert a.ai_availability()['automatic_paused']
    monkeypatch.setattr('clawindex_next.adapters.llm_explanation',valid)
    a.explanation(adid,True)
    assert a.saved_explanation(adid)['status']=='MODEL_OK' and a.ai_availability() is None
    assert explain(db,bdid,valid,model_identity())['summary']=='规则仍可运行'


def test_pause_expires_and_configuration_changes_do_not_inherit_old_failure(seeded,monkeypatch):
    db,a,b,adid,bdid=decisions(seeded)
    diag=failure_diagnostic(error(),'provider')
    point=datetime.now(timezone.utc)
    record_failure(db,model_identity(),diag,point-timedelta(hours=7))
    assert paused(db,model_identity()) is None
    record_failure(db,model_identity(),diag,point)
    assert paused(db,model_identity())
    old_key=config_key(model_identity())
    monkeypatch.setenv('VNEXT_LLM_API_KEY','new-private-key')
    assert config_key(model_identity())!=old_key and paused(db,model_identity()) is None
    assert 'new-private-key' not in config_key(model_identity())


def test_old_success_cannot_clear_a_newer_failure(seeded):
    db,*_=seeded
    point=datetime.now(timezone.utc)
    record_failure(db,'model',failure_diagnostic(error(),'provider'),point)
    record_success(db,'model',point-timedelta(seconds=1))
    assert paused(db,'model',point)
    record_success(db,'model',point+timedelta(seconds=1))
    assert paused(db,'model',point) is None


def test_existing_successful_explanation_remains_usable_during_pause(seeded):
    db,a,b,adid,bdid=decisions(seeded)
    explain(db,bdid,valid,model_identity())
    def fail(_):raise error()
    explain(db,adid,fail,model_identity())
    assert paused(db,model_identity())
    assert explain(db,bdid,lambda _:pytest.fail('已有解读不应调用接口'),model_identity())['summary']=='规则仍可运行'


def test_transient_timeout_does_not_lock_out_other_indices(seeded):
    db,a,b,adid,bdid=decisions(seeded)
    def timeout(_):raise APITimeoutError(request=httpx.Request('POST','https://example.invalid'))
    explain(db,adid,timeout,model_identity())
    assert a.ai_availability() is None
    assert explain(db,bdid,valid,model_identity())['summary']=='规则仍可运行'


def test_ui_explains_pause_while_showing_program_generated_content(seeded,monkeypatch):
    monkeypatch.setenv('VNEXT_LLM_BASE_URL','https://example.invalid')
    monkeypatch.setenv('VNEXT_LLM_MODEL','test-model')
    db,a,b,adid,bdid=decisions(seeded)
    def fail(_):raise error()
    explain(db,adid,fail,model_identity())
    app=login(db,monkeypatch)
    button(app,'查看解读').click().run()
    assert not app.exception
    assert any('程序生成的解读' in x.value for x in app.markdown)
    assert any('程序判断继续运行' in x.value for x in app.caption)


def test_worker_finishes_both_users_inspections_when_ai_balance_is_empty(seeded,monkeypatch):
    db,a,b,aid,baid=seeded
    jobs=[enqueue(db,assignment,refresh=False,include_ai=True) for assignment in (aid,baid)]
    calls=[]
    def fail(_):
        calls.append(1)
        raise error()
    monkeypatch.setattr('clawindex_next.adapters.llm_explanation',fail)
    worker=Worker(db,Evaluator(db,FixtureCalendar()))
    assert worker.run_job() and worker.run_job() and not worker.run_job()
    assert calls==[1]
    assert {r['status'] for r in db.rows('SELECT status FROM jobs')}=={'SUCCEEDED'}
    assert len(db.rows('SELECT * FROM signal_runs'))==2
    assert len(db.rows('SELECT * FROM personal_decisions'))==2
    assert len(db.rows("SELECT * FROM explanations WHERE status='FAILED_TEMPLATE'"))==2


@pytest.mark.parametrize('variable,value',[
    ('VNEXT_LLM_BASE_URL','https://changed.invalid'),('VNEXT_LLM_MODEL','another-model')])
def test_changed_endpoint_or_model_is_not_paused_by_previous_provider_failure(seeded,monkeypatch,variable,value):
    db,*_=seeded
    record_failure(db,model_identity(),failure_diagnostic(error(),'provider'))
    assert paused(db,model_identity())
    monkeypatch.setenv(variable,value)
    assert paused(db,model_identity()) is None
