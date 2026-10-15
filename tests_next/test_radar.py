import copy
import json
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from clawindex_next.calendar import MarketCalendar
from clawindex_next.catalog import KEY
from clawindex_next.migration import instrument, insert_instrument
from clawindex_next.radar import BENCHMARK, Radar, latest, publish, read, request, snapshot, universe, view, write
from clawindex_next.radar_rules import RULE, analyze, classify, frame_for, market_state, pair_key, select
from clawindex_next.storage import canonical, digest, now
from conftest import FixtureCalendar, price_rows
from test_evolution import T0
from test_ui_redesign import login, button


def bars(drift=.001, seed=1, n=330, falling=False):
    rng = np.random.default_rng(seed)
    prices = 100*np.exp(np.cumsum(drift+rng.normal(0,.002,n)))
    if falling:
        prices = prices[::-1]
    dates,rows = price_rows(n,prices=list(prices))
    for i,row in enumerate(rows):
        row.update(pe=30-i*.02,pb=3-i*.002,quality='PROVIDER_PRICE')
    return dates,rows


def public_item(code,name,level,parent=None,industry=None):
    return instrument(code,name,{'industry_name':name,'level':level,'src':'SW2021','parent_code':parent,'industry_code':industry})


@pytest.fixture
def radar_seed(seeded):
    db,a,b,*_ = seeded
    items = [public_item('801010.SI','测试行业甲','L1',industry='110000'),
             public_item('801020.SI','测试行业乙','L1',industry='120000'),
             public_item('801030.SI','测试行业丙','L1',industry='130000'),
             public_item('801011.SI','测试甲子行业','L2',parent='110000',industry='110100'),
             public_item('801099.SI','测试三级排除','L3',parent='110100')]
    with db.connect(True) as c:
        for i,item in enumerate([BENCHMARK,*items]):
            insert_instrument(c,item)
            dates,rows = bars(.0006 if item['code']==BENCHMARK['code'] else .0009+i*.00015,seed=20+i)
            for row in rows:
                c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,'radar-fixture',?,'FIXTURE',?)",(item['code'],row['date'],canonical(row),now(),digest(row)))
    return db,a,b,items


def report_for(seed,instant=T0):
    db,a,b,items = seed
    return snapshot(db,universe(db),FixtureCalendar(),instant)


def test_scope_is_all_verified_l1_l2_and_no_l3(radar_seed):
    db,*_ = radar_seed
    rows = universe(db)
    assert {r['metadata']['level'] for r in rows}=={'L1','L2'}
    assert len(rows)==4 and '801099.SI' not in {r['code'] for r in rows}


def test_classification_metadata_does_not_verify_unknown_codes_or_keep_nan():
    unknown = instrument('801999.SI','未核实指数')
    assert not unknown['metadata']['identity_verified'] and unknown['asset_kind']=='UNKNOWN'
    known = instrument('801010.SI','测试',{'industry_name':'测试','level':'L1','src':'SW2021',
                                           'parent_code':float('nan'),'industry_code':'110000'})
    assert known['metadata']['parent_code'] is None
    assert json.loads(canonical(known))['metadata']['industry_code']=='110000'


def test_current_complete_catalog_is_authoritative_and_keeps_parent_links(radar_seed):
    from clawindex_next.catalog import refresh
    db,*_ = radar_seed
    class Provider:
        def _call(self,endpoint,**kwargs):
            level = kwargs['level']
            return pd.DataFrame([{'index_code':{'L1':'801010.SI','L2':'801011.SI','L3':'801099.SI'}[level],
                                 'industry_name':level,'industry_code':'110100' if level=='L2' else '110000',
                                 'parent_code':'110000' if level=='L2' else float('nan')}])
    refresh(db,Provider(),T0)
    rows = universe(db)
    assert {r['code'] for r in rows}=={'801010.SI','801011.SI'}
    assert next(r for r in rows if r['metadata']['level']=='L2')['metadata']['parent_code']=='110000'


def test_daily_adapter_checks_cross_section_and_normalizes_amount():
    from clawindex_next.adapters import TushareAdapter
    calls = []
    class Client:
        def sw_daily(self,**kwargs):
            calls.append(kwargs)
            base = {'ts_code':'801010.SI','trade_date':'20261015','open':100.,'close':101.,'high':102.,'low':99.,'amount':12.5,'pe':20.,'pb':2.}
            return pd.DataFrame([base,{**base,'ts_code':'000300.SH'},{**base,'ts_code':'801020.SI','trade_date':'20261014'}])
    rows = TushareAdapter(Client(),pause=lambda _:None).fetch_sw_day('2026-10-15')
    assert list(rows)==['801010.SI'] and rows['801010.SI']['amount']==125000.
    assert calls[0]['trade_date']=='20261015' and 'ts_code' not in calls[0]


