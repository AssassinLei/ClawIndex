"""Bounded, restartable automatic repairs. Empty replies never fabricate bars."""
import json
import math
from datetime import timedelta

import pandas as pd

from .calendar import grace_for
from .storage import canonical, digest


def latest_rows(store, code, connection=None):
    sql = 'SELECT o.* FROM observations o WHERE code=? AND id=(SELECT MAX(id) FROM observations WHERE code=o.code AND trade_date=o.trade_date)'
    rows = connection.execute(sql,(code,)).fetchall() if connection is not None else store.rows(sql,(code,))
    return {r['trade_date']:json.loads(r['payload']) for r in rows}


def valid_price(row):
    values = [row.get(k) for k in ('open','high','low','close')]
    return all(isinstance(x,(int,float)) and math.isfinite(x) and x>0 for x in values) and values[1] >= max(values[0],values[2],values[3]) and values[2] <= min(values[0],values[1],values[3])


def history_start(item):
    # HKTECH's pre-launch back-calculated closes do not have executable OHLC
    # in index_global. Keep imported records, but never retry them as gaps or
    # treat them as tradeable backtest sessions.
    floor = '2020-07-27' if item['code'] == 'HKTECH' else '2016-01-01'
    base = str(json.loads(item['metadata']).get('base_date') or '')
    try:
        date = pd.Timestamp(base)
        return max(floor,date.date().isoformat()) if not pd.isna(date) else floor
    except (ValueError,TypeError):
        return floor


def windows(days, maximum=3):
    """Quarter windows, newest missing quarter first; at most one visit per quarter."""
    grouped = {}
    for day in days:
        period = pd.Timestamp(day).to_period('Q')
        grouped[str(period)] = (period.start_time.date().isoformat(),period.end_time.date().isoformat())
    return sorted(grouped.values(),reverse=True)[:maximum]


def readiness(frame, rows, spec):
    """Describe the exact input window used by the rule, never count raw rows."""
    required = spec['ma']+spec['slope_window']
    recent = frame.tail(required)
    known = {r['date']:r for r in rows}
    missing = [day for day in recent.index if day not in known]
    invalid = [day for day in recent.index if day in known and not bool(recent.loc[day,'valid_price'])]
    continuous = 0
    for valid in reversed(recent.valid_price.tolist()):
        if not valid:
            break
        continuous += 1
    return {'required_sessions':required,'window_sessions':len(recent),
            'valid_sessions':int(recent.valid_price.sum()),'continuous_valid_sessions':continuous,
            'short_sessions':max(0,required-len(recent)),
            'missing_dates':missing,'invalid_dates':invalid}


def put_rows(store, adapter, item, rows, instant, supplement=False):
    expected = adapter.calendar.expected(item['calendar'],instant,grace_for(item))
    if not rows:
        return 0
    valid_days = set(adapter.calendar.sessions(item['calendar'],min(r['date'] for r in rows),expected))
    changed = 0
    with store.connect(True) as c:
        # Merge against the revision protected by this writer lock. The radar
        # may have committed fresh prices while a factor request was in flight.
        known = latest_rows(store,item['code'],c)
        for row in rows:
            day = row['date']
            if day not in valid_days or day>expected:
                continue
            old = known.get(day,{})
            if supplement:
                if not old or not valid_price(old):
                    continue
                merged = {**old}
                if row.get('factors'):
                    # Replace the endpoint's own fields, preserve other sources.
                    merged['factors'] = {**old.get('factors',{}),**row['factors']}
                    merged['factor_source'] = row['factor_source']
                for k in row:
                    if k not in ('date','factors','factor_source'):
                        merged[k] = row[k]
            else:
                merged = {**old,**row}
                # Retain professional values but never retain obsolete price evidence.
                if row.get('source_endpoint') == 'index_daily' and old.get('basic_source') == 'index_dailybasic':
                    from .factors import BASIC_FIELDS
                    # index_daily has no valuation columns. Its absent values
                    # must not erase index_dailybasic then get restored every
                    # repair cycle, creating artificial evidence revisions.
                    for field in BASIC_FIELDS:
                        if row.get(field) is None and field in old:
                            merged[field] = old[field]
                    merged['basic_source'] = old['basic_source']
                    merged['valuation_source'] = old.get('valuation_source')
            if old == merged:
                continue
            published = adapter.calendar.published_at(item['calendar'],day,grace_for(item))
            available = max(published,instant) if supplement else published
            row_hash = digest(merged)
            if c.execute('SELECT id FROM observations WHERE code=? AND trade_date=? AND row_hash=?',(item['code'],day,row_hash)).fetchone():
                # A vendor can revert to an earlier value. Reusing an old hash
                # must not leave the superseded revision as the current record.
                latest_id = c.execute('SELECT MAX(id) FROM observations WHERE code=? AND trade_date=?',(item['code'],day)).fetchone()[0]
                row_hash = digest({'payload':merged,'after_revision':latest_id})
            c.execute('INSERT OR IGNORE INTO observations(code,trade_date,payload,source,fetched_at,available_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?,?)',
                      (item['code'],day,canonical(merged),'tushare',instant.isoformat(),available.isoformat(),
                       'OBSERVED_SUPPLEMENT_AT_FETCH' if supplement else 'ESTIMATED_CLOSE_PLUS_VENDOR_GRACE',row_hash))
            known[day] = merged
            changed += 1
    return changed


