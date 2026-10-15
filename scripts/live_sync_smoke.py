"""Actual provider integration on an isolated database; no worker or channels."""
import json
import os
import sys
import tempfile
from datetime import datetime,timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from clawindex_next.adapters import ProviderError, TushareAdapter
from clawindex_next.calendar import grace_for
from clawindex_next.data_maintenance import latest_rows, valid_price
from clawindex_next.factors import TECH_FIELDS
from clawindex_next.migration import insert_instrument
from clawindex_next.storage import Store, canonical


def main():
    load_dotenv(ROOT/'.env.next')
    source = Store(os.environ.get('VNEXT_DB_PATH',str(ROOT/'quant_system_next.db')))
    item = source.rows("SELECT DISTINCT i.* FROM instruments i JOIN assignments a ON a.code=i.code WHERE a.active=1 AND i.market='CN' AND i.code LIKE '%.SI' ORDER BY i.code LIMIT 1")[0]
    item['metadata'] = json.loads(item['metadata'])
    adapter = TushareAdapter()
    end = adapter.calendar.expected(item['calendar'],grace_minutes=grace_for(item))
    start = (pd.Timestamp(end)-pd.Timedelta(days=45)).date().isoformat()
    cache = (ROOT/'.pytest_cache').resolve()
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache) as folder:
        path = Path(folder).resolve()
        assert path.is_relative_to(cache)
        db = Store.create(path/'live-sync.db')
        with db.connect(True) as c:
            insert_instrument(c,item)
        for _ in range(2):
            adapter.sync(db,item['code'],start=start)
        rows = latest_rows(db,item['code'])
        latest = rows[end]
        expected = adapter.calendar.sessions(item['calendar'],min(rows),end)
        result = {'at':datetime.now(timezone.utc).isoformat(),'isolated_database':True,'code':item['code'],
                  'latest':end,'price_sessions':len(rows),'missing_sessions':len(set(expected)-set(rows)),
                  'valid_price_sessions':sum(valid_price(r) for r in rows.values()),
                  'latest_professional_fields':sum(latest.get('factors',{}).get(k) is not None for k in TECH_FIELDS),
                  'amount_unit':latest['amount_unit'],'factor_source':latest.get('factor_source'),
                  'users':db.rows('SELECT COUNT(*) n FROM users')[0]['n'],
                  'notifications':db.rows('SELECT COUNT(*) n FROM delivery_attempts')[0]['n'],
                  'integrity':db.rows('PRAGMA integrity_check')[0]['integrity_check']}
        assert result['missing_sessions'] == 0 and result['valid_price_sessions'] == result['price_sessions']
        assert result['latest_professional_fields'] == 78 and result['users'] == 0 and result['notifications'] == 0
    (ROOT/'reports/2026-10-13-live-sync-smoke.json').write_text(canonical(result),encoding='utf-8')
    print(canonical(result))


if __name__ == '__main__':
    try:
        main()
    except ProviderError as exc:
        print(str(exc).split(':')[0],file=sys.stderr)
        raise SystemExit(1)
