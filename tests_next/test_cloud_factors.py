import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from clawindex_next.adapters import TushareAdapter
from clawindex_next.cloud import backup, healthy, sqlite_safe
from clawindex_next.data_maintenance import latest_rows, history_start, put_rows
from clawindex_next.engine import DEFAULT_SPEC, feature_frame, features_at, personal_decision, replay, step, validate_spec
from clawindex_next.evolution import Evolution
from clawindex_next.factors import PROFILES, TECH_FIELDS, authorized, candidates, coverage, diagnostics
from clawindex_next.migration import insert_instrument, instrument
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical, digest, now
from clawindex_next.syncing import MarketSync
from clawindex_next.validation import policy_config
from clawindex_next.worker import Worker
from conftest import FixtureCalendar, price_rows


T0 = datetime(2026,8,12,15,tzinfo=timezone.utc)


def factor_spec(key='technical_v1',ma=120):
    return {**DEFAULT_SPEC,'name':'trend_factor_v2','factor_profile':key,'ma':ma}


def professional_values():
    return {**{k:1. for k in TECH_FIELDS},'macd_dif_bfq':2.,'macd_dea_bfq':1.,
            'dmi_pdi_bfq':25.,'dmi_mdi_bfq':10.,'dmi_adx_bfq':25.,'rsi_bfq_12':60.,
            'rsi_bfq_24':60.,'atr_bfq':2.,'boll_mid_bfq':50.,'boll_upper_bfq':500.}


def cn_item(db,code='850531.SI',base='20250801'):
    item = {'code':code,'name':'测试行业','market':'CN','calendar':'XSHG','timezone':'Asia/Shanghai',
            'currency':'CNY','asset_kind':'EQUITY_SECTOR_INDEX','publisher':'fixture',
            'metadata':{'identity_verified':True,'base_date':base}}
    with db.connect(True) as c:
        insert_instrument(c,item)
    return item


class Client:
    def __init__(self):
        self.calls = []
        self.missing = set()
        self.forbidden = False
        self.factor_missing = set()

    def rows(self,kwargs,factor=False):
        dates = pd.bdate_range(kwargs['start_date'],kwargs['end_date'])
        return pd.DataFrame([{'ts_code':kwargs['ts_code'],'trade_date':d.strftime('%Y%m%d'),
                             'open':100.,'high':101.,'low':99.,'close':100.,'amount':10.,'pe':20.,'pb':2.,
                             **(professional_values() if factor else {})} for d in dates
                             if d.date().isoformat() not in (self.factor_missing if factor else self.missing)])

    def sw_daily(self,**kwargs):
        self.calls.append(('price',kwargs))
        return self.rows(kwargs)

    def idx_factor_pro(self,**kwargs):
        self.calls.append(('factor',kwargs))
        if self.forbidden:
            raise RuntimeError('权限不足 token=SECRET')
        return self.rows(kwargs,True)


def provider(client):
    return TushareAdapter(client,FixtureCalendar(),pause=lambda _:None,minimum_interval=0)


def test_automatic_repair_is_bounded_resumable_and_restores_internal_hole(seeded):
    db,a,b,aid,baid = seeded
    cn_item(db,base='20240801')
    client = Client()
    client.missing.add('2026-02-10')
    p = provider(client)
    # New symbols get latest bars immediately, then older quarters in bounded batches.
    p.sync(db,'850531.SI',instant=T0)
    assert len([x for x in client.calls if x[0]=='price']) <= 5
    assert '2026-08-12' in latest_rows(db,'850531.SI')
    state = json.loads(db.rows("SELECT value FROM settings WHERE key='history_repair:850531.SI'")[0]['value'])
    assert state['pending']
    p = provider(client) # process restart
    p.sync(db,'850531.SI',instant=T0+timedelta(minutes=3))
    assert '2026-02-10' not in latest_rows(db,'850531.SI')
    client.missing.clear()
    p.sync(db,'850531.SI',instant=T0+timedelta(hours=7))
    assert '2026-02-10' in latest_rows(db,'850531.SI')
    assert json.loads(db.rows("SELECT value FROM settings WHERE key='history_repair:850531.SI'")[0]['value'])['missing_sessions'] == 0


