"""Shared, restartable L1/L2 discovery. No assignments or orders are created here."""
import json
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from .calendar import MarketCalendar
from .catalog import KEY as CATALOG_KEY, entries, refresh as refresh_catalog
from .data_maintenance import put_rows, valid_price
from .migration import insert_instrument
from .radar_rules import RULE, RULE_ID, analyze, frame_for, select
from .storage import canonical, digest


BENCHMARK = {'code':'000300.SH','name':'沪深300','market':'CN','calendar':'XSHG','timezone':'Asia/Shanghai','currency':'CNY',
             'asset_kind':'EQUITY_INDEX','publisher':'中证指数','metadata':{'source':'radar_benchmark_registry_v1','identity_verified':True,'tradable':False}}


def read(store, key, default=None):
    row = store.rows('SELECT value FROM settings WHERE key=?',(key,))
    return json.loads(row[0]['value']) if row else default


def write(store, key, value):
    with store.connect(True) as c:
        c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,canonical(value)))


def enabled(store):
    return read(store,'radar:enabled',read(store,'daily_sync_enabled',True))


def universe(store):
    # The first release intentionally covers all verified L1 and L2, never L3.
    catalog = read(store,CATALOG_KEY,{})
    directory = catalog.get('items',[]) if catalog.get('complete') else entries(store)
    return [r for r in directory if r['code'].endswith('.SI') and r['market']=='CN' and r['metadata'].get('level') in ('L1','L2')
            and r['metadata'].get('source_version') in (None,'SW2021') and r['metadata'].get('identity_verified')]


def expected(calendar, instant):
    # Compare every index at the same published CN session, including the benchmark.
    return calendar.expected('XSHG',instant,210)


def load(store, item, day, instant, calendar):
    start = (pd.Timestamp(day)-pd.DateOffset(years=3)).date().isoformat()
    rows = store.rows('SELECT o.* FROM observations o WHERE code=? AND trade_date>=? AND trade_date<=? AND (available_at IS NULL OR available_at<=?) AND id=(SELECT MAX(id) FROM observations WHERE code=o.code AND trade_date=o.trade_date AND (available_at IS NULL OR available_at<=?)) ORDER BY trade_date',
                      (item['code'],start,day,instant.isoformat(),instant.isoformat()))
    if not rows:
        return pd.DataFrame(),{'code':item['code'],'as_of':day,'observation_max_id':0,'row_count':0}
    sessions = calendar.sessions('XSHG',rows[0]['trade_date'],day)
    data = [json.loads(row['payload']) for row in rows]
    # Unknown imported price provenance must first be refreshed by the provider.
    for row in data:
        if row.get('quality')=='LEGACY_PROVENANCE_UNKNOWN':
            row['close'] = None
    frame = frame_for(data,sessions)
    source = {'code':item['code'],'as_of':day,'start':sessions[0],'row_count':len(rows),
              'observation_max_id':max(r['id'] for r in rows),'observation_digest':digest([(r['id'],r['row_hash']) for r in rows]),
              'availability':'历史发布时间可能估计；每次扫描按当时已保存的版本留证'}
    return frame,source


def latest(store):
    pointer = read(store,'radar:latest')
    return read(store,'radar:snapshot:'+pointer) if pointer else None


def request(store, uid, calendar=None, instant=None):
    instant = instant or datetime.now(timezone.utc)
    day = expected(calendar or MarketCalendar(),instant)
    current = latest(store)
    if current and current['as_of']==day and current['rule_id']==RULE_ID and current['status']=='COMPLETE':
        return 'ALREADY_DONE'
    with store.connect(True) as c:
        # A shared receipt is a wake-up request, never a user identity for reads.
        old = c.execute("SELECT value FROM settings WHERE key='radar:requested'").fetchone()
        if not old or json.loads(old[0]).get('as_of')!=day:
            value = {'as_of':day,'created_at':instant.isoformat()}
            c.execute("INSERT INTO settings VALUES('radar:requested',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(canonical(value),))
        c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('radar:receipt:'+str(uid),canonical({'as_of':day,'requested_at':instant.isoformat()})))
    return 'QUEUED'


def snapshot(store, items, calendar, instant):
    day = expected(calendar,instant)
    frames,sources = {},{}
    for item in [BENCHMARK,*items]:
        frames[item['code']],sources[item['code']] = load(store,item,day,instant,calendar)
    report = analyze(items,frames,frames[BENCHMARK['code']],sources)
    report.update(as_of=day,universe=[{'code':r['code'],'name':r['name'],'level':r['metadata'].get('level')} for r in items],
                  universe_id=digest([(r['code'],r['metadata']) for r in items]))
    return report


