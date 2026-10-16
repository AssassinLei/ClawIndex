import json
from datetime import datetime, timedelta, timezone

import pytest

from clawindex_next.radar import publish
from clawindex_next.radar_notifications import deliver as deliver_radar
from clawindex_next.service import Evaluator, UserService
from clawindex_next.storage import canonical, now
from clawindex_next.webhook_schedule import allowed, config_key, next_times, normalize, read, validate_expression
from clawindex_next.worker import Worker
from conftest import FixtureCalendar
from test_evolution import T0
from test_radar import radar_seed, report_for
from test_radar_notifications_tracking import notices
from test_research_delivery import enable
from test_ui_redesign import login, button
from test_webhooks import URL


def instant(text):
    return datetime.fromisoformat(text)


@pytest.mark.parametrize('expression', ['', '* * * *', '* * * * * *', '@daily', '61 19 * * *',
                                     '0 25 * * *', '0 0 30 2 *', '*/0 * * * *', 'R 19 * * *', None, []])
def test_invalid_cron_is_rejected(expression):
    with pytest.raises(ValueError):validate_expression(expression)


@pytest.mark.parametrize('expression', ['30 19 * * mon-fri', '0 9,15 * * mon,wed,fri',
                                     '*/30 9-15 * * 1-5', '0 9 * * 0,7', '0 0 29 feb *'])
def test_five_field_lists_ranges_steps_names_and_leap_day_are_supported(expression):
    assert validate_expression(expression)==expression


@pytest.mark.parametrize('time,expected', [
    ('2026-10-16T19:29:59+08:00',False), ('2026-10-16T19:30:00+08:00',True),
    ('2026-10-16T19:34:59+08:00',True), ('2026-10-16T19:35:00+08:00',False),
    ('2026-10-17T19:30:00+08:00',False), ('2026-10-19T11:30:00+00:00',True),
])
def test_delivery_uses_beijing_weekdays_and_bounded_window(seeded,time,expected):
    db,a,*_=seeded
    cid=a.set_channel(URL,'test',mode='ALL',schedule={'enabled':True,'cron':'30 19 * * mon-fri'})
    with db.connect() as c:
        assert allowed(c,a.user['id'],cid,instant(time)) is expected


def test_sunday_numeric_convention_and_no_catchup_across_midnight(seeded):
    db,a,*_=seeded
    cid=a.set_channel(URL,'test',mode='ALL')
    for expression in ('59 23 * * 0','59 23 * * 7','59 23 * * sun'):
        a.configure_channel_schedule(cid,True,expression)
        with db.connect() as c:
            assert allowed(c,a.user['id'],cid,instant('2026-10-18T23:59:30+08:00'))
            assert not allowed(c,a.user['id'],cid,instant('2026-10-19T00:00:30+08:00'))


def test_day_of_month_and_weekday_follow_standard_cron_or_semantics(seeded):
    db,a,*_=seeded
    cid=a.set_channel(URL,'test',mode='ALL',schedule={'enabled':True,'cron':'0 9 1 * mon'})
    with db.connect() as c:
        assert allowed(c,a.user['id'],cid,instant('2026-10-19T09:00:00+08:00'))
        assert allowed(c,a.user['id'],cid,instant('2026-11-01T09:00:00+08:00'))
        assert not allowed(c,a.user['id'],cid,instant('2026-10-20T09:00:00+08:00'))


def test_next_three_times_skip_weekend_and_immediate_mode_has_no_dates():
    config=normalize({'enabled':True,'cron':'30 19 * * mon-fri'})
    assert next_times(config,instant('2026-10-16T11:29:00+00:00'))==[
        '2026-10-16T19:30:00+08:00','2026-10-19T19:30:00+08:00','2026-10-20T19:30:00+08:00']
    assert next_times(normalize(None))==[]


def test_schedule_is_private_per_channel_and_invalid_update_preserves_previous_value(seeded):
    db,a,b,*_=seeded
    first=a.set_channel(URL,'first',mode='ALL')
    second=a.set_channel(URL,'second',mode='ALL')
    other=b.set_channel(URL,'other',mode='ALL')
    a.configure_channel_schedule(first,True,'0 9 * * mon')
    before=a.channel_schedule(first)
    assert UserService(type(db)(db.path),a.token).channel_schedule(first)['cron']==before['cron']
    with pytest.raises(ValueError):a.configure_channel_schedule(first,True,'invalid')
    assert a.channel_schedule(first)['cron']==before['cron']
    assert not a.channel_schedule(second)['enabled'] and not b.channel_schedule(other)['enabled']
    with pytest.raises(PermissionError):b.channel_schedule(first)
    with pytest.raises(PermissionError):b.configure_channel_schedule(first,False,'* * * * *')


