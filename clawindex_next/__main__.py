import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from .auth import Auth
from .engine import DEFAULT_SPEC, validate_spec
from .migration import migrate
from .service import Evaluator
from .storage import Store, canonical, now


def main(argv=None):
    p = argparse.ArgumentParser(description="ClawIndex VNext 离线管理；永不自动改写旧库")
    p.add_argument("--db",default=str(Path(__file__).resolve().parent.parent/"quant_system_next.db"))
    sub = p.add_subparsers(dest="command",required=True)
    sub.add_parser("init")
    m = sub.add_parser("migrate")
    m.add_argument("--source",required=True)
    m.add_argument("--dry-run",action="store_true")
    u = sub.add_parser("create-user",help="创建账号或设置账号管理员权限，无需密码")
    u.add_argument("username")
    u.add_argument("--admin",action="store_true")
    w = sub.add_parser("worker")
    w.add_argument("--once",action="store_true")
    w.add_argument("--owner",help=argparse.SUPPRESS)
    sub.add_parser("doctor")
    upgrade_parser = sub.add_parser("upgrade",help="备份后显式升级自动进化表结构；请先停止服务")
    upgrade_parser.add_argument("--backup",required=True)
    backup_parser = sub.add_parser('backup',help='一致性备份到新文件，可在服务运行时执行')
    backup_parser.add_argument('--output',required=True)
    i = sub.add_parser("inspect")
    i.add_argument("--assignment",type=int)
    r = sub.add_parser("research")
    r.add_argument("--user",required=True)
    r.add_argument("--code",required=True)
    e = sub.add_parser("export-feedback")
    e.add_argument("--user",required=True)
    e.add_argument("--output",required=True)
    args = p.parse_args(argv)
    if args.command == "migrate":
        print(canonical(migrate(args.source,args.db,args.dry_run)))
        return
    if args.command == "init":
        db = Store.create(args.db)
        with db.connect(True) as c:
            c.execute("INSERT INTO strategy_versions VALUES(?,?,?)", (validate_spec(DEFAULT_SPEC),canonical(DEFAULT_SPEC),now()))
        print("新版空库已创建；输入账号名即可登录并创建账号，请从可信源登记标的")
        return
    db = Store(args.db)
    if args.command == "upgrade":
        from .upgrades import upgrade
        print(canonical(upgrade(db,args.backup)))
    elif args.command == 'backup':
        from .cloud import backup
        print(canonical(backup(db,args.output)))
    elif args.command == "create-user":
        Auth(db).provision(args.username,args.admin)
        print("账号权限已保存，原会话已失效；没有启用通知或调度")
    elif args.command == "worker":
        from .worker import Worker
        worker = Worker(db,owner=args.owner or f"pid-{os.getpid()}-{uuid.uuid4().hex}")
        if args.once:
            print(canonical(worker.once()))
        else:
            import signal
            signal.signal(signal.SIGTERM,lambda *_:worker.stopping.set())
            signal.signal(signal.SIGINT,lambda *_:worker.stopping.set())
            worker.run()
    elif args.command == "inspect":
        ids = [args.assignment] if args.assignment else [r["id"] for r in db.rows("SELECT id FROM assignments WHERE active=1")]
        for aid in ids:
            try:
                signal,decision = Evaluator(db).evaluate(aid)
                print(canonical({"assignment":aid,"signal":signal,"decision":decision}))
            except Exception as exc:
                db.health("inspection","FAILED",f"assignment {aid}: {type(exc).__name__}")
                print(canonical({"assignment":aid,"status":"FAILED","reason_type":type(exc).__name__}))
    elif args.command == "doctor":
        with db.connect() as c:
            import sqlite3
            print(canonical({"version":c.execute("PRAGMA user_version").fetchone()[0],"sqlite_version":sqlite3.sqlite_version,"python_version":sys.version.split()[0],"integrity":c.execute("PRAGMA integrity_check").fetchone()[0],"foreign_key_errors":len(c.execute("PRAGMA foreign_key_check").fetchall()),"users":c.execute("SELECT count(*) FROM users").fetchone()[0],"active_watchlists":c.execute("SELECT count(*) FROM assignments WHERE active=1").fetchone()[0],"scheduler_enabled":c.execute("SELECT value FROM settings WHERE key='scheduler_enabled'").fetchone()[0],"enabled_channels":c.execute("SELECT count(*) FROM channels WHERE enabled=1").fetchone()[0]}))
    elif args.command in ("research","export-feedback"):
        users = db.rows("SELECT * FROM users WHERE username=?", (args.user,))
        if not users:
            raise ValueError("用户不存在")
        uid = users[0]["id"]
        if args.command == "research":
            from .research import study
            if not db.rows("SELECT id FROM assignments WHERE user_id=? AND code=?", (uid,args.code)):
                raise ValueError("不是该用户的监控")
            manifest,_ = Evaluator(db).inputs(args.code)
            rid,report = study(db,uid,args.code,manifest)
            print(canonical({"research_id":rid,"scenario_trials":report["scenario_trials"],"status":"RESEARCH"}))
        else:
            payload = {"positions":db.rows("SELECT code,status,payload,reported_at FROM position_reports WHERE user_id=?", (uid,)),"feedback":db.rows("SELECT decision_id,status,payload,created_at FROM feedback WHERE user_id=?", (uid,))}
            with Path(args.output).open("x",encoding="utf-8") as f:
                f.write(canonical(payload))
            print("新增持仓和执行反馈已导出；含个人资料，请妥善保存")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent/".env.next")
        main()
    except (ValueError,FileExistsError,PermissionError) as exc:
        print(str(exc),file=sys.stderr)
        sys.exit(2)
