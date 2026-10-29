"""Long-lived workers must retain a useful notice until its allowed send window."""
import copy
from datetime import timedelta

import pytest

from clawindex_next.radar import publish
from clawindex_next.radar_notifications import deliver
from clawindex_next.service import Evaluator
from clawindex_next.worker import Worker
from conftest import FixtureCalendar
from test_evolution import T0
from test_radar import radar_seed,report_for
from test_radar_notifications_tracking import notices
from test_webhooks import URL
from test_radar_notice_dedup import duplicate


class DatedCalendar(FixtureCalendar):
    def expected(self,name,instant=None,grace_minutes=150):
        return instant.date().isoformat()


def next_report(db,report,point):
    changed = copy.deepcopy(report)
    changed['as_of']=point.date().isoformat()
    publish(db,changed,point)
    return changed


def test_unsent_persistent_opportunity_moves_to_current_evidence_at_next_cron(radar_seed):
    db,a,*_ = radar_seed
    a.set_channel(URL,'scheduled',mode='ALL',radar_mode='ALL',schedule={'enabled':True,'cron':'0 23 * * thu'})
    report = report_for(radar_seed)
    publish(db,report,T0) # Wednesday: the opportunity is still waiting for Thursday.
    calls=[]
    assert not deliver(db,lambda url,p:calls.append(p),DatedCalendar(),T0)
    later = T0+timedelta(days=1)
    next_report(db,report,later)
    while deliver(db,lambda url,p:calls.append(p),DatedCalendar(),later):pass
    assert len(calls)==len(report['selected'])
    assert all(p['radar_state'] in ('WATCH','READY') for p in calls)
    assert all(n['as_of']=='2026-08-13' for n in notices(db) if n['status']=='SENT')


def test_expiring_old_unsent_notice_does_not_lose_later_current_opportunity(radar_seed):
    db,a,*_ = radar_seed
    a.set_channel(URL,'scheduled',mode='ALL',radar_mode='ALL',schedule={'enabled':True,'cron':'0 23 * * thu'})
    report = report_for(radar_seed)
    publish(db,report,T0)
    later = T0+timedelta(days=1)
    calls=[]
    # The worker checks the queue before the fresh Thursday scan has completed.
    while deliver(db,lambda *_:None,DatedCalendar(),later):pass
    assert all(n['status']=='EXPIRED' for n in notices(db))
    next_report(db,report,later)
    while deliver(db,lambda url,p:calls.append(p),DatedCalendar(),later):pass
    assert len(calls)==len(report['selected'])


def test_delivered_persistent_opportunity_is_not_resent_on_later_days(radar_seed):
    db,a,*_ = radar_seed
    a.set_channel(URL,'immediate',mode='ALL',radar_mode='ALL')
    report = report_for(radar_seed)
    publish(db,report,T0)
    calls=[]
    while deliver(db,lambda url,p:calls.append(p),DatedCalendar(),T0):pass
    count=len(calls)
    for i in range(1,4):
        later=T0+timedelta(days=i)
        report=next_report(db,report,later)
        while deliver(db,lambda url,p:calls.append(p),DatedCalendar(),later):pass
    assert len(calls)==count


def test_worker_checks_opportunities_before_running_user_jobs(seeded,monkeypatch):
    db,*_=seeded
    worker=Worker(db,Evaluator(db,FixtureCalendar()),sender=lambda *_:None)
    order=[]
    monkeypatch.setattr('clawindex_next.radar_notifications.deliver',lambda *args,**kwargs:order.append('opportunity') or False)
    monkeypatch.setattr(worker,'run_job',lambda *args,**kwargs:order.append('job') or False)
    monkeypatch.setattr(worker,'schedule',lambda *args,**kwargs:None)
    monkeypatch.setattr('clawindex_next.evolution.Evolution.tick',lambda *args,**kwargs:None)
    monkeypatch.setattr('clawindex_next.radar.Radar.tick',lambda *args,**kwargs:None)
    worker.once()
    assert order.index('opportunity')<order.index('job')


def test_worker_rechecks_time_sensitive_notifications_between_jobs(seeded,monkeypatch):
    db,*_=seeded
    worker=Worker(db,Evaluator(db,FixtureCalendar()),sender=lambda *_:None)
    order=[]
    monkeypatch.setattr('clawindex_next.radar_notifications.deliver',lambda *args,**kwargs:order.append('opportunity') or False)
    count=0
    def job():
        nonlocal count
        if count>=2:return False
        count+=1
        order.append('job'+str(count))
        worker.last_notice_check-=11 # A bounded job has taken time in this fixture.
        return True
    monkeypatch.setattr(worker,'run_job',job)
    monkeypatch.setattr(worker,'schedule',lambda *args,**kwargs:None)
    monkeypatch.setattr('clawindex_next.evolution.Evolution.tick',lambda *args,**kwargs:None)
    monkeypatch.setattr('clawindex_next.radar.Radar.tick',lambda *args,**kwargs:None)
    worker.once()
    assert 'opportunity' in order[order.index('job1')+1:order.index('job2')]


