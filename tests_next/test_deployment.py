"""Deployment data preservation and host CLI tests, without Docker or live services."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from clawindex_next.deployment import import_database, inspect_database
from clawindex_next.storage import Store


ROOT = Path(__file__).resolve().parents[1]


def test_import_preserves_all_existing_gen2_data_and_source(seeded,tmp_path):
    db,a,b,aid,*_=seeded
    a.report_position('HKTECH','HELD')
    a.set_channel('https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder','test',mode='OFF')
    target=tmp_path/'cloud'/'next.db'
    assert inspect_database(target)=={'state':'ABSENT'}
    assert import_database(db.path,target)['state']=='IMPORTED'
    cloud=Store(target)
    for table in ('users','assignments','observations','position_reports','channels','settings'):
        assert cloud.rows(f'SELECT * FROM {table} ORDER BY rowid')==db.rows(f'SELECT * FROM {table} ORDER BY rowid')
    assert inspect_database(target)=={'state':'READY','schema':2}
    assert not cloud.rows('SELECT * FROM outbox')


def test_import_never_overwrites_existing_server_data(seeded,tmp_path):
    db,*_=seeded
    target=tmp_path/'existing.db'
    target.write_bytes(b'keep-server-data')
    with pytest.raises(FileExistsError):import_database(db.path,target)
    assert target.read_bytes()==b'keep-server-data'


def test_import_rejects_old_schema_without_creating_target(tmp_path):
    source=tmp_path/'old.db'
    with sqlite3.connect(source) as c:c.execute('CREATE TABLE old(id)')
    target=tmp_path/'next.db'
    with pytest.raises(ValueError,match='schema 2'):import_database(source,target)
    assert not target.exists()


def test_import_publish_race_does_not_clobber_another_database(seeded,tmp_path,monkeypatch):
    db,*_=seeded
    target=tmp_path/'racing.db'
    link=os.link
    def race(staging,destination):
        Path(destination).write_bytes(b'other-deployment')
        return link(staging,destination)
    monkeypatch.setattr('clawindex_next.deployment.os.link',race)
    with pytest.raises(FileExistsError):import_database(db.path,target)
    assert target.read_bytes()==b'other-deployment'
    assert not list(tmp_path.glob('*.importing-*'))


@pytest.fixture
def host_cli(tmp_path):
    bash=shutil.which('bash')
    if not bash:
        candidate=Path('C:/Program Files/Git/bin/bash.exe')
        bash=str(candidate) if candidate.exists() else None
    if not bash:pytest.skip('Bash not available on this host; Linux CI covers deploy.sh')
    shutil.copyfile(ROOT/'deploy.sh',tmp_path/'deploy.sh')
    (tmp_path/'.env.cloud').write_text('TUSHARE_TOKEN=test-only\n',encoding='utf-8')
    fake=tmp_path/'fake-host.sh'
    fake.write_text('''
flock() { return 0; }
git() {
  printf 'git %s\\n' "$*" >> "$EVENTS"
  case "$1" in
    branch) printf 'Gen2\\n';;
    rev-parse) printf '012345abcdef\\n';;
    status) printf '%s' "${DIRTY:-}";;
  esac
}
docker() {
  printf 'docker %s\\n' "$*" >> "$EVENTS"
  case "$*" in
    inspect*) printf 'clawindex:previous\\n';;
    *' ps -aq worker') printf 'test-worker\\n';;
    *'clawindex_next.deployment status') return "${DB_STATUS:-0}";;
    *' build worker') return "${BUILD_STATUS:-0}";;
  esac
  return 0
}
''',encoding='utf-8',newline='\n')
    events=tmp_path/'events.txt'
    def run(*args,**overrides):
        env={**os.environ,'BASH_ENV':fake.as_posix(),'EVENTS':events.as_posix(),**overrides}
        result=subprocess.run([bash,(tmp_path/'deploy.sh').as_posix(),*args],cwd=tmp_path,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
        return result,events.read_text(encoding='utf-8') if events.exists() else ''
    return run,tmp_path


def test_empty_deploy_initializes_then_starts_both_services(host_cli):
    run,_=host_cli
    result,events=run('install','--empty',DB_STATUS='10')
    assert result.returncode==0,result.stderr
    assert 'ops init' in events and 'up -d --wait --wait-timeout 180 worker web' in events
    assert events.index('ops init')<events.index('up -d')


def test_repeated_empty_deploy_refuses_to_replace_database(host_cli):
    run,_=host_cli
    result,events=run('install','--empty')
    assert result.returncode!=0 and 'ops init' not in events and 'up -d' not in events


def test_existing_deploy_refuses_import_without_overwriting(host_cli):
    run,path=host_cli
    source=path/'source.db'
    source.write_bytes(b'fake-consistent-export')
    result,events=run('install','--import-db',source.as_posix())
    assert result.returncode!=0 and 'deployment import' not in events


def test_update_backs_up_before_fetching_and_does_not_remove_volumes(host_cli):
    run,_=host_cli
    result,events=run('update')
    assert result.returncode==0,result.stderr
    assert events.index('ops backup')<events.index('git fetch')<events.index('build worker')<events.index('up -d')
    assert 'merge --ff-only origin/Gen2' in events
    assert 'down' not in events and 'ops init' not in events and 'reset' not in events


def test_failed_build_never_stops_running_services(host_cli):
    run,_=host_cli
    result,events=run('update',BUILD_STATUS='7')
    assert result.returncode!=0 and 'ops backup' in events
    assert 'up -d' not in events and ' stop ' not in events and 'down' not in events


def test_dirty_worktree_blocks_update_before_backup_or_fetch(host_cli):
    run,_=host_cli
    result,events=run('update',DIRTY=' M app_next.py')
    assert result.returncode!=0 and 'git fetch' not in events and 'ops backup' not in events


def test_new_admin_command_does_not_start_workers_or_notifications(host_cli):
    run,_=host_cli
    result,events=run('admin','测试账号')
    assert result.returncode==0,result.stderr
    assert 'ops create-user --admin -- 测试账号' in events
    assert 'up -d' not in events
