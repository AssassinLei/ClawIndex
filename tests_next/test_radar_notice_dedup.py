"""Only current opportunities may reach a channel, once per evidence day/phase."""
import copy
from datetime import timedelta

import pytest

from clawindex_next.radar import latest,publish,write
from clawindex_next.radar_notifications import deliver
from conftest import FixtureCalendar
from test_evolution import T0
from test_radar import radar_seed,report_for
from test_radar_notifications_tracking import notices
from test_webhooks import URL


def channel(seed):
    db,a,*_ = seed
    cid = a.set_channel(URL,'test',mode='ALL',radar_mode='ALL')
    report = report_for(seed)
    publish(db,report,T0)
    return db,a,cid,report


def drain(db,point=T0):
    sent = []
    while deliver(db,lambda url,p:sent.append(p),FixtureCalendar(),point):
        assert len(sent)<20
    return sent


def duplicate(db,item,suffix,**updates):
    row = {**item,'id':item['id']+suffix,**updates}
    write(db,'radar:notice:'+row['id'],row)
    return row


def test_rejection_is_recorded_in_system_but_never_enqueues_withdrawal(radar_seed):
    db,a,cid,report = channel(radar_seed)
    assert drain(db)
    code = notices(db)[0]['code']
    next(r for r in report['rows'] if r['code']==code)['state']='REJECTED'
    publish(db,report,T0+timedelta(minutes=1))
    assert any(e['kind']=='WITHDRAWN' and e['code']==code for e in latest(db)['events'])
    assert not [n for n in notices(db) if n['kind']=='WITHDRAWN']
    assert not drain(db,T0+timedelta(minutes=2))


def test_legacy_queued_withdrawals_are_cancelled_even_before_cron_window(radar_seed):
    db,a,cid,report = channel(radar_seed)
    drain(db)
    sent = notices(db)[0]
    code = sent['code']
    next(r for r in report['rows'] if r['code']==code)['state']='REJECTED'
    publish(db,report,T0+timedelta(minutes=1))
    a.configure_channel_schedule(cid,True,'30 23 * * mon-fri')
    for i in range(3):
        duplicate(db,sent,'-withdraw-'+str(i),kind='WITHDRAWN',status='PENDING',attempts=0,next_at=T0.isoformat())
    assert not drain(db,T0+timedelta(minutes=2))
    assert all(n['status']=='CANCELLED' for n in notices(db) if n['kind']=='WITHDRAWN')


def test_old_duplicate_opportunity_rows_are_sent_only_once(radar_seed):
    db,a,cid,report = channel(radar_seed)
    first = notices(db)[0]
    for i in range(3):
        duplicate(db,first,'-legacy-'+str(i))
    sent = drain(db)
    assert len(sent)==len({p['radar_name'] for p in sent})
    assert len(sent)==len(report['selected'])


def test_ranking_reentry_and_revisions_do_not_repeat_within_same_evidence_day(radar_seed):
    db,a,cid,report = channel(radar_seed)
    initial = drain(db)
    code = notices(db)[0]['code']
    original = copy.deepcopy(next(r for r in report['rows'] if r['code']==code))
    next(r for r in report['rows'] if r['code']==code)['state']='REJECTED'
    publish(db,report,T0+timedelta(minutes=1))
    report['rows']=[original if r['code']==code else r for r in report['rows']]
    report['sources'][code]['observation_max_id']+=1
    publish(db,report,T0+timedelta(minutes=2))
    assert not drain(db,T0+timedelta(minutes=3))
    assert len([n for n in notices(db) if n['code']==code and n['status']=='SENT'])==1


@pytest.mark.parametrize('hide_at',['before_queue','after_queue'])
def test_hidden_indexes_never_deliver_any_opportunity_notice(radar_seed,hide_at):
    db,a,cid,report = channel(radar_seed)
    code = notices(db)[0]['code']
    if hide_at=='after_queue':
        # Legacy duplicate still pending when the user hides the index.
        duplicate(db,notices(db)[0],'-legacy')
    a.radar_preferences(ignore=code)
    sent = drain(db)
    name = next(r['name'] for r in report['rows'] if r['code']==code)
    assert all(p['radar_name']!=name for p in sent)
    assert all(n['status'] in ('CANCELLED','EXPIRED') for n in notices(db) if n['code']==code)


def test_active_claim_blocks_only_same_index_and_evidence_day(radar_seed):
    db,a,cid,report = channel(radar_seed)
    first = notices(db)[0]
    duplicate(db,first,'-inflight',status='SENDING',attempts=1,claim_owner='other-worker',
              claim_until=(T0+timedelta(seconds=30)).isoformat())
    sent = drain(db)
    name = next(r['name'] for r in report['rows'] if r['code']==first['code'])
    assert all(p['radar_name']!=name for p in sent)
    assert sent # Other indexes are not blocked by this request.


