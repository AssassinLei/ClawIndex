import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta

import pytest

from clawindex_next.auth import Auth
from clawindex_next.engine import DEFAULT_SPEC
from clawindex_next.explanations import explain, validate_explanation
from clawindex_next.service import Evaluator
from clawindex_next.storage import Store,canonical,digest,now
from clawindex_next.worker import Worker
from conftest import FixtureCalendar


def test_fail_closed_wrong_or_missing_database(tmp_path):
    with pytest.raises(ValueError): Store(tmp_path/"missing.db")
    import sqlite3
    with sqlite3.connect(tmp_path/"legacy.db") as c: c.execute("create table users(x)")
    with pytest.raises(ValueError): Store(tmp_path/"legacy.db")


def test_auth_ownership_and_invalidation(seeded):
    db,a,b,aid,baid = seeded
    with pytest.raises(PermissionError): b.submit(aid)
    with pytest.raises(PermissionError): b.unwatch(aid)
    with pytest.raises(PermissionError): b.set_scheduler(True)
    with pytest.raises(ValueError): Auth(db).login("   ")
    Auth(db).provision("alice",True)
    with pytest.raises(PermissionError): a.watchlist()


def test_append_only_positions_and_no_weight_guess(seeded):
    db,a,b,aid,baid = seeded
    a.report_position("HKTECH","HELD",{"target_completion":.6})
    a.report_position("HKTECH","EMPTY")
    assert len(db.rows("SELECT * FROM position_reports WHERE user_id=?", (a.user["id"],))) == 2
    assert a.position("HKTECH")["status"] == "EMPTY"
    assert b.position("HKTECH")['basis'] == 'DEFAULT_EMPTY'
    with pytest.raises(ValueError): a.report_position("HKTECH","UNKNOWN",{"market_value":"100","currency":"CNY"})


def test_fractional_fund_shares_are_not_rounded_to_currency_precision(seeded):
    db,a,b,aid,baid = seeded
    a.report_position("HKTECH","HELD",{"shares":"1234.123456"})
    assert a.position("HKTECH")["shares"] == "1234.123456"
    with pytest.raises(ValueError): a.report_position("HKTECH","EMPTY",{"shares":"0.001"})


def test_job_idempotency_workers_and_lease_recovery(seeded):
    db,a,b,aid,baid = seeded
    def submit(): return a.submit(aid)
    with ThreadPoolExecutor(4) as pool: ids = list(pool.map(lambda _:submit(),range(8)))
    assert len(set(ids)) == 1 and len(db.rows("SELECT * FROM jobs")) == 1
    ev = Evaluator(db,FixtureCalendar())
    workers = [Worker(db,ev,owner=str(i)) for i in range(2)]
    with ThreadPoolExecutor(2) as pool: processed = list(pool.map(lambda w:w.run_job(),workers))
    assert sum(processed) == 1
    assert db.rows("SELECT status FROM jobs")[0]["status"] == "SUCCEEDED"
    assert len(db.rows("SELECT * FROM signal_runs")) == 1
    assert workers[0].lease("test",instant=datetime(2026,1,1,tzinfo=timezone.utc))
    assert not workers[1].lease("test",instant=datetime(2026,1,1,tzinfo=timezone.utc))
    assert workers[1].lease("test",instant=datetime(2026,1,2,tzinfo=timezone.utc))


def test_shared_market_personal_context_isolated_and_feedback_not_position(seeded):
    db,a,b,aid,baid = seeded
    a.report_position("HKTECH","HELD")
    ev = Evaluator(db,FixtureCalendar())
    s1,d1 = ev.evaluate(aid)
    s2,d2 = ev.evaluate(baid)
    assert json.loads(a.results()[0]["decision"])["action"] == "HOLD"
    decision = json.loads(b.results()[0]["decision"])
    assert decision['action'] == 'WAIT' and decision['position_basis'] == 'DEFAULT_EMPTY'
    assert len(db.rows("SELECT * FROM datasets")) == 1
    a.feedback(d1,"EXECUTED","人工确认")
    assert a.position("HKTECH")["status"] == "HELD"
    with pytest.raises(PermissionError): b.feedback(d1,"EXECUTED")
    with pytest.raises(PermissionError): b.explanation(d1)
    with pytest.raises(ValueError): a.feedback(d1,"DEFERRED","")
    a.unwatch(aid)
    assert len(a.history()) == 1 and len(db.rows("SELECT * FROM observations")) == 160


