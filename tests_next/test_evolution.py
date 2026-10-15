import json
import sqlite3
from datetime import datetime,timedelta,timezone
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from clawindex_next.engine import DEFAULT_SPEC, feature_frame, replay, validate_spec, personal_decision
from clawindex_next.evolution import Evolution, frozen_prefix
from clawindex_next.execution import confirm
from clawindex_next.service import Evaluator
from clawindex_next.storage import Store, SCHEMA, canonical, digest, now
from clawindex_next.upgrades import upgrade
from clawindex_next.validation import DEFAULT_POLICY, policy_config, gates, select_candidate
from clawindex_next.research import backtest
from conftest import FixtureCalendar,price_rows


T0 = datetime(2026,8,12,15,tzinfo=timezone.utc)


class Calendar(FixtureCalendar):
    day = "2026-08-12"
    def expected(self,*args,**kwargs):
        return self.day


def long_history(db):
    dates,rows = price_rows(800)
    with db.connect(True) as c:
        c.execute("DELETE FROM observations")
        for r in rows:
            c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)", ("HKTECH",r["date"],canonical(r),"fixture",now(),"FIXTURE",digest(r)))
    return dates,rows


def add_day(db,calendar,day,price=300):
    row = price_rows(1,end=day,prices=[price])[1][0]
    with db.connect(True) as c:
        c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)", ("HKTECH",day,canonical(row),"fixture",now(),"FIXTURE",digest(row)))
    calendar.day = day


def freeze(seeded,monkeypatch,auto=False):
    db,a,b,aid,baid = seeded
    long_history(db)
    pid = a.set_evolution("HKTECH",{"auto_promote":auto})
    calendar = Calendar()
    ev = Evolution(db,Evaluator(db,calendar))
    rid = ev.request(pid,T0)
    # Lifecycle tests force a historical winner; separate tests exercise the gates.
    monkeypatch.setattr("clawindex_next.evolution.select_candidate",lambda *_:({**DEFAULT_SPEC,"ma":90},{"reasons":[],"test_consumed":True}))
    run = db.rows("SELECT * FROM evolution_runs WHERE id=?", (rid,))[0]
    manifest = json.loads(run["manifest"])
    config = json.loads(run["payload"])
    report = backtest(manifest["data"],manifest["sessions"],evaluation_start=config["evaluation_start"])
    with db.connect(True) as c:
        for i,scenario in enumerate(config["plan"]):
            c.execute("INSERT INTO evolution_trials VALUES(?,?,?)", (rid,i,canonical({**report,"spec":scenario["spec"],"cost_bps_per_side":scenario["cost"],"execution_delay_sessions":scenario["delay"]})))
        c.execute("UPDATE evolution_runs SET cursor=? WHERE id=?", (len(json.loads(run["payload"])["plan"]),rid))
    ev.finish(rid,T0)
    return db,a,b,ev,calendar,rid,pid


def test_upgrade_is_explicit_backed_up_and_preserves_settings(tmp_path):
    path = tmp_path/"v1.db"
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
        c.execute("INSERT INTO settings VALUES('scheduler_enabled','true')")
        c.execute("INSERT INTO settings VALUES('daily_sync_enabled','false')")
    db = Store(path)
    assert db.version == 1
    assert not db.rows("SELECT name FROM sqlite_master WHERE name='evolution_runs'")
    result = upgrade(db,tmp_path/"backup.db")
    assert result["changed"] and Store(path).version == 2
    assert db.rows("SELECT value FROM settings WHERE key='daily_sync_enabled'")[0]["value"] == "false"
    with sqlite3.connect(tmp_path/"backup.db") as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not upgrade(db,tmp_path/"unused.db")["changed"]
    assert not (tmp_path/"unused.db").exists()


def test_upgrade_refuses_overwriting_backup(tmp_path):
    path = tmp_path/"old.db"
    with sqlite3.connect(path) as c: c.executescript(SCHEMA)
    backup = tmp_path/"existing.db"
    backup.write_bytes(b"keep")
    with pytest.raises(FileExistsError): upgrade(Store(path),backup)
    assert backup.read_bytes() == b"keep" and Store(path).version == 1


