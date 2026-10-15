"""Offline real-process startup check on a disposable database and free port."""
import pathlib
import socket
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

import start_next
from clawindex_next.auth import Auth
from clawindex_next.calendar import MarketCalendar
from clawindex_next.migration import insert_instrument,instrument
from clawindex_next.service import UserService
from clawindex_next.storage import Store,canonical,digest,now


def main():
    root = (ROOT/".pytest_cache").resolve()
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as work:
        path = pathlib.Path(work).resolve()
        assert path.is_relative_to(root)
        store = Store.create(path/"smoke.db")
        calendar = MarketCalendar()
        end = calendar.expected("XHKG")
        dates = calendar.sessions("XHKG",str(int(end[:4])-1)+"-01-01",end)[-160:]
        with store.connect(True) as c:
            c.execute("INSERT INTO settings VALUES('daily_sync_enabled','false')")
            insert_instrument(c,instrument("HKTECH","恒生科技"))
            for i,date in enumerate(dates):
                price = 100+i*.2
                row = {"date":date,"open":price,"high":price+1,"low":price-1,"close":price,"amount":None,"pe":None,"pb":None}
                c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)", ("HKTECH",date,canonical(row),"smoke",now(),"FIXTURE",digest(row)))
        user = UserService(store,Auth(store).login("smoke"))
        job = user.submit(user.assign("HKTECH"))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1",0))
            port = probe.getsockname()[1]
        original_sleep,started = time.sleep,time.monotonic()
        done = False
        def monitor(seconds):
            nonlocal done
            original_sleep(seconds)
            if done:
                return
            if time.monotonic()-started>30:
                done = True
                raise RuntimeError("combined startup timeout")
            if store.rows("SELECT status FROM jobs WHERE id=?", (job,))[0]["status"] != "SUCCEEDED":
                return
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health",timeout=1) as response:
                    if response.status != 200:
                        return
            except OSError:
                return
            done = True
            print("Real-process smoke: UI HTTP 200, queued inspection SUCCEEDED",flush=True)
            raise KeyboardInterrupt()
        start_next.time.sleep = monitor
        try:
            try:
                start_next.launch(store.path,port,True)
            except KeyboardInterrupt:
                pass
        finally:
            start_next.time.sleep = original_sleep
        with socket.socket() as probe:
            probe.bind(("127.0.0.1",port))
        assert store.rows("SELECT count(*) n FROM leases WHERE name LIKE 'worker:%'")[0]["n"] == 0
        assert store.rows("SELECT status FROM jobs WHERE id=?", (job,))[0]["status"] == "SUCCEEDED"
        print("Both services stopped, UI port released, worker heartbeat removed",flush=True)


if __name__ == "__main__":
    main()
