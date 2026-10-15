"""Offline visual QA. All prices are synthetic; no worker, API or real channels."""
import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from clawindex_next.auth import Auth
from clawindex_next.calendar import MarketCalendar
from clawindex_next.migration import insert_instrument, instrument
from clawindex_next.radar import BENCHMARK, publish, snapshot, universe, write
from clawindex_next.storage import Store, canonical, digest, now


def seed(path):
    store = Store.create(path)
    Auth(store).provision('机会体验',True)
    calendar = MarketCalendar()
    instant = datetime.now(timezone.utc)
    day = calendar.expected('XSHG',instant,210)
    dates = calendar.sessions('XSHG',str(int(day[:4])-3)+'-01-01',day)[-400:]
    items = [instrument(code,name,{'industry_name':name,'level':level,'src':'SW2021','industry_code':industry,'parent_code':parent})
             for code,name,level,industry,parent in [('801010.SI','示例·行业甲','L1','110000',None),('801020.SI','示例·行业乙','L1','120000',None),
                                                   ('801030.SI','示例·行业丙','L1','130000',None),('801011.SI','示例·甲子行业','L2','110100','110000'),
                                                   ('801021.SI','示例·乙子行业','L2','120100','120000')]]
    with store.connect(True) as c:
        c.execute("INSERT INTO settings VALUES('daily_sync_enabled','false')")
        for j,item in enumerate([BENCHMARK,*items]):
            insert_instrument(c,item)
            rng = np.random.default_rng(100+j)
            prices = 100*np.exp(np.cumsum((.0007 if j==0 else .0012+j*.0002)+rng.normal(0,.002,len(dates))))
            for i,(date,price) in enumerate(zip(dates,prices)):
                row = {'date':date,'open':float(price),'high':float(price)+1,'low':float(price)-1,'close':float(price),'amount':1000.,'pe':30-i*.02,'pb':3-i*.002,'quality':'UI_FIXTURE'}
                c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,'UI_FIXTURE',?,'FIXTURE',?)",(item['code'],date,canonical(row),now(),digest(row)))
        c.execute("INSERT INTO leases VALUES('worker:radar-ui-fixture','UI_FIXTURE',?)",((instant+timedelta(hours=3)).isoformat(),))
    write(store,'radar:enabled',True)
    publish(store,snapshot(store,universe(store),calendar,instant),instant)
    return store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8877)
    args = parser.parse_args()
    work = ROOT/'.pytest_cache'
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='radar-ui-',dir=work) as directory:
        path = Path(directory)/'preview.db'
        seed(path)
        env = {**os.environ,'VNEXT_DB_PATH':str(path),'VNEXT_CLOUD':'false','STREAMLIT_BROWSER_GATHER_USAGE_STATS':'false','TUSHARE_TOKEN':'','VNEXT_LLM_API_KEY':''}
        print(f'Synthetic radar preview on port {args.port}',flush=True)
        process = subprocess.Popen([sys.executable,'-m','streamlit','run','app_next.py','--server.port',str(args.port),'--server.address','127.0.0.1','--server.headless','true'],cwd=ROOT,env=env)
        try:
            process.wait()
        finally:
            process.terminate()
            process.wait(timeout=15)


if __name__=='__main__':
    main()
