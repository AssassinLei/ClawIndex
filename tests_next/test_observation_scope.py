"""Legacy global revision IDs must not strand scoped HKTECH results."""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from clawindex_next.decision_lifecycle import current_revision, evidence_current
from clawindex_next.engine import ALGORITHM
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical, digest, now
from clawindex_next.worker import Worker
from conftest import FixtureCalendar


def append_prelaunch(db):
    row = {'date':'2018-06-29','close':4000.,'open':None,'high':None,'low':None}
    with db.connect(True) as c:
        revision = c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',
                             ('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row))).lastrowid
    return row,revision


def test_upgrade_replaces_legacy_result_with_higher_ignored_revision(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    a.set_inspection_ai(False)
    instant = datetime.now(timezone.utc)
    evaluator = Evaluator(db,FixtureCalendar())
    old_sid,old_did = evaluator.evaluate(aid,instant)
    row,ignored_id = append_prelaunch(db)
    # Reproduce a pre-upgrade manifest: its newest inserted ID belongs to an
    # early close-only bar, larger than every post-launch usable revision.
    with db.connect(True) as c:
        saved = c.execute('SELECT dataset_id,feature_id FROM signal_runs WHERE id=?',(old_sid,)).fetchone()
        manifest = json.loads(c.execute('SELECT manifest FROM datasets WHERE id=?',(saved['dataset_id'],)).fetchone()[0])
        manifest['data'].insert(0,row)
        manifest['observations'].insert(0,{'id':ignored_id,'hash':digest(row),'available_at':None,'availability_basis':'FIXTURE'})
        legacy_dataset = digest(manifest)
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?)',(legacy_dataset,'HKTECH','2026-08-12',canonical(manifest),now()))
        features = json.loads(c.execute('SELECT payload FROM feature_snapshots WHERE id=?',(saved['feature_id'],)).fetchone()[0])
        features['algorithm'] = 'factor_features_v3'
        legacy_feature = digest({'legacy':features,'dataset':legacy_dataset})
        c.execute('INSERT INTO feature_snapshots VALUES(?,?,?,?,?)',(legacy_feature,legacy_dataset,'factor_features_v3',canonical(features),now()))
        signal = json.loads(c.execute('SELECT result FROM signal_runs WHERE id=?',(old_sid,)).fetchone()[0])
        signal.update(dataset_id=legacy_dataset,feature_id=legacy_feature)
        legacy_sid = digest({'legacy_signal':signal})
        c.execute('INSERT INTO signal_runs VALUES(?,?,?,?,?,?,?)',(legacy_sid,aid,legacy_dataset,legacy_feature,'2026-08-12',canonical(signal),now()))
        decision = c.execute('SELECT result FROM personal_decisions WHERE id=?',(old_did,)).fetchone()[0]
        legacy_did = digest({'legacy_decision':legacy_sid})
        c.execute('INSERT INTO personal_decisions VALUES(?,?,?,?,?,?)',(legacy_did,legacy_sid,a.user['id'],None,decision,now()))
        c.execute('UPDATE current_signals SET signal_id=? WHERE assignment_id=?',(legacy_sid,aid))
        old_sid,old_did = legacy_sid,legacy_did
        context = [None,None,None]
        local_day = instant.astimezone(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
        old_receipt = digest({'revision':ignored_id,'context':context,'expected':'2026-08-12','algorithm':ALGORITHM,'evaluation_day':local_day,'include_ai':False})
        c.execute('INSERT INTO settings VALUES(?,?)',('auto_inspect:'+str(aid),old_receipt))
    assert not a.decision_state(old_did)['current']
    worker = Worker(db,evaluator)
    worker.enqueue_inspections(db.rows('SELECT * FROM assignments WHERE id=?',(aid,)),'HKTECH','2026-08-12',instant)
    assert worker.run_job()
    result = a.results()[0]
    assert result['signal_id']!=old_sid
    assert a.decision_state(result['decision_id'])['current']
    with db.connect() as c:
        assert current_revision(c,'HKTECH','2026-08-12',instant)<ignored_id
        assert evidence_current(c,result['signal_id'],'HKTECH','2026-08-12',instant)
    assert a.request_inspection(aid,False)['status']=='ALREADY_DONE'


def test_ignored_old_closes_do_not_invalidate_today_or_trigger_new_inspection(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    a.set_inspection_ai(False)
    sid,did = Evaluator(db,FixtureCalendar()).evaluate(aid)
    append_prelaunch(db)
    assert a.decision_state(did)['current']
    assert a.request_inspection(aid,False)['status']=='ALREADY_DONE'
    worker = Worker(db,Evaluator(db,FixtureCalendar()))
    assignments = db.rows('SELECT * FROM assignments WHERE id=?',(aid,))
    worker.enqueue_inspections(assignments,'HKTECH','2026-08-12',datetime.now(timezone.utc))
    assert not db.rows('SELECT * FROM jobs')
    assert a.results()[0]['signal_id']==sid


def test_real_postlaunch_correction_still_invalidates_result(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    a.set_inspection_ai(False)
    evaluator = Evaluator(db,FixtureCalendar())
    sid,did = evaluator.evaluate(aid)
    append_prelaunch(db)
    with db.connect(True) as c:
        row = json.loads(c.execute("SELECT payload FROM observations WHERE code='HKTECH' AND trade_date='2026-08-12' ORDER BY id DESC LIMIT 1").fetchone()[0])
        row['close'] += .2
        c.execute('INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?)',
                  ('HKTECH',row['date'],canonical(row),'fixture',now(),'FIXTURE',digest(row)))
    assert not a.decision_state(did)['current']
    new_sid,new_did = evaluator.evaluate(aid)
    assert new_sid!=sid and a.results()[0]['signal_id']==new_sid
    assert a.decision_state(new_did)['current']