def test_fixed_rules_find_absolute_and_relative_trend_without_probability(radar_seed):
    report = report_for(radar_seed)
    assert report['status']=='COMPLETE' and report['coverage']==1
    assert any(r['state']=='READY' for r in report['rows'])
    assert 0<len(report['selected'])<=3
    assert not {'801010.SI','801011.SI'} <= set(report['selected'])
    assert report['rule']['version']=='opportunity_radar_v1'
    assert 'probability' not in canonical(report)


def test_all_falling_does_not_force_a_winner():
    dates,data = bars(falling=True)
    bench = frame_for(data,dates)
    item = public_item('801010.SI','测试','L1')
    report = analyze([item],{item['code']:bench},bench)
    assert report['status']=='COMPLETE' and report['selected']==[]
    assert report['rows'][0]['state']=='REJECTED'


def test_weak_market_never_upgrades_candidate_to_ready(radar_seed):
    db,a,b,items = radar_seed
    dates,data = bars(falling=True)
    bench = frame_for(data,dates)
    from clawindex_next.radar import load
    frames = {r['code']:load(db,r,dates[-1],T0,FixtureCalendar())[0] for r in universe(db)}
    report = analyze(universe(db),frames,bench)
    assert not report['market']['entry_allowed']
    assert all(r['state']!='READY' for r in report['rows'])


def test_missing_or_future_data_is_not_filled_or_presented_as_an_opportunity(radar_seed):
    db,a,b,items = radar_seed
    with db.connect(True) as c:
        c.execute('UPDATE observations SET available_at=? WHERE code=? AND trade_date=?',((T0+timedelta(days=2)).isoformat(),'801020.SI','2026-08-12'))
        c.execute("DELETE FROM observations WHERE code='801030.SI' AND trade_date='2026-08-10'")
    report = report_for(radar_seed)
    assert report['status']=='PREPARING' and report['selected']==[]
    assert next(r for r in report['rows'] if r['code']=='801020.SI')['state']=='DATA_WAIT'
    assert next(r for r in report['rows'] if r['code']=='801030.SI')['state']=='DATA_WAIT'


def test_risk_gate_beats_relative_ranking():
    dates,data = bars()
    bench = frame_for(bars(.0001,seed=30)[1],dates)
    data[-1].update(open=data[-1]['open']*2,high=data[-1]['high']*2,low=data[-1]['low']*2,close=data[-1]['close']*2)
    row = classify(public_item('801010.SI','测试','L1'),frame_for(data,dates),bench,market_state(bench))
    assert row['state']=='REJECTED' and row['reasons']


def test_same_family_and_correlated_candidates_are_not_counted_as_diversification(radar_seed):
    report = report_for(radar_seed)
    rows = [copy.deepcopy(r) for r in report['rows'] if r['state'] in ('WATCH','READY')]
    assert len(rows)>=2
    correlations = {pair_key(a['code'],b['code']):.99 for a in rows for b in rows if a!=b}
    chosen,suppressed = select(rows,correlations)
    assert len(chosen)==1 and len(suppressed)==len(rows)-1


def test_snapshots_are_immutable_deduplicated_and_show_withdrawals(radar_seed):
    db,*_ = radar_seed
    report = report_for(radar_seed)
    assert publish(db,report,T0)
    first = latest(db)
    assert not publish(db,report,T0+timedelta(minutes=5))
    changed = copy.deepcopy(report)
    code = first['selected'][0]
    row = next(r for r in changed['rows'] if r['code']==code)
    row.update(state='REJECTED',reasons=['趋势条件失效'])
    changed['selected'] = [r for r in changed['selected'] if r!=code]
    assert publish(db,changed,T0+timedelta(minutes=10))
    assert any(e['kind']=='WITHDRAWN' and e['code']==code for e in latest(db)['events'])
    assert read(db,'radar:snapshot:'+first['id'])==first


