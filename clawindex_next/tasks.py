"""Durable request/progress metadata alongside schema-1 jobs, without ALTER TABLE."""
import json
import uuid
from datetime import datetime, timezone

from .storage import canonical, now


ACTIVE = ('PENDING','RUNNING','RETRY')
TERMINAL = ('SUCCEEDED','FAILED','CANCELLED')


def reconcile(store, user_id=None, instant=None):
    """Retire unclaimable jobs without discarding resumable or live work."""
    stamp = (instant or datetime.now(timezone.utc)).isoformat()
    scope = 'assignment_id IN (SELECT id FROM assignments WHERE (? IS NULL OR user_id=?))'
    with store.connect(True) as c:
        c.execute(f"UPDATE jobs SET status='CANCELLED',error='对应监控或规则已停用，任务已结束',claim_until=NULL,updated_at=? WHERE {scope} AND assignment_id IN (SELECT id FROM assignments WHERE active=0) AND (status IN ('PENDING','RETRY') OR (status='RUNNING' AND COALESCE(claim_until,'')<=?))",(stamp,user_id,user_id,stamp))
        c.execute(f"UPDATE jobs SET status='FAILED',error='已达到3次尝试上限，任务已结束',claim_until=NULL,updated_at=? WHERE {scope} AND attempts>=3 AND (status IN ('PENDING','RETRY') OR (status='RUNNING' AND COALESCE(claim_until,'')<=?))",(stamp,user_id,user_id,stamp))


def enqueue(store, assignment_id, refresh=True, include_ai=False, kind="inspection", start=None, decision_id=None):
    request = {"refresh":bool(refresh), "include_ai":bool(include_ai), "kind":kind, "start":start}
    if decision_id:
        request['decision_id'] = decision_id
    with store.connect(True) as c:
        # Repeated clicks and different tabs reuse the same active request.
        rows = c.execute("SELECT j.id,j.status,s.value FROM jobs j LEFT JOIN settings s ON s.key='request:'||j.id WHERE j.assignment_id=? AND j.status IN ('PENDING','RUNNING','RETRY') ORDER BY j.created_at", (assignment_id,)).fetchall()
        for row in rows:
            old = json.loads(row['value']) if row['value'] else {'refresh':False,'include_ai':False,'kind':'inspection','start':None}
            if old == request:
                return row["id"]
            same_kind = old.get('kind','inspection') == kind
            same_target = old.get('decision_id') == decision_id
            # Pending inspections may be upgraded to include a refresh/AI. A
            # running job can accept AI until its atomic completion check, but
            # cannot be told to repeat a sync phase it has already passed.
            can_merge = same_kind and same_target and (row['status'] != 'RUNNING' or
                        ((not refresh or old.get('refresh')) and (not start or old.get('start') and old['start']<=start)))
            if can_merge:
                merged = {**old,**request,'refresh':bool(old.get('refresh') or refresh),'include_ai':bool(old.get('include_ai') or include_ai),
                          'start':min(x for x in (old.get('start'),start) if x) if old.get('start') or start else None}
                c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",('request:'+row['id'],canonical(merged)))
                return row['id']
        jid, stamp = uuid.uuid4().hex, now()
        c.execute("INSERT INTO jobs(id,assignment_id,status,created_at,updated_at) VALUES(?,?,'PENDING',?,?)", (jid,assignment_id,stamp,stamp))
        c.execute("INSERT INTO settings VALUES(?,?)", ("request:"+jid,canonical(request)))
        c.execute("INSERT INTO settings VALUES(?,?)", ("progress:"+jid,canonical({"phase":"queued","detail":"请求已保存，后台将自动处理","updated_at":stamp})))
    return jid


def request_for(store, jid):
    rows = store.rows("SELECT value FROM settings WHERE key=?", ("request:"+jid,))
    return json.loads(rows[0]["value"]) if rows else {}


def progress(store, jid, owner, phase, detail):
    with store.connect(True) as c:
        # A recovered claim must not be overwritten by its previous worker.
        if not c.execute("SELECT id FROM jobs WHERE id=? AND claim_owner=? AND status='RUNNING'", (jid,owner)).fetchone():
            return
        c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("progress:"+jid,canonical({"phase":phase,"detail":detail,"updated_at":now()})))
