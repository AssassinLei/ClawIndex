"""Identify local processes by command, workspace, database and creation time."""
import os
from pathlib import Path
import subprocess
import time


def _same_path(value, target, cwd):
    try:
        path = Path(value)
        return (path if path.is_absolute() else Path(cwd)/path).resolve() == Path(target).resolve()
    except (ValueError,OSError):
        return False


def classify(args, cwd, root, db):
    """Do not identify a process solely by executable name or listening port."""
    root,db = Path(root).resolve(),Path(db).resolve()
    if any(_same_path(a,root/"app_next.py",cwd) for a in args) and "streamlit" in args and "run" in args:
        return "web"
    if "clawindex_next" in args and "worker" in args and _same_path(cwd,root,root):
        if "--db" in args and args.index("--db")+1 < len(args) and _same_path(args[args.index("--db")+1],db,cwd):
            return "worker"
        return "other_database"
    if any(_same_path(a,root/"start_next.py",cwd) for a in args):
        return "launcher"
    return None


def inventory(root, db):
    import psutil
    records = []
    for p in psutil.process_iter(["pid","ppid","name","create_time"]):
        if p.pid == os.getpid() or not (p.info["name"] or "").lower().startswith("python"):
            continue
        try:
            args,cwd = p.cmdline(),p.cwd()
            role = classify(args,cwd,root,db)
            if role:
                records.append({"pid":p.pid,"parent":p.ppid(),"created":p.create_time(),"role":role,"args":args,"cwd":cwd})
        except (psutil.Error,OSError):
            continue
    return records


def orphan_group(records, listener_pid):
    """Accept the old unregistered launcher only when web/worker are a sibling pair."""
    by_pid = {r["pid"]:r for r in records}
    web = by_pid.get(listener_pid)
    if not web or web["role"] != "web":
        return []
    # Windows virtualenv redirectors spawn a second interpreter with the same command.
    while web["parent"] in by_pid and by_pid[web["parent"]]["role"] == "web":
        web = by_pid[web["parent"]]
    parent = by_pid.get(web["parent"])
    if parent and parent["role"] == "launcher":
        return [] # A live supervisor must not be killed by a competing launcher.
    siblings = [r for r in records if r["parent"] == web["parent"] and r["role"] in ("worker","other_database") and abs(r["created"]-web["created"]) < 5]
    if len(siblings) != 1 or siblings[0]["role"] != "worker":
        return []
    return [web,siblings[0]]


def owned_child(child, record, root, db):
    if classify(child.cmdline(),child.cwd(),root,db) in ("web","worker"):
        return True
    # Hidden Windows processes may own a system console host. Accept only the
    # original, direct system child; never accept an arbitrary conhost by name.
    system = os.environ.get("SystemRoot")
    return bool(os.name == "nt" and system and child.name().lower() == "conhost.exe"
                and child.ppid() == record["pid"] and 0 <= child.create_time()-record["created"] < 5
                and Path(child.exe()).resolve() == (Path(system)/"System32"/"conhost.exe").resolve())


def terminate_verified(records, root, db):
    """Reverify before signalling, including PID-reuse protection."""
    import psutil
    owners = set()
    verified = []
    for record in records:
        try:
            p = psutil.Process(record["pid"])
            if p.create_time() != record["created"] or classify(p.cmdline(),p.cwd(),root,db) != record["role"]:
                raise RuntimeError("服务身份已变化，未停止任何未知进程")
            # Every descendant must be an identified web/worker, not an unrelated child.
            for child in p.children(recursive=True):
                if not owned_child(child,record,root,db):
                    raise RuntimeError("服务包含未确认子进程，请手动检查")
            if record["role"] == "worker" and "--owner" in record["args"]:
                owners.add(record["args"][record["args"].index("--owner")+1])
            verified.append(p)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise RuntimeError("无法核实旧服务身份或子进程，未停止未知进程") from None
    for p in verified:
        # Recheck immediately before the PID-based Windows tree operation.
        if p.create_time() != next(r["created"] for r in records if r["pid"] == p.pid):
            raise RuntimeError("进程编号已复用，停止操作取消")
        if os.name == "nt":
            result = subprocess.run(["taskkill","/PID",str(p.pid),"/T","/F"],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
            if result.returncode and p.is_running():
                raise RuntimeError("无法停止已确认的旧服务，请检查进程权限")
        else:
            children = p.children(recursive=True)
            p.terminate()
            for child in children:
                child.terminate()
            _,alive = psutil.wait_procs([p,*children],timeout=5)
            for child in alive:
                child.kill()
        psutil.wait_procs([p],timeout=5)
    return owners


def recover_orphan(root, db, port):
    import psutil
    records = inventory(root,db)
    try:
        listeners = {c.pid for c in psutil.net_connections(kind="tcp") if c.status == psutil.CONN_LISTEN and c.laddr.port == port}
    except psutil.Error:
        return False
    if len(listeners) != 1:
        return False
    group = orphan_group(records,next(iter(listeners)))
    if not group:
        return False
    owners = terminate_verified(group,root,db)
    from .storage import Store,now
    with Store(db).connect(True) as c:
        for owner in owners:
            c.execute("DELETE FROM leases WHERE name=? AND owner=?", ("worker:"+owner,owner))
            c.execute("UPDATE jobs SET claim_until=?,updated_at=? WHERE status='RUNNING' AND claim_owner=?", (now(),now(),owner))
    # Wait for the kernel to release the listener before the caller rechecks the port.
    for _ in range(30):
        current = {c.pid for c in psutil.net_connections(kind="tcp") if c.status == psutil.CONN_LISTEN and c.laddr.port == port}
        if not current:
            return True
        time.sleep(.1)
    raise RuntimeError("旧服务已停止，但端口尚未释放，请稍后重试")
