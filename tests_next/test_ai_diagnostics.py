import json
from types import SimpleNamespace

import httpx
from openai import APITimeoutError, APIConnectionError, APIStatusError
import pytest
from streamlit.testing.v1 import AppTest

from clawindex_next.adapters import ProviderError, llm_explanation
from clawindex_next.explanations import explain, validate_explanation, model_identity, ExplanationValidationError
from clawindex_next.llm_diagnostics import failure_diagnostic
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical
from conftest import FixtureCalendar


def response(summary='规则证据说明',**changes):
    return canonical(dict(summary=summary,evidence_refs=['close'],limitations=[],counterarguments=[],scenarios=[],**changes))


@pytest.mark.parametrize('text',['不保证收益','不能保证收益','无法保证收益','本分析不代表保证收益','本系统并不保证收益'])
def test_negative_guarantee_disclaimers_are_valid(text):
    assert validate_explanation(response(text),{'close':100})['summary']==text


@pytest.mark.parametrize('text',['保证收益','不能保证收益，但这个策略保证收益','不保证收益，明天确定上涨','建议卖出','立即买入','必须卖出','不能不保证收益','并非不保证收益','不是不保证收益'])
def test_disclaimers_cannot_hide_positive_guarantees_or_orders(text):
    with pytest.raises(ExplanationValidationError) as caught:
        validate_explanation(response(text),{'close':100})
    assert caught.value.code=='UNSAFE_CLAIM'


@pytest.mark.parametrize('status,code',[(400,'BAD_REQUEST'),(401,'AUTHENTICATION'),(402,'INSUFFICIENT_BALANCE'),(403,'PERMISSION'),(404,'MODEL_NOT_FOUND'),(408,'TIMEOUT'),(422,'BAD_REQUEST'),(429,'RATE_LIMIT'),(500,'PROVIDER_UNAVAILABLE'),(503,'PROVIDER_UNAVAILABLE')])
def test_http_diagnostics_never_save_raw_errors_or_keys(status,code):
    request=httpx.Request('POST','https://example.invalid/chat/completions')
    exc=APIStatusError('secret-sk-should-not-leak',response=httpx.Response(status,request=request),body={'error':'secret-sk-should-not-leak'})
    result=failure_diagnostic(exc,'provider')
    assert result['code']==code and result['http_status']==status
    assert 'secret' not in canonical(result)


@pytest.mark.parametrize('factory,code',[(APITimeoutError,'TIMEOUT'),(APIConnectionError,'CONNECTION')])
def test_network_errors_are_distinguished(factory,code):
    assert failure_diagnostic(factory(request=httpx.Request('POST','https://example.invalid')),'provider')['code']==code


@pytest.mark.parametrize('error,code',[
    (ProviderError('LLM_NOT_CONFIGURED'),'NOT_CONFIGURED'),
    (ProviderError('LLM_EMPTY_RESPONSE'),'EMPTY_RESPONSE'),
    (ProviderError('LLM_INCOMPLETE_RESPONSE'),'INCOMPLETE_RESPONSE'),
    (ProviderError('LLM_INVALID_CONFIG'),'INVALID_CONFIG'),
    (RuntimeError('api-key-secret'),'UNEXPECTED'),
])
def test_safe_internal_error_codes(error,code):
    result=failure_diagnostic(error,'provider')
    assert result['code']==code
    assert 'api-key' not in canonical(result)


def test_failure_is_persisted_for_user_and_retry_replaces_template_only(seeded):
    db,a,b,aid,baid=seeded
    sid,did=Evaluator(db,FixtureCalendar()).evaluate(aid)
    before=db.rows('SELECT result FROM signal_runs WHERE id=?',(sid,))
    def fail(payload):
        raise APITimeoutError(request=httpx.Request('POST','https://example.invalid'))
    result=explain(db,did,fail,'test-model')
    assert result['diagnostic']['code']=='TIMEOUT'
    log=db.rows("SELECT * FROM health WHERE component='explanation'")[-1]
    assert log['user_id']==a.user['id']
    assert json.loads(log['detail'])['decision_id']==did
    assert not db.rows("SELECT * FROM leases WHERE name LIKE 'explanation:%'")
    explain(db,did,lambda _:response(),'test-model')
    record=db.rows("SELECT * FROM explanations WHERE model='test-model'")[0]
    assert record['status']=='MODEL_OK' and 'diagnostic' not in json.loads(record['payload'])
    assert db.rows('SELECT result FROM signal_runs WHERE id=?',(sid,))==before