@pytest.mark.parametrize("value",[{"max_drawdown":float("nan")},{"ma_min":130},{"shadow_sessions":10},{"auto_promote":"true"},{"unknown":1}])
def test_policy_bounds(value):
    with pytest.raises(ValueError): policy_config(value,DEFAULT_SPEC)


def test_policy_and_results_are_user_scoped(seeded):
    db,a,b,aid,baid = seeded
    aid = a.set_evolution("HKTECH",{})
    assert a.evolution_policy("HKTECH")["id"] == aid
    assert b.evolution_policy("HKTECH") is None
    assert b.evolution_history("HKTECH") == []
    with pytest.raises(PermissionError): a.set_evolution("IXIC",{})


def test_sample_budget_dedup_and_frozen_data(seeded):
    db,a,b,aid,baid = seeded
    pid = a.set_evolution("HKTECH",{})
    ev = Evolution(db,Evaluator(db,Calendar()))
    with pytest.raises(ValueError,match="504"): ev.request(pid,T0)
    long_history(db)
    rid = ev.request(pid,T0)
    assert ev.request(pid,T0) == rid
    before = db.rows("SELECT manifest FROM evolution_runs WHERE id=?", (rid,))[0]["manifest"]
    add_day(db,Calendar(),"2026-08-13")
    ev.run_trial(T0)
    assert db.rows("SELECT manifest FROM evolution_runs WHERE id=?", (rid,))[0]["manifest"] == before
    assert db.rows("SELECT cursor FROM evolution_runs WHERE id=?", (rid,))[0]["cursor"] == 1
    with db.connect(True) as c:
        c.execute("UPDATE evolution_runs SET status='REJECTED' WHERE id=?", (rid,))
    with pytest.raises(ValueError,match="周期"): ev.request(pid,T0+timedelta(days=1))
    # Budget spans policy versions; replacing a policy cannot silently erase trials.
    pid = a.set_evolution("HKTECH",{"annual_trial_budget":18})
    with pytest.raises(ValueError,match="预算"): ev.request(pid,T0+timedelta(days=31))


def test_empty_cash_is_not_an_evolution_winner():
    candidate = {"max_drawdown":0.,"return":0.,"exposure":0.,"trades_year":0.,"trade_count":0}
    incumbent = {**candidate,"max_drawdown":-.3,"return":.15}
    reasons = gates(candidate,incumbent,{"return":.2},{"return":0.,"max_drawdown":0.},DEFAULT_POLICY,True)
    assert any("空仓" in r for r in reasons) and any("交易次数不足" in r for r in reasons)


def test_validation_cannot_select_runner_up_after_failed_test(monkeypatch):
    import clawindex_next.validation as module
    reports = [{"spec":{**DEFAULT_SPEC,"ma":ma},"cost_bps_per_side":10,"execution_delay_sessions":1,"curve":{"date":list(range(600))}} for ma in (90,120,150)]
    def segment(report,lo,hi,annual=252):
        ma = report["spec"]["ma"]
        dd = {90:.10,120:.12,150:.11}[ma]
        if lo >= 360 and ma == 90: dd = .30
        return {"max_drawdown":-dd,"return":.1,"exposure":.8,"trades_year":2,"trade_count":2},{"return":.12},{"return":.1,"max_drawdown":-.2}
    monkeypatch.setattr(module,"segment",segment)
    chosen,assessment = select_candidate(reports,DEFAULT_SPEC,DEFAULT_POLICY)
    assert chosen is None and assessment["selected_spec"]["ma"] == 90
    assert assessment["test_consumed"] and assessment["reasons"]