def test_preferences_watch_and_history_are_user_scoped(radar_seed,monkeypatch):
    db,a,b,*_ = radar_seed
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    publish(db,report_for(radar_seed),T0)
    code = a.radar()['candidates'][0]['code']
    aid = a.add_radar_watch(code)
    assert next(w for w in a.watchlist() if w['code']==code)['catchup']==1
    assert next(w for w in a.watchlist() if w['code']==code)['stage']=='RESEARCH'
    assert not any(w['code']==code for w in b.watchlist())
    assert code not in {r['code'] for r in a.radar()['candidates']}
    assert code in {r['code'] for r in b.radar()['candidates']}
    a.radar_preferences(hide_watched=False,ignore=code)
    assert code not in {r['code'] for r in a.radar()['candidates']}
    assert code in {r['code'] for r in b.radar()['candidates']}
    a.radar_preferences(restore=code)
    assert code in {r['code'] for r in a.radar()['candidates']}
    assert a.add_radar_watch(code)==aid
    assert a.radar_history() and b.radar_history()


def test_stale_snapshot_has_no_current_candidates(radar_seed):
    db,a,b,*_ = radar_seed
    publish(db,report_for(radar_seed),T0)
    class Later(FixtureCalendar):
        def expected(self,*args):return '2026-08-13'
    state = view(db,a.user['id'],a.watchlist(),Later(),T0+timedelta(days=1))
    assert state['stale'] and not state['candidates']
    assert request(db,a.user['id'],Later(),T0+timedelta(days=1))=='QUEUED'


def test_batch_maintenance_is_bounded_shared_and_does_not_store_l3(radar_seed):
    db,a,b,items = radar_seed
    calls = []
    class Provider:
        def fetch_sw_day(self,day):
            calls.append(day)
            return {r['code']:{'date':day,'open':100.,'high':102.,'low':99.,'close':101.,'pe':20.,'pb':2.,'quality':'PROVIDER_PRICE'} for r in items}
    radar = Radar(db,FixtureCalendar(),Provider())
    before = db.rows("SELECT COUNT(*) n FROM observations WHERE code='801099.SI'")[0]['n']
    radar.maintain(universe(db),'2026-08-12',T0,maximum=2,budget_seconds=60)
    assert len(calls)==2
    assert db.rows("SELECT COUNT(*) n FROM observations WHERE code='801099.SI'")[0]['n']==before
    assert read(db,'radar:batch:'+calls[0])


def test_permission_failures_are_safe_and_have_a_shared_cooldown(radar_seed):
    from clawindex_next.adapters import ProviderError
    db,a,b,*_ = radar_seed
    class Provider:
        calls = 0
        def fetch_sw_day(self,day):
            self.calls += 1
            raise ProviderError('NO_PERMISSION: do-not-log-private-token')
    provider = Provider()
    radar = Radar(db,FixtureCalendar(),provider)
    radar.maintain(universe(db),'2026-08-12',T0)
    radar.maintain(universe(db),'2026-08-12',T0+timedelta(minutes=1))
    assert provider.calls==1
    assert 'private-token' not in canonical(a.health())
    assert read(db,'radar:provider_cooldown')['next_at']


def test_radar_tick_recovers_and_adds_no_user_assignments(radar_seed):
    db,a,b,*_ = radar_seed
    write(db,'radar:enabled',True)
    # All local evidence is ready; prevent the optional deeper-history repair.
    class Provider:
        def fetch_sw_day(self,day):return {}
    before = db.rows('SELECT * FROM assignments')
    assert Radar(db,FixtureCalendar(),Provider()).tick(T0)
    assert latest(db)['status']=='COMPLETE'
    assert db.rows("SELECT status FROM health WHERE component='radar' ORDER BY id DESC LIMIT 1")[0]['status']=='OK'
    assert db.rows('SELECT * FROM assignments')==before
    assert not db.rows('SELECT * FROM outbox')
    assert request(db,a.user['id'],FixtureCalendar(),T0)=='ALREADY_DONE'


def test_discovery_page_works_without_watches_and_exposes_candidates(radar_seed,monkeypatch):
    db,a,b,*_ = radar_seed
    publish(db,report_for(radar_seed),T0)
    app = login(db,monkeypatch,'雷达新用户')
    app.sidebar.radio[0].set_value('发现机会').run()
    assert not app.exception
    assert app.title[0].value=='发现机会'
    assert any(x.label=='加入我的指数' for x in app.button)
    button(app,'刷新机会').click().run()
    assert not app.exception and any('已复用' in x.value for x in app.info)
    button(app,'加入我的指数').click().run()
    assert not app.exception and any('已加入我的指数' in x.value for x in app.success)
