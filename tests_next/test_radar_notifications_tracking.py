import copy
import json
from datetime import timedelta

import pytest

from clawindex_next.radar import BENCHMARK, latest, publish, read, write
from clawindex_next.radar_notifications import deliver, enqueue
from clawindex_next.radar_tracking import outcomes
from clawindex_next.storage import canonical, digest, now
from clawindex_next.webhooks import message_body
from conftest import FixtureCalendar, price_rows
from test_evolution import T0
from test_radar import radar_seed, report_for
from test_webhooks import URL


def notices(db):
    return [json.loads(r['value']) for r in db.rows("SELECT value FROM settings WHERE key LIKE 'radar:notice:%'")]


def test_radar_notifications_default_off_and_do_not_use_trade_outbox(radar_seed):
    db,a,b,*_ = radar_seed
    a.set_channel(URL,'trade',mode='BUY_SELL')
    publish(db,report_for(radar_seed),T0)
    assert not notices(db) and not db.rows('SELECT * FROM outbox')


def test_per_user_per_channel_filters_and_safe_payload(radar_seed):
    db,a,b,*_ = radar_seed
    cid = a.set_channel(URL,'a',mode='BUY_ONLY')
    other = b.set_channel(URL,'b',mode='ALL')
    a.set_radar_channel(cid,'ALL')
    assert b.radar_channel_mode(other)=='OFF'
    with pytest.raises(PermissionError):b.set_radar_channel(cid,'ALL')
    with pytest.raises(PermissionError):b.radar_channel_mode(cid)
    publish(db,report_for(radar_seed),T0)
    queue = notices(db)
    assert queue and all(r['user_id']==a.user['id'] and r['channel_id']==cid for r in queue)
    assert len(queue)<=3
    sent = []
    while deliver(db,lambda url,p:sent.append(p),FixtureCalendar(),T0):pass
    assert len(sent)==len(queue)
    assert all({'radar_name','radar_state'} <= set(p) <= {'radar_name','radar_state','radar_level','radar_parent'} for p in sent)
    assert 'test-placeholder' not in canonical(notices(db))
    assert not db.rows('SELECT * FROM outbox')
    assert all(r['status']=='SENT' for r in notices(db))


def test_ready_filter_and_persistent_candidate_do_not_send_daily_repeats(radar_seed):
    db,a,b,*_ = radar_seed
    cid = a.set_channel(URL,'ready',mode='ALL')
    a.set_radar_channel(cid,'READY')
    report = report_for(radar_seed)
    for row in report['rows']:
        if row['state']=='READY':row['state']='WATCH'
    publish(db,report,T0)
    assert not notices(db)
    changed = copy.deepcopy(report)
    code = changed['selected'][0]
    next(r for r in changed['rows'] if r['code']==code)['state']='READY'
    publish(db,changed,T0+timedelta(minutes=1))
    assert notices(db) and all(r['code']==code and r['event_state']=='READY' for r in notices(db))
    count = len(notices(db))
    # A fresh evidence revision with the same opportunity states is not a new alert.
    changed['sources'][code]['observation_max_id'] += 1
    publish(db,changed,T0+timedelta(minutes=2))
    assert len(notices(db))==count


@pytest.mark.parametrize('change',['disable','delete','expired','ignore','rule'])
def test_send_time_rechecks_channel_freshness_and_preferences(radar_seed,monkeypatch,change):
    db,a,b,*_ = radar_seed
    cid = a.set_channel(URL,'a',mode='ALL')
    a.set_radar_channel(cid,'ALL')
    publish(db,report_for(radar_seed),T0)
    if change=='disable':a.configure_channel(cid,'OFF')
    elif change=='delete':a.delete_channel(cid)
    elif change=='ignore':
        for item in notices(db):a.radar_preferences(ignore=item['code'])
    elif change=='rule':
        report = latest(db)
        report['rule_id'] = 'obsolete-rule'
        write(db,'radar:snapshot:'+report['id'],report)
    class Calendar(FixtureCalendar):
        def expected(self,*args,**kwargs):return '2026-08-13' if change=='expired' else '2026-08-12'
    while deliver(db,lambda *_:pytest.fail('must not send'),Calendar(),T0):pass
    assert all(r['status'] in ('CANCELLED','EXPIRED') for r in notices(db))


def test_withdrawal_after_ranking_change_is_kept_in_system_only(radar_seed):
    db,a,*_ = radar_seed
    cid = a.set_channel(URL,'ready',mode='ALL')
    a.set_radar_channel(cid,'READY')
    report = report_for(radar_seed)
    publish(db,report,T0)
    while deliver(db,lambda *_:None,FixtureCalendar(),T0):pass
    code = next(r['code'] for r in notices(db) if r['status']=='SENT')
    # It slips out of the top three while still a valid observation.
    revised = copy.deepcopy(report)
    row = next(r for r in revised['rows'] if r['code']==code)
    row['state']='WATCH'
    revised['selected']=[c for c in revised['selected'] if c!=code]
    publish(db,revised,T0+timedelta(minutes=1))
    row['state']='REJECTED'
    publish(db,revised,T0+timedelta(minutes=2))
    withdrawn = [r for r in notices(db) if r['kind']=='WITHDRAWN']
    assert not withdrawn
    while deliver(db,lambda *_:None,FixtureCalendar(),T0+timedelta(minutes=3)):pass
    assert not [r for r in notices(db) if r['kind']=='WITHDRAWN']
    revised['sources'][code]['observation_max_id']+=1
    publish(db,revised,T0+timedelta(minutes=4))
    assert not [r for r in notices(db) if r['kind']=='WITHDRAWN']


