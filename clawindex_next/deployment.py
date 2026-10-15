"""Offline deployment checks and no-clobber import of a consistent Gen2 export."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from .storage import Store, canonical


def inspect_database(path):
    path = Path(path).resolve()
    if not path.exists():
        return {'state':'ABSENT'}
    if not path.is_file():
        raise ValueError('数据库路径不是文件')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('PRAGMA query_only=ON')
        if c.execute('PRAGMA user_version').fetchone()[0]!=2:
            raise ValueError('需要schema 2的新版数据库；旧版库请先迁移，不会自动覆盖或初始化')
        if c.execute('PRAGMA quick_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('数据库完整性或外键校验失败')
        for table in ('users','assignments','observations','settings','evolution_runs','evolution_candidates','execution_confirmations'):
            c.execute(f'SELECT * FROM {table} LIMIT 0')
    return {'state':'READY','schema':2}


def import_database(source, target):
    source,target = Path(source).resolve(),Path(target).resolve()
    if target.exists():
        raise FileExistsError('服务器已有数据库，禁止覆盖；日常更新请使用update')
    if inspect_database(source)['state']!='READY':
        raise ValueError('导入文件不存在')
    target.parent.mkdir(parents=True,exist_ok=True)
    staging = target.with_name(target.name+'.importing-'+uuid.uuid4().hex)
    try:
        with staging.open('xb'):
            pass
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as incoming, closing(sqlite3.connect(staging)) as outgoing:
            incoming.backup(outgoing,pages=256,sleep=.01)
        inspect_database(staging)
        Store(staging) # Validate the complete evolution schema used by the application.
        if hasattr(os,'chown'):
            owner = target.parent.stat()
            os.chown(staging,owner.st_uid,owner.st_gid)
        staging.chmod(0o600)
        # Publish on the same volume without replacing a concurrently created DB.
        os.link(staging,target)
    finally:
        staging.unlink(missing_ok=True)
    return {'state':'IMPORTED','schema':2}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['status','config','import'])
    parser.add_argument('--source')
    args = parser.parse_args()
    path = os.environ.get('VNEXT_DB_PATH','/data/quant_system_next.db')
    if args.command=='config':
        if not os.environ.get('TUSHARE_TOKEN','').strip():
            raise ValueError('请先在.env.cloud中填写TUSHARE_TOKEN；AI密钥可选')
        print('行情凭据已配置（未联网验证），未输出密钥')
    elif args.command=='status':
        result = inspect_database(path)
        print(canonical(result))
        if result['state']=='ABSENT':
            raise SystemExit(10)
    else:
        if not args.source:
            raise ValueError('请指定--source一致性备份文件')
        print(canonical(import_database(args.source,path)))


if __name__=='__main__':
    try:
        main()
    except (ValueError,OSError,sqlite3.Error) as exc:
        # Do not print connection strings, environment values or SQL payloads.
        print('部署检查失败：'+(str(exc) if isinstance(exc,(ValueError,FileExistsError)) else type(exc).__name__),file=sys.stderr)
        raise SystemExit(2)