def publish(store, report, instant):
    rid = digest(report)
    with store.connect(True) as c:
        if c.execute('SELECT key FROM settings WHERE key=?',('radar:snapshot:'+rid,)).fetchone():
            return False
        previous_id = c.execute("SELECT value FROM settings WHERE key='radar:last_complete'").fetchone()
        previous = None
        if previous_id:
            old = c.execute('SELECT value FROM settings WHERE key=?',('radar:snapshot:'+json.loads(previous_id[0]),)).fetchone()
            previous = json.loads(old[0]) if old else None
        old_rows = {r['code']:r for r in previous['rows']} if previous else {}
        events = []
        if report['status']=='COMPLETE':
            for row in report['rows']:
                old = old_rows.get(row['code'])
                if row['state'] in ('READY','WATCH') and (not old or old['state'] not in ('READY','WATCH')):
                    events.append({'code':row['code'],'kind':'NEW','state':row['state']})
                elif row['state']=='READY' and old and old['state']=='WATCH':
                    events.append({'code':row['code'],'kind':'READY','state':'READY'})
                elif old and old['state'] in ('READY','WATCH') and row['state']=='REJECTED':
                    events.append({'code':row['code'],'kind':'WITHDRAWN','state':'REJECTED'})
            # A removed directory entry is a scope change, not an investment failure.
            for code,old in old_rows.items():
                if code not in {r['code'] for r in report['rows']} and old['state'] in ('READY','WATCH'):
                    events.append({'code':code,'kind':'SCOPE_REMOVED','state':'REMOVED'})
        saved = {**report,'id':rid,'created_at':instant.isoformat(),'events':events,'previous_complete':previous['id'] if previous else None}
        c.execute('INSERT INTO settings VALUES(?,?)',('radar:snapshot:'+rid,canonical(saved)))
        c.execute("INSERT INTO settings VALUES('radar:latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(canonical(rid),))
        if report['status']=='COMPLETE':
            for code in set(report['selected'])-set(previous['selected'] if previous else []):
                row = next(r for r in report['rows'] if r['code']==code)
                discovery = {'code':code,'name':row['name'],'as_of':report['as_of'],'created_at':instant.isoformat(),
                             'state':row['state'],'kind':row['kind'],'report_id':rid,'rule_id':RULE_ID}
                c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('radar:discovery:'+digest({'report':rid,'code':code}),canonical(discovery)))
            c.execute("INSERT INTO settings VALUES('radar:last_complete',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(canonical(rid),))
            c.execute("DELETE FROM settings WHERE key='radar:requested' AND json_extract(value,'$.as_of')<=?",(report['as_of'],))
            from .radar_notifications import enqueue
            enqueue(store,saved,connection=c)
    return True


class Radar:
    def __init__(self, store, calendar=None, provider=None):
        self.store,self.calendar,self.provider = store,calendar or MarketCalendar(),provider

    def provider_for(self):
        if self.provider is None:
            from .adapters import TushareAdapter
            from .syncing import rate_limit
            self.provider = TushareAdapter(calendar=self.calendar,rate_limiter=lambda:rate_limit(self.store))
        return self.provider

    def put_batch(self, day, bars, items, instant):
        allowed = {item['code'] for item in items}
        if not bars or day not in self.calendar.sessions('XSHG',day,day):
            return 0
        published = self.calendar.published_at('XSHG',day,210).isoformat()
        count = 0
        with self.store.connect(True) as c:
            old = {r['code']:r for r in c.execute('SELECT o.* FROM observations o WHERE trade_date=? AND id=(SELECT MAX(id) FROM observations WHERE code=o.code AND trade_date=o.trade_date)',(day,))}
            for code,row in bars.items():
                if code not in allowed or row.get('date')!=day or not valid_price(row):
                    continue
                before = json.loads(old[code]['payload']) if code in old else {}
                merged = {**before,**row}
                if merged==before:
                    continue
                row_hash = digest(merged)
                if c.execute('SELECT id FROM observations WHERE code=? AND trade_date=? AND row_hash=?',(code,day,row_hash)).fetchone():
                    row_hash = digest({'payload':merged,'after_revision':old[code]['id']})
                c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,available_at,availability_basis,row_hash) VALUES(?,?,?,'tushare',?,?,'ESTIMATED_CLOSE_PLUS_VENDOR_GRACE',?)",(code,day,canonical(merged),instant.isoformat(),published,row_hash))
                count += 1
        return count

    def maintain(self, items, day, instant, heartbeat=lambda:None, maximum=6, budget_seconds=12):
        """Bounded cross-section backfill; latest 260 sessions before older history."""
        started = time.monotonic()
        if read(self.store,'radar:provider_cooldown',{}).get('next_at','')>instant.isoformat():
            return
        benchmark,_ = load(self.store,BENCHMARK,day,instant,self.calendar)
        from .radar_rules import usable
        if not usable(benchmark):
            state = read(self.store,'radar:benchmark',{})
            if state.get('next_at','')<=instant.isoformat():
                try:
                    provider = self.provider_for()
                    start = (pd.Timestamp(day)-pd.Timedelta(days=450)).date().isoformat()
                    rows = provider.fetch(BENCHMARK,start,day,instant)
                    from types import SimpleNamespace
                    put_rows(self.store,SimpleNamespace(calendar=self.calendar),BENCHMARK,rows,instant)
                    write(self.store,'radar:benchmark',{'next_at':(instant+timedelta(minutes=30)).isoformat()})
                except Exception as exc:
                    self.failure('radar:benchmark',exc,instant)
                heartbeat()
            return
        codes = {r['code'] for r in items}
        if not codes:
            return
        first = (pd.Timestamp(day)-pd.DateOffset(years=3)).date().isoformat()
        days = self.calendar.sessions('XSHG',first,day)
        counts = {r['trade_date']:r['n'] for r in self.store.rows("SELECT trade_date,COUNT(DISTINCT code) n FROM observations o WHERE code IN ("+','.join('?' for _ in codes)+") AND trade_date>=? AND trade_date<=? AND (available_at IS NULL OR available_at<=?) AND json_extract(payload,'$.close')>0 AND json_extract(payload,'$.open')>0 AND json_extract(payload,'$.low')>0 AND json_extract(payload,'$.high')>=MAX(json_extract(payload,'$.open'),json_extract(payload,'$.close'),json_extract(payload,'$.low')) AND json_extract(payload,'$.low')<=MIN(json_extract(payload,'$.open'),json_extract(payload,'$.close'),json_extract(payload,'$.high')) AND COALESCE(json_extract(payload,'$.quality'),'')<>'LEGACY_PROVENANCE_UNKNOWN' AND id=(SELECT MAX(id) FROM observations WHERE code=o.code AND trade_date=o.trade_date AND (available_at IS NULL OR available_at<=?)) GROUP BY trade_date",(*sorted(codes),first,day,instant.isoformat(),instant.isoformat()))}
        recent = list(reversed(days[-RULE['minimum_sessions']:]))
        queue = recent+list(reversed(days[:-RULE['minimum_sessions']]))
        provider = None
        done = 0
        for date in queue:
            state = read(self.store,'radar:batch:'+date,{})
            # Revisit the current cross-section periodically for provider revisions.
            complete = counts.get(date,0)>=len(codes)
            if (complete and date!=day) or state.get('next_at','')>instant.isoformat():
                continue
            try:
                provider = provider or self.provider_for()
                if not hasattr(provider,'fetch_sw_day'):
                    missing = next((item for item in items if not usable(load(self.store,item,day,instant,self.calendar)[0])),None)
                    if missing:
                        start = (pd.Timestamp(day)-pd.Timedelta(days=450)).date().isoformat()
                        from types import SimpleNamespace
                        put_rows(self.store,SimpleNamespace(calendar=self.calendar),missing,provider.fetch(missing,start,day,instant),instant)
                    break
                bars = provider.fetch_sw_day(date)
                count = self.put_batch(date,bars,items,instant)
                write(self.store,'radar:batch:'+date,{'next_at':(instant+timedelta(hours=6) if bars else instant+timedelta(minutes=30)).isoformat(),'returned':len(set(bars)&codes),'changed':count})
            except Exception as exc:
                self.failure('radar:batch:'+date,exc,instant)
                # Avoid hammering many dates after a common permission/network failure.
                break
            done += 1
            heartbeat()
            if done>=maximum or time.monotonic()-started>=budget_seconds:
                break
        return done>0

    def failure(self, key, exc, instant):
        from .syncing import error_message
        text = error_message(exc)
        delay = timedelta(hours=24) if str(exc).startswith('NO_PERMISSION') else timedelta(minutes=30)
        write(self.store,key,{'next_at':(instant+delay).isoformat(),'error':text})
        write(self.store,'radar:provider_cooldown',{'next_at':(instant+delay).isoformat(),'error':text})
        self.store.health('radar','WAITING',text+'；机会雷达后台稍后重试。')

    def tick(self, instant=None, heartbeat=lambda:None):
        instant = instant or datetime.now(timezone.utc)
        manual = read(self.store,'radar:requested')
        if not enabled(self.store) and not manual:
            return False
        items = universe(self.store)
        if not items:
            refresh_catalog(self.store,self.provider,instant)
            items = universe(self.store)
        day = expected(self.calendar,instant)
        if not items:
            write(self.store,'radar:progress',{'as_of':day,'status':'DIRECTORY','detail':'后台正在准备申万L1、L2目录，无需手动同步。','total':0})
            return False
        with self.store.connect(True) as c:
            for item in [BENCHMARK,*items]:
                insert_instrument(c,item)
        state = read(self.store,'radar:check',{})
        revision = self.store.rows('SELECT MAX(id) n FROM observations WHERE trade_date<=? AND (available_at IS NULL OR available_at<=?)',(day,instant.isoformat()))[0]['n']
        signature = digest({'day':day,'universe':[(r['code'],r['metadata']) for r in items],'revision':revision,'rule':RULE_ID})
        if state.get('signature')==signature and state.get('next_at','')>instant.isoformat() and not manual:
            return False
        # Only an empty/data-incomplete scan needs full history maintenance.
        maintenance_pending = self.maintain(items,day,instant,heartbeat)
        heartbeat()
        report = snapshot(self.store,items,self.calendar,instant)
        changed = publish(self.store,report,instant)
        progress = {'as_of':day,'status':report['status'],'available':report['available'],'total':report['total'],
                    'detail':'扫描完成，未达标时允许没有新机会。' if report['status']=='COMPLETE' else '后台分批准备共同历史，覆盖达到门槛后展示候选。'}
        cooldown = read(self.store,'radar:provider_cooldown',{})
        if report['status']!='COMPLETE' and cooldown.get('next_at','')>instant.isoformat():
            progress['detail'] = cooldown.get('error','行情接口暂未就绪')+'；后台会自动重试。'
        write(self.store,'radar:progress',progress)
        health_status = 'OK' if report['status']=='COMPLETE' else 'WAITING'
        previous_health = self.store.rows("SELECT status,detail FROM health WHERE component='radar' AND user_id IS NULL ORDER BY id DESC LIMIT 1")
        if not previous_health or (previous_health[0]['status'],previous_health[0]['detail'])!=(health_status,progress['detail']):
            self.store.health('radar',health_status,progress['detail'])
        revision = self.store.rows('SELECT MAX(id) n FROM observations WHERE trade_date<=? AND (available_at IS NULL OR available_at<=?)',(day,instant.isoformat()))[0]['n']
        signature = digest({'day':day,'universe':[(r['code'],r['metadata']) for r in items],'revision':revision,'rule':RULE_ID})
        write(self.store,'radar:check',{'signature':signature,'next_at':(instant+timedelta(minutes=30) if report['status']=='COMPLETE' and not maintenance_pending else instant+timedelta(seconds=20)).isoformat()})
        return changed


def preferences(store, uid):
    return read(store,'radar:preferences:'+str(uid),{'hide_watched':True,'ignored':[]})


def view(store, uid, watches, calendar=None, instant=None):
    instant = instant or datetime.now(timezone.utc)
    calendar = calendar or MarketCalendar()
    report = latest(store)
    base = {'report':report,'progress':read(store,'radar:progress',{}),'enabled':enabled(store),'candidates':[],
            'suppressed':[],'stale':False,'preferences':preferences(store,uid)}
    if not report:
        return base
    base['stale'] = report['as_of']!=expected(calendar,instant) or report['rule_id']!=RULE_ID
    if base['stale'] or report['status']!='COMPLETE':
        return base
    prefs = base['preferences']
    excluded = set(prefs['ignored'])|({r['code'] for r in watches} if prefs['hide_watched'] else set())
    base['candidates'],base['suppressed'] = select(report['rows'],report['correlations'],excluded)
    return base