def test_retry_is_bounded_and_raw_error_never_leaks(radar_seed):
    db,a,b,*_ = radar_seed
    cid = a.set_channel(URL,'a',mode='ALL')
    a.set_radar_channel(cid,'ALL')
    publish(db,report_for(radar_seed),T0)
    def failure(*args):raise RuntimeError(URL)
    for i in range(3):
        while deliver(db,failure,FixtureCalendar(),T0+timedelta(minutes=10*i)):pass
    assert all(r['status']=='FAILED' and r['attempts']==3 for r in notices(db))
    assert 'test-placeholder' not in canonical(notices(db))+canonical(a.health())


def test_disabling_during_send_does_not_revive_queue_and_audits_actual_attempt(radar_seed):
    db,a,b,*_ = radar_seed
    cid = a.set_channel(URL,'a',mode='ALL')
    a.set_radar_channel(cid,'ALL')
    publish(db,report_for(radar_seed),T0)
    assert deliver(db,lambda *_:a.set_radar_channel(cid,'OFF'),FixtureCalendar(),T0)
    assert all(r['status']=='CANCELLED' for r in notices(db))
    audits = [json.loads(r['value']) for r in db.rows("SELECT value FROM settings WHERE key LIKE 'radar:delivery:%'")]
    assert len(audits)==1 and audits[0]['status']=='SENT'


def test_opportunity_cards_are_explicit_and_trade_cards_stay_minimal():
    card = message_body({'radar_name':'测试指数','radar_state':'WATCH'})
    assert '观察' in card['card']['elements'][0]['text']['content']
    assert '不是买入指令' in card['card']['elements'][0]['text']['content']
    assert message_body({'index_name':'测试指数','action':'BUY'})['card']['elements']==[{'tag':'div','text':{'tag':'plain_text','content':'操作：买入'}}]


def add_future(db,code,dates,prices):
    with db.connect(True) as c:
        for day,price in zip(dates,prices):
            row = {'date':day,'open':price,'high':price+1,'low':price-1,'close':price,'pe':20.,'pb':2.}
            c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,'forward-fixture',?,'FIXTURE',?)",(code,day,canonical(row),now(),digest(row)))


def test_forward_tracking_keeps_losers_and_observation_timing(radar_seed):
    db,a,b,*_ = radar_seed
    publish(db,report_for(radar_seed),T0)
    report = latest(db)
    code = report['selected'][0]
    dates = [d.date().isoformat() for d in __import__('pandas').bdate_range('2026-08-13',periods=65)]
    add_future(db,code,dates,[100-i*.5 for i in range(len(dates))])
    add_future(db,BENCHMARK['code'],dates,[100+i*.1 for i in range(len(dates))])
    class Later(FixtureCalendar):
        def expected(self,*args):return dates[-1]
    results = outcomes(db,Later(),T0+timedelta(days=120))
    record = next(r for r in results if r['code']==code and r['horizon']==60)
    assert record['status']=='MATURED' and record['return']<0 and record['relative_return']<0
    assert record['entry_date']=='2026-08-13' and record['max_drawdown']<0
    assert record['report_id']==report['id'] and record['overlapping_samples']


def test_tracking_never_assumes_a_fill_before_the_actual_display_time(radar_seed):
    db,*_ = radar_seed
    # Thursday 10:00 China time: Thursday's open has already happened.
    instant = T0+timedelta(hours=11)
    publish(db,report_for(radar_seed),instant)
    results = outcomes(db,FixtureCalendar(),instant)
    assert results and all(r['status']=='WAITING' and r['entry_date'] is None for r in results)


def test_tracking_keeps_distinct_same_day_episodes(radar_seed):
    db,*_ = radar_seed
    publish(db,report_for(radar_seed),T0)
    record = json.loads(db.rows("SELECT value FROM settings WHERE key LIKE 'radar:discovery:%' LIMIT 1")[0]['value'])
    second = {**record,'created_at':(T0+timedelta(hours=1)).isoformat(),'report_id':'separate-episode'}
    write(db,'radar:discovery:second-episode',second)
    records = [r for r in outcomes(db,FixtureCalendar(),T0+timedelta(hours=2)) if r['code']==record['code'] and r['horizon']==20]
    assert len(records)==2 and len({r['created_at'] for r in records})==2


def test_missing_forward_prices_are_not_forward_filled(radar_seed):
    db,*_ = radar_seed
    publish(db,report_for(radar_seed),T0)
    code = latest(db)['selected'][0]
    dates = [d.date().isoformat() for d in __import__('pandas').bdate_range('2026-08-13',periods=20)]
    add_future(db,code,dates[1:],[100.]*19)
    add_future(db,BENCHMARK['code'],dates,[100.]*20)
    class Later(FixtureCalendar):
        def expected(self,*args):return dates[-1]
    record = next(r for r in outcomes(db,Later(),T0+timedelta(days=45)) if r['code']==code and r['horizon']==20)
    assert record['status']=='DATA_WAIT' and record['return'] is None
