"""Stable unattended operation: wake research only for actionable new evidence."""
import json
from datetime import timedelta

import pytest

from clawindex_next.radar import BENCHMARK,Radar,latest,publish,read,request,write
from clawindex_next.storage import canonical,digest,now
from conftest import FixtureCalendar,price_rows
from test_evolution import T0
from test_radar import radar_seed,report_for
from test_radar_notifications_tracking import add_future


def revise(db,code,day='2026-08-12'):
    rows = db.rows('SELECT payload FROM observations WHERE code=? AND trade_date=? ORDER BY id DESC LIMIT 1',(code,day))
    row = json.loads(rows[0]['payload']) if rows else price_rows(1,end=day)[1][0]
    row = {**row,'pe':row.get('pe',0)+1}
    with db.connect(True) as c:
        c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,'audit',?,'FIXTURE',?)",(code,day,canonical(row),now(),digest(row)))


def track_scans(monkeypatch):
    import clawindex_next.radar as module
    original = module.snapshot
    calls = []
    def scan(*args,**kwargs):
        calls.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'snapshot',scan)
    return calls


@pytest.mark.parametrize('code,day',[('HKTECH','2026-08-12'),('801099.SI','2026-08-12'),('801010.SI','2020-01-02')])
def test_unrelated_or_out_of_window_data_never_restarts_radar(radar_seed,monkeypatch,code,day):
    db,*_ = radar_seed
    write(db,'radar:enabled',True)
    radar = Radar(db,FixtureCalendar())
    monkeypatch.setattr(radar,'maintain',lambda *args:None)
    scans = track_scans(monkeypatch)
    assert radar.tick(T0)
    revise(db,code,day)
    assert not radar.tick(T0+timedelta(minutes=1))
    assert len(scans)==1


def test_related_evidence_and_rule_change_bypass_cached_scan(radar_seed,monkeypatch):
    db,*_ = radar_seed
    write(db,'radar:enabled',True)
    radar = Radar(db,FixtureCalendar())
    monkeypatch.setattr(radar,'maintain',lambda *args:None)
    scans = track_scans(monkeypatch)
    assert radar.tick(T0)
    revise(db,'801010.SI')
    assert radar.tick(T0+timedelta(minutes=1))
    monkeypatch.setattr('clawindex_next.radar.RULE_ID','audit-rule-new')
    # Rule ID in the signature changes even if the analyzer's fixture report is identical.
    radar.tick(T0+timedelta(minutes=2))
    assert len(scans)==3


def test_pending_manual_request_does_not_repeat_scans_during_provider_cooldown(radar_seed,monkeypatch):
    from clawindex_next.adapters import ProviderError
    db,a,*_ = radar_seed
    with db.connect(True) as c:
        c.execute("DELETE FROM observations WHERE code='801010.SI' AND trade_date='2026-08-11'")
    class Offline:
        calls = 0
        def fetch_sw_day(self,day):
            self.calls+=1
            raise ProviderError('NO_PERMISSION: fixture')
    provider = Offline()
    radar = Radar(db,FixtureCalendar(),provider)
    scans = track_scans(monkeypatch)
    assert request(db,a.user['id'],FixtureCalendar(),T0)=='QUEUED'
    assert radar.tick(T0)
    assert latest(db)['status']=='PREPARING'
    for minute in (1,2,30,60):
        assert not radar.tick(T0+timedelta(minutes=minute))
    assert provider.calls==1 and len(scans)==1
    assert read(db,'radar:requested') # The request remains durable while data is unavailable.
    # Another shared source repairs the relevant gap. Recompute immediately,
    # even though this provider's own permission cooldown still applies.
    revise(db,'801010.SI','2026-08-11')
    assert radar.tick(T0+timedelta(minutes=61))
    assert latest(db)['status']=='COMPLETE' and read(db,'radar:requested') is None
    assert provider.calls==1 and len(scans)==2


@pytest.mark.parametrize('source',['candidate','benchmark'])
def test_forward_tracking_waits_for_verified_prices_in_both_sources(radar_seed,source):
    from clawindex_next.radar_tracking import outcomes
    db,*_ = radar_seed
    publish(db,report_for(radar_seed),T0)
    code = latest(db)['selected'][0]
    dates = FixtureCalendar().sessions('XSHG','2026-08-13','2026-09-09')
    assert len(dates)==20
    for target in (code,BENCHMARK['code']):
        add_future(db,target,dates,[100.+i for i in range(20)])
    class Later(FixtureCalendar):
        def expected(self,*args):return dates[-1]
    record = lambda: next(r for r in outcomes(db,Later(),T0+timedelta(days=45)) if r['code']==code and r['horizon']==20)
    assert record()['status']=='MATURED'
    target = code if source=='candidate' else BENCHMARK['code']
    with db.connect(True) as c:
        c.execute("UPDATE observations SET payload=json_set(payload,'$.quality','LEGACY_PROVENANCE_UNKNOWN') WHERE code=? AND trade_date=?",(target,dates[3]))
    invalid = record()
    assert invalid['status']=='DATA_WAIT' and invalid['return'] is None and invalid['relative_return'] is None
    row = db.rows('SELECT payload FROM observations WHERE code=? AND trade_date=? ORDER BY id DESC LIMIT 1',(target,dates[3]))[0]
    verified = {**json.loads(row['payload']),'quality':'PROVIDER_PRICE'}
    with db.connect(True) as c:
        c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,'verified-refresh',?,'FIXTURE',?)",(target,dates[3],canonical(verified),now(),digest(verified)))
    assert record()['status']=='MATURED'
