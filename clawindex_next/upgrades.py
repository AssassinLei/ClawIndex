"""Explicit, backed-up schema upgrade. Application startup never migrates."""
import sqlite3
from contextlib import closing
from pathlib import Path

EVOLUTION_SCHEMA = """
CREATE TABLE evolution_policies(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), code TEXT NOT NULL REFERENCES instruments(code), active INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX current_evolution_policy ON evolution_policies(user_id,code) WHERE active=1;
CREATE TABLE evolution_runs(id TEXT PRIMARY KEY, policy_id TEXT NOT NULL REFERENCES evolution_policies(id), assignment_id INTEGER NOT NULL REFERENCES assignments(id), status TEXT NOT NULL, manifest TEXT NOT NULL, payload TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, claim_owner TEXT, claim_until TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE evolution_trials(run_id TEXT NOT NULL REFERENCES evolution_runs(id), scenario INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,scenario));
CREATE TABLE evolution_candidates(id TEXT PRIMARY KEY REFERENCES evolution_runs(id), status TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE evolution_observations(candidate_id TEXT NOT NULL REFERENCES evolution_candidates(id), as_of TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(candidate_id,as_of));
CREATE TABLE strategy_promotions(id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE REFERENCES evolution_candidates(id), user_id INTEGER NOT NULL REFERENCES users(id), code TEXT NOT NULL, old_assignment INTEGER NOT NULL REFERENCES assignments(id), new_assignment INTEGER NOT NULL REFERENCES assignments(id), payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE strategy_events(id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES evolution_candidates(id), user_id INTEGER NOT NULL REFERENCES users(id), code TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE execution_confirmations(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), decision_id TEXT NOT NULL REFERENCES personal_decisions(id), request_key TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(user_id,request_key));
"""


def upgrade(store, backup_path):
    """Return a manifest; require a new backup path and serialize DDL in one transaction."""
    from .storage import now
    target = Path(backup_path).resolve()
    if target == store.path:
        raise ValueError("备份不能覆盖当前数据库")
    if store.version == 2:
        return {"version":2,"changed":False}
    def ensure_quiet(c):
        running = c.execute("SELECT name FROM leases WHERE expires_at>? AND (name LIKE 'worker:%' OR name LIKE 'market_sync:%' OR name IN ('scheduler','evolution')) LIMIT 1", (now(),)).fetchone()
        task = c.execute("SELECT id FROM jobs WHERE status='RUNNING' AND claim_until>? LIMIT 1", (now(),)).fetchone()
        delivery = c.execute("SELECT id FROM outbox WHERE status='SENDING' AND claim_until>? LIMIT 1", (now(),)).fetchone()
        if running or task or delivery:
            raise ValueError("后台或任务仍有有效租约，请先停止服务后再升级；不会强制结束进程")
    with store.connect() as c:
        ensure_quiet(c)
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open("xb"):
        pass
    try:
        with store.connect() as source, closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or dest.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("升级备份校验失败")
        with store.connect(True) as c:
            ensure_quiet(c)
            if c.execute("PRAGMA user_version").fetchone()[0] == 1:
                for statement in EVOLUTION_SCHEMA.split(";"):
                    if statement.strip():
                        c.execute(statement)
                c.execute("PRAGMA user_version=2")
                c.execute("INSERT INTO settings VALUES('schema_upgrade_v2',?)", (now(),))
        store.version = 2
        return {"version":2,"changed":True,"backup":str(target)}
    except BaseException:
        # Preserve the backup for diagnosis; never delete an existing user's file.
        raise
