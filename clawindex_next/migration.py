"""Offline, append-only migration. Source connection is always read-only."""
import hashlib
import json
import sqlite3
from pathlib import Path

from .engine import DEFAULT_SPEC, clean_number, validate_spec
from .storage import Store, canonical, digest, now


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""):
            h.update(block)
    return h.hexdigest()


def instrument(code, name, industry=None):
    industry = industry or {}
    if industry:
        industry = {**industry,**{key:(industry.get(key) if isinstance(industry.get(key),str) else None) for key in ('parent_code','industry_code')}}
    if code.endswith(".SI") and industry:
        return dict(code=code,name=industry["industry_name"],market="CN",calendar="XSHG",timezone="Asia/Shanghai",currency="CNY",asset_kind="EQUITY_SECTOR_INDEX",publisher="申万",metadata={"source":"legacy_industry_list", "level":industry.get("level"),"source_version":industry.get("src"),"industry_code":industry.get('industry_code'),"parent_code":industry.get('parent_code'),"identity_verified":True,"legacy_name":name,"tradable":False})
    if code in ("HKTECH","IXIC"):
        hk = code == "HKTECH"
        return dict(code=code,name="恒生科技" if hk else "纳斯达克综合",market="HK" if hk else "US",calendar="XHKG" if hk else "XNYS",timezone="Asia/Hong_Kong" if hk else "America/New_York",currency="HKD" if hk else "USD",asset_kind="EQUITY_INDEX",publisher="Hang Seng" if hk else "Nasdaq",metadata={"source":"provider_code_registry_v1","identity_verified":True,"tradable":False,"us_calendar_basis":"NYSE common sessions; verify Nasdaq exceptional closures" if not hk else None})
    return dict(code=code,name=name,market="UNKNOWN",calendar="UNKNOWN",timezone="UTC",currency="UNKNOWN",asset_kind="UNKNOWN",publisher="UNKNOWN",metadata={"source":"legacy_unverified","identity_verified":False,"tradable":False})


def insert_instrument(c, item):
    c.execute("INSERT OR IGNORE INTO instruments VALUES(?,?,?,?,?,?,?,?,?)",tuple(item[k] for k in ("code","name","market","calendar","timezone","currency","asset_kind","publisher"))+(canonical(item["metadata"]),))


