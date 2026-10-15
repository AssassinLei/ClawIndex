"""Offline release evidence using a migrated copy, with no live APIs or messages."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from clawindex_next.engine import DEFAULT_SPEC
from clawindex_next.migration import file_hash
from clawindex_next.research import study
from clawindex_next.service import Evaluator
from clawindex_next.storage import Store,canonical,now


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db",required=True)
    p.add_argument("--source",required=True)
    p.add_argument("--output",required=True)
    args = p.parse_args()
    before = file_hash(args.source)
    db = Store(args.db)
    ev = Evaluator(db)
    report = {"generated_at":now(),"mode":"OFFLINE_MIGRATED_COPY","source_sha256_before":before,"migration":json.loads(db.rows("SELECT payload FROM migration_runs ORDER BY id DESC LIMIT 1")[0]["payload"]),"inspections":[],"research":[]}
    assignments = db.rows("SELECT * FROM assignments WHERE active=1 ORDER BY id")
    for a in assignments:
        sid,did = ev.evaluate(a["id"])
        signal = json.loads(db.rows("SELECT result FROM signal_runs WHERE id=?", (sid,))[0]["result"])
        decision = json.loads(db.rows("SELECT result FROM personal_decisions WHERE id=?", (did,))[0]["result"])
        report["inspections"].append({"code":a["code"],"market_as_of":signal["as_of"],"data_status":signal["data_status"],"state_label":signal["state_label"],"personal_status":decision["status"],"amount":decision["amount"]})
    seen = set()
    for a in assignments:
        if a["code"] in seen:
            continue
        seen.add(a["code"])
        try:
            manifest,_ = ev.inputs(a["code"])
            rid,research = study(db,a["user_id"],a["code"],manifest,DEFAULT_SPEC)
            primary = [r for r in research["results"] if r["spec"] == DEFAULT_SPEC and r["execution_delay_sessions"] == 1]
            report["research"].append({"code":a["code"],"research_id":rid,"research_usable":research["research_usable"],"parameter_trials":research["parameter_trials"],"scenario_trials":research["scenario_trials"],"primary_cost_scenarios":[{k:r[k] for k in ("cost_bps_per_side","evaluation_start","evaluation_end","strategy","buy_hold","trade_count","missing_session_count","comparison_status","splits")} for r in primary]})
            print(a["code"],"research saved",flush=True)
        except Exception as exc:
            report["research"].append({"code":a["code"],"status":"FAILED","error_type":type(exc).__name__})
            print(a["code"],"FAILED",type(exc).__name__,flush=True)
    report["source_sha256_after"] = file_hash(args.source)
    report["source_unchanged"] = before == report["source_sha256_after"]
    report["notifications_created"] = db.rows("SELECT count(*) n FROM outbox")[0]["n"]
    report["enabled_channels"] = db.rows("SELECT count(*) n FROM channels WHERE enabled=1")[0]["n"]
    with db.connect() as c:
        report["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
        report["foreign_key_errors"] = len(c.execute("PRAGMA foreign_key_check").fetchall())
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    assert report["source_unchanged"] and report["notifications_created"] == 0 and report["enabled_channels"] == 0
    print("release evidence saved",flush=True)


if __name__ == "__main__":
    main()