def test_invalid_creation_is_atomic_and_delete_erases_schedule(seeded):
    db,a,*_=seeded
    with pytest.raises(ValueError):a.set_channel(URL,'invalid',mode='ALL',schedule={'enabled':True,'cron':'invalid'})
    assert not a.channels()
    cid=a.set_channel(URL,'valid',mode='ALL',schedule={'enabled':True,'cron':'0 9 * * mon'})
    a.delete_channel(cid)
    assert not db.rows('SELECT key FROM settings WHERE key=?',(config_key(a.user['id'],cid),))


def test_legacy_channels_stay_immediate_and_corrupt_schedule_fails_closed(seeded):
    db,a,*_=seeded
    cid=a.set_channel(URL,'test',mode='ALL')
    with db.connect(True) as c:c.execute('DELETE FROM settings WHERE key=?',(config_key(a.user['id'],cid),))
    with db.connect() as c:assert allowed(c,a.user['id'],cid,instant('2026-10-17T03:00:00+08:00'))
    with db.connect(True) as c:c.execute('INSERT INTO settings VALUES(?,?)',(config_key(a.user['id'],cid),'invalid-json'))
    with db.connect() as c:
        assert read(c,a.user['id'],cid).get('error')
        assert not allowed(c,a.user['id'],cid,instant('2026-10-16T19:30:00+08:00'))


def operation_queue(seeded):
    db,a,b,*_=seeded
    aid=enable(db,a)
    scheduled=a.set_channel(URL,'scheduled',mode='BUY_SELL',schedule={'enabled':True,'cron':'30 19 * * mon-fri'})
    immediate=a.set_channel(URL+'2','immediate',mode='BUY_SELL')
    evaluator=Evaluator(db,FixtureCalendar())
    point=instant('2026-08-12T11:20:00+00:00')
    evaluator.evaluate(aid,point)
    with db.connect(True) as c:c.execute('UPDATE outbox SET next_at=?',(point.isoformat(),))
    return db,a,scheduled,immediate,evaluator,point


def test_future_scheduled_trade_does_not_block_immediate_channel_or_consume_attempts(seeded):
    db,a,scheduled,immediate,evaluator,point=operation_queue(seeded)
    sent=[]
    worker=Worker(db,evaluator,sender=lambda url,p:sent.append(url))
    assert worker.deliver(point) and sent==[URL+'2']
    assert not worker.deliver(point)
    waiting=db.rows('SELECT * FROM outbox WHERE channel_id=?',(scheduled,))[0]
    assert waiting['status']=='PENDING' and waiting['attempts']==0 and not waiting['claim_owner']
    assert worker.deliver(point+timedelta(minutes=10)) and sent==[URL+'2',URL]
    assert not worker.deliver(point+timedelta(minutes=11))


def test_schedule_edit_takes_effect_on_existing_queue_and_stale_actions_are_never_sent(seeded):
    db,a,scheduled,immediate,evaluator,point=operation_queue(seeded)
    worker=Worker(db,evaluator,sender=lambda *_:None)
    assert worker.deliver(point)
    a.configure_channel_schedule(scheduled,False,'30 19 * * mon-fri')
    assert worker.deliver(point)
    # A separate queue must still revalidate decision expiry after waiting for Cron.
    with db.connect(True) as c:
        c.execute("UPDATE outbox SET status='PENDING',attempts=0 WHERE channel_id=?",(scheduled,))
        c.execute("UPDATE personal_decisions SET result=json_set(result,'$.expires_at','2000-01-01') WHERE id IN (SELECT decision_id FROM outbox WHERE channel_id=?)",(scheduled,))
    a.configure_channel_schedule(scheduled,True,'30 19 * * mon-fri')
    worker.sender=lambda *_:pytest.fail('expired action must not send')
    assert worker.deliver(point+timedelta(minutes=10))
    assert db.rows('SELECT status FROM outbox WHERE channel_id=?',(scheduled,))[0]['status']=='EXPIRED'


