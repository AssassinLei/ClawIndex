"""Launch the actual UI with isolated, synthetic data and no external services.

Run: python scripts/preview_next_ui.py --port 8876
This is a visual QA fixture, not a deployment entry point.
"""
import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clawindex_next.auth import Auth
from clawindex_next.calendar import MarketCalendar
from clawindex_next.migration import insert_instrument, instrument
from clawindex_next.service import Evaluator, UserService
from clawindex_next.storage import Store, canonical, digest, now


def seed(path):
    db = Store.create(path)
    auth = Auth(db)
    auth.provision("界面体验", True)
    svc = UserService(db, auth.login("界面体验"))
    calendar = MarketCalendar()
    end = calendar.expected("XHKG")
    dates = calendar.sessions("XHKG",str(int(end[:4])-2)+"-01-01",end)[-400:]
    with db.connect(True) as c:
        c.execute("INSERT INTO settings VALUES('daily_sync_enabled','false')")
        for code,name in (("HKTECH","恒生科技"),("801730.SI","电力设备"),("801880.SI","汽车")):
            item = instrument(code,name,{'industry_name':name,'level':'L1','src':'SW2021'} if code.endswith('.SI') else None)
            # All fixtures share a fixed calendar for reproducible visual checks.
            item['calendar'] = 'XHKG'
            insert_instrument(c,item)
            for i,date in enumerate(dates):
                price = 100+i*.2
                if code == '801730.SI' and i>=len(dates)-20:
                    price -= (i-(len(dates)-20))*2
                row = dict(date=date,open=price,high=price+1,low=price-1,close=price,amount=1000.,pe=20.,pb=2.)
                c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)", (code,date,canonical(row),'UI_FIXTURE',now(),'FIXTURE',digest(row)))
    for code in ('HKTECH','801730.SI','801880.SI'):
        aid = svc.assign(code,catchup=code=='HKTECH')
        if code=='801730.SI':
            svc.report_position(code,'HELD')
        Evaluator(db).evaluate(aid)
    # Explicitly labeled synthetic heartbeat: no worker, channels, API calls or trades.
    with db.connect(True) as c:
        c.execute("INSERT INTO leases VALUES('worker:ui-fixture','UI_FIXTURE',?)",((datetime.now(timezone.utc)+timedelta(hours=3)).isoformat(),))
    return db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8876)
    args = parser.parse_args()
    cache = ROOT/'.pytest_cache'
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ui-preview-',dir=cache) as work:
        path = Path(work)/'preview.db'
        seed(path)
        env = {**os.environ,'VNEXT_DB_PATH':str(path),'VNEXT_CLOUD':'false','STREAMLIT_BROWSER_GATHER_USAGE_STATS':'false',
               'TUSHARE_TOKEN':'','VNEXT_LLM_API_KEY':''}
        print(f"Synthetic preview: http://127.0.0.1:{args.port} ; account: 界面体验",flush=True)
        process = subprocess.Popen([sys.executable,'-m','streamlit','run','app_next.py','--server.port',str(args.port),'--server.address','127.0.0.1','--server.headless','true'],cwd=ROOT,env=env)
        try:
            process.wait()
        finally:
            process.terminate()
            process.wait(timeout=15)


if __name__=='__main__':
    main()