def test_forward_observations_are_daily_idempotent_and_delayed(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch)
    assert db.rows("SELECT status FROM evolution_candidates")[0]["status"] == "FORWARD_SHADOW"
    ev.observe(rid,T0)
    assert not db.rows("SELECT * FROM evolution_observations")
    add_day(db,calendar,"2026-08-13",300)
    instant = T0+timedelta(days=1)
    ev.observe(rid,instant)
    ev.observe(rid,instant)
    points = db.rows("SELECT * FROM evolution_observations")
    assert len(points) == 1 and points[0]["as_of"] == "2026-08-13"
    point = json.loads(points[0]["payload"])
    assert point["candidate_trades"] == 1 and point["candidate"] < 1
    assert json.loads(db.rows("SELECT state FROM evolution_candidates")[0]["state"])["count"] == 1
    assert not db.rows("SELECT * FROM strategy_promotions")


def test_skipped_market_day_does_not_get_backfilled(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch)
    add_day(db,calendar,"2026-08-13")
    add_day(db,calendar,"2026-08-14")
    ev.observe(rid,T0+timedelta(days=2))
    assert db.rows("SELECT status FROM evolution_candidates")[0]["status"] == "QUARANTINED"
    assert not db.rows("SELECT * FROM evolution_observations")


def test_revision_and_policy_changes_invalidate_frozen_candidate(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch)
    add_day(db,calendar,"2026-08-12",310)
    add_day(db,calendar,"2026-08-13")
    ev.observe(rid,T0+timedelta(days=1))
    assert db.rows("SELECT status FROM evolution_candidates")[0]["status"] == "QUARANTINED"
    a.set_evolution("HKTECH",{"enabled":False})
    assert not a.evolution_policy("HKTECH")["payload"]["config"]["enabled"]


def eligible(db,rid,calendar):
    payload = json.loads(db.rows("SELECT payload FROM evolution_candidates WHERE id=?", (rid,))[0]["payload"])
    days = [d.date().isoformat() for d in pd.bdate_range(start=pd.Timestamp(payload["frozen_as_of"])+pd.offsets.BDay(),periods=payload["policy"]["shadow_sessions"])]
    for day in days:
        add_day(db,calendar,day)
    with db.connect(True) as c:
        row = c.execute("SELECT payload,state FROM evolution_candidates WHERE id=?", (rid,)).fetchone()
        payload,state = json.loads(row[0]),json.loads(row[1])
        payload["forward_assessment"] = {"reasons":[],"fixed_horizon":True}
        state["count"] = payload["policy"]["shadow_sessions"]
        state["last_as_of"] = days[-1]
        for i,day in enumerate(days):
            point = {"candidate":1.12 if i==30 else 1.1,"incumbent":1.15 if i==30 else 1.,"hold":1.15 if i==30 else 1.1,"candidate_trades":2 if i==len(days)-1 else 1,"incumbent_trades":2 if i==len(days)-1 else 1,"candidate_exposure":1 if i<len(days)-1 else 0,"incumbent_exposure":1 if i<len(days)-1 else 0}
            c.execute("INSERT INTO evolution_observations VALUES(?,?,?)", (rid,day,canonical(point)))
        c.execute("UPDATE evolution_candidates SET status='ELIGIBLE',payload=?,state=? WHERE id=?", (canonical(payload),canonical(state),rid))
    return datetime.fromisoformat(days[-1]+"T15:00:00+00:00")


def test_promotion_needs_opt_in_and_preserves_user_context(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch)
    instant = eligible(db,rid,calendar)
    assert not ev.promote(rid,instant)
    with db.connect(True) as c:
        row = c.execute("SELECT payload FROM evolution_policies WHERE id=?", (pid,)).fetchone()
        policy = json.loads(row[0]); policy["config"]["auto_promote"] = True
        c.execute("UPDATE evolution_policies SET payload=? WHERE id=?", (canonical(policy),pid))
    a.report_position("HKTECH","HELD")
    before = db.rows("SELECT * FROM position_reports")
    assert ev.promote(rid,instant)
    assert not ev.promote(rid,instant)
    assert a.watchlist()[0]["strategy_id"] == validate_spec({**DEFAULT_SPEC,"ma":90})
    assert b.watchlist()[0]["strategy_id"] == validate_spec(DEFAULT_SPEC)
    assert db.rows("SELECT * FROM position_reports") == before
    assert len(db.rows("SELECT * FROM strategy_promotions")) == 1
    assert db.rows("SELECT status FROM jobs")[0]["status"] == "PENDING"