def test_factor_permissions_do_not_block_prices_and_retry_without_secret(seeded):
    db,a,b,aid,baid = seeded
    cn_item(db)
    client = Client()
    client.forbidden = True
    p = provider(client)
    p.sync(db,'850531.SI',instant=T0)
    assert '2026-08-12' in latest_rows(db,'850531.SI')
    status = db.rows("SELECT value FROM settings WHERE key='factor_sync:850531.SI:idx_factor_pro'")[0]['value']
    assert 'NO_PERMISSION' in status and 'SECRET' not in status
    count = len([r for r in client.calls if r[0]=='factor'])
    p.sync(db,'850531.SI',instant=T0+timedelta(hours=2))
    assert len([r for r in client.calls if r[0]=='factor']) == count
    client.forbidden = False
    p.sync(db,'850531.SI',instant=T0+timedelta(days=1,hours=1))
    assert latest_rows(db,'850531.SI')['2026-08-12']['factors']['macd_dif_bfq'] == 2


def test_network_permission_error_is_not_reported_as_membership_failure():
    from clawindex_next.adapters import ProviderError
    class Blocked:
        def sw_daily(self,**kwargs): raise PermissionError('network permission denied')
    with pytest.raises(ProviderError,match='NETWORK_FAILED'):
        provider(Blocked())._call('sw_daily',ts_code='850531.SI')


def test_automatic_identity_lookup_uses_the_requested_index_market(seeded):
    db,a,b,aid,baid = seeded
    with db.connect(True) as c:
        insert_instrument(c,instrument('000300.CSI','待核实指数'))
    class Basic:
        def index_basic(self,**kwargs):
            assert kwargs['market'] == 'CSI'
            return pd.DataFrame([{'ts_code':'000300.CSI','name':'沪深300','publisher':'中证','base_date':'20041231'}])
    item = provider(Basic()).ensure_identity(db,'000300.CSI')
    assert item['calendar'] == 'XSHG' and json.loads(item['metadata'])['identity_verified']


def test_professional_columns_preserve_price_units_and_append_only_revisions(seeded):
    db,a,b,aid,baid = seeded
    cn_item(db)
    p = provider(Client())
    p.sync(db,'850531.SI',instant=T0)
    row = latest_rows(db,'850531.SI')['2026-08-12']
    assert len(row['factors']) == 78
    assert row['amount'] == 100000 and row['amount_unit'] == 'CNY'
    assert row['source_endpoint'] == 'sw_daily' and row['factor_source'] == 'idx_factor_pro'
    versions = db.rows("SELECT available_at,availability_basis FROM observations WHERE code='850531.SI' AND trade_date='2026-08-12' ORDER BY id")
    assert len(versions) == 2
    assert versions[-1]['available_at'] == T0.isoformat() and versions[-1]['availability_basis'] == 'OBSERVED_SUPPLEMENT_AT_FETCH'
    count = len(db.rows("SELECT id FROM observations WHERE code='850531.SI'"))
    p.sync(db,'850531.SI',instant=T0+timedelta(minutes=3))
    # More missing history may arrive, but today's identical evidence does not proliferate.
    assert len(db.rows("SELECT id FROM observations WHERE code='850531.SI' AND trade_date='2026-08-12'")) == 2
    assert len(db.rows("SELECT id FROM observations WHERE code='850531.SI'")) >= count


def test_vendor_reversion_is_a_new_evidence_revision(seeded):
    db,a,b,aid,baid = seeded
    item = db.rows("SELECT * FROM instruments WHERE code='HKTECH'")[0]
    adapter = provider(Client())
    original = latest_rows(db,'HKTECH')['2026-08-12']
    corrected = {**original,'pe':40.}
    put_rows(db,adapter,item,[corrected],T0)
    put_rows(db,adapter,item,[original],T0+timedelta(minutes=1))
    assert latest_rows(db,'HKTECH')['2026-08-12']['pe'] == original['pe']
    assert len(db.rows("SELECT id FROM observations WHERE code='HKTECH' AND trade_date='2026-08-12'")) == 3


def test_missing_factor_window_is_not_filled_with_future_or_zero(seeded):
    db,a,b,aid,baid = seeded
    cn_item(db)
    client = Client()
    client.factor_missing.add('2026-08-11')
    p = provider(client)
    p.sync(db,'850531.SI',instant=T0)
    data = list(latest_rows(db,'850531.SI').values())
    days = sorted(r['date'] for r in data)
    frame = feature_frame(data,days,factor_spec())
    assert features_at(frame,'2026-08-11')['macd_dif_bfq'] is None
    assert features_at(frame,'2026-08-12')['macd_dif_bfq'] == 2


