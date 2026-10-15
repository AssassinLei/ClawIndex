"""Offline Compose acceptance on an exclusive disposable database, never production."""
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from clawindex_next.auth import Auth
from clawindex_next.calendar import MarketCalendar
from clawindex_next.migration import insert_instrument, instrument
from clawindex_next.service import UserService
from clawindex_next.storage import Store, canonical, digest, now
from clawindex_next.tasks import enqueue


def main():
    path = Path(os.environ['VNEXT_DB_PATH'])
    role = sys.argv[1]
    if role == 'prepare':
        db = Store.create(path)
        calendar = MarketCalendar()
        end = calendar.expected('XHKG')
        dates = calendar.sessions('XHKG',str(int(end[:4])-1)+'-01-01',end)[-180:]
        with db.connect(True) as c:
            c.execute("INSERT INTO settings VALUES('daily_sync_enabled','false')")
            insert_instrument(c,instrument('HKTECH','云端离线烟测'))
            for i,day in enumerate(dates):
                price = 100+i*.1
                row = {'date':day,'open':price,'high':price+1,'low':price-1,'close':price}
                c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',('HKTECH',day,canonical(row),'offline_smoke',now(),'FIXTURE',digest(row)))
        svc = UserService(db,Auth(db).login('offline-smoke'))
        enqueue(db,svc.assign('HKTECH'),refresh=False)
        print('Offline smoke database prepared; sync and channels disabled')
    elif role == 'submit':
        db = Store(path)
        aid = db.rows('SELECT id FROM assignments WHERE active=1')[0]['id']
        enqueue(db,aid,refresh=False)
        print('Offline job queued')
    elif role == 'check':
        db = Store(path)
        for _ in range(180): # Includes the 5-minute claim recovery after SIGKILL.
            jobs = db.rows('SELECT status FROM jobs')
            if jobs and all(j['status'] == 'SUCCEEDED' for j in jobs):
                with urllib.request.urlopen('http://web:8502/_stcore/health',timeout=5) as response:
                    assert response.status == 200
                assert db.rows('SELECT COUNT(*) n FROM personal_decisions')[0]['n'] > 0
                assert db.rows('SELECT COUNT(*) n FROM delivery_attempts')[0]['n'] == 0
                print('PASS: UI HTTP 200, queued jobs complete, decisions persisted, no notifications')
                return
            time.sleep(2)
        raise RuntimeError('Compose smoke timed out')
    elif role == 'stopped':
        db = Store(path)
        assert not db.rows("SELECT name FROM leases WHERE name LIKE 'worker:%' AND expires_at>?",(now(),))
        print('PASS: graceful shutdown released worker heartbeat')


if __name__ == '__main__':
    main()
