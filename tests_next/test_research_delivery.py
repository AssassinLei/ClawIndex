import json
from datetime import datetime,timezone,timedelta

import pytest

from clawindex_next.engine import DEFAULT_SPEC,feature_frame,replay,validate_spec
from clawindex_next.research import backtest,study
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical,now
from clawindex_next.worker import Worker
from conftest import FixtureCalendar,price_rows


def test_backtest_next_session_open_and_no_missing_open_fill():
    dates,rows = price_rows(180)
    live = replay(feature_frame(rows,dates))
    signal_date = next(s["as_of"] for s in live if s["event"] == "BUY")
    ix = dates.index(signal_date)
    rows[ix+1]["open"] = None
    report = backtest(rows,dates,cost_bps=10)
    trade = report["trades"][0]
    assert trade["signal_date"] == signal_date and trade["execution_date"] == dates[ix+2]
    assert trade["price"] == rows[ix+2]["open"]
    assert report["fees_initial_capital"] > 0 and report["cash_return"] == 0
    assert report["missing_session_count"] == 1


def test_cost_sensitivity_oos_and_live_event_parity():
    dates,rows = price_rows(240)
    free = backtest(rows,dates,cost_bps=0)
    costly = backtest(rows,dates,cost_bps=30)
    assert costly["strategy"]["return"] < free["strategy"]["return"]
    assert set(free["splits"]) == {"development","validation","held_out_test"}
    events = [s for s in replay(feature_frame(rows,dates)) if s["event"] != "NONE"]
    assert free["trades"][0]["signal_date"] == events[0]["as_of"]
    assert free["trades"][0]["execution_date"] > events[0]["as_of"]


def test_terminal_order_is_unexecuted():
    dates,rows = price_rows(130,prices=[100.]*129+[150.])
    r = backtest(rows,dates,{**DEFAULT_SPEC,"confirmation":1})
    assert not r["trades"] and r["pending_unexecuted"]["signal_date"] == dates[-1]
    with pytest.raises(ValueError): backtest(rows[:124],dates[:124])


def enable(db,svc,code="HKTECH"):
    with db.connect(True) as c:
        c.execute("INSERT INTO research_runs VALUES(?,?,?,?,?)", (str(svc.user["id"]),svc.user["id"],code,canonical({"strategy_id":validate_spec(DEFAULT_SPEC),"research_usable":True}),now()))
    return svc.assign(code,stage="ENABLED",catchup=True)


def test_same_url_different_users_both_delivered_and_episode_dedup(seeded):
    db,a,b,aid,baid = seeded
    aid,baid = enable(db,a),enable(db,b)
    for svc in (a,b):
        svc.report_position("HKTECH","EMPTY")
        svc.set_channel("https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder","fixture",True)
    ev = Evaluator(db,FixtureCalendar())
    for x in (aid,baid,aid): ev.evaluate(x)
    assert len(db.rows("SELECT * FROM outbox")) == 2
    sent=[]
    worker = Worker(db,ev,sender=lambda u,p:sent.append(p))
    assert worker.deliver() and worker.deliver() and not worker.deliver()
    assert len(sent) == 2
    assert {r["status"] for r in db.rows("SELECT status FROM outbox")} == {"SENT"}


def test_notification_retry_bounded_and_channel_disabled(seeded):
    db,a,b,aid,baid = seeded
    aid = enable(db,a)
    a.report_position("HKTECH","EMPTY")
    channel = a.set_channel("https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder","fixture",True)
    ev = Evaluator(db,FixtureCalendar())
    ev.evaluate(aid)
    def fail(u,p): raise RuntimeError("fake failure")
    worker = Worker(db,ev,sender=fail)
    a.toggle_channel(channel,False)
    assert not worker.deliver()
    a.toggle_channel(channel,True)
    for _ in range(3):
        with db.connect(True) as c: c.execute("UPDATE outbox SET next_at='2000-01-01T00:00:00+00:00'")
        assert worker.deliver()
    assert not worker.deliver()
    assert db.rows("SELECT status,attempts FROM outbox")[0] == {"status":"FAILED","attempts":3}
    assert len(db.rows("SELECT * FROM delivery_attempts")) == 3


def test_research_does_not_enable_trading_and_records_trials(seeded):
    db,a,b,aid,baid = seeded
    manifest,_ = Evaluator(db,FixtureCalendar()).inputs("HKTECH")
    rid,report = study(db,a.user["id"],"HKTECH",manifest)
    assert report["parameter_trials"] == 3 and report["scenario_trials"] == 18
    assert a.watchlist()[0]["stage"] == "RESEARCH"
    assert not db.rows("SELECT * FROM outbox")
