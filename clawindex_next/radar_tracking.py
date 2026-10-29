"""Frozen discovery cohorts, including losses. No portfolio or win-rate claims."""
import json
from datetime import datetime, timezone

import numpy as np

from .radar import BENCHMARK, expected
from .data_maintenance import valid_price


def outcomes(store, calendar, instant=None, limit=200):
    instant = instant or datetime.now(timezone.utc)
    end = expected(calendar,instant)
    records = [json.loads(r['value']) for r in store.rows("SELECT value FROM settings WHERE key LIKE 'radar:discovery:%' ORDER BY json_extract(value,'$.created_at') DESC,key LIMIT ?",(min(max(int(limit),1),500),))]
    result,cache = [],{}
    versions = {}
    def prices(code):
        if code not in cache:
            rows = store.rows('SELECT o.* FROM observations o WHERE code=? AND trade_date<=? AND (available_at IS NULL OR available_at<=?) AND id=(SELECT MAX(id) FROM observations WHERE code=o.code AND trade_date=o.trade_date AND (available_at IS NULL OR available_at<=?)) ORDER BY trade_date',(code,end,instant.isoformat(),instant.isoformat()))
            cache[code] = {r['trade_date']:json.loads(r['payload']) for r in rows}
        return cache[code]
    for record in records:
        if not record.get('rule_version'):
            report_id = record.get('report_id')
            if report_id not in versions:
                snapshot = store.rows('SELECT value FROM settings WHERE key=?',('radar:snapshot:'+str(report_id),))
                versions[report_id] = json.loads(snapshot[0]['value']).get('rule',{}).get('version','历史版本') if snapshot else '历史版本'
            record = {**record,'rule_version':versions[report_id]}
        # Distinct discovery episodes on one date keep their actual publication times.
        published = datetime.fromisoformat(record['created_at'])
        days = calendar.sessions('XSHG',record['as_of'],end)
        after = []
        for day in days:
            opened = calendar.opened_at('XSHG',day) if hasattr(calendar,'opened_at') else datetime.fromisoformat(day+'T01:30:00+00:00')
            if day>record['as_of'] and opened>published and opened<=instant:
                after.append(day)
        stock,benchmark = prices(record['code']),prices(BENCHMARK['code'])
        for horizon in (20,60):
            item = {**record,'horizon':horizon,'entry_date':after[0] if after else None,'status':'WAITING',
                    'return':None,'benchmark_return':None,'relative_return':None,'max_drawdown':None,
                    'cost_bps_per_side':10,'overlapping_samples':True,'basis':'DISCOVERY_PRICE_TRACKING'}
            if len(after)>=horizon:
                window = after[:horizon]
                if all(day in stock and day in benchmark
                       and valid_price(stock[day]) and valid_price(benchmark[day])
                       and stock[day].get('quality')!='LEGACY_PROVENANCE_UNKNOWN'
                       and benchmark[day].get('quality')!='LEGACY_PROVENANCE_UNKNOWN' for day in window):
                    fee = .001
                    values = [1.]+[stock[day]['close']/stock[window[0]]['open']/(1+fee) for day in window]
                    values[-1] *= 1-fee
                    net = values[-1]-1
                    base = benchmark[window[-1]]['close']/benchmark[window[0]]['open']*(1-fee)/(1+fee)-1
                    curve = np.asarray(values)
                    item.update(status='MATURED',end_date=window[-1],**{'return':net,'benchmark_return':base,
                                'relative_return':(1+net)/(1+base)-1,'max_drawdown':float(np.min(curve/np.maximum.accumulate(curve)-1))})
                else:
                    item['status'] = 'DATA_WAIT'
            result.append(item)
    return result