def migrate(source, target=None, dry_run=False):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("源库不存在")
    if target and Path(target).resolve() == source:
        raise ValueError("源库和目标库必须不同")
    if target and Path(target).exists() and not dry_run:
        raise FileExistsError("目标库已存在；迁移不会覆盖它")
    before = file_hash(source)
    original = sqlite3.connect(source.as_uri()+"?mode=ro",uri=True)
    original.execute("PRAGMA query_only=ON")
    snapshot = sqlite3.connect(":memory:")
    snapshot.row_factory = sqlite3.Row
    try:
        original.backup(snapshot)
    finally:
        original.close()
    try:
        tables = [r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        expected = {"users","fund_pool","daily_market_data","idx_factor_data","inspection_log","user_position","industry_list","webhook_urls","app_settings","fund_custom_prompts"}
        if not expected <= set(tables):
            raise ValueError("不是受支持的旧版生产数据库")
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("源库完整性检查失败")
        data = {t:[dict(r) for r in snapshot.execute('SELECT * FROM "'+t+'"')] for t in tables}
        counts = {t:len(rows) for t,rows in data.items()}
        from .storage import SCHEMA_VERSION
        summary = {"source_sha256":before,"source_counts":counts,"schema_version":SCHEMA_VERSION,"positions_basis":"TARGET_COMPLETION_NOT_PORTFOLIO_WEIGHT","legacy_prompts":"ARCHIVED_NOT_EXECUTED","legacy_replay":"INPUT_VERSION_AND_AVAILABILITY_UNKNOWN","notifications_enabled":False,"source_consistent_snapshot":True}
        if dry_run:
            return summary
        if not target:
            raise ValueError("需要明确的目标路径")
        target = Path(target).resolve()
        staging = target.with_name(target.name+".migrating")
        db = Store.create(staging)
        strategy_id = validate_spec(DEFAULT_SPEC)
        stamp = now()
        with db.connect(True) as c:
            c.execute("INSERT INTO strategy_versions VALUES(?,?,?)", (strategy_id,canonical(DEFAULT_SPEC),stamp))
            for r in data["users"]:
                c.execute("INSERT INTO users(username,created_at) VALUES(?,?)", (r["username"],r["created_at"]))
            users = {r["username"]:r["id"] for r in c.execute("SELECT * FROM users")}
            for t,rows in data.items():
                c.executemany("INSERT INTO legacy_archive(table_name,legacy_user,row_hash,payload) VALUES(?,?,?,?)", [(t,r.get("username"),digest(r),canonical(r)) for r in rows])
            industries = {r["index_code"]:r for r in data["industry_list"]}
            names = {r["fund_code"]:r["fund_name"] for r in data["inspection_log"]+data["fund_pool"]}
            for r in data["daily_market_data"]:
                names.setdefault(r["fund_code"],r["name"] or r["fund_code"])
            for code,name in names.items():
                insert_instrument(c,instrument(code,name,industries.get(code)))
            for r in data["fund_pool"]:
                c.execute("INSERT INTO assignments(user_id,code,strategy_id,stage,created_at) VALUES(?,?,?,?,?)", (users[r["username"]],r["fund_code"],strategy_id,"RESEARCH",stamp))
            for r in data["user_position"]:
                payload = {"target_completion":r["position_ratio"],"unrealized_pnl_ratio":r["unrealized_pnl_ratio"],"basis":"LEGACY_MANUAL_UNCONFIRMED","status":"UNKNOWN"}
                c.execute("INSERT INTO position_reports(user_id,code,status,payload,reported_at) VALUES(?,?,?,?,?)", (users[r["username"]],r["fund_code"],"UNKNOWN",canonical(payload),r["updated_at"]+"T00:00:00+08:00"))
            observations = []
            for r in data["daily_market_data"]:
                # Unknown old amount/risk-free/valuation provenance is preserved, not used as verified features.
                row = {"date":r["trade_date"],**{k:clean_number(r[k+"_price"]) for k in ("open","high","low","close")},"amount":None,"pe":None,"pb":None,"legacy_amount":r["amount"],"legacy_pe":r["pe"],"legacy_pb":r["pb"],"amount_unit":"UNKNOWN","quality":"LEGACY_PROVENANCE_UNKNOWN"}
                observations.append((r["fund_code"],r["trade_date"],canonical(row),"legacy_snapshot",stamp,None,"UNKNOWN_HISTORICAL_RELEASE",digest(row)))
            c.executemany("INSERT INTO observations(code,trade_date,payload,source,fetched_at,available_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?,?)", observations)
            for r in data["webhook_urls"]:
                c.execute("INSERT INTO channels(user_id,url,label,enabled) VALUES(?,?,?,0)", (users[r["username"]],r["url"],r["label"] or ""))
            for r in data["app_settings"]:
                c.execute("INSERT INTO settings VALUES(?,?)", ("legacy:"+r["key"],r["value"]))
            for t,rows in data.items():
                archived = list(c.execute("SELECT row_hash FROM legacy_archive WHERE table_name=?", (t,)))
                if sorted(r[0] for r in archived) != sorted(digest(r) for r in rows):
                    raise ValueError("逐行迁移校验失败："+t)
            summary["archive_verified"] = True
            summary["source_sha256_after"] = file_hash(source)
            # A concurrent writer may legitimately change the main file; record it, never claim immutability.
            summary["source_main_file_unchanged"] = summary["source_sha256_after"] == before
            c.execute("INSERT INTO migration_runs(payload,created_at) VALUES(?,?)", (canonical(summary),stamp))
        with db.connect() as c:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or c.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("目标库完整性或外键校验失败；保留迁移暂存库供检查")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.execute("PRAGMA journal_mode=DELETE")
        # Atomic no-clobber rename on Windows. No source file is moved or removed.
        if target.exists():
            raise FileExistsError("目标路径在迁移期间被创建；保留暂存库")
        staging.rename(target)
        with Store(target).connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
        return summary
    finally:
        snapshot.close()