@pytest.mark.parametrize('key',list(PROFILES))
def test_factor_entry_filters_share_exit_engine_and_do_not_read_future(key):
    dates,rows = price_rows(200)
    for row in rows:
        row['factors'] = professional_values()
    spec = factor_spec(key)
    before = features_at(feature_frame(rows,dates,spec),dates[150])
    rows[-1]['close'] *= 10
    rows[-1]['factors']['macd_dif_bfq'] = -999
    after = features_at(feature_frame(rows,dates,spec),dates[150])
    assert before == after
    feature = {**before,'below':3,'above':0}
    for field in PROFILES[key]['fields']:
        if field != 'close': feature[field] = None
    signal = step(feature,'LONG',spec,dates[150])
    assert signal['event'] == 'SELL' and signal['exit_active'] and not signal['entry_active']


def test_technical_filter_really_changes_entries_and_has_no_exit_veto():
    dates,rows = price_rows(180)
    for row in rows:
        row['factors'] = {**professional_values(),'macd_dif_bfq':-1}
    baseline = replay(feature_frame(rows,dates))
    filtered = replay(feature_frame(rows,dates,factor_spec()),factor_spec())
    assert any(r['event']=='BUY' for r in baseline)
    assert not any(r['event']=='BUY' for r in filtered)
    rows[-1]['factors']['macd_dif_bfq'] = 2
    filtered = replay(feature_frame(rows,dates,factor_spec()),factor_spec())
    assert filtered[-1]['event'] == 'BUY'


def test_factor_coverage_controls_candidates_and_respects_old_authorization():
    dates,rows = price_rows(800)
    for row in rows: row['factors'] = professional_values()
    frame = feature_frame(rows,dates)
    config = policy_config({},DEFAULT_SPEC)
    variants,evidence = candidates(DEFAULT_SPEC,config,frame,dates[155])
    assert any(v.get('factor_profile') == 'technical_v1' for v in variants)
    old_config = {k:v for k,v in config.items() if not k.startswith('factor_')}
    assert not authorized(factor_spec(),DEFAULT_SPEC,old_config)
    assert authorized(factor_spec(),DEFAULT_SPEC,config)
    assert not authorized({**factor_spec(),'confirmation':4},DEFAULT_SPEC,config)
    rows[600]['factors']['macd_dif_bfq'] = None
    variants,evidence = candidates(DEFAULT_SPEC,config,feature_frame(rows,dates),dates[155])
    assert not evidence['technical_v1']['eligible']
    assert not any(v.get('factor_profile') == 'technical_v1' for v in variants)
    assert any(v['name']=='trend_confirm_v1' for v in variants)


def test_new_evolution_plan_contains_factor_cost_and_delay_trials(seeded):
    db,a,b,aid,baid = seeded
    dates,rows = price_rows(800)
    for row in rows: row['factors'] = professional_values()
    with db.connect(True) as c:
        c.execute('DELETE FROM observations')
        for row in rows:
            c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    pid = a.set_evolution('HKTECH',{})
    ev = Evolution(db,Evaluator(db,FixtureCalendar()))
    rid = ev.request(pid,T0)
    payload = json.loads(db.rows('SELECT payload FROM evolution_runs WHERE id=?',(rid,))[0]['payload'])
    assert len(payload['plan']) == 60
    assert payload['factor_diagnostics']['used_for_selection'] is False
    assert len({x['spec'].get('factor_profile') for x in payload['plan']}) == 5
    assert ev.run_trial(T0)
    report = json.loads(db.rows('SELECT payload FROM evolution_trials')[0]['payload'])
    assert report['comparison_status'] == 'EXPLORATORY'


def test_all_factor_diagnostics_exclude_validation_future_labels():
    dates,rows = price_rows(800)
    for i,row in enumerate(rows):
        row['factors'] = {**professional_values(),'macd_dif_bfq':float(i%23)}
    start = dates[155]
    before = diagnostics(feature_frame(rows,dates),start)
    assert len(before['factors']) >= 78
    assert before['factors'][0]['paired_sessions'] == int((800-155)*.6)-20
    for row in rows[-250:]:
        row['close'] *= 10
        row['factors']['macd_dif_bfq'] = -999
    after = diagnostics(feature_frame(rows,dates),start)
    assert before == after


