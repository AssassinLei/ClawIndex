"""Regression scenarios found by the 2026-10-15 end-to-end review; never use live services."""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from clawindex_next.adapters import ProviderError
from clawindex_next.data_maintenance import latest_rows, put_rows
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical, digest, now
from clawindex_next.ui_evolution import today_items
from clawindex_next.worker import Worker
from conftest import FixtureCalendar, price_rows
from test_cloud_factors import Client, cn_item, provider, T0
from test_evolution import add_day, freeze
from test_radar import radar_seed, report_for
from test_radar_notifications_tracking import notices
from test_research_delivery import enable
from test_webhooks import URL


def revised_price(db):
    record = db.rows("SELECT * FROM observations WHERE code='HKTECH' ORDER BY trade_date DESC,id DESC LIMIT 1")[0]
    payload = {**json.loads(record['payload']),'pe':11.}
    with db.connect(True) as c:
        c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,'review',?,'FIXTURE',?)",('HKTECH',record['trade_date'],canonical(payload),now(),digest(payload)))


def buying(seeded):
    db,a,*_ = seeded
    aid = enable(db,a)
    a.set_channel(URL,'test',mode='BUY_SELL')
    ev = Evaluator(db,FixtureCalendar())
    return db,a,aid,ev,ev.evaluate(aid)[1]


def test_pending_buy_tracks_latest_recalculated_decision(seeded):
    db,a,aid,ev,old = buying(seeded)
    a.set_budget('HKTECH',dict(limit='1000',used='0',cash='500',currency='CNY'))
    current = ev.evaluate(aid)[1]
    assert current!=old
    assert db.rows('SELECT decision_id FROM outbox')==[{'decision_id':current}]
    sent=[]
    assert Worker(db,ev,sender=lambda *args:sent.append(args)).deliver()
    assert len(sent)==1


@pytest.mark.parametrize('feedback',['SUBMITTED','EXECUTED','DEFERRED'])
def test_processed_episode_stays_processed_after_revision_and_new_day(seeded,monkeypatch,feedback):
    db,a,aid,ev,old = buying(seeded)
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    a.confirm_execution(old,feedback,'等待确认')
    revised_price(db)
    current = ev.evaluate(aid)[1]
    assert current!=old
    assert today_items(a)[0]['handled'] and today_items(a)[0]['category']=='quiet'
    next_day=ev.evaluate(aid,datetime.now(timezone.utc)+timedelta(days=1))[1]
    assert next_day!=current
    a.feedback(next_day,'ACKNOWLEDGED') # Reading the explanation does not undo a submitted/processed instruction.
    assert today_items(a)[0]['handled']
    sent=[]
    worker=Worker(db,ev,sender=lambda *args:sent.append(args))
    while worker.deliver():pass
    assert not sent


def test_revised_evidence_invalidates_display_and_pending_notification(seeded,monkeypatch):
    db,a,aid,ev,_ = buying(seeded)
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    revised_price(db)
    assert today_items(a)[0]['category']=='waiting'
    assert Worker(db,ev,sender=lambda *_:pytest.fail('outdated evidence must not send')).deliver()
    assert db.rows('SELECT status FROM outbox')[0]['status']=='EXPIRED'


def test_final_trade_delivery_crash_retires_expired_claim(seeded):
    db,a,aid,ev,_ = buying(seeded)
    with db.connect(True) as c:
        c.execute("UPDATE outbox SET status='SENDING',attempts=3,claim_owner='dead',claim_until='2000-01-01T00:00:00+00:00'")
    Worker(db,ev,sender=lambda *_:pytest.fail('fourth send forbidden')).deliver()
    assert db.rows('SELECT status,claim_owner FROM outbox')==[{'status':'FAILED','claim_owner':None}]