def test_candidate_cannot_promote_after_manual_strategy_change(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch,True)
    instant = eligible(db,rid,calendar)
    a.assign("HKTECH",{**DEFAULT_SPEC,"ma":150})
    assert not ev.promote(rid,instant)
    assert not db.rows("SELECT * FROM strategy_promotions")


def test_observation_uses_one_fixed_horizon_not_repeated_lucky_checks(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch)
    days = [d.date().isoformat() for d in pd.bdate_range(start="2026-08-13",periods=63)]
    # Fast lifecycle test uses a short ready feature provider but real executions.
    monkeypatch.setattr("clawindex_next.evolution.feature_frame",lambda *args,**kwargs:object())
    monkeypatch.setattr("clawindex_next.evolution.features_at",lambda frame,day:{"open":300.,"close":300.,"strategy_ready":True,"valid_price":True,"above":4,"below":0,"ma90_slope":.01,"ma120_slope":.01})
    for day in days:
        add_day(db,calendar,day)
        ev.observe(rid,datetime.fromisoformat(day+"T15:00:00+00:00"))
    row = db.rows("SELECT * FROM evolution_candidates")[0]
    assert row["status"] == "REJECTED" # no improvement and only one simulated fill
    assert len(db.rows("SELECT * FROM evolution_observations")) == 63
    add_day(db,calendar,(pd.Timestamp(days[-1])+pd.offsets.BDay()).date().isoformat())
    ev.observe(rid)
    assert len(db.rows("SELECT * FROM evolution_observations")) == 63


def test_confirmation_is_atomic_idempotent_and_not_an_inferred_fill(seeded):
    db,a,b,aid,baid = seeded
    aid = a.assign("HKTECH",catchup=True)
    a.report_position("HKTECH","EMPTY")
    a.set_budget("HKTECH",{"limit":"10000","used":"0","cash":"1000","currency":"CNY"})
    did = Evaluator(db,FixtureCalendar()).evaluate(aid)[1]
    a.confirm_execution(did,"SUBMITTED")
    assert a.position("HKTECH")["status"] == "EMPTY"
    before = len(db.rows("SELECT * FROM position_reports"))
    cid = a.confirm_execution(did,"EXECUTED",position="HELD")
    assert a.confirm_execution(did,"EXECUTED",position="HELD") == cid
    assert len(db.rows("SELECT * FROM position_reports")) == before+1
    assert a.position("HKTECH")["status"] == "HELD" and "shares" not in a.position("HKTECH")
    budget = json.loads(db.rows("SELECT payload FROM budgets ORDER BY id DESC LIMIT 1")[0]["payload"])
    assert budget["cash"] is None and budget["needs_confirmation"]
    with pytest.raises(PermissionError): b.confirm_execution(did,"EXECUTED")
    result = personal_decision({"data_status":"OK","exit_active":False,"model_state":"LONG","event":"BUY","entry_active":True},{"status":"EMPTY","reported_at":now()}, {**budget,"reported_at":now()})
    assert result["status"] == "CONTEXT_REQUIRED" and result["action"] is None


def test_stale_confirmation_cannot_overwrite_new_holdings(seeded):
    db,a,b,aid,baid = seeded
    aid = a.assign("HKTECH",catchup=True)
    a.report_position("HKTECH","EMPTY")
    did = Evaluator(db,FixtureCalendar()).evaluate(aid)[1]
    a.report_position("HKTECH","HELD",{"shares":12})
    with pytest.raises(ValueError,match="已更新"): a.confirm_execution(did,"EXECUTED",position="HELD")
    assert a.position("HKTECH")["shares"] == "12.000000"
    assert not db.rows("SELECT * FROM execution_confirmations")
    a.confirm_execution(did,"EXECUTED") # feedback remains possible without overwriting