def repair(adapter, store, item, instant, explicit_start=None):
    from .adapters import ProviderError
    code = item['code']
    expected = adapter.calendar.expected(item['calendar'],instant,grace_for(item))
    first = explicit_start or history_start(item)
    all_days = adapter.calendar.sessions(item['calendar'],first,expected)
    records = latest_rows(store,code)
    key = 'history_repair:'+code
    old = store.rows('SELECT value FROM settings WHERE key=?',(key,))
    state = json.loads(old[0]['value']) if old else {}
    attempts = state.get('windows',{})
    stalls = state.get('stalled_windows',{})
    recent = max(first,(pd.Timestamp(expected)-pd.Timedelta(days=10 if records else 90)).date().isoformat())
    # Explicit dates are supported for operators, but the normal UI needs none.
    from zoneinfo import ZoneInfo
    local_day = instant.astimezone(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
    latest_valid = expected in records and valid_price(records[expected]) and records[expected].get('quality') != 'LEGACY_PROVENANCE_UNKNOWN'
    last_check = state.get('latest_checked_at')
    latest_due = (not last_check or state.get('expected') != expected or
                  (not latest_valid and instant.isoformat() >= state.get('latest_retry_at','')) or
                  (latest_valid and state.get('latest_checked_day') != local_day))
    ranges = [(max(first,a),min(expected,b)) for a,b in windows([d for d in all_days if d>=recent],maximum=2)] if latest_due or explicit_start else []
    latest_ranges = set(ranges)
    missing = [d for d in all_days if d not in records or not valid_price(records[d]) or records[d].get('quality') == 'LEGACY_PROVENANCE_UNKNOWN']
    specs = store.rows('SELECT s.spec FROM assignments a JOIN strategy_versions s ON s.id=a.strategy_id WHERE a.code=? AND a.active=1',(code,))
    required = max([125]+[json.loads(r['spec'])['ma']+json.loads(r['spec'])['slope_window'] for r in specs])
    essential_days = set(all_days[-required:])
    essential_missing = [d for d in missing if d in essential_days]
    essential_due = state.get('essential_expected') != expected or state.get('essential_retry_at','')<=instant.isoformat()
    ready = [d for d in missing if attempts.get(str(pd.Timestamp(d).to_period('Q')),'') <= instant.isoformat()]
    current_quarters = {str(pd.Timestamp(a).to_period('Q')) for a,b in ranges}
    ready = [d for d in ready if str(pd.Timestamp(d).to_period('Q')) not in current_quarters]
    urgent = windows(essential_missing) if essential_due else []
    chosen = list(dict.fromkeys(urgent+windows(ready)))
    ranges += [(max(first,a),min(expected,b)) for a,b in chosen if str(pd.Timestamp(a).to_period('Q')) not in current_quarters][:3]
    changed = 0
    used_fallback = False
    for a,b in ranges:
        before = sum(a<=d<=b for d in missing)
        try:
            rows = adapter.fetch(item,a,b,instant)
            changed += put_rows(store,adapter,item,rows,instant)
        except ProviderError as exc:
            if str(exc).startswith('NO_PERMISSION'):
                raise
            store.health('sync','RETRY',code+'：该段历史暂未获取，后台定期重试')
        current = latest_rows(store,code)
        after = sum(a<=d<=b and (d not in current or not valid_price(current[d]) or current[d].get('quality') == 'LEGACY_PROVENANCE_UNKNOWN') for d in all_days)
        critical = [d for d in essential_missing if a<=d<=b]
        # A quarter may temporarily return empty despite a valid latest quote.
        # Try one smaller interval per batch before cooling down the key gap.
        if critical and after>=before and not used_fallback:
            used_fallback = True
            end = max(critical)
            start = max(a,(pd.Timestamp(end)-pd.Timedelta(days=31)).date().isoformat())
            try:
                rows = adapter.fetch(item,start,end,instant)
                changed += put_rows(store,adapter,item,rows,instant)
            except ProviderError as exc:
                if str(exc).startswith('NO_PERMISSION'):
                    raise
            current = latest_rows(store,code)
            after = sum(a<=d<=b and (d not in current or not valid_price(current[d]) or current[d].get('quality') == 'LEGACY_PROVENANCE_UNKNOWN') for d in all_days)
        # Revisit unfillable provider windows later; move on to the other quarters.
        quarter = str(pd.Timestamp(a).to_period('Q'))
        stalls[quarter] = min(4,stalls.get(quarter,0)+1) if before and after>=before else 0
        critical_remaining = any(d not in current or not valid_price(current[d]) or current[d].get('quality') == 'LEGACY_PROVENANCE_UNKNOWN' for d in critical)
        delay = timedelta(minutes=30) if critical_remaining else timedelta(hours=min(24,6*2**max(0,stalls[quarter]-1)))
        attempts[quarter] = (instant+delay).isoformat()
    if latest_ranges:
        state.update(latest_checked_at=instant.isoformat(),latest_checked_day=local_day,
                     latest_retry_at=(instant+timedelta(minutes=30)).isoformat())
    records = latest_rows(store,code)
    remaining = [d for d in all_days if d not in records or not valid_price(records[d]) or records[d].get('quality') == 'LEGACY_PROVENANCE_UNKNOWN']
    essential_remaining = [d for d in remaining if d in essential_days]
    if essential_due:
        state.update(essential_expected=expected,essential_retry_at=(instant+timedelta(minutes=2 if len(essential_remaining)<len(essential_missing) else 30)).isoformat())
    can_repair = any(attempts.get(str(pd.Timestamp(d).to_period('Q')),'') <= instant.isoformat() for d in remaining)
    next_due = (instant+timedelta(minutes=2)).isoformat() if can_repair or not remaining else min(attempts[str(pd.Timestamp(d).to_period('Q'))] for d in remaining)
    if essential_remaining:
        next_due = min(next_due,state['essential_retry_at'])
    details = {'checked_at':instant.isoformat(),'expected':expected,'start':first,'missing_sessions':len(remaining),
               'pending':bool(remaining),'next_due':next_due,'windows':attempts,'stalled_windows':stalls,
               'required_sessions':required,'essential_missing_sessions':len(essential_remaining),
               **{k:state[k] for k in ('latest_checked_at','latest_checked_day','latest_retry_at','essential_expected','essential_retry_at') if k in state}}
    with store.connect(True) as c:
        c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,canonical(details)))
    # Optional supplements never prevent price repair or essential sell signals.
    factor_pending = adapter.sync_supplements(store,item,instant)
    if factor_pending:
        details['pending'] = True
        if not can_repair:
            factor = store.rows('SELECT value FROM settings WHERE key=?',('factor_sync:'+code,))
            due = json.loads(factor[0]['value']).get('next_due') if factor else None
            factor_due = due or (instant+timedelta(minutes=30)).isoformat()
            details['next_due'] = min(details['next_due'],factor_due) if remaining else factor_due
        with store.connect(True) as c:
            c.execute('UPDATE settings SET value=? WHERE key=?',(canonical(details),key))
    if expected not in records or not valid_price(records[expected]):
        raise ProviderError('DELAYED: 已保存可取得的历史，最新行情尚未到达，后台会重试')
    return changed