def test_final_discovery_delivery_crash_does_not_send_fourth_attempt(radar_seed):
    from clawindex_next.radar import publish
    from clawindex_next.radar_notifications import deliver
    db,a,*_=radar_seed
    cid=a.set_channel(URL,'test',mode='ALL')
    a.set_radar_channel(cid,'ALL')
    publish(db,report_for(radar_seed),T0)
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value=json_set(value,'$.status','SENDING','$.attempts',3,'$.claim_until','2000-01-01T00:00:00+00:00') WHERE key LIKE 'radar:notice:%'")
    while deliver(db,lambda *_:pytest.fail('fourth send forbidden'),FixtureCalendar(),T0):pass
    assert all(r['status']=='FAILED' and r['attempts']==3 for r in notices(db))


def test_history_cooldown_remains_scheduled_until_missing_day_is_repaired(seeded):
    db,*_=seeded
    cn_item(db,base='20260101')
    client=Client()
    client.missing.add('2026-02-10')
    p=provider(client)
    p.sync_supplements=lambda *_:False # Isolate the price-repair scheduler from factor backfill.
    p.sync(db,'850531.SI',instant=T0)
    state=json.loads(db.rows("SELECT value FROM settings WHERE key='history_repair:850531.SI'")[0]['value'])
    assert state['missing_sessions']==1 and state['pending']
    assert state['next_due']>=(T0+timedelta(hours=6)).isoformat()
    client.missing.clear()
    p.sync(db,'850531.SI',instant=T0+timedelta(hours=7))
    assert '2026-02-10' in latest_rows(db,'850531.SI')


def test_temporary_factor_failure_keeps_automatic_retry_pending(seeded):
    db,*_=seeded
    cn_item(db,base='20260801')
    class Offline(Client):
        def idx_factor_pro(self,**kwargs):
            raise ProviderError('NETWORK_FAILED: fixture')
    provider(Offline()).sync(db,'850531.SI',instant=T0)
    state=json.loads(db.rows("SELECT value FROM settings WHERE key='history_repair:850531.SI'")[0]['value'])
    assert state['pending']
    assert state['next_due']>T0.isoformat()


def test_price_writer_preserves_factor_revision_committed_just_before_lock(seeded,monkeypatch):
    db,*_=seeded
    item=db.rows("SELECT * FROM instruments WHERE code='HKTECH'")[0]
    original=db.connect
    pending=[True]
    @contextmanager
    def connect(write=False):
        if write and pending and pending.pop():
            with original(True) as c:
                record=c.execute("SELECT * FROM observations WHERE code='HKTECH' ORDER BY trade_date DESC,id DESC LIMIT 1").fetchone()
                value={**json.loads(record['payload']),'factors':{'macd_dif_bfq':42.}}
                c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,'concurrent-factor',?,'FIXTURE',?)",('HKTECH',record['trade_date'],canonical(value),now(),digest(value)))
        with original(write) as c:yield c
    monkeypatch.setattr(db,'connect',connect)
    row=latest_rows(db,'HKTECH')['2026-08-12']
    row={**row,'pe':12.}
    adapter=type('Adapter',(),{'calendar':FixtureCalendar()})()
    put_rows(db,adapter,item,[row],T0)
    saved=latest_rows(db,'HKTECH')['2026-08-12']
    assert saved['pe']==12. and saved['factors']['macd_dif_bfq']==42.