@pytest.mark.parametrize('raw,expected',[
    ('not json','INVALID_JSON'),
    ('{"summary":"incomplete"}','INVALID_STRUCTURE'),
    (canonical({'summary':'说明','evidence_refs':['invented'],'limitations':[],'counterarguments':[],'scenarios':[]}),'UNKNOWN_EVIDENCE'),
    (response('建议买入'),'UNSAFE_CLAIM'),
])
def test_validation_errors_have_distinct_persisted_categories(seeded,raw,expected):
    db,a,b,aid,baid=seeded
    _,did=Evaluator(db,FixtureCalendar()).evaluate(aid)
    assert explain(db,did,lambda _:raw,'fake')['diagnostic']['code']==expected


def mock_client(monkeypatch,finish='stop',content=None):
    calls={}
    def create(**kwargs):
        calls['request']=kwargs
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,message=SimpleNamespace(content=content if content is not None else response()))])
    def client(**kwargs):
        calls['client']=kwargs
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr('openai.OpenAI',client)
    monkeypatch.setenv('VNEXT_LLM_API_KEY','test-only-key')
    for key in ('VNEXT_LLM_TIMEOUT_SECONDS','VNEXT_LLM_MAX_TOKENS','VNEXT_LLM_THINKING'):
        monkeypatch.delenv(key,raising=False)
    return calls


def test_deepseek_short_explanation_uses_bounded_non_thinking_request(monkeypatch):
    calls=mock_client(monkeypatch)
    monkeypatch.setenv('VNEXT_LLM_BASE_URL','https://api.deepseek.com/v1')
    llm_explanation({'features':{'close':100}})
    assert calls['client']['timeout']==60 and calls['client']['max_retries']==0
    assert calls['request']['max_tokens']==1800
    assert calls['request']['extra_body']=={'thinking':{'type':'disabled'}}
    assert calls['request']['response_format']=={'type':'json_object'}


def test_other_compatible_providers_do_not_receive_deepseek_parameters(monkeypatch):
    calls=mock_client(monkeypatch)
    monkeypatch.setenv('VNEXT_LLM_BASE_URL','https://other.invalid/v1')
    llm_explanation({})
    assert 'extra_body' not in calls['request']


@pytest.mark.parametrize('name,value', [('VNEXT_LLM_TIMEOUT_SECONDS','NaN'),('VNEXT_LLM_TIMEOUT_SECONDS','120'),('VNEXT_LLM_MAX_TOKENS','bad'),('VNEXT_LLM_THINKING','surprise')])
def test_invalid_configuration_is_rejected_before_network(monkeypatch,name,value):
    calls=mock_client(monkeypatch)
    monkeypatch.setenv(name,value)
    with pytest.raises(ProviderError,match='LLM_INVALID_CONFIG'):
        llm_explanation({})
    assert not calls


@pytest.mark.parametrize('finish,content,code',[('length','partial','LLM_INCOMPLETE_RESPONSE'),('stop','  ','LLM_EMPTY_RESPONSE')])
def test_empty_or_truncated_response_is_not_parsed_as_success(monkeypatch,finish,content,code):
    mock_client(monkeypatch,finish,content)
    with pytest.raises(ProviderError,match=code):
        llm_explanation({})


def test_request_mode_change_invalidates_ai_cache_but_timeout_does_not(monkeypatch):
    monkeypatch.setenv('VNEXT_LLM_THINKING','disabled')
    initial=model_identity()
    monkeypatch.setenv('VNEXT_LLM_TIMEOUT_SECONDS','50')
    assert model_identity()==initial
    monkeypatch.setenv('VNEXT_LLM_THINKING','enabled')
    assert model_identity()!=initial


def test_ui_displays_specific_failure_without_raw_provider_body():
    app=AppTest.from_string('''
from clawindex_next.ui_components import explanation_panel
explanation_panel({'status':'FAILED_TEMPLATE','created_at':'2026-10-14T14:00:00+00:00','payload':{
 'summary':'程序解读','limitations':[],'counterarguments':[],'scenarios':[],
 'diagnostic':{'code':'TIMEOUT','message':'AI接口响应超时，请稍后重试。','phase':'provider'}}})
''').run()
    assert not app.exception
    assert any('响应超时' in x.value for x in app.warning)
    assert any(x.label=='AI失败诊断详情' for x in app.expander)
