import json
from datetime import datetime,timezone

from clawindex_next.engine import DEFAULT_SPEC
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical,digest,now
from clawindex_next.worker import Worker
from conftest import FixtureCalendar
from test_research_delivery import enable


def test_old_date_and_old_revision_cannot_replace_current_projection(seeded):
    db,a,b,aid,baid = seeded
    calendar = FixtureCalendar()
    ev = Evaluator(db,calendar)
    old_manifest,quality = ev.inputs("HKTECH")
    old_sid,_ = ev.evaluate(aid)
    row = db.rows("SELECT * FROM observations ORDER BY id DESC LIMIT 1")[0]
    new_row = json.loads(row["payload"])
    new_row["pe"] = 10.
    with db.connect(True) as c:
        c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)", ("HKTECH",row["trade_date"],canonical(new_row),"fixture_revision",now(),"FIXTURE",digest(new_row)))
    new_sid,_ = ev.evaluate(aid)
    class OldVersion(Evaluator):
        def inputs(self,code,instant=None): return old_manifest,quality
    OldVersion(db,calendar).evaluate(aid)
    assert db.rows("SELECT signal_id FROM current_signals WHERE assignment_id=?", (aid,))[0]["signal_id"] == new_sid
    truncated = {**old_manifest,"as_of":old_manifest["sessions"][-2],"sessions":old_manifest["sessions"][:-1],"observations":old_manifest["observations"][:-1],"data":old_manifest["data"][:-1]}
    class OldDate(Evaluator):
        def inputs(self,code,instant=None): return truncated,"OK"
    OldDate(db,calendar).evaluate(aid)
    assert db.rows("SELECT signal_id FROM current_signals WHERE assignment_id=?", (aid,))[0]["signal_id"] == new_sid


def test_changed_position_invalidates_pending_notification(seeded):
    db,a,b,aid,baid = seeded
    aid = enable(db,a)
    a.report_position("HKTECH","EMPTY")
    a.set_channel("https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder","fixture",True)
    ev = Evaluator(db,FixtureCalendar())
    ev.evaluate(aid)
    a.report_position("HKTECH","HELD")
    sent=[]
    w = Worker(db,ev,sender=lambda *args:sent.append(args))
    assert w.deliver() and not sent
    assert db.rows("SELECT status FROM outbox")[0]["status"] == "EXPIRED"


def test_deferred_feedback_invalidates_pending_notification(seeded):
    db,a,b,aid,baid = seeded
    aid = enable(db,a)
    a.report_position("HKTECH","EMPTY")
    a.set_channel("https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder","fixture",True)
    ev = Evaluator(db,FixtureCalendar())
    _,did = ev.evaluate(aid)
    a.feedback(did,"DEFERRED","等待确认资金用途")
    sent=[]
    assert Worker(db,ev,sender=lambda *args:sent.append(args)).deliver()
    assert not sent


def test_worker_recovers_crashed_claim_and_scheduler_off_is_offline(seeded):
    db,a,b,aid,baid = seeded
    jid = a.submit(aid)
    with db.connect(True) as c:
        c.execute("UPDATE jobs SET status='RUNNING',claim_owner='crashed',claim_until='2000-01-01T00:00:00+00:00' WHERE id=?", (jid,))
    class NoNetwork:
        def sync(self,*args): raise AssertionError("调度关闭时不应联网")
    w = Worker(db,Evaluator(db,FixtureCalendar()),provider=NoNetwork())
    result = w.once()
    assert result["jobs_processed"] == 1
    assert db.rows("SELECT status FROM jobs")[0]["status"] == "SUCCEEDED"


def test_job_retry_backoff_and_exhausted_crash_are_visible(seeded):
    db,a,b,aid,baid = seeded
    jid = a.submit(aid)
    class Broken:
        def evaluate(self,*args): raise ValueError("bad data")
    worker = Worker(db,Broken())
    assert worker.run_job()
    assert not worker.run_job()
    assert db.rows("SELECT status,attempts FROM jobs")[0] == {"status":"RETRY","attempts":1}
    with db.connect(True) as c:
        c.execute("UPDATE jobs SET status='RUNNING',attempts=3,claim_until='2000-01-01T00:00:00+00:00' WHERE id=?", (jid,))
    assert not worker.run_job()
    assert db.rows("SELECT status FROM jobs")[0]["status"] == "FAILED"


def test_atomic_signal_and_outbox_survive_explanation_failure(seeded,monkeypatch):
    import clawindex_next.service as module
    db,a,b,aid,baid = seeded
    aid = enable(db,a)
    a.report_position("HKTECH","EMPTY")
    a.set_channel("https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder","fixture",True)
    def broken(*args): raise TimeoutError("model unavailable")
    monkeypatch.setattr(module,"explain",broken)
    try: Evaluator(db,FixtureCalendar()).evaluate(aid)
    except TimeoutError: pass
    assert len(db.rows("SELECT * FROM signal_runs")) == 1
    assert len(db.rows("SELECT * FROM personal_decisions")) == 1
    assert len(db.rows("SELECT * FROM outbox")) == 1