def test_forward_simulation_never_fills_open_before_candidate_freeze(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,_=freeze(seeded,monkeypatch)
    payload=json.loads(db.rows('SELECT payload FROM evolution_candidates WHERE id=?',(rid,))[0]['payload'])
    payload['frozen_at']=(T0+timedelta(hours=11)).isoformat() # Next day 10:00 Hong Kong, after open.
    with db.connect(True) as c:
        c.execute('UPDATE evolution_candidates SET payload=? WHERE id=?',(canonical(payload),rid))
    monkeypatch.setattr(calendar,'opened_at',lambda name,day:datetime.fromisoformat(day+'T01:30:00+00:00'))
    add_day(db,calendar,'2026-08-13',300)
    ev.observe(rid,T0+timedelta(days=1))
    point=json.loads(db.rows('SELECT payload FROM evolution_observations WHERE candidate_id=?',(rid,))[0]['payload'])
    assert point['candidate_trades']==point['incumbent_trades']==0 and point['hold']==1.
    add_day(db,calendar,'2026-08-14',301)
    ev.observe(rid,T0+timedelta(days=2))
    state=json.loads(db.rows('SELECT state FROM evolution_candidates WHERE id=?',(rid,))[0]['state'])
    assert state['candidate']['trades']==1


def test_unverified_legacy_prices_do_not_generate_actionable_signal(seeded):
    db,a,b,aid,*_=seeded
    with db.connect(True) as c:
        c.execute("UPDATE observations SET payload=json_set(payload,'$.quality','LEGACY_PROVENANCE_UNKNOWN') WHERE code='HKTECH'")
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    decision=json.loads(a.results()[0]['decision'])
    assert decision['status']=='BLOCKED' and decision['action'] is None


def test_old_feature_algorithm_is_not_reused_as_today_complete(seeded,monkeypatch):
    db,a,aid,ev,_=buying(seeded)
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    with db.connect(True) as c:c.execute("UPDATE feature_snapshots SET algorithm='factor_features_v2'")
    assert a.request_inspection(aid,include_ai=False)['status']!='ALREADY_DONE'


def test_malformed_optional_factors_do_not_block_price_exit_analysis(seeded):
    import pandas as pd
    db,*_=seeded
    cn_item(db,base='20260801')
    class Malformed(Client):
        def idx_factor_pro(self,**kwargs):return pd.DataFrame([{'trade_date':'invalid','ts_code':'850531.SI'}])
    provider(Malformed()).sync(db,'850531.SI',instant=T0)
    assert '2026-08-12' in latest_rows(db,'850531.SI')
    state=json.loads(db.rows("SELECT value FROM settings WHERE key='factor_sync:850531.SI:idx_factor_pro'")[0]['value'])
    assert state['status']=='RETRY'


def test_validation_boundary_loss_is_included():
    from clawindex_next.validation import segment
    report={'curve':{'date':['a','b','c','d'],'strategy':[1.,1.,.5,.5],
                     'buy_hold':[1.,1.,1.,1.],'exposure':[0.,1.,1.,1.]},'trades':[]}
    strategy,_,_=segment(report,2,4)
    assert strategy['return']==-.5 and strategy['max_drawdown']==-.5


def test_invalid_catalog_reply_keeps_last_good_directory(seeded):
    import pandas as pd
    from clawindex_next.catalog import refresh, KEY
    db,*_=seeded
    class Good:
        def _call(self,*args,**kwargs):
            return pd.DataFrame([{'index_code':'801010.SI','industry_name':'行业目录'}])
    refresh(db,Good(),T0)
    class Malformed:
        def _call(self,*args,**kwargs):return pd.DataFrame([{'wrong_field':'invalid-reply'}])
    refresh(db,Malformed(),T0+timedelta(days=8))
    catalog=json.loads(db.rows('SELECT value FROM settings WHERE key=?',(KEY,))[0]['value'])
    assert not catalog['complete'] and catalog['items'][0]['name']=='行业目录'


def test_new_model_episode_is_not_hidden_by_old_processed_feedback(seeded,monkeypatch):
    db,a,aid,ev,did=buying(seeded)
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    a.confirm_execution(did,'DEFERRED','等待')
    _,rows=price_rows(prices=[100+i*.2 if i<135 else 95. if i<146 else 150.+i-146 for i in range(160)])
    with db.connect(True) as c:
        for row in rows[-25:]:
            c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,'new-episode',?,'FIXTURE',?)",('HKTECH',row['date'],canonical(row),now(),digest(row)))
    ev.evaluate(aid)
    assert today_items(a)[0]['category']=='action' and not today_items(a)[0]['handled']


def test_stale_feedback_can_be_recorded_but_cannot_overwrite_current_position(seeded):
    db,a,aid,ev,did=buying(seeded)
    count=len(db.rows('SELECT * FROM position_reports'))
    revised_price(db)
    with pytest.raises(ValueError,match='已更新'):
        a.confirm_execution(did,'EXECUTED',position='HELD')
    assert len(db.rows('SELECT * FROM position_reports'))==count
    assert a.confirm_execution(did,'EXECUTED')


