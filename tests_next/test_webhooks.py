import json
from datetime import datetime, timedelta, timezone

import pytest

from clawindex_next.adapters import ProviderError, send_feishu
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical, digest, now
from clawindex_next.webhooks import ACTIONS, message_body, validate_url
from clawindex_next.worker import Worker
from conftest import FixtureCalendar, price_rows
from test_research_delivery import enable
from test_ui import button
from test_ui_redesign import login

URL='https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder'


def prepare(seeded,action):
    db,a,b,aid,baid=seeded
    aid=enable(db,a)
    if action=='WAIT':
        with db.connect(True) as c:
            c.execute('UPDATE assignments SET catchup=0 WHERE id=?',(aid,))
    if action in ('HOLD','SELL'):
        a.report_position('HKTECH','HELD')
    if action=='SELL':
        dates,rows=price_rows(prices=[100+i*.2 if i<140 else 128-(i-140)*2 for i in range(160)])
        with db.connect(True) as c:
            for row in rows[-20:]:
                c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    return db,a,b,aid


@pytest.mark.parametrize('mode',['OFF','ALL','BUY_ONLY','BUY_SELL'])
@pytest.mark.parametrize('action',['BUY','SELL','HOLD','WAIT'])
def test_each_mode_filters_real_personal_actions(seeded,mode,action):
    db,a,b,aid=prepare(seeded,action)
    a.set_channel(URL,'test',mode=mode)
    ev=Evaluator(db,FixtureCalendar())
    ev.evaluate(aid)
    assert json.loads(a.results()[0]['decision'])['action']==action
    rows=db.rows('SELECT payload FROM outbox')
    assert bool(rows)==(action in ACTIONS[mode])
    sent=[]
    Worker(db,ev,sender=lambda url,p:sent.append(p)).deliver()
    if rows:
        assert sent==[{'index_name':'恒生科技','action':action}]
    else:
        assert not sent


def test_channels_and_filter_changes_are_user_scoped(seeded):
    db,a,b,aid,baid=seeded
    first=a.set_channel(URL,'alice',mode='ALL')
    second=b.set_channel(URL,'bob',mode='BUY_ONLY')
    assert [x['id'] for x in a.channels()]==[first]
    assert [x['id'] for x in b.channels()]==[second]
    assert 'url' not in a.channels()[0]
    a.configure_channel(first,'BUY_SELL')
    assert b.channels()[0]['mode']=='BUY_ONLY'
    for operation in (lambda:b.configure_channel(first,'ALL'),lambda:b.toggle_channel(first,False),lambda:b.test_channel(first,sender=lambda *_:pytest.fail('must not send'))):
        with pytest.raises(PermissionError):operation()
    assert a.channels()[0]['mode']=='BUY_SELL'


def test_multiple_channels_have_independent_filters(seeded):
    db,a,b,aid=prepare(seeded,'SELL')
    buy=a.set_channel(URL,'buy',mode='BUY_ONLY')
    sell=a.set_channel(URL+'2','buy and sell',mode='BUY_SELL')
    all_modes=a.set_channel(URL+'3','all',mode='ALL')
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    assert {x['channel_id'] for x in db.rows('SELECT channel_id FROM outbox')}=={sell,all_modes}


def test_existing_channels_keep_buy_sell_filter_and_off_is_preserved(seeded):
    db,a,b,aid,baid=seeded
    with db.connect(True) as c:
        cid=c.execute('INSERT INTO channels(user_id,url,label,enabled) VALUES(?,?,?,1)',(a.user['id'],URL,'legacy')).lastrowid
    assert a.channels()[0]['mode']=='BUY_SELL'
    a.configure_channel(cid,'BUY_ONLY')
    a.toggle_channel(cid,False)
    assert a.channels()[0]['mode']=='OFF'
    a.toggle_channel(cid,True)
    assert a.channels()[0]['mode']=='BUY_ONLY'


def test_worker_rechecks_new_filter_before_sending_old_queued_sell(seeded):
    db,a,b,aid=prepare(seeded,'SELL')
    cid=a.set_channel(URL,'test',mode='BUY_SELL')
    ev=Evaluator(db,FixtureCalendar())
    ev.evaluate(aid)
    a.configure_channel(cid,'BUY_ONLY')
    sent=[]
    assert Worker(db,ev,sender=lambda *args:sent.append(args)).deliver()
    assert not sent and db.rows('SELECT status FROM outbox')[0]['status']=='EXPIRED'


def test_worker_simplifies_previously_queued_long_message(seeded):
    db,a,b,aid=prepare(seeded,'BUY')
    a.set_channel(URL,'test',True)
    ev=Evaluator(db,FixtureCalendar())
    ev.evaluate(aid)
    with db.connect(True) as c:
        c.execute('UPDATE outbox SET payload=?',(canonical({'text':'sensitive-old-body amount and reasons'}),))
    sent=[]
    Worker(db,ev,sender=lambda url,p:sent.append(p)).deliver()
    assert sent==[{'index_name':'恒生科技','action':'BUY'}]


