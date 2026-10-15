"""Run the UI and durable worker together; stop only the children we started."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

from dotenv import load_dotenv

from clawindex_next.storage import Store

ROOT = Path(__file__).resolve().parent


def stop_child(child):
    if child.poll() is not None:
        return
    if os.name == "nt":
        # venv python.exe can be a redirector that launches another interpreter.
        # Terminating only the redirector leaves the actual service running.
        subprocess.run(["taskkill","/PID",str(child.pid),"/T","/F"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,timeout=15,check=False)
    else:
        child.terminate()
    if child.poll() is None:
        child.terminate()


def launch(db_path, port=8502, headless=False, popen=subprocess.Popen):
    db_path = Path(db_path).resolve()
    Store(db_path)
    # Recover only verified orphan siblings; never kill an unknown port owner.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1",port))
        except OSError:
            from clawindex_next.local_runtime import recover_orphan
            print(f"端口{port}正在使用，正在核验是否为本项目遗留服务…",flush=True)
            try:
                recovered = recover_orphan(ROOT,db_path,port)
            except ImportError:
                raise OSError("缺少本地进程管理依赖，请重新安装 requirements-next-tested.txt") from None
            if not recovered:
                raise OSError(f"端口{port}正在使用，未启动重复后台。可以先打开 http://127.0.0.1:{port}；若是其他程序，请使用 --port 指定其他端口。未停止未知进程或仍由启动器管理的服务。") from None
            print("已清理本项目的遗留页面与后台，正在重新启动。",flush=True)
            probe.bind(("127.0.0.1",port))
    env = {**os.environ,"VNEXT_DB_PATH":str(db_path),"PYTHONUNBUFFERED":"1"}
    flags = {"creationflags":subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    (ROOT/"logs").mkdir(exist_ok=True)
    children,logs = [],[]
    owner = "launcher-"+uuid.uuid4().hex
    try:
        for name,command in (
            ("worker",[sys.executable,"-m","clawindex_next","--db",str(db_path),"worker","--owner",owner]),
            ("ui",[sys.executable,"-m","streamlit","run",str(ROOT/"app_next.py"),"--server.address","127.0.0.1","--server.port",str(port),"--server.headless",str(headless).lower(),"--browser.gatherUsageStats","false"]),
        ):
            log = (ROOT/"logs"/f"vnext-{name}.log").open("a",encoding="utf-8")
            logs.append(log)
            children.append(popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,**flags))
        print(f"新版已启动：http://127.0.0.1:{port}；页面和后台一起运行。",flush=True)
        print("输入原账号名登录。请保留此窗口；按 Ctrl+C 停止页面与后台。",flush=True)
        while True:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError("页面或后台已退出。请查看 logs/vnext-ui.log 和 logs/vnext-worker.log。")
            time.sleep(1)
    finally:
        for child in reversed(children):
            stop_child(child)
        for child in reversed(children):
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for log in logs:
            log.close()
        if children:
            with Store(db_path).connect(True) as c:
                c.execute("DELETE FROM leases WHERE name=? AND owner=?", ("worker:"+owner,owner))


def main():
    load_dotenv(ROOT/".env.next")
    parser = argparse.ArgumentParser(description="同时启动ClawIndex新版页面与后台")
    parser.add_argument("--db",default=os.environ.get("VNEXT_DB_PATH",str(ROOT/"quant_system_next.db")))
    parser.add_argument("--port",type=int,default=8502)
    parser.add_argument("--headless",action="store_true")
    args = parser.parse_args()
    try:
        launch(args.db,args.port,args.headless)
    except KeyboardInterrupt:
        print("页面与后台已停止。")
    except (ValueError,OSError,RuntimeError) as exc:
        print(f"启动未完成：{exc}",file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