def test_failed_send_must_obey_both_retry_backoff_and_cron_window(seeded):
    db,a,scheduled,immediate,evaluator,point=operation_queue(seeded)
    worker=Worker(db,evaluator,sender=lambda *_:None)
    assert worker.deliver(point) # Immediate channel goes first.
    def fail(*args):
        raise ConnectionError('offline-test')
    worker.sender=fail
    late=point+timedelta(minutes=14,seconds=50) # 19:34:50, inside the 19:30 window.
    assert worker.deliver(late)
    assert not worker.deliver(late+timedelta(seconds=5)) # Retry backoff still active.
    assert not worker.deliver(late+timedelta(seconds=31)) # Backoff ended, but time window closed.
    row=db.rows('SELECT * FROM outbox WHERE channel_id=?',(scheduled,))[0]
    assert row['status']=='RETRY' and row['attempts']==1


def test_radar_uses_same_time_gate_and_does_not_block_other_channels(radar_seed):
    db,a,*_=radar_seed
    scheduled=a.set_channel(URL,'scheduled',mode='ALL',radar_mode='ALL',schedule={'enabled':True,'cron':'0 23 * * mon-fri'})
    immediate=a.set_channel(URL+'2','immediate',mode='ALL',radar_mode='ALL')
    publish(db,report_for(radar_seed),T0)
    sent=[]
    before=T0-timedelta(minutes=1)
    # Make notices due before both checks without changing their market evidence date.
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value=json_set(value,'$.next_at',?) WHERE key LIKE 'radar:notice:%'",(before.isoformat(),))
    while deliver_radar(db,lambda url,p:sent.append(url),FixtureCalendar(),before):pass
    assert sent and set(sent)=={URL+'2'}
    assert all(r['attempts']==0 for r in notices(db) if r['channel_id']==scheduled)
    while deliver_radar(db,lambda url,p:sent.append(url),FixtureCalendar(),T0):pass
    assert URL in sent and all(r['status']=='SENT' for r in notices(db))


def test_test_message_bypasses_schedule_and_does_not_change_it(seeded):
    db,a,*_=seeded
    cid=a.set_channel(URL,'scheduled',mode='OFF',schedule={'enabled':True,'cron':'0 9 * * mon'})
    calls=[]
    assert a.test_channel(cid,sender=lambda url,p:calls.append(p))
    assert calls==[{'text':'你好，世界'}]
    assert a.channel_schedule(cid)['enabled'] and not a.channels()[0]['enabled']


def test_schedule_editor_validates_saves_and_previews_without_sending(seeded,monkeypatch):
    db,a,*_=seeded
    cid=a.set_channel(URL,'test',mode='ALL')
    monkeypatch.setattr('clawindex_next.adapters.send_feishu',lambda *_:pytest.fail('configuration must not send'))
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('通知与运行').run()
    next(x for x in app.checkbox if x.key=='schedule_on_'+str(cid)).check()
    next(x for x in app.text_input if x.key=='schedule_cron_'+str(cid)).set_value('0 9 * * mon,wed,fri')
    button(app,'保存发送时间').click().run()
    assert not app.exception and a.channel_schedule(cid)['enabled']
    assert any('接下来三次计划时间' in x.value for x in app.caption)
    previous=a.channel_schedule(cid)['cron']
    next(x for x in app.text_input if x.key=='schedule_cron_'+str(cid)).set_value('invalid')
    button(app,'保存发送时间').click().run()
    assert not app.exception and app.error and a.channel_schedule(cid)['cron']==previous


def test_new_webhook_form_saves_schedule_atomically(seeded,monkeypatch):
    db,a,*_=seeded
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('通知与运行').run()
    next(x for x in app.text_input if x.label=='飞书机器人通知地址').set_value(URL)
    next(x for x in app.selectbox if x.label=='新Webhook推送方式').set_value('BUY_ONLY')
    next(x for x in app.checkbox if x.label=='新Webhook按Cron定时发送').check()
    next(x for x in app.text_input if x.label=='新Webhook Cron表达式').set_value('30 19 * * mon-fri')
    button(app,'保存Webhook').click().run()
    assert not app.exception
    config=a.channel_schedule(a.channels()[0]['id'])
    assert config['enabled'] and config['cron']=='30 19 * * mon-fri'
