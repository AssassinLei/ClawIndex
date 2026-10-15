import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from clawindex_next.adapters import ProviderError
from clawindex_next.explanations import explain
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical, digest, now
from clawindex_next.syncing import MarketSync, rate_limit
from clawindex_next.tasks import request_for
from clawindex_next.worker import Worker
from conftest import FixtureCalendar, price_rows
from test_ui import button


class FakeProvider:
    def __init__(self):
        self.calls = []

    def sync(self, store, code, start=None, instant=None):
        self.calls.append((code,start))
        return 160


def test_manual_refresh_and_ai_pipeline_reuses_shared_market_but_keeps_users(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    provider = FakeProvider()
    a.report_position("HKTECH","HELD")
    ajob = a.submit(aid,refresh=True,include_ai=True)
    bjob = b.submit(baid,refresh=True)
    calls = []
    def ai(payload):
        calls.append(payload)
        return canonical({"summary":"趋势证据解读","evidence_refs":["close"],"limitations":["未来未知"],"counterarguments":["可能错过反弹"],"scenarios":[]})
    monkeypatch.setattr("clawindex_next.adapters.llm_explanation",ai)
    worker = Worker(db,Evaluator(db,FixtureCalendar()),provider=provider)
    assert worker.run_job() and worker.run_job()
    assert len(provider.calls) == 1 and len(calls) == 1
    assert {r["status"] for r in db.rows("SELECT status FROM jobs")} == {"SUCCEEDED"}
    assert len(a.jobs([ajob,bjob])) == 1 and len(b.jobs([ajob,bjob])) == 1
    assert a.saved_explanation(a.results()[0]["decision_id"])["status"] == "MODEL_OK"
    assert json.loads(a.results()[0]["decision"])["action"] == "HOLD"
    assert json.loads(b.results()[0]["decision"])['position_basis'] == 'DEFAULT_EMPTY'
    assert not db.rows("SELECT * FROM delivery_attempts")


def test_refresh_clicks_deduplicated_and_completed_request_can_run_again(seeded):
    db,a,b,aid,baid = seeded
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda _:a.submit(aid,refresh=True),range(8)))
    assert len(set(ids)) == 1
    Worker(db,Evaluator(db,FixtureCalendar()),provider=FakeProvider()).run_job()
    assert a.submit(aid,refresh=True) != ids[0]
    with pytest.raises(PermissionError): b.submit(aid,refresh=True)
    with pytest.raises(ValueError): a.submit(aid,refresh=True,start="bad-date")


def test_sync_failure_does_not_create_new_result_or_expose_provider_secret(seeded):
    db,a,b,aid,baid = seeded
    ev = Evaluator(db,FixtureCalendar())
    old = ev.evaluate(aid)
    job = a.submit(aid,refresh=True)
    class Broken:
        def sync(self,*args,**kwargs):
            raise RuntimeError("secret-token-do-not-expose")
    worker = Worker(db,ev,provider=Broken())
    for _ in range(3):
        assert worker.run_job()
        with db.connect(True) as c:
            c.execute("UPDATE jobs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job,))
    record = a.jobs([job])[0]
    assert record["status"] == "FAILED"
    assert "secret-token" not in canonical(record)
    assert a.results()[0]["signal_id"] == old[0]
    assert len(db.rows("SELECT * FROM signal_runs")) == 1


def test_daily_all_users_default_and_success_dedup_and_weekend_skip(seeded):
    db,a,b,aid,baid = seeded
    with db.connect(True) as c:
        c.execute("DELETE FROM settings WHERE key='daily_sync_enabled'")
        from clawindex_next.migration import insert_instrument, instrument
        insert_instrument(c,instrument("IXIC","纳斯达克"))
        _,rows = price_rows()
        for row in rows:
            c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)", ("IXIC",row["date"],canonical(row),"fixture",now(),"FIXTURE",digest(row)))
    b.assign("IXIC")
    assert a.daily_sync_enabled()
    with pytest.raises(PermissionError): b.set_daily_sync(False)
    provider = FakeProvider()
    worker = Worker(db,Evaluator(db,FixtureCalendar()),provider=provider)
    instant = datetime(2026,8,12,12,tzinfo=timezone.utc)
    worker.schedule(instant)
    worker.schedule(instant+timedelta(hours=1))
    assert [code for code,_ in provider.calls] == ["HKTECH","IXIC"] # includes Bob-only index; shared index fetched once
    assert len(db.rows("SELECT * FROM jobs")) == 3 # maintenance now also prepares personal operations
    assert not db.rows('SELECT * FROM outbox') # rule/channel authorization remains separate
    worker.schedule(datetime(2026,8,15,12,tzinfo=timezone.utc))
    assert len(provider.calls) == 2
    a.set_daily_sync(False)
    worker.schedule(instant+timedelta(days=1))
    assert len(provider.calls) == 2


