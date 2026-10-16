"""Shared index directory; searches never call providers on every keystroke."""
import json
from datetime import datetime, timedelta, timezone

from .migration import instrument
from .storage import canonical


KEY = 'index_catalog:SW2021'


def entries(store):
    result = {code:instrument(code,code) for code in ('HKTECH','IXIC')}
    for row in store.rows("SELECT payload FROM legacy_archive WHERE table_name='industry_list'"):
        raw = json.loads(row['payload'])
        code = raw.get('index_code','')
        if code.endswith('.SI') and raw.get('industry_name'):
            result[code] = instrument(code,raw['industry_name'],raw)
    cached = store.rows('SELECT value FROM settings WHERE key=?',(KEY,))
    for item in json.loads(cached[0]['value']).get('items',[]) if cached else []:
        result[item['code']] = item
    for item in store.rows('SELECT * FROM instruments'):
        item['metadata'] = json.loads(item['metadata'])
        if item['metadata'].get('identity_verified'):
            current = result.get(item['code'])
            if current and current['metadata'].get('source')=='tushare:index_classify:SW2021':
                item['name'] = current['name']
                item['metadata'].update({k:v for k,v in current['metadata'].items() if k in ('level','parent_code','industry_code','source_version') and v is not None})
            result[item['code']] = item
    return sorted(result.values(),key=lambda item:(item['name'],item['code']))


def search(store, keyword='', limit=100):
    query = keyword.strip().casefold()
    rows = [item for item in entries(store) if not query or query in item['name'].casefold() or query in item['code'].casefold()]
    return rows[:limit]


def refresh_due(store, instant):
    state = store.rows('SELECT value FROM settings WHERE key=?',(KEY,))
    state = json.loads(state[0]['value']) if state else {}
    return not (state.get('format_version')==2 and state.get('next_check','')>instant.isoformat())


def refresh(store, provider=None, instant=None):
    """Worker-maintained directory; preserve the last good copy on API failure."""
    instant = instant or datetime.now(timezone.utc)
    state = store.rows('SELECT value FROM settings WHERE key=?',(KEY,))
    state = json.loads(state[0]['value']) if state else {}
    if state.get('format_version')==2 and state.get('next_check','') > instant.isoformat():
        return
    try:
        if provider is None:
            from .adapters import TushareAdapter
            from .syncing import rate_limit
            provider = TushareAdapter(rate_limiter=lambda:rate_limit(store))
        if not hasattr(provider,'_call'):
            return
        items = {}
        complete = True
        for level in ('L1','L2','L3'):
            frame = provider._call('index_classify',level=level,src='SW2021')
            if frame is None or frame.empty:
                complete = False
                continue
            accepted = 0
            for raw in frame.to_dict('records'):
                code = raw.get('index_code','')
                if not isinstance(code,str) or not code.endswith('.SI') or not isinstance(raw.get('industry_name'),str) or not raw['industry_name'].strip():
                    continue
                details = {'industry_name':raw['industry_name'],'level':level,'src':'SW2021'}
                for key in ('industry_code','parent_code'):
                    value = raw.get(key)
                    if isinstance(value,str) and value.strip():
                        details[key] = value
                item = instrument(code,raw['industry_name'],details)
                item['metadata']['source'] = 'tushare:index_classify:SW2021'
                items[code] = item
                accepted += 1
            if not accepted:
                complete = False
        if not complete:
            items = {**{item['code']:item for item in state.get('items',[])},**items}
        state.update(items=list(items.values()),complete=complete,format_version=2,next_check=(instant+timedelta(days=7) if complete else instant+timedelta(hours=1)).isoformat())
    except Exception:
        state['format_version'] = 2
        state['next_check'] = (instant+timedelta(hours=1)).isoformat()
        store.health('catalog','WAITING','指数搜索目录暂未更新，保留已有目录，后台稍后重试。')
    with store.connect(True) as c:
        c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(KEY,canonical(state)))