@pytest.mark.parametrize('action',['HOLD','WAIT'])
def test_all_mode_dedup_is_per_market_date(seeded,action):
    db,a,b,aid=prepare(seeded,action)
    a.set_channel(URL,'test',mode='ALL')
    ev=Evaluator(db,FixtureCalendar())
    instant=datetime.now(timezone.utc)
    ev.evaluate(aid,instant)
    ev.evaluate(aid,instant+timedelta(days=1))
    assert len(db.rows('SELECT * FROM outbox'))==1
    row=price_rows(1,end='2026-08-13')[1][0]
    row.update(open=133.,high=134.,low=132.,close=133.)
    with db.connect(True) as c:
        c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    class NextDay(FixtureCalendar):
        def expected(self,*args,**kwargs):return '2026-08-13'
    Evaluator(db,NextDay()).evaluate(aid,instant+timedelta(days=2))
    assert len(db.rows('SELECT * FROM outbox'))==2


def test_test_message_is_immediate_exact_and_does_not_save_or_enable(seeded):
    db,a,b,aid,baid=seeded
    sent=[]
    assert a.test_webhook(URL,sender=lambda url,p:sent.append((url,p)))
    assert sent==[(URL,{'text':'你好，世界'})]
    assert not a.channels() and not db.rows('SELECT * FROM outbox')
    cid=a.set_channel(URL,'off',mode='OFF')
    a.test_channel(cid,sender=lambda url,p:sent.append((url,p)))
    assert len(sent)==2 and a.channels()[0]['mode']=='OFF'


def test_hold_revision_does_not_duplicate_same_day_notification(seeded):
    db,a,b,aid=prepare(seeded,'HOLD')
    a.set_channel(URL,'test',mode='ALL')
    ev=Evaluator(db,FixtureCalendar())
    ev.evaluate(aid)
    first=json.loads(a.results()[0]['signal'])['triggered_at']
    row=price_rows()[1][124]
    row.update(open=50.,high=51.,low=49.,close=50.)
    with db.connect(True) as c:
        c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',('HKTECH',row['date'],canonical(row),'revision',now(),'FIXTURE',digest(row)))
    ev.evaluate(aid)
    assert first!=json.loads(a.results()[0]['signal'])['triggered_at']
    assert json.loads(a.results()[0]['decision'])['action']=='HOLD'
    assert len(db.rows('SELECT * FROM outbox'))==1
    sent=[]
    assert Worker(db,ev,sender=lambda url,p:sent.append(p)).deliver()
    assert sent==[{'index_name':'恒生科技','action':'HOLD'}]


def test_test_failure_does_not_expose_url_or_token(seeded):
    db,a,b,aid,baid=seeded
    def failure(*args):raise RuntimeError(URL)
    with pytest.raises(RuntimeError) as error:a.test_webhook(URL,sender=failure)
    assert URL not in str(error.value) and 'test-placeholder' not in str(error.value)
    assert not a.channels()


@pytest.mark.parametrize('url',['','http://open.feishu.cn/open-apis/bot/v2/hook/abc','https://other.invalid/open-apis/bot/v2/hook/abc',
    'https://open.feishu.cn/open-apis/bot/v2/hook/','https://open.feishu.cn:444/open-apis/bot/v2/hook/abc',
    URL+'?token=bad',URL+'#bad','https://name@open.feishu.cn/open-apis/bot/v2/hook/abc'])
def test_invalid_test_urls_are_rejected_before_network(seeded,url):
    db,a,b,aid,baid=seeded
    with pytest.raises(ValueError):a.test_webhook(url,sender=lambda *_:pytest.fail('no request'))


def test_card_contains_only_name_and_action_as_plain_text():
    requests=[]
    class Response:
        status_code=200
        def raise_for_status(self):pass
        def json(self):return {'code':0}
    send_feishu(URL,{'index_name':'电池 <at id=all>','action':'BUY'},post=lambda url,**kw:requests.append(kw) or Response())
    assert requests[0]['allow_redirects'] is False
    body=requests[0]['json']
    assert body['msg_type']=='interactive'
    assert body['card']['header']['title']=={'tag':'plain_text','content':'电池 <at id=all>'}
    assert body['card']['elements']==[{'tag':'div','text':{'tag':'plain_text','content':'操作：买入'}}]
    assert message_body({'text':'你好，世界'})=={'msg_type':'text','content':{'text':'你好，世界'}}


def test_redirect_does_not_send_webhook_secret_to_another_destination():
    class Response:
        status_code=302
        def raise_for_status(self):pass
        def json(self):return {'code':0}
    with pytest.raises(ProviderError,match='REDIRECT_BLOCKED'):
        send_feishu(URL,{'text':'test'},post=lambda *a,**k:Response())