@pytest.mark.parametrize('factor_side',['candidate','incumbent'])
def test_late_factors_wait_before_freeze_and_same_session_observation(seeded,monkeypatch,factor_side):
    db,a,b,aid,baid = seeded
    dates,rows = price_rows(800)
    for row in rows: row['factors'] = professional_values()
    with db.connect(True) as c:
        c.execute('DELETE FROM observations')
        for row in rows:
            c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    class Calendar(FixtureCalendar):
        day = '2026-08-12'
        def expected(self,*args,**kwargs): return self.day
    calendar = Calendar()
    ev = Evolution(db,Evaluator(db,calendar))
    if factor_side == 'incumbent': a.assign('HKTECH',factor_spec())
    pid = a.set_evolution('HKTECH',{})
    rid = ev.request(pid,T0)
    run = db.rows('SELECT payload FROM evolution_runs WHERE id=?',(rid,))[0]
    plan = json.loads(run['payload'])['plan']
    from clawindex_next.research import backtest
    report = backtest(rows,dates)
    with db.connect(True) as c:
        for n,trial in enumerate(plan):
            c.execute('INSERT INTO evolution_trials VALUES(?,?,?)',(rid,n,canonical({**report,'spec':trial['spec'],'cost_bps_per_side':trial['cost'],'execution_delay_sessions':trial['delay']})))
        c.execute('UPDATE evolution_runs SET cursor=? WHERE id=?',(len(plan),rid))
    chosen = factor_spec(ma=90) if factor_side == 'candidate' else {**DEFAULT_SPEC,'ma':90}
    monkeypatch.setattr('clawindex_next.evolution.select_candidate',lambda *_:(chosen,{'reasons':[]}))
    def put_day(day,with_factors):
        row = price_rows(1,end=day,prices=[300.])[1][0]
        if with_factors: row['factors'] = professional_values()
        with db.connect(True) as c:
            c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',('HKTECH',day,canonical(row),'fixture',now(),'FIXTURE',digest(row)))
        calendar.day = day
    put_day('2026-08-13',False)
    ev.finish(rid,T0+timedelta(days=1))
    assert db.rows('SELECT status FROM evolution_runs WHERE id=?',(rid,))[0]['status'] == 'PENDING'
    assert not ev.run_trial(T0+timedelta(days=1,minutes=10))
    put_day('2026-08-13',True)
    ev.finish(rid,T0+timedelta(days=1,minutes=31))
    assert db.rows('SELECT status FROM evolution_candidates WHERE id=?',(rid,))[0]['status'] == 'FORWARD_SHADOW'
    put_day('2026-08-14',False)
    ev.observe(rid,T0+timedelta(days=2))
    assert not db.rows('SELECT * FROM evolution_observations')
    put_day('2026-08-14',True)
    ev.observe(rid,T0+timedelta(days=2,minutes=30))
    assert len(db.rows('SELECT * FROM evolution_observations')) == 1


def test_default_and_old_known_positions_do_not_block_direction_or_invent_money():
    signal = {'data_status':'OK','event':'BUY','entry_active':True,'exit_active':False,'model_state':'LONG'}
    assert personal_decision(signal,instant=T0)['action'] == 'BUY'
    assert personal_decision(signal,{'status':'UNKNOWN','basis':'LEGACY_MANUAL_UNCONFIRMED'},instant=T0)['position_basis'] == 'DEFAULT_EMPTY'
    held = {'status':'HELD','market_value':'1000','currency':'CNY','reported_at':(T0-timedelta(days=90)).isoformat()}
    mapping = {'verified':True,'currency':'CNY'}
    decision = personal_decision({**signal,'exit_active':True},held,mapping=mapping,instant=T0)
    assert decision['action'] == 'SELL' and decision['amount'] is None


def test_daily_sync_also_generates_operations_without_separate_manual_inspection(seeded):
    db,a,b,aid,baid = seeded
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value='true' WHERE key='daily_sync_enabled'")
    class Cached:
        def sync(self,*args,**kwargs): pass
    worker = Worker(db,Evaluator(db,FixtureCalendar()),provider=Cached())
    worker.schedule(T0)
    assert len(db.rows('SELECT * FROM jobs')) == 2
    assert worker.run_job() and worker.run_job()
    assert a.results() and b.results()


