"""Read-only live capability probe; writes counts only, never credentials or accounts."""
import json
import sys
from datetime import datetime,timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from clawindex_next.adapters import ProviderError, TushareAdapter
from clawindex_next.calendar import grace_for
from clawindex_next.factors import TECH_FIELDS
from clawindex_next.storage import Store, canonical


def main():
    load_dotenv(ROOT/'.env.next')
    import os
    db = Store(os.environ.get('VNEXT_DB_PATH',str(ROOT/'quant_system_next.db')))
    watches = db.rows("SELECT DISTINCT i.* FROM instruments i JOIN assignments a ON a.code=i.code WHERE a.active=1 AND i.market='CN' AND i.code LIKE '%.SI' ORDER BY i.code")
    result = {'at':datetime.now(timezone.utc).isoformat(),'read_only':True,'results':[]}
    if not watches:
        result['detail'] = '没有可探测的国内行业指数'
    else:
        try:
            adapter = TushareAdapter()
            item = watches[0]
            end = adapter.calendar.expected(item['calendar'],grace_minutes=grace_for(item))
            start = (pd.Timestamp(end)-pd.Timedelta(days=7)).date().isoformat()
            for endpoint,fields in [('sw_daily','ts_code,trade_date,open,high,low,close,pe,pb,amount'),('idx_factor_pro','ts_code,trade_date,'+','.join(TECH_FIELDS))]:
                try:
                    frame = adapter._call(endpoint,ts_code=item['code'],start_date=start.replace('-',''),end_date=end.replace('-',''),fields=fields)
                    result['results'].append({'code':item['code'],'endpoint':endpoint,'status':'OK' if frame is not None and not frame.empty else 'EMPTY',
                                             'rows':len(frame) if frame is not None else 0,'columns':len(frame.columns) if frame is not None else 0,
                                             'latest':str(frame.trade_date.max()) if frame is not None and not frame.empty else None})
                except ProviderError as exc:
                    result['results'].append({'code':item['code'],'endpoint':endpoint,'status':str(exc).split(':')[0]})
        except ProviderError as exc:
            result['detail'] = str(exc).split(':')[0]
    output = ROOT/'reports/2026-10-13-factor-access-probe.json'
    output.write_text(canonical(result),encoding='utf-8')
    print(canonical(result))


if __name__ == '__main__':
    main()
