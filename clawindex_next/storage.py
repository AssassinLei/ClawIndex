from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


SCHEMA = """
CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT, admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
CREATE TABLE instruments(code TEXT PRIMARY KEY, name TEXT NOT NULL, market TEXT NOT NULL, calendar TEXT NOT NULL, timezone TEXT NOT NULL, currency TEXT NOT NULL, asset_kind TEXT NOT NULL, publisher TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE strategy_versions(id TEXT PRIMARY KEY, spec TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE assignments(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), code TEXT NOT NULL REFERENCES instruments(code), strategy_id TEXT NOT NULL REFERENCES strategy_versions(id), stage TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, catchup INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX one_active_assignment ON assignments(user_id,code) WHERE active=1;
CREATE TABLE observations(id INTEGER PRIMARY KEY, code TEXT NOT NULL REFERENCES instruments(code), trade_date TEXT NOT NULL, payload TEXT NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL, available_at TEXT, availability_basis TEXT NOT NULL, row_hash TEXT NOT NULL, UNIQUE(code,trade_date,row_hash));
CREATE INDEX obs_date ON observations(code,trade_date,id);
CREATE TABLE datasets(id TEXT PRIMARY KEY, code TEXT NOT NULL REFERENCES instruments(code), as_of TEXT NOT NULL, manifest TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE feature_snapshots(id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id), algorithm TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE position_reports(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), code TEXT NOT NULL REFERENCES instruments(code), status TEXT NOT NULL, payload TEXT NOT NULL, reported_at TEXT NOT NULL);
CREATE TABLE budgets(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), code TEXT NOT NULL REFERENCES instruments(code), payload TEXT NOT NULL, reported_at TEXT NOT NULL);
CREATE TABLE product_mappings(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), code TEXT NOT NULL REFERENCES instruments(code), payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE signal_runs(id TEXT PRIMARY KEY, assignment_id INTEGER NOT NULL REFERENCES assignments(id), dataset_id TEXT NOT NULL REFERENCES datasets(id), feature_id TEXT NOT NULL REFERENCES feature_snapshots(id), as_of TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE personal_decisions(id TEXT PRIMARY KEY, signal_id TEXT NOT NULL REFERENCES signal_runs(id), user_id INTEGER NOT NULL REFERENCES users(id), report_id INTEGER REFERENCES position_reports(id), result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE current_signals(assignment_id INTEGER PRIMARY KEY REFERENCES assignments(id), signal_id TEXT NOT NULL REFERENCES signal_runs(id), as_of TEXT NOT NULL);
CREATE TABLE explanations(id INTEGER PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES personal_decisions(id), input_hash TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(decision_id,input_hash,model));
CREATE TABLE feedback(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), decision_id TEXT NOT NULL REFERENCES personal_decisions(id), status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE channels(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), url TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE outbox(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), channel_id INTEGER NOT NULL REFERENCES channels(id), decision_id TEXT NOT NULL REFERENCES personal_decisions(id), dedup_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT NOT NULL, claim_until TEXT, claim_owner TEXT);
CREATE TABLE delivery_attempts(id INTEGER PRIMARY KEY, outbox_id INTEGER NOT NULL REFERENCES outbox(id), status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE jobs(id TEXT PRIMARY KEY, assignment_id INTEGER NOT NULL REFERENCES assignments(id), status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, claim_owner TEXT, claim_until TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE leases(name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE health(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), component TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE research_runs(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), code TEXT NOT NULL REFERENCES instruments(code), payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE legacy_archive(id INTEGER PRIMARY KEY, table_name TEXT NOT NULL, legacy_user TEXT, row_hash TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE migration_runs(id INTEGER PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError("数据库不存在；请先运行 init 或 migrate，禁止启动时创建空库")
        with self.connect() as c:
            self.version = c.execute("PRAGMA user_version").fetchone()[0]
            if self.version not in (1,SCHEMA_VERSION):
                raise ValueError("数据库版本不符；不能将旧版数据库作为新版数据库")
            c.execute("SELECT id FROM users LIMIT 1")
            if self.version == 2:
                try:
                    for table in ("evolution_policies","evolution_runs","evolution_trials","evolution_candidates","evolution_observations","strategy_promotions","strategy_events","execution_confirmations"):
                        c.execute(f"SELECT * FROM {table} LIMIT 0")
                except sqlite3.DatabaseError as exc:
                    raise ValueError("自动进化表不完整，请检查升级或恢复配套备份") from exc

    @classmethod
    def create(cls, path):
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents accidentally replacing a production database.
        with path.open("xb"):
            pass
        c = sqlite3.connect(path)
        try:
            from .upgrades import EVOLUTION_SCHEMA
            c.executescript(SCHEMA+EVOLUTION_SCHEMA+"\nPRAGMA user_version=2;")
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("INSERT INTO settings VALUES('scheduler_enabled','false')")
            c.commit()
        finally:
            c.close()
        return cls(path)

    @contextmanager
    def connect(self, write=False):
        c = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        try:
            if write:
                c.execute("BEGIN IMMEDIATE")
            yield c
            if write:
                c.commit()
        except BaseException:
            if write:
                c.rollback()
            raise
        finally:
            c.close()

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def health(self, component, status, detail, user_id=None):
        with self.connect(True) as c:
            c.execute("INSERT INTO health(user_id,component,status,detail,created_at) VALUES(?,?,?,?,?)", (user_id,component,status,detail,now()))