def test_daily_rechecks_new_session_and_failed_sync_retries_after_cooldown(seeded):
    db,a,b,aid,baid = seeded
    a.set_daily_sync(True)
    class LaterCalendar(FixtureCalendar):
        def expected(self,name,instant=None,grace_minutes=150):
            return "2026-08-13" if instant.hour >= 12 else "2026-08-12"
    class LaterProvider(FakeProvider):
        def sync(self,store,code,start=None,instant=None):
            super().sync(store,code,start,instant)
            if instant.hour >= 12:
                _,rows = price_rows(1,end="2026-08-13")
                with store.connect(True) as c:
                    for r in rows:
                        c.execute("INSERT OR IGNORE INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)", (code,r["date"],canonical(r),"fixture",now(),"FIXTURE",digest(r)))
    provider = LaterProvider()
    worker = Worker(db,Evaluator(db,LaterCalendar()),provider=provider)
    worker.schedule(datetime(2026,8,13,8,tzinfo=timezone.utc))
    worker.schedule(datetime(2026,8,13,13,tzinfo=timezone.utc))
    assert len(provider.calls) == 2
    class Broken(FakeProvider):
        def sync(self,*args,**kwargs):
            self.calls.append(1)
            raise ProviderError("DELAYED: not yet")
    broken = Broken()
    worker = Worker(db,Evaluator(db,FixtureCalendar()),provider=broken)
    worker.schedule(datetime(2026,8,14,8,tzinfo=timezone.utc))
    worker.schedule(datetime(2026,8,14,8,10,tzinfo=timezone.utc))
    worker.schedule(datetime(2026,8,14,8,31,tzinfo=timezone.utc))
    assert len(broken.calls) == 2


