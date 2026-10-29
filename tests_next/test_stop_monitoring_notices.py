"""Stopping monitoring must not resurrect the same opportunity via another feed."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from clawindex_next.radar import preferences,publish
from clawindex_next.radar_notifications import deliver,selection
from conftest import FixtureCalendar
from test_evolution import T0
from test_radar import radar_seed,report_for
from test_radar_notifications_tracking import notices
from test_webhooks import URL


def test_unwatch_cancels_own_queued_opportunity_and_preserves_other_accounts(radar_seed):
    db,a,b,*_=radar_seed
    a.radar_preferences(hide_watched=False)
    a.set_channel(URL,'own',mode='ALL',radar_mode='ALL')
    b.set_channel(URL,'other',mode='ALL',radar_mode='ALL')
    report=report_for(radar_seed)
    publish(db,report,T0)
    code=notices(db)[0]['code']
    aid=a.assign(code)
    a.unwatch(aid)
    assert code in preferences(db,a.user['id'])['ignored']
    assert not preferences(db,a.user['id'])['hide_watched']
    assert code not in selection(db,a.user['id'],report)
    assert code in selection(db,b.user['id'],report)
    assert all(n['status']=='CANCELLED' for n in notices(db) if n['user_id']==a.user['id'] and n['code']==code)
    assert all(n['status']=='PENDING' for n in notices(db) if n['user_id']==b.user['id'])
    name=next(r['name'] for r in report['rows'] if r['code']==code)
    calls=[]
    while deliver(db,lambda url,p:calls.append(p),FixtureCalendar(),T0):pass
    assert sum(p['radar_name']==name for p in calls)==1 # The other account may receive its own notice.
    a.radar_preferences(restore=code)
    assert code in selection(db,a.user['id'],report)


def test_stale_unwatch_does_not_hide_restored_opportunity_or_stop_new_assignment(radar_seed):
    db,a,b,*_=radar_seed
    code=report_for(radar_seed)['selected'][0]
    aid=a.assign(code)
    a.unwatch(aid)
    a.radar_preferences(restore=code)
    newer=a.assign(code)
    a.unwatch(aid)
    assert code not in preferences(db,a.user['id'])['ignored']
    assert next(w for w in a.watchlist() if w['code']==code)['id']==newer
    with pytest.raises(PermissionError):b.unwatch(newer)


def test_parallel_personal_preferences_do_not_overwrite_each_other(radar_seed):
    db,a,*_=radar_seed
    codes=[r['code'] for r in report_for(radar_seed)['rows']]
    with ThreadPoolExecutor(4) as pool:
        futures=[pool.submit(a.radar_preferences,ignore=code) for code in codes]
        futures.append(pool.submit(a.radar_preferences,hide_watched=False))
        for f in futures:f.result()
    prefs=preferences(db,a.user['id'])
    assert set(prefs['ignored'])==set(codes) and not prefs['hide_watched']
