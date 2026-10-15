from pathlib import Path
import socket

import pytest

import start_next
from clawindex_next.local_runtime import classify,orphan_group


def record(pid,parent,role,at=100.):
    return {"pid":pid,"parent":parent,"role":role,"created":at}


def test_identity_requires_project_and_explicit_database(tmp_path):
    db = tmp_path/"next.db"
    assert classify(["python","-m","streamlit","run",str(tmp_path/"app_next.py")],tmp_path,tmp_path,db) == "web"
    assert classify(["python","-m","streamlit","run","different.py"],tmp_path,tmp_path,db) is None
    assert classify(["python","-m","clawindex_next","--db",str(db),"worker"],tmp_path,tmp_path,db) == "worker"
    assert classify(["python","-m","clawindex_next","--db","another.db","worker"],tmp_path,tmp_path,db) == "other_database"
    assert classify(["python","unrelated.py"],tmp_path,tmp_path,db) is None


def test_only_orphan_sibling_group_is_recoverable():
    rows = [record(10,99,"web"),record(11,10,"web"),record(20,99,"worker"),record(21,20,"worker")]
    assert [r["pid"] for r in orphan_group(rows,11)] == [10,20]
    assert not orphan_group(rows+[record(99,90,"launcher")],11)
    assert not orphan_group([record(10,99,"web"),record(20,98,"worker")],10)
    assert not orphan_group([record(10,99,"web"),record(20,99,"other_database")],10)
    assert not orphan_group([record(10,99,"web"),record(20,99,"worker",at=110.)],10)
    assert not orphan_group(rows,30)


def test_verified_recovery_rechecks_port_before_launch(seeded,monkeypatch,tmp_path):
    db,*_ = seeded
    listener = socket.socket()
    listener.bind(("127.0.0.1",0)); listener.listen()
    port = listener.getsockname()[1]
    def recover(root,path,asked_port):
        assert asked_port == port and Path(path) == db.path
        listener.close()
        return True
    monkeypatch.setattr("clawindex_next.local_runtime.recover_orphan",recover)
    monkeypatch.setattr(start_next,"ROOT",tmp_path)
    records = []
    class Child:
        def __init__(self,*args,**kwargs):
            self.pid=100+len(records); self.done=False; records.append(self)
        def poll(self): return 0 if self.done else None
        def terminate(self): self.done=True
        def wait(self,timeout=None): return 0
    monkeypatch.setattr(start_next,"stop_child",lambda p:p.terminate())
    def stop(_): raise KeyboardInterrupt()
    monkeypatch.setattr(start_next.time,"sleep",stop)
    with pytest.raises(KeyboardInterrupt): start_next.launch(db.path,port,popen=Child)
    assert len(records)==2 and all(p.done for p in records)


def test_pid_reuse_prevents_any_signal(seeded,monkeypatch):
    import psutil
    from clawindex_next.local_runtime import terminate_verified
    db,*_ = seeded
    class Reused:
        def create_time(self): return 200.
    monkeypatch.setattr(psutil,"Process",lambda _:Reused())
    with pytest.raises(RuntimeError,match="身份"):
        terminate_verified([record(100,99,"web",100.)],db.path.parent,db.path)


def test_only_original_system_console_child_is_allowed(tmp_path,monkeypatch):
    import os
    from clawindex_next.local_runtime import owned_child
    if os.name != "nt":
        pytest.skip("Windows console child")
    monkeypatch.setenv("SystemRoot",str(tmp_path/"Windows"))
    class Console:
        def cmdline(self): return ["conhost.exe","0x4"]
        def cwd(self): return str(tmp_path/"Windows")
        def name(self): return "conhost.exe"
        def ppid(self): return 10
        def create_time(self): return 100.1
        def exe(self): return str(tmp_path/"Windows"/"System32"/"conhost.exe")
    assert owned_child(Console(),record(10,99,"web"),tmp_path,tmp_path/"db")
    class WrongPath(Console):
        def exe(self): return str(tmp_path/"unknown"/"conhost.exe")
    assert not owned_child(WrongPath(),record(10,99,"web"),tmp_path,tmp_path/"db")
