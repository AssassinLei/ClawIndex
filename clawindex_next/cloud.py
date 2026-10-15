"""Cloud preflight and role health checks; no side effects at import time."""
import argparse
import os
import sqlite3
from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone, timedelta

from .storage import Store


def sqlite_safe(version):
    return version >= (3,51,3) or (3,50,7) <= version < (3,51,0) or (3,44,6) <= version < (3,45,0)


def preflight():
    if os.environ.get('VNEXT_CLOUD') == 'true' and not sqlite_safe(sqlite3.sqlite_version_info):
        raise ValueError('云端需使用包含WAL修复的SQLite；请使用项目Docker镜像')
    db = Store(os.environ.get('VNEXT_DB_PATH','/data/quant_system_next.db'))
    if db.version != 2:
        raise ValueError('请停止服务，备份后升级数据库，再启动云端服务')
    return db


def healthy(db, role, instant=None):
    instant = instant or datetime.now(timezone.utc)
    with db.connect() as c:
        c.execute('SELECT id FROM users LIMIT 1')
        if role == 'worker':
            if not c.execute("SELECT name FROM leases WHERE name LIKE 'worker:%' AND expires_at>?",(instant.isoformat(),)).fetchone():
                return False
            progress = c.execute("SELECT value FROM settings WHERE key='worker_loop_completed'").fetchone()
            # First boot has a bounded grace for price backfill; steady-state loops
            # must report progress independently of the heartbeat.
            if progress and instant-datetime.fromisoformat(progress[0]) > timedelta(minutes=20):
                return False
    return True


def backup(db, output):
    path = Path(output).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb'):
        pass
    # SQLite's backup API includes committed WAL records. No raw live-file copy.
    with db.connect() as source, closing(sqlite3.connect(path)) as target:
        source.backup(target,pages=256,sleep=.01)
        integrity = target.execute('PRAGMA integrity_check').fetchone()[0]
        fk = target.execute('PRAGMA foreign_key_check').fetchall()
        if integrity != 'ok' or fk:
            raise ValueError('备份完整性检查失败，请保留原库')
    return {'backup':str(path),'integrity':integrity,'foreign_key_errors':0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('role',choices=['web','worker','preflight'])
    args = parser.parse_args()
    db = preflight()
    if not healthy(db,args.role):
        raise SystemExit(1)
    if args.role == 'web':
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:8502/_stcore/health',timeout=5) as response:
            if response.status != 200:
                raise SystemExit(1)


if __name__ == '__main__':
    main()