def test_sync_only_job_and_market_lock_release(seeded):
    db,a,b,aid,baid = seeded
    job = a.submit(aid,refresh=True,kind="sync",start="2020-01-01")
    provider = FakeProvider()
    assert Worker(db,Evaluator(db,FixtureCalendar()),provider=provider).run_job()
    assert not db.rows("SELECT * FROM signal_runs")
    assert a.jobs([job])[0]["status"] == "SUCCEEDED"
    assert provider.calls == [("HKTECH","2020-01-01")]
    syncer = MarketSync(db,FixtureCalendar(),provider)
    with db.connect(True) as c:
        c.execute("DELETE FROM settings WHERE key='market_sync:HKTECH'")
        c.execute("INSERT INTO leases VALUES(?,?,?)", ("market_sync:HKTECH","other",(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()))
    with pytest.raises(ProviderError,match="SYNC_BUSY"):
        syncer.refresh("HKTECH")
    assert len(provider.calls) == 1


def test_failed_ai_can_retry_without_changing_signal(seeded):
    db,a,b,aid,baid = seeded
    sid,did = Evaluator(db,FixtureCalendar()).evaluate(aid)
    explain(db,did,lambda _:"bad json","retry-model")
    result = explain(db,did,lambda _:canonical({"summary":"重新解读成功","evidence_refs":[],"limitations":[],"counterarguments":[],"scenarios":[]}),"retry-model")
    assert result["summary"] == "重新解读成功"
    assert db.rows("SELECT status FROM explanations WHERE model='retry-model'")[0]["status"] == "MODEL_OK"
    assert a.results()[0]["signal_id"] == sid


def test_pacing_shared_across_callers(seeded,monkeypatch):
    db,*_ = seeded
    class FixedClock(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime(2026,8,12,12,tzinfo=timezone.utc)
    monkeypatch.setattr("clawindex_next.syncing.datetime",FixedClock)
    sleeps = []
    rate_limit(db,pause=sleeps.append)
    rate_limit(db,pause=sleeps.append)
    assert sleeps == [0,2.1]


def test_ai_failure_keeps_inspection_success_and_program_explanation(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    job = a.submit(aid,refresh=True,include_ai=True)
    monkeypatch.setattr("clawindex_next.adapters.llm_explanation",lambda _:"invalid json")
    Worker(db,Evaluator(db,FixtureCalendar()),provider=FakeProvider()).run_job()
    assert a.jobs([job])[0]["status"] == "SUCCEEDED"
    did = a.results()[0]["decision_id"]
    assert a.saved_explanation(did)["status"] == "FAILED_TEMPLATE"


def test_friday_us_publication_updates_on_local_saturday(seeded):
    db,a,b,aid,baid = seeded
    from clawindex_next.migration import insert_instrument, instrument
    with db.connect(True) as c:
        insert_instrument(c,instrument("IXIC","纳斯达克"))
        _,rows = price_rows(1,end="2026-08-14")
        for row in rows:
            c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)", ("IXIC",row["date"],canonical(row),"fixture",now(),"FIXTURE",digest(row)))
    b.assign("IXIC")
    a.set_daily_sync(True)
    class PublishedFriday(FixtureCalendar):
        def expected(self,name,instant=None,grace_minutes=150):
            return "2026-08-14" if name == "XNYS" else "2026-08-12"
        def published_at(self,name,day,grace_minutes=150):
            return datetime.fromisoformat(day+"T22:30:00+00:00") if name == "XNYS" else super().published_at(name,day,grace_minutes)
    provider = FakeProvider()
    Worker(db,Evaluator(db,PublishedFriday()),provider=provider).schedule(datetime(2026,8,15,1,tzinfo=timezone.utc))
    assert provider.calls == [("IXIC",None)]


def test_ui_old_analysis_is_not_presented_as_current_after_background_refresh(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    monkeypatch.setenv("VNEXT_DB_PATH",str(db.path))
    monkeypatch.setattr("clawindex_next.service.UserService.market_status",lambda self,code:{"expected":"2026-08-13","latest":"2026-08-13"})
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app_next.py"),default_timeout=30).run()
    app.text_input[0].set_value("alice")
    button(app,"登录").click().run()
    assert not app.exception
    assert any("暂不作为当前操作" in w.value for w in app.warning)
    button(app,"查看全部信号详情").click().run()
    assert any("较早行情" in w.value for w in app.warning)
    assert any(m.label == "针对你的判断" and m.value == "需要重新巡检" for m in app.metric)
    assert a.results()[0]["as_of"] == "2026-08-12"


def test_ui_dialog_chinese_evidence_persistent_ai_and_completion(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    _,did = Evaluator(db,FixtureCalendar()).evaluate(aid)
    monkeypatch.setenv("VNEXT_DB_PATH",str(db.path))
    monkeypatch.setattr("clawindex_next.service.UserService.market_status",lambda self,code:{"expected":"2026-08-12","latest":"2026-08-12"})
    monkeypatch.setattr("clawindex_next.adapters.llm_explanation",lambda _:canonical({"summary":"可读的AI解读","evidence_refs":["close"],"limitations":["有限样本"],"counterarguments":["反弹可能"],"scenarios":[{"assumption":"出现震荡","implication":"规则可能反复触发"}]}))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app_next.py"),default_timeout=30).run()
    app.text_input[0].set_value("alice")
    button(app,"登录").click().run()
    button(app,"查看全部信号详情").click().run()
    assert any(m.label == "指数信号" and m.value == "继续持有" for m in app.metric)
    assert app.multiselect[0].options == ["买入信号","卖出信号","继续持有","空仓等待","数据未通过"]
    button(app,"生成AI解释").click().run()
    assert not app.exception
    assert any("可读的AI解读" in m.value for m in app.markdown)
    app.run() # persisted after unrelated rerun
    assert any("可读的AI解读" in m.value for m in app.markdown)
    button(app,"提交全部巡检").click().run()
    assert not app.exception
    assert app.session_state["loading_jobs"]
    job = a.jobs()[0]["id"]
    assert request_for(db,job)["refresh"]
    assert any("关闭此窗口不会取消任务" in c.value for c in app.caption)
    Worker(db,Evaluator(db,FixtureCalendar()),provider=FakeProvider()).run_job()
    app.run()
    assert not app.exception
    assert any("已完成 1 项" in s.value for s in app.success)