@pytest.mark.parametrize("raw",["not json",'{"summary":"持有"}', '{"action":"SELL"}', '{"summary":"建议卖出","evidence_refs":[],"limitations":[],"counterarguments":[],"scenarios":[]}'])
def test_llm_failures_do_not_mutate_signal(seeded,raw):
    db,a,b,aid,baid = seeded
    sid,did = Evaluator(db,FixtureCalendar()).evaluate(aid)
    before = db.rows("SELECT result FROM signal_runs WHERE id=?", (sid,))[0]["result"]
    result = explain(db,did,lambda payload:raw,"fake")
    assert "指数状态" in result["summary"]
    assert db.rows("SELECT status FROM explanations WHERE model='fake'")[0]["status"] == "FAILED_TEMPLATE"
    assert db.rows("SELECT result FROM signal_runs WHERE id=?", (sid,))[0]["result"] == before


def test_revised_observation_new_version_old_evidence_preserved(seeded):
    db,a,b,aid,baid = seeded
    ev = Evaluator(db,FixtureCalendar())
    first,_ = ev.evaluate(aid)
    r = db.rows("SELECT * FROM observations ORDER BY id DESC LIMIT 1")[0]
    payload = json.loads(r["payload"])
    payload.update(close=70.,open=70.,low=69.,high=71.)
    with db.connect(True) as c:
        c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)", (r["code"],r["trade_date"],canonical(payload),"revision",now(),"FIXTURE",digest(payload)))
    second,_ = ev.evaluate(aid)
    assert second != first
    assert len(db.rows("SELECT * FROM signal_runs")) == 2
    assert len(db.rows("SELECT * FROM datasets")) == 2


def test_future_factor_and_unavailable_observation_do_not_leak(seeded):
    db,a,b,aid,baid = seeded
    r = db.rows("SELECT * FROM observations ORDER BY id DESC LIMIT 1")[0]
    payload = json.loads(r["payload"])
    payload.update(pe=9999.)
    with db.connect(True) as c:
        c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,available_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?,?)", (r["code"],r["trade_date"],canonical(payload),"future",now(),"2099-01-01T00:00:00+00:00","FIXTURE",digest(payload)))
    manifest,_ = Evaluator(db,FixtureCalendar()).inputs("HKTECH")
    assert manifest["data"][-1]["pe"] == 20.


def test_unvalidated_rules_cannot_enable(seeded):
    db,a,b,aid,baid = seeded
    with pytest.raises(ValueError): a.assign("HKTECH",stage="ENABLED")
    with pytest.raises(ValueError): a.assign("HKTECH",{**DEFAULT_SPEC,"custom_rule":"eval('sell')"})


def test_model_configuration_changes_invalidate_explanation_cache(seeded,monkeypatch):
    import clawindex_next.adapters as module
    db,a,b,aid,baid = seeded
    _,did = Evaluator(db,FixtureCalendar()).evaluate(aid)
    calls=[]
    def provider(payload):
        calls.append(payload)
        return canonical({"summary":"规则证据说明","evidence_refs":["close"],"limitations":["未来未知"],"counterarguments":[],"scenarios":[]})
    monkeypatch.setattr(module,"llm_explanation",provider)
    monkeypatch.setenv("VNEXT_LLM_MODEL","model-one")
    a.explanation(did,True)
    a.explanation(did,True)
    assert len(calls) == 1
    monkeypatch.setenv("VNEXT_LLM_MODEL","model-two")
    a.explanation(did,True)
    assert len(calls) == 2
