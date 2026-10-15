"""Shared evidence freshness and episode feedback for UI, execution and delivery."""
import json

from .engine import ALGORITHM


HANDLED = {'SUBMITTED','EXECUTED','PARTIAL','DEFERRED','CANCELLED','EXPIRED'}


def evidence_current(c, signal_id, code, day, instant):
    row = c.execute("SELECT d.manifest,f.algorithm FROM signal_runs s JOIN datasets d ON d.id=s.dataset_id JOIN feature_snapshots f ON f.id=s.feature_id WHERE s.id=?",(signal_id,)).fetchone()
    if not row or row['algorithm']!=ALGORITHM:
        return False
    manifest = json.loads(row['manifest'])
    frozen = max((r['id'] for r in manifest['observations']),default=None)
    revision = c.execute("SELECT MAX(id) FROM observations WHERE code=? AND trade_date<=? AND (available_at IS NULL OR available_at<=?)",(code,day,instant.isoformat())).fetchone()[0]
    return frozen is not None and frozen==revision


def feedback(c, uid, decision_id):
    row = c.execute("SELECT s.assignment_id,s.result signal,d.result decision FROM personal_decisions d JOIN signal_runs s ON s.id=d.signal_id WHERE d.id=? AND d.user_id=?",(decision_id,uid)).fetchone()
    if not row:
        return []
    action = json.loads(row['decision']).get('action')
    if action not in ('BUY','SELL'):
        result = c.execute('SELECT status FROM feedback WHERE user_id=? AND decision_id=? ORDER BY id DESC LIMIT 1',(uid,decision_id)).fetchall()
    else:
        # Revisions and daily expiry create new decision IDs for one instruction.
        # Acknowledgement belongs to that instruction until a new model episode.
        episode = json.loads(row['signal']).get('triggered_at')
        result = c.execute("SELECT f.status FROM feedback f JOIN personal_decisions d ON d.id=f.decision_id JOIN signal_runs s ON s.id=d.signal_id WHERE f.user_id=? AND d.user_id=? AND s.assignment_id=? AND json_extract(d.result,'$.action')=? AND json_extract(s.result,'$.triggered_at') IS ? ORDER BY CASE WHEN f.status IN ('SUBMITTED','EXECUTED','PARTIAL','DEFERRED','CANCELLED','EXPIRED') THEN 0 ELSE 1 END,f.id DESC LIMIT 1",(uid,uid,row['assignment_id'],action,episode)).fetchall()
    return [dict(r) for r in result]