def test_page_tests_draft_then_saves_and_updates_each_channel(seeded,monkeypatch):
    db,a,b,aid,baid=seeded
    sent=[]
    monkeypatch.setattr('clawindex_next.adapters.send_feishu',lambda url,p:sent.append((url,p)))
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('通知与运行').run()
    next(x for x in app.text_input if x.label=='飞书机器人通知地址').set_value(URL)
    next(x for x in app.text_input if x.label=='渠道名称').set_value('我的买入提醒')
    button(app,'测试：发送你好，世界').click().run()
    assert not app.exception and sent==[(URL,{'text':'你好，世界'})] and not a.channels()
    next(x for x in app.selectbox if x.label=='新Webhook推送方式').set_value('BUY_ONLY')
    button(app,'保存Webhook').click().run()
    assert not app.exception
    cid=a.channels()[0]['id']
    assert a.channels()[0]['mode']=='BUY_ONLY' and not b.channels()
    next(x for x in app.selectbox if x.key=='channel_mode_'+str(cid)).set_value('OFF').run()
    assert not app.exception and a.channels()[0]['mode']=='OFF'
    button(app,'发送测试消息').click().run()
    assert not app.exception and len(sent)==2 and a.channels()[0]['mode']=='OFF'


def test_address_reveal_is_explicit_and_owner_scoped(seeded):
    db,a,b,aid,baid=seeded
    cid=a.set_channel(URL,'test',mode='BUY_ONLY')
    assert a.channel_url(cid)==URL
    assert 'url' not in a.channels()[0]
    with pytest.raises(PermissionError):b.channel_url(cid)
    with pytest.raises(PermissionError):b.delete_channel(cid)
    assert a.channel_url(cid)==URL


@pytest.mark.parametrize('status',['PENDING','RETRY','SENDING'])
def test_delete_cancels_unsent_messages_and_prevents_reactivation(seeded,status):
    db,a,b,aid=prepare(seeded,'BUY')
    cid=a.set_channel(URL,'test',mode='BUY_ONLY')
    other=b.set_channel(URL,'other',mode='ALL')
    ev=Evaluator(db,FixtureCalendar())
    ev.evaluate(aid)
    with db.connect(True) as c:
        c.execute('UPDATE outbox SET status=?,claim_owner=?,claim_until=?',(status,'old-worker',(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()))
    assert a.delete_channel(cid) and a.delete_channel(cid)
    assert not a.channels() and b.channel_url(other)==URL
    assert db.rows('SELECT url,enabled FROM channels WHERE id=?',(cid,))[0]=={'url':'','enabled':0}
    row=db.rows('SELECT status,claim_owner,claim_until FROM outbox')[0]
    assert row=={'status':'CANCELLED','claim_owner':None,'claim_until':None}
    assert not db.rows("SELECT key FROM settings WHERE key LIKE 'webhook_filter:'||?||':'||?",(a.user['id'],cid))
    for operation in (lambda:a.channel_url(cid),lambda:a.configure_channel(cid,'ALL'),lambda:a.toggle_channel(cid,True),lambda:a.test_channel(cid,sender=lambda *_:pytest.fail('deleted'))):
        with pytest.raises(PermissionError):operation()
    assert not Worker(db,ev,sender=lambda *_:pytest.fail('deleted')).deliver()
    with db.connect() as c:
        assert not list(c.execute('PRAGMA foreign_key_check'))


def test_delete_preserves_completed_delivery_history(seeded):
    db,a,b,aid=prepare(seeded,'BUY')
    cid=a.set_channel(URL,'history',mode='BUY_SELL')
    ev=Evaluator(db,FixtureCalendar())
    ev.evaluate(aid)
    assert Worker(db,ev,sender=lambda *_:None).deliver()
    before=db.rows('SELECT * FROM delivery_attempts')
    a.delete_channel(cid)
    assert db.rows('SELECT * FROM delivery_attempts')==before
    assert db.rows('SELECT status FROM outbox')[0]['status']=='SENT'


def test_finishing_request_cannot_restore_deleted_channel_or_queue(seeded):
    db,a,b,aid=prepare(seeded,'BUY')
    cid=a.set_channel(URL,'inflight',mode='BUY_ONLY')
    ev=Evaluator(db,FixtureCalendar())
    ev.evaluate(aid)
    def send_then_delete(*args):
        a.delete_channel(cid)
    assert Worker(db,ev,sender=send_then_delete).deliver()
    assert not a.channels()
    assert db.rows('SELECT status FROM outbox')[0]['status']=='CANCELLED'
    # A request already in progress can finish; retain its actual attempt result.
    assert db.rows('SELECT status FROM delivery_attempts')[0]['status']=='SENT'


def test_page_reveals_hides_and_deletes_saved_address(seeded,monkeypatch):
    db,a,b,aid,baid=seeded
    cid=a.set_channel(URL,'我的地址',mode='OFF')
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('通知与运行').run()
    assert not app.code
    button(app,'查看地址').click().run()
    assert not app.exception and any(x.value==URL for x in app.code)
    button(app,'隐藏地址').click().run()
    assert not app.exception and not app.code
    button(app,'查看地址').click().run()
    button(app,'确认删除').click().run()
    assert not app.exception and not app.code and not a.channels()
    assert not app.session_state.get('show_webhook_address_'+str(cid),False)
    assert any('Webhook已删除' in x.value for x in app.success)
