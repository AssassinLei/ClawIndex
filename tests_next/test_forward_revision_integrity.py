"""A forward record cannot silently mix earlier observations and revised bars."""
import json
from datetime import timedelta

import pytest

from clawindex_next.storage import canonical
from test_evolution import T0,add_day,eligible,freeze


def test_revision_of_already_observed_day_stops_further_forward_simulation(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch)
    add_day(db,calendar,'2026-08-13',300)
    ev.observe(rid,T0+timedelta(days=1))
    assert len(db.rows('SELECT * FROM evolution_observations WHERE candidate_id=?',(rid,)))==1
    add_day(db,calendar,'2026-08-13',310) # Vendor corrects a post-freeze bar.
    add_day(db,calendar,'2026-08-14',301)
    ev.observe(rid,T0+timedelta(days=2))
    row = db.rows('SELECT status,payload FROM evolution_candidates WHERE id=?',(rid,))[0]
    assert row['status']=='QUARANTINED' and '已观察行情发生修订' in json.loads(row['payload'])['quarantine_reason']
    assert len(db.rows('SELECT * FROM evolution_observations WHERE candidate_id=?',(rid,)))==1
    assert not db.rows('SELECT * FROM strategy_promotions')


@pytest.mark.parametrize('damage',['revised_observed_bar','missing_dataset','incomplete_snapshot','legacy_unverified_chain'])
def test_completed_forward_candidate_cannot_promote_with_revised_or_missing_history(seeded,monkeypatch,damage):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch,True)
    instant = eligible(db,rid,calendar)
    if damage=='revised_observed_bar':
        day = db.rows('SELECT as_of FROM evolution_observations WHERE candidate_id=? ORDER BY as_of DESC LIMIT 1 OFFSET 1',(rid,))[0]['as_of']
        add_day(db,calendar,day,310)
        calendar.day=instant.date().isoformat()
    elif damage=='missing_dataset':
        with db.connect(True) as c:
            row=c.execute('SELECT as_of,payload FROM evolution_observations WHERE candidate_id=? ORDER BY as_of DESC LIMIT 1',(rid,)).fetchone()
            point=json.loads(row['payload'])
            point.pop('dataset')
            c.execute('UPDATE evolution_observations SET payload=? WHERE candidate_id=? AND as_of=?',(canonical(point),rid,row['as_of']))
    elif damage=='incomplete_snapshot':
        with db.connect(True) as c:
            row=c.execute('SELECT payload FROM evolution_observations WHERE candidate_id=? ORDER BY as_of DESC LIMIT 1',(rid,)).fetchone()
            dataset=json.loads(row['payload'])['dataset']
            snapshot=json.loads(c.execute('SELECT manifest FROM datasets WHERE id=?',(dataset,)).fetchone()[0])
            snapshot.pop('sessions')
            c.execute('UPDATE datasets SET manifest=? WHERE id=?',(canonical(snapshot),dataset))
    else:
        with db.connect(True) as c:
            state=json.loads(c.execute('SELECT state FROM evolution_candidates WHERE id=?',(rid,)).fetchone()[0])
            state.pop('history_guard_version')
            c.execute('UPDATE evolution_candidates SET state=? WHERE id=?',(canonical(state),rid))
    assert not ev.promote(rid,instant)
    assert not db.rows('SELECT * FROM strategy_promotions')
    assert db.rows('SELECT status FROM evolution_candidates WHERE id=?',(rid,))[0]['status']=='QUARANTINED'


def test_evidence_page_rechecks_saved_observations_even_without_new_inspection(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid = freeze(seeded,monkeypatch,True)
    instant = eligible(db,rid,calendar)
    assert ev.promote(rid,instant)
    assert a.strategy_assessment('HKTECH')['level']=='FORWARD_CHECKED'
    day=db.rows('SELECT as_of FROM evolution_observations WHERE candidate_id=? ORDER BY as_of DESC LIMIT 1 OFFSET 1',(rid,))[0]['as_of']
    add_day(db,calendar,day,310)
    assert a.strategy_assessment('HKTECH')['level']=='APPLIED_UNVERIFIED'
    # Viewing evidence must preserve the audit and not switch a rule by itself.
    assert len(db.rows('SELECT * FROM strategy_promotions'))==1
    assert db.rows('SELECT status FROM evolution_candidates WHERE id=?',(rid,))[0]['status']=='ACTIVE'
