"""Read-only source snapshot -> disposable v2 upgrade -> full row-hash comparison."""
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile
from contextlib import closing

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from clawindex_next.storage import Store,canonical
from clawindex_next.upgrades import upgrade


def summary(path):
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as c:
        c.row_factory = sqlite3.Row
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {}
        for table in tables:
            h = hashlib.sha256()
            count = 0
            for row in c.execute('SELECT * FROM "'+table+'" ORDER BY rowid'):
                h.update((canonical(dict(row))+"\n").encode())
                count += 1
            result[table] = {"rows":count,"sha256":h.hexdigest()}
        return result


def main():
    source = pathlib.Path(sys.argv[1] if len(sys.argv)>1 else ROOT/"quant_system_next.db").resolve()
    cache = ROOT/".pytest_cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache) as directory:
        assert pathlib.Path(directory).resolve().is_relative_to(ROOT.resolve())
        copy = pathlib.Path(directory)/"copy.db"
        with closing(sqlite3.connect(source.as_uri()+"?mode=ro",uri=True)) as c, closing(sqlite3.connect(copy)) as d:
            c.backup(d)
        before = summary(copy)
        upgrade(Store(copy),pathlib.Path(directory)/"before.db")
        after = summary(copy)
        for table,value in before.items():
            if table != "settings":
                assert after[table] == value,table
        with Store(copy).connect() as c:
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
        print(canonical({"source_modified":False,"original_tables_verified":len(before),"rows_verified":sum(v["rows"] for k,v in before.items() if k!="settings"),"result":"PASS"}))


if __name__ == "__main__":
    main()