def test_ui_evolution_is_chinese_and_requires_policy_confirmation(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    monkeypatch.setenv("VNEXT_DB_PATH",str(db.path))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app_next.py"),default_timeout=30).run()
    app.text_input[0].set_value("alice")
    next(x for x in app.button if x.label == "登录").click().run()
    app.sidebar.radio[0].set_value("策略与研究").run()
    assert not app.exception
    next(x for x in app.button if x.label == "保存自动进化配置").click().run()
    assert not a.evolution_policy("HKTECH") and app.error
    next(x for x in app.checkbox if x.label.startswith("我确认上述范围")).check()
    next(x for x in app.button if x.label == "保存自动进化配置").click().run()
    assert not app.exception and a.evolution_policy("HKTECH")
    assert not a.evolution_policy("HKTECH")["payload"]["config"]["auto_promote"]
    app.sidebar.radio[0].set_value("今日操作").run()
    assert not app.exception


def test_real_bounded_research_completes_without_auto_selecting_profit(seeded):
    db,a,b,aid,baid = seeded
    long_history(db)
    pid = a.set_evolution("HKTECH",{})
    ev = Evolution(db,Evaluator(db,Calendar()))
    rid = ev.request(pid,T0)
    for _ in range(60):
        if not ev.run_trial(T0): break
    run = db.rows("SELECT * FROM evolution_runs")[0]
    assert run["status"] == "REJECTED" # steady rise gives no drawdown improvement
    payload = json.loads(run["payload"])
    assert run["cursor"] == len(payload["plan"])
    assert payload["test_consumed"]
    assert len(db.rows("SELECT * FROM evolution_trials")) == len(payload["plan"])
    assert not db.rows("SELECT * FROM strategy_promotions")


def test_research_lease_recovery_and_failures_are_bounded(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    long_history(db)
    pid = a.set_evolution("HKTECH",{})
    ev = Evolution(db,Evaluator(db,Calendar()),"one")
    rid = ev.request(pid,T0)
    with db.connect(True) as c:
        c.execute("UPDATE evolution_runs SET status='RUNNING',claim_owner='other',claim_until=?", ((T0+timedelta(minutes=5)).isoformat(),))
    assert not ev.run_trial(T0)
    def broken(*_): raise ValueError("fixture failure")
    monkeypatch.setattr("clawindex_next.evolution.backtest",broken)
    for _ in range(5): ev.run_trial(T0+timedelta(minutes=6))
    run = db.rows("SELECT * FROM evolution_runs")[0]
    assert run["status"] == "FAILED" and run["attempts"] == 3
    assert not db.rows("SELECT * FROM evolution_trials")


def test_missing_evidence_cannot_trigger_promotion(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch,True)
    with db.connect(True) as c:
        row = c.execute("SELECT payload,state FROM evolution_candidates").fetchone()
        payload,state = json.loads(row[0]),json.loads(row[1])
        payload["forward_assessment"] = {"reasons":[]}
        state["count"] = 63
        c.execute("UPDATE evolution_candidates SET status='ELIGIBLE',payload=?,state=?", (canonical(payload),canonical(state)))
    assert not ev.promote(rid,T0)


def test_rollback_is_scoped_and_preserves_holdings(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch,True)
    instant = eligible(db,rid,calendar)
    assert ev.promote(rid,instant)
    a.report_position("HKTECH","HELD",{"shares":10})
    before = db.rows("SELECT * FROM position_reports")
    with pytest.raises(PermissionError): ev.rollback(rid,b.user["id"],instant)
    ev.rollback(rid,a.user["id"],instant)
    assert a.watchlist()[0]["strategy_id"] == validate_spec(DEFAULT_SPEC)
    assert a.evolution_policy("HKTECH") is None
    assert db.rows("SELECT * FROM position_reports") == before
    assert len(db.rows("SELECT * FROM strategy_events WHERE kind='ROLLBACK'")) == 1
    with pytest.raises(ValueError): ev.rollback(rid,a.user["id"],instant)


def test_auto_inspection_and_research_wait_for_user_jobs(seeded):
    from clawindex_next.worker import Worker
    db,a,b,aid,baid = seeded
    long_history(db)
    pid = a.set_evolution("HKTECH",{})
    worker = Worker(db,Evaluator(db,Calendar()),sender=lambda *_:pytest.fail("must not notify"))
    worker.once()
    assert len(db.rows("SELECT * FROM jobs")) == 1
    assert not db.rows("SELECT * FROM evolution_trials")
    worker.once()
    assert db.rows("SELECT status FROM jobs")[0]["status"] == "SUCCEEDED"
    assert len(db.rows("SELECT * FROM evolution_trials")) == 1
    assert not db.rows("SELECT * FROM jobs WHERE assignment_id=?", (baid,))


def test_new_policy_cancels_old_experiment_and_annual_budget_remains(seeded):
    db,a,b,aid,baid = seeded
    long_history(db)
    pid = a.set_evolution("HKTECH",{})
    ev = Evolution(db,Evaluator(db,Calendar()))
    rid = ev.request(pid,T0)
    a.set_evolution("HKTECH",{"enabled":False})
    assert db.rows("SELECT status FROM evolution_runs WHERE id=?", (rid,))[0]["status"] == "CANCELLED"
    assert not ev.run_trial(T0)


def test_ui_quick_execution_updates_only_explicitly_confirmed_state(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    aid = a.assign("HKTECH",catchup=True)
    a.report_position("HKTECH","EMPTY")
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    monkeypatch.setenv("VNEXT_DB_PATH",str(db.path))
    monkeypatch.setattr("clawindex_next.service.UserService.market_status",lambda *_:{"expected":"2026-08-12","latest":"2026-08-12"})
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app_next.py"),default_timeout=30).run()
    app.text_input[0].set_value("alice")
    next(x for x in app.button if x.label=="登录").click().run()
    next(x for x in app.selectbox if x.label=="处理结果").set_value("EXECUTED")
    next(x for x in app.checkbox if x.label=="同时确认成交后的真实持仓状态").check()
    next(x for x in app.button if x.label=="保存处理结果").click().run()
    assert not app.exception
    assert a.position("HKTECH")["status"] == "HELD"
    assert b.position("HKTECH")['basis'] == 'DEFAULT_EMPTY'


def test_promotion_cannot_change_unapproved_confirmation_rule(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch,True)
    instant = eligible(db,rid,calendar)
    with db.connect(True) as c:
        c.execute("UPDATE evolution_candidates SET payload=json_set(payload,'$.spec.confirmation',10)")
    assert not ev.promote(rid,instant)


def test_submission_suppresses_waiting_notification_without_changing_holdings(seeded):
    from test_research_delivery import enable
    from clawindex_next.worker import Worker
    db,a,b,aid,baid = seeded
    aid = enable(db,a)
    a.report_position("HKTECH","EMPTY")
    a.set_channel("https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder","fixture",True)
    evaluator = Evaluator(db,FixtureCalendar())
    did = evaluator.evaluate(aid)[1]
    a.confirm_execution(did,"SUBMITTED")
    sent = []
    assert Worker(db,evaluator,sender=lambda *args:sent.append(args)).deliver()
    assert not sent and a.position("HKTECH")["status"] == "EMPTY"
    assert db.rows("SELECT status FROM outbox")[0]["status"] == "EXPIRED"


def test_upgrade_refuses_live_worker_lease_before_creating_backup(tmp_path):
    path = tmp_path/"v1.db"
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
        c.execute("INSERT INTO leases VALUES('worker:live','live',?)", ((datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),))
    with pytest.raises(ValueError,match="租约"): upgrade(Store(path),tmp_path/"backup.db")
    assert not (tmp_path/"backup.db").exists() and Store(path).version == 1