def test_repeated_scans_before_cron_keep_one_current_candidate_row(radar_seed):
    db,a,cid,report = channel(radar_seed)
    a.configure_channel_schedule(cid,True,'10 23 * * mon-fri')
    first = notices(db)[0]
    code = first['code']
    original = copy.deepcopy(next(r for r in report['rows'] if r['code']==code))
    for i in range(3):
        next(r for r in report['rows'] if r['code']==code)['state']='REJECTED'
        publish(db,report,T0+timedelta(minutes=i*2+1))
        report['rows']=[copy.deepcopy(original) if r['code']==code else r for r in report['rows']]
        report['sources'][code]['observation_max_id']+=1
        publish(db,report,T0+timedelta(minutes=i*2+2))
    assert len([n for n in notices(db) if n['code']==code and n['status']=='PENDING'])==1
    assert not drain(db,T0+timedelta(minutes=7))
    sent = drain(db,T0+timedelta(minutes=10))
    assert sum(p['radar_name']==original['name'] for p in sent)==1
    assert all(p['radar_state'] in ('WATCH','READY') for p in sent)


def test_pending_watch_is_merged_into_ready_before_scheduled_delivery(radar_seed):
    db,a,*_ = radar_seed
    cid = a.set_channel(URL,'test',mode='ALL',radar_mode='ALL',schedule={'enabled':True,'cron':'10 23 * * mon-fri'})
    report = report_for(radar_seed)
    code = report['selected'][0]
    row = next(r for r in report['rows'] if r['code']==code)
    row['state']='WATCH'
    publish(db,report,T0)
    row['state']='READY'
    publish(db,report,T0+timedelta(minutes=1))
    target = [n for n in notices(db) if n['code']==code]
    assert len(target)==2 and sum(n['status']=='PENDING' for n in target)==1
    sent = drain(db,T0+timedelta(minutes=10))
    assert [p['radar_state'] for p in sent if p['radar_name']==row['name']]==['READY']


def test_delivered_watch_may_upgrade_once_but_never_repeat_ready(radar_seed):
    db,a,*_ = radar_seed
    a.set_channel(URL,'test',mode='ALL',radar_mode='ALL')
    report = report_for(radar_seed)
    code = report['selected'][0]
    row = next(r for r in report['rows'] if r['code']==code)
    row['state']='WATCH'
    publish(db,report,T0)
    assert [p['radar_state'] for p in drain(db) if p['radar_name']==row['name']]==['WATCH']
    row['state']='READY'
    publish(db,report,T0+timedelta(minutes=1))
    assert [p['radar_state'] for p in drain(db,T0+timedelta(minutes=1)) if p['radar_name']==row['name']]==['READY']
    for i in range(2):
        row['state']='WATCH'
        report['sources'][code]['observation_max_id']+=1
        publish(db,report,T0+timedelta(minutes=i*2+2))
        row['state']='READY'
        publish(db,report,T0+timedelta(minutes=i*2+3))
    assert not drain(db,T0+timedelta(minutes=6))


def test_actual_success_receipt_prevents_resend_after_disable_during_request(radar_seed):
    db,a,cid,report = channel(radar_seed)
    first = notices(db)[0]
    # An old WATCH message is sent with the current READY content.
    write(db,'radar:notice:'+first['id'],{**first,'event_state':'WATCH'})
    calls = []
    def sender(url,payload):
        calls.append(payload)
        a.set_radar_channel(cid,'OFF')
    assert deliver(db,sender,FixtureCalendar(),T0)
    assert len(calls)==1 and calls[0]['radar_state']=='READY'
    a.set_radar_channel(cid,'ALL')
    duplicate(db,first,'-after-actual-success')
    assert not drain(db)


def test_new_evidence_day_can_notify_a_genuinely_reappearing_opportunity(radar_seed):
    db,a,cid,report = channel(radar_seed)
    drain(db)
    code = notices(db)[0]['code']
    row = next(r for r in report['rows'] if r['code']==code)
    original_state = row['state']
    row['state']='REJECTED'
    publish(db,report,T0+timedelta(minutes=1))
    row['state']=original_state
    report['as_of']='2026-08-13'
    publish(db,report,T0+timedelta(days=1))
    class NextDay(FixtureCalendar):
        def expected(self,*args,**kwargs):return '2026-08-13'
    sent = []
    while deliver(db,lambda url,p:sent.append(p),NextDay(),T0+timedelta(days=1)):pass
    assert sum(p['radar_name']==row['name'] for p in sent)==1