def test_cloud_new_day_renews_decision_expiry_and_preserves_notification_episode_dedup(seeded):
    db,a,b,aid,baid = seeded
    with db.connect(True) as c:
        c.execute("UPDATE assignments SET catchup=1,stage='ENABLED' WHERE id=?",(aid,))
        c.execute("INSERT INTO channels(user_id,url,label,enabled) VALUES(?,'https://example.invalid','离线测试',1)",(a.user['id'],))
    ev = Evaluator(db,FixtureCalendar())
    s1,d1 = ev.evaluate(aid,T0)
    s2,d2 = ev.evaluate(aid,T0+timedelta(days=1,minutes=1))
    assert s1 == s2 and d1 != d2
    decision = json.loads(db.rows('SELECT result FROM personal_decisions WHERE id=?',(d2,))[0]['result'])
    assert datetime.fromisoformat(decision['expires_at']) > T0+timedelta(days=1,minutes=1)
    assert len(db.rows('SELECT * FROM outbox')) == 1


def test_weekend_continues_pending_history_repair_without_new_session(seeded):
    db,a,b,aid,baid = seeded
    weekend = datetime(2026,8,15,12,tzinfo=timezone.utc)
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value='true' WHERE key='daily_sync_enabled'")
        c.execute('INSERT INTO settings VALUES(?,?)',('history_repair:HKTECH',canonical({'pending':True,'next_due':T0.isoformat()})))
    class Cached:
        calls = 0
        def sync(self,*args,**kwargs): self.calls += 1
    cached = Cached()
    worker = Worker(db,Evaluator(db,FixtureCalendar()),provider=cached)
    worker.schedule(weekend)
    assert cached.calls == 1


def test_automatic_default_research_preserves_opt_out_and_requires_promotion_authorization(seeded):
    db,a,b,aid,baid = seeded
    dates,rows = price_rows(800)
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value='true' WHERE key='daily_sync_enabled'")
        c.execute('DELETE FROM observations')
        for row in rows:
            c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    ev = Evolution(db,Evaluator(db,FixtureCalendar()))
    ev.schedule(T0)
    assert len(db.rows('SELECT * FROM evolution_policies')) == 2
    assert not a.evolution_policy('HKTECH')['payload']['config']['auto_promote']
    assert a.evolution_policy('HKTECH')['payload']['config']['factor_research']
    b.set_evolution('HKTECH',{'enabled':False})
    from clawindex_next.evolution import save_policy
    saved = b.evolution_policy('HKTECH')['id']
    assert save_policy(db,b.user['id'],'HKTECH',{},only_if_missing=True) == saved
    ev.schedule(T0+timedelta(days=1))
    assert not b.evolution_policy('HKTECH')['payload']['config']['enabled']
    with db.connect(True) as c:
        c.execute('UPDATE evolution_policies SET active=0 WHERE user_id=?',(b.user['id'],))
    ev.schedule(T0+timedelta(days=2))
    assert b.evolution_policy('HKTECH') is None


@pytest.mark.parametrize('version,safe',[((3,45,3),False),((3,51,2),False),((3,51,3),True),((3,54,0),True),((3,44,6),True),((3,50,7),True)])
def test_cloud_sqlite_version_gate(version,safe):
    assert sqlite_safe(version) == safe


def test_online_backup_is_exclusive_and_preserves_committed_rows(seeded,tmp_path):
    db,a,b,aid,baid = seeded
    a.report_position('HKTECH','HELD')
    output = tmp_path/'backup.db'
    assert backup(db,output)['integrity'] == 'ok'
    with sqlite3.connect(output) as c:
        assert c.execute('SELECT COUNT(*) FROM position_reports').fetchone()[0] == 1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    with pytest.raises(FileExistsError): backup(db,output)


def test_graceful_worker_stop_clears_heartbeat_and_cloud_health(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    worker = Worker(db,owner='test-cloud')
    def once():
        worker.pulse()
        assert healthy(db,'worker')
        worker.stopping.set()
    monkeypatch.setattr(worker,'once',once)
    worker.run(interval=0)
    assert not healthy(db,'worker')


def test_worker_exits_after_three_loop_failures_for_container_restart(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    worker = Worker(db,owner='test-failure')
    def broken(): raise RuntimeError('sanitized by runtime')
    monkeypatch.setattr(worker,'once',broken)
    with pytest.raises(RuntimeError,match='连续异常'): worker.run(interval=0)
    assert len(db.rows("SELECT * FROM health WHERE component='worker'")) == 3
    assert not healthy(db,'worker')
