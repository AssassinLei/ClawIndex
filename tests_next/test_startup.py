import socket

import pytest

import start_next
from clawindex_next.storage import now
from clawindex_next.worker import Worker,worker_status


def test_manual_rescue_scoped_to_account_and_no_sync_or_notification(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    a.submit(aid)
    b.submit(baid)
    monkeypatch.setattr('clawindex_next.service.Evaluator.evaluate',lambda *args:None)
    def forbidden(*args): raise AssertionError("手动处理不应同步或投递通知")
    monkeypatch.setattr(Worker,"schedule",forbidden)
    monkeypatch.setattr(Worker,"deliver",forbidden)
    assert a.process_pending() == 1
    assert db.rows("SELECT status FROM jobs WHERE assignment_id=?", (aid,))[0]["status"] == "SUCCEEDED"
    assert db.rows("SELECT status FROM jobs WHERE assignment_id=?", (baid,))[0]["status"] == "PENDING"


def test_worker_heartbeat_visibility_and_expiry(seeded):
    db,a,b,aid,baid = seeded
    assert not worker_status(db)
    worker = Worker(db)
    worker.pulse()
    assert worker_status(db)
    with db.connect(True) as c: c.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00'")
    assert not worker_status(db)


def test_launcher_passes_same_database_and_stops_children(seeded,monkeypatch,tmp_path):
    db,a,b,aid,baid = seeded
    monkeypatch.setattr(start_next,"ROOT",tmp_path)
    records=[]
    class Child:
        def __init__(self,command,**kwargs):
            self.command,self.kwargs,self.stopped = command,kwargs,False
            self.pid = len(records)+100
            records.append(self)
        def poll(self): return 0 if self.stopped else None
        def terminate(self): self.stopped = True
        def wait(self,timeout=None): return 0
    def stop(_): raise KeyboardInterrupt()
    monkeypatch.setattr(start_next.time,"sleep",stop)
    monkeypatch.setattr(start_next,"stop_child",lambda child:child.terminate())
    with pytest.raises(KeyboardInterrupt): start_next.launch(db.path,port=0,popen=Child)
    assert len(records) == 2 and all(c.stopped for c in records)
    assert "worker" in records[0].command and "streamlit" in records[1].command
    assert all(c.kwargs["env"]["VNEXT_DB_PATH"] == str(db.path) for c in records)
    assert records[0].command[records[0].command.index("--db")+1] == str(db.path)


def test_launcher_port_in_use_does_not_start_worker(seeded):
    db,a,b,aid,baid = seeded
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0)); listener.listen()
        port = listener.getsockname()[1]
        def forbidden(*args,**kwargs): raise AssertionError("端口冲突不应启动任何进程")
        with pytest.raises(OSError): start_next.launch(db.path,port,popen=forbidden)