def test_idle_notification_checkpoints_are_throttled(seeded,monkeypatch):
    db,*_=seeded
    worker=Worker(db,Evaluator(db,FixtureCalendar()),sender=lambda *_:None)
    calls=[]
    monkeypatch.setattr('clawindex_next.radar_notifications.deliver',lambda *args,**kwargs:calls.append(1) or False)
    worker.notification_checkpoint(force=True)
    worker.notification_checkpoint()
    assert len(calls)==1
    worker.last_notice_check-=11
    worker.notification_checkpoint()
    assert len(calls)==2


def test_old_duplicate_rows_cannot_bypass_retry_backoff(radar_seed):
    db,a,*_ = radar_seed
    a.set_channel(URL,'immediate',mode='ALL',radar_mode='ALL')
    report=report_for(radar_seed)
    publish(db,report,T0)
    target=notices(db)[0]
    retry=duplicate(db,target,'-retry',status='RETRY',attempts=1,next_at=(T0+timedelta(seconds=30)).isoformat())
    name=next(r['name'] for r in report['rows'] if r['code']==target['code'])
    calls=[]
    while deliver(db,lambda url,p:calls.append(p),DatedCalendar(),T0):pass
    assert all(p['radar_name']!=name for p in calls)
    while deliver(db,lambda url,p:calls.append(p),DatedCalendar(),T0+timedelta(seconds=31)):pass
    assert sum(p['radar_name']==name for p in calls)==1
    sent=next(n for n in notices(db) if n['id']==retry['id'])
    assert sent['status']=='SENT' and sent['attempts']==2


def test_old_duplicate_rows_cannot_restart_an_exhausted_event(radar_seed):
    db,a,*_ = radar_seed
    a.set_channel(URL,'immediate',mode='ALL',radar_mode='ALL')
    report=report_for(radar_seed)
    publish(db,report,T0)
    target=notices(db)[0]
    duplicate(db,target,'-exhausted',status='FAILED',attempts=3)
    name=next(r['name'] for r in report['rows'] if r['code']==target['code'])
    calls=[]
    while deliver(db,lambda url,p:calls.append(p),DatedCalendar(),T0):pass
    assert all(p['radar_name']!=name for p in calls)
    assert all(n['status'] in ('FAILED','CANCELLED') for n in notices(db) if n['code']==target['code'])


def test_retry_count_and_backoff_survive_daily_evidence_refresh(radar_seed):
    db,a,*_ = radar_seed
    a.set_channel(URL,'immediate',mode='ALL',radar_mode='ALL')
    report=report_for(radar_seed)
    publish(db,report,T0)
    def failed(*args):raise ConnectionError('offline-fixture')
    # Each initial opportunity has made one real network attempt.
    while deliver(db,failed,DatedCalendar(),T0):pass
    later=T0+timedelta(days=1)
    next_report(db,report,later)
    current=[n for n in notices(db) if n['as_of']=='2026-08-13']
    assert current and all(n['attempts']==1 and n['status']=='RETRY' for n in current)
    calls=[]
    while deliver(db,lambda url,p:calls.append(p),DatedCalendar(),later):pass
    assert len(calls)==len(report['selected'])
    assert all(n['attempts']==2 for n in notices(db) if n['status']=='SENT')


@pytest.mark.parametrize('status',['CANCELLED','EXPIRED'])
def test_reactivated_current_day_notice_preserves_carried_network_backoff(radar_seed,status):
    from clawindex_next.radar import write
    from clawindex_next.storage import digest
    db,a,*_ = radar_seed
    a.set_channel(URL,'immediate',mode='ALL',radar_mode='ALL')
    report=report_for(radar_seed)
    publish(db,report,T0)
    old=notices(db)[0]
    later=T0+timedelta(days=1)
    due=later+timedelta(seconds=45)
    write(db,'radar:notice:'+old['id'],{**old,'status':'RETRY','attempts':1,'next_at':due.isoformat()})
    identity=digest({'user':old['user_id'],'channel':old['channel_id'],'as_of':later.date().isoformat(),'code':old['code'],'state':old['event_state']})
    write(db,'radar:notice:'+identity,{**old,'id':identity,'as_of':later.date().isoformat(),'status':status})
    next_report(db,report,later)
    current=next(n for n in notices(db) if n['id']==identity)
    assert current['attempts']==1 and current['next_at']==due.isoformat() and current['status']=='RETRY'
    calls=[]
    while deliver(db,lambda url,p:calls.append(p),DatedCalendar(),later):pass
    name=next(r['name'] for r in report['rows'] if r['code']==old['code'])
    assert all(p['radar_name']!=name for p in calls)
    while deliver(db,lambda url,p:calls.append(p),DatedCalendar(),due):pass
    assert sum(p['radar_name']==name for p in calls)==1
