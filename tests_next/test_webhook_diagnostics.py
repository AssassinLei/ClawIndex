"""Provider diagnostics must survive UI/delivery paths without exposing secrets."""
import json

import pytest
import requests

from clawindex_next.adapters import WebhookError, send_feishu
from clawindex_next.service import Evaluator
from clawindex_next.worker import Worker
from conftest import FixtureCalendar
from test_ui import button
from test_ui_redesign import login
from test_webhooks import URL, prepare


class Response:
    def __init__(self, body=None, status=200):
        self.body,self.status_code = body,status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('private '+URL)

    def json(self):
        if isinstance(self.body,Exception):
            raise self.body
        return self.body


def sender_for(response):
    return lambda url,payload: send_feishu(url,payload,post=lambda *a,**kw:response)


@pytest.mark.parametrize('field',['code','StatusCode'])
@pytest.mark.parametrize('code,description',[(19024,'关键词'),(19022,'IP'),(19021,'签名'),(19001,'失效'),(19999,'拒绝')])
def test_business_error_reaches_user_and_private_health(seeded,field,code,description):
    db,a,b,*_ = seeded
    sender = sender_for(Response({field:code,'msg':URL,'StatusMessage':URL}))
    with pytest.raises(RuntimeError) as error:
        a.test_webhook(URL,sender=sender)
    assert str(code) in str(error.value) and description in str(error.value)
    log = [row for row in a.health() if row['component']=='notification']
    assert log[-1]['detail']==str(error.value) and log[-1]['status']=='FAILED'
    assert not [row for row in b.health() if row['component']=='notification']
    assert URL not in json.dumps(log) and 'test-placeholder' not in str(error.value)
    assert not a.channels() and not db.rows('SELECT * FROM outbox')


@pytest.mark.parametrize('body',[None,[],{}, {'code':False},{'code':'0'}, {'code':URL},ValueError(URL)])
def test_malformed_response_is_not_mistaken_for_success(body):
    with pytest.raises(WebhookError,match='INVALID_RESPONSE') as error:
        sender_for(Response(body))(URL,{'text':'你好，世界'})
    assert URL not in str(error.value)


@pytest.mark.parametrize('field',['code','StatusCode'])
def test_both_success_formats(field):
    sender_for(Response({field:0}))(URL,{'text':'你好，世界'})


def test_http_status_has_safe_diagnostic(seeded):
    db,a,*_ = seeded
    with pytest.raises(RuntimeError) as error:
        a.test_webhook(URL,sender=sender_for(Response({'code':0},403)))
    assert 'HTTP' in str(error.value) and '403' in str(error.value)
    assert URL not in str(error.value)


@pytest.mark.parametrize('kind,description',[(requests.Timeout,'超时'),(requests.ConnectionError,'无法连接'),(requests.exceptions.SSLError,'证书')])
def test_transport_error_survives_adapter_without_secret(seeded,kind,description):
    db,a,*_ = seeded
    def post(*args,**kwargs):
        raise kind('private '+URL)
    with pytest.raises(RuntimeError) as error:
        a.test_webhook(URL,sender=lambda url,p:send_feishu(url,p,post=post))
    assert description in str(error.value)
    assert 'test-placeholder' not in str(error.value)
    assert 'test-placeholder' not in json.dumps(a.health())


def test_formal_delivery_keeps_business_error_and_retry_audit(seeded):
    db,a,b,aid = prepare(seeded,'BUY')
    a.set_channel(URL,'test',mode='ALL')
    evaluator = Evaluator(db,FixtureCalendar())
    evaluator.evaluate(aid)
    worker = Worker(db,evaluator,sender=sender_for(Response({'code':19024,'msg':URL})))
    assert worker.deliver()
    outbox = db.rows('SELECT * FROM outbox')[0]
    audit = db.rows('SELECT * FROM delivery_attempts')[0]
    assert outbox['status']=='RETRY' and outbox['attempts']==1
    assert audit['status']=='RETRY' and '19024' in audit['detail']
    assert '操作' in audit['detail'] and URL not in audit['detail']
    assert any('19024' in row['detail'] for row in a.health())
    assert not any('19024' in row['detail'] for row in b.health())


def test_page_shows_keyword_failure_and_does_not_claim_success(seeded,monkeypatch):
    db,a,*_ = seeded
    monkeypatch.setattr('clawindex_next.adapters.send_feishu',sender_for(Response({'code':19024,'msg':URL})))
    app = login(db,monkeypatch)
    app.sidebar.radio[0].set_value('通知与运行').run()
    next(x for x in app.text_input if x.label=='飞书机器人通知地址').set_value(URL)
    button(app,'测试：发送你好，世界').click().run()
    assert not app.exception
    assert any('19024' in x.value and '关键词' in x.value for x in app.error)
    assert not any('已发送' in x.value for x in app.success)
    assert not a.channels()