def test_old_forward_evidence_without_actual_freeze_time_is_quarantined(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,_=freeze(seeded,monkeypatch)
    payload=json.loads(db.rows('SELECT payload FROM evolution_candidates WHERE id=?',(rid,))[0]['payload'])
    payload.pop('frozen_at')
    with db.connect(True) as c:c.execute('UPDATE evolution_candidates SET payload=? WHERE id=?',(canonical(payload),rid))
    add_day(db,calendar,'2026-08-13',300)
    ev.observe(rid,T0+timedelta(days=1))
    assert db.rows('SELECT status FROM evolution_candidates WHERE id=?',(rid,))[0]['status']=='QUARANTINED'
    assert not db.rows('SELECT * FROM strategy_promotions')


def test_daily_sync_receipt_does_not_hide_a_later_position_change(seeded):
    db,a,b,aid,_=seeded
    a.report_position('HKTECH','EMPTY')
    ev=Evaluator(db,FixtureCalendar())
    ev.evaluate(aid)
    a.report_position('HKTECH','HELD')
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value='true' WHERE key='daily_sync_enabled'")
        c.execute("INSERT INTO settings VALUES('auto_sync:HKTECH',?)",(canonical({'day':T0.date().isoformat(),'expected':'2026-08-12'}),))
    class NoNetwork:
        def sync(self,*args,**kwargs):pytest.fail('completed price sync must be reused')
    worker=Worker(db,ev,provider=NoNetwork())
    worker.schedule(T0)
    assert db.rows("SELECT id FROM jobs WHERE assignment_id=? AND status='PENDING'",(aid,))
    while worker.run_job():pass
    assert json.loads(a.results()[0]['decision'])['action']=='HOLD'
    count=len(db.rows('SELECT * FROM jobs'))
    worker.schedule(T0+timedelta(minutes=1))
    assert len(db.rows('SELECT * FROM jobs'))==count


def test_legacy_missing_receipt_resumes_even_if_recent_sync_was_marked_complete(seeded):
    db,*_=seeded
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value='true' WHERE key='daily_sync_enabled'")
        for key,value in [('auto_sync:HKTECH',{'day':T0.date().isoformat(),'expected':'2026-08-12'}),
                          ('market_sync:HKTECH',{'status':'OK','expected':'2026-08-12','checked_at':T0.isoformat()}),
                          ('history_repair:HKTECH',{'pending':False,'missing_sessions':1,'next_due':(T0-timedelta(minutes=1)).isoformat()})]:
            c.execute('INSERT INTO settings VALUES(?,?)',(key,canonical(value)))
    calls=[]
    class Provider:
        def sync(self,store,code,**kwargs):calls.append(code)
    Worker(db,Evaluator(db,FixtureCalendar()),provider=Provider()).schedule(T0)
    assert calls==['HKTECH']


def test_late_observation_cannot_execute_at_an_already_passed_next_open(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,_=freeze(seeded,monkeypatch)
    state=json.loads(db.rows('SELECT state FROM evolution_candidates WHERE id=?',(rid,))[0]['state'])
    for name in ('candidate','incumbent'):
        state[name].update(model='FLAT',pending=None)
    with db.connect(True) as c:c.execute('UPDATE evolution_candidates SET state=? WHERE id=?',(canonical(state),rid))
    monkeypatch.setattr(calendar,'opened_at',lambda name,day:datetime.fromisoformat(day+'T01:30:00+00:00'))
    add_day(db,calendar,'2026-08-13',300)
    ev.observe(rid,T0+timedelta(days=1,hours=11)) # Read Thursday's close on Friday after open.
    add_day(db,calendar,'2026-08-14',301)
    ev.observe(rid,T0+timedelta(days=2))
    saved=json.loads(db.rows('SELECT state FROM evolution_candidates WHERE id=?',(rid,))[0]['state'])
    assert saved['candidate']['trades']==0 and saved['incumbent']['trades']==0
