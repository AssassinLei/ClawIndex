import json
from pathlib import Path

import pytest

from clawindex_next.migration import file_hash,migrate
from clawindex_next.storage import Store,digest


SOURCE = Path(__file__).resolve().parents[1]/"quant_system20261012.db"


@pytest.mark.skipif(not SOURCE.exists(),reason="生产快照是本地私有输入，不加入Git")
def test_production_migration_preserves_every_row_and_source(tmp_path):
    before = file_hash(SOURCE)
    plan = migrate(SOURCE,dry_run=True)
    target = tmp_path/"next.db"
    result = migrate(SOURCE,target)
    assert before == file_hash(SOURCE) == result["source_sha256_after"]
    db = Store(target)
    counts = {r["table_name"]:r["n"] for r in db.rows("SELECT table_name,count(*) n FROM legacy_archive GROUP BY table_name")}
    assert counts == plan["source_counts"]
    assert counts["inspection_log"] == 275
    assert len(db.rows("SELECT * FROM position_reports WHERE status='UNKNOWN'")) == 10
    assert len(db.rows("SELECT * FROM assignments WHERE active=1")) == 11
    assert len(db.rows("SELECT * FROM users WHERE password_hash IS NULL")) == 4
    assert all(r["row_hash"] == digest(json.loads(r["payload"])) for r in db.rows("SELECT row_hash,payload FROM legacy_archive"))
    assert db.rows("SELECT count(*) n FROM channels WHERE enabled=1")[0]["n"] == 0
    assert db.rows("SELECT value FROM settings WHERE key='scheduler_enabled'")[0]["value"] == "false"
    gold = db.rows("SELECT * FROM instruments WHERE code='850531.SI'")[0]
    assert gold["asset_kind"] == "EQUITY_SECTOR_INDEX"
    assert not db.rows("SELECT * FROM outbox")
    with db.connect() as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(FileExistsError): migrate(SOURCE,target)
    with pytest.raises(ValueError): migrate(SOURCE,SOURCE)
