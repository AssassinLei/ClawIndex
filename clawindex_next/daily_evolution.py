"""User-requested daily historical adjustment, distinct from forward promotion.

Daily checks reuse overlapping history. They are never labelled independent
out-of-sample or forward evidence, and never grant ongoing auto-promotion rights.
"""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .engine import validate_spec
from .evolution import frozen_prefix, require_v2, stamp
from .factors import authorized
from .storage import canonical, digest
from .validation import select_candidate


MODE = 'DAILY_APPLY'


def request(evolution, policy_id, instant=None):
    require_v2(evolution.store)
    instant = instant or datetime.now(timezone.utc)
    day = instant.astimezone(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
    with evolution.store.connect(True) as c:
        policy = c.execute('SELECT * FROM evolution_policies WHERE id=? AND active=1',(policy_id,)).fetchone()
        if not policy:
            raise ValueError('进化设置已更新，请刷新后重试')
        # Deduplicate by account/index/local day, including across policy edits.
        old = c.execute("SELECT r.id FROM evolution_runs r JOIN evolution_policies p ON p.id=r.policy_id WHERE p.user_id=? AND p.code=? AND json_extract(r.payload,'$.mode')=? AND (json_extract(r.payload,'$.requested_day')=? OR r.status IN ('WAITING_DATA','PENDING','RUNNING') OR EXISTS(SELECT 1 FROM evolution_candidates k WHERE k.id=r.id AND k.status='READY_TO_APPLY')) ORDER BY r.created_at DESC LIMIT 1",
                        (policy['user_id'],policy['code'],MODE,day)).fetchone()
        if old:
            return old[0]
        assignment = c.execute('SELECT a.*,s.spec FROM assignments a JOIN strategy_versions s ON s.id=a.strategy_id WHERE a.user_id=? AND a.code=? AND a.active=1',(policy['user_id'],policy['code'])).fetchone()
        if not assignment:
            raise ValueError('请先添加自己的指数')
        payload = json.loads(policy['payload'])
        base = json.loads(assignment['spec'])
        if not authorized(base,payload['anchor_spec'],payload['config']):
            raise ValueError('现行规则超出研究范围，请先保存新的进化设置')
        rid = digest({'mode':MODE,'user':policy['user_id'],'code':policy['code'],'day':day})
        config = {'mode':MODE,'requested_day':day,'requested_at':stamp(instant),'policy':payload['config'],
                  'base_spec':base,'plan':[],'historical_only':True,'repeated_history':True,
                  'waiting_reason':'后台正在准备行情，缺口自动补齐后继续。'}
        c.execute("INSERT INTO evolution_runs(id,policy_id,assignment_id,status,manifest,payload,created_at,updated_at) VALUES(?,?,?,'WAITING_DATA','{}',?,?,?)",
                  (rid,policy_id,assignment['id'],canonical(config),stamp(instant),stamp(instant)))
    return rid


def prepare(evolution, instant=None):
    instant = instant or datetime.now(timezone.utc)
    rows = evolution.store.rows("SELECT r.*,p.payload policy_payload FROM evolution_runs r JOIN evolution_policies p ON p.id=r.policy_id JOIN assignments a ON a.id=r.assignment_id WHERE r.status='WAITING_DATA' AND p.active=1 AND a.active=1 AND (json_extract(r.payload,'$.retry_after') IS NULL OR json_extract(r.payload,'$.retry_after')<=?) ORDER BY r.created_at LIMIT 1",(stamp(instant),))
    if not rows:
        return
    run = rows[0]
    config = json.loads(run['payload'])
    assignment = evolution.store.rows('SELECT a.*,s.spec FROM assignments a JOIN strategy_versions s ON s.id=a.strategy_id WHERE a.id=?',(run['assignment_id'],))[0]
    try:
        manifest,plan = evolution.plan(assignment,json.loads(run['policy_payload']),instant)
    except (ValueError,RuntimeError):
        # Data repair runs in the normal shared scheduler; no user maintenance.
        config.update(waiting_reason='行情或历史尚未就绪，后台自动维护后继续；当前规则保留。',retry_after=stamp(instant+timedelta(minutes=5)))
        with evolution.store.connect(True) as c:
            c.execute("UPDATE evolution_runs SET payload=?,updated_at=? WHERE id=? AND status='WAITING_DATA'",(canonical(config),stamp(instant),run['id']))
        return
    config.pop('retry_after',None)
    config.pop('waiting_reason',None)
    config.update(plan)
    # The same bounded candidate/cost/delay grid as automatic research. The
    # annual automatic research budget stays separate from explicit daily runs.
    with evolution.store.connect(True) as c:
        c.execute("UPDATE evolution_runs SET status='PENDING',manifest=?,payload=?,updated_at=? WHERE id=? AND status='WAITING_DATA' AND EXISTS(SELECT 1 FROM evolution_policies WHERE id=evolution_runs.policy_id AND active=1) AND EXISTS(SELECT 1 FROM assignments WHERE id=evolution_runs.assignment_id AND active=1)",
                  (canonical(manifest),canonical(config),stamp(instant),run['id']))


def finish(evolution, run, reports, instant):
    config = json.loads(run['payload'])
    chosen,assessment = select_candidate(reports,config['base_spec'],config['policy'])
    config.update(assessment=assessment,test_consumed=True)
    with evolution.store.connect(True) as c:
        current = c.execute("SELECT r.id FROM evolution_runs r JOIN evolution_policies p ON p.id=r.policy_id JOIN assignments a ON a.id=r.assignment_id WHERE r.id=? AND r.status IN ('PENDING','RUNNING') AND r.cursor=? AND p.active=1 AND a.active=1",(run['id'],len(config['plan']))).fetchone()
        if not current:
            return
        if chosen:
            payload = {'mode':MODE,'spec':chosen,'base_spec':config['base_spec'],'policy':config['policy'],
                       'assessment':assessment,'historical_only':True,'repeated_history':True}
            c.execute("INSERT OR IGNORE INTO evolution_candidates VALUES(?,'READY_TO_APPLY',?,?,?,?)",
                      (run['id'],canonical(payload),canonical({'count':0}),stamp(instant),stamp(instant)))
        c.execute('UPDATE evolution_runs SET status=?,payload=?,updated_at=? WHERE id=?',('FROZEN' if chosen else 'REJECTED',canonical(config),stamp(instant),run['id']))
    if chosen:
        apply(evolution,run['id'],instant)


def apply(evolution, rid, instant=None):
    """Atomically replace only this request's incumbent, preserving stage/holdings."""
    instant = instant or datetime.now(timezone.utc)
    store = evolution.store
    with store.connect(True) as c:
        row = c.execute("SELECT k.*,r.payload run_payload,r.manifest,r.assignment_id,p.user_id,p.code,p.active,p.payload policy_payload FROM evolution_candidates k JOIN evolution_runs r ON r.id=k.id JOIN evolution_policies p ON p.id=r.policy_id WHERE k.id=? AND k.status='READY_TO_APPLY'",(rid,)).fetchone()
        if not row:
            return False
        config = json.loads(row['run_payload'])
        payload = json.loads(row['payload'])
        policy = json.loads(row['policy_payload'])
        old = c.execute('SELECT * FROM assignments WHERE id=? AND active=1',(row['assignment_id'],)).fetchone()
        if not row['active'] or not old or config.get('mode')!=MODE or payload.get('mode')!=MODE or config['policy']!=policy['config'] or old['strategy_id']!=validate_spec(config['base_spec']):
            c.execute("UPDATE evolution_candidates SET status='QUARANTINED',updated_at=? WHERE id=?",(stamp(instant),rid))
            return False
        reports = [json.loads(r['payload']) for r in c.execute('SELECT payload FROM evolution_trials WHERE run_id=? ORDER BY scenario',(rid,))]
        if len(reports)!=len(config['plan']):
            return False
        chosen,assessment = select_candidate(reports,config['base_spec'],config['policy'])
        if not chosen or chosen!=payload['spec'] or chosen==config['base_spec'] or assessment.get('reasons') or not authorized(chosen,policy['anchor_spec'],config['policy']):
            c.execute("UPDATE evolution_candidates SET status='QUARANTINED',updated_at=? WHERE id=?",(stamp(instant),rid))
            return False
        try:
            manifest,quality = evolution.evaluator.inputs(row['code'],instant)
        except (ValueError,RuntimeError):
            return False
        frozen = json.loads(row['manifest'])
        if quality!='OK' or manifest['as_of']!=frozen['as_of'] or frozen_prefix(manifest,frozen['as_of'])!=config['prefix_hash']:
            c.execute("UPDATE evolution_candidates SET status='QUARANTINED',payload=json_set(payload,'$.quarantine_reason','研究期间行情发生更新，下次进化将使用最新数据'),updated_at=? WHERE id=?",(stamp(instant),rid))
            return False
        sending = c.execute("SELECT o.id FROM outbox o JOIN personal_decisions d ON d.id=o.decision_id JOIN signal_runs s ON s.id=d.signal_id WHERE s.assignment_id=? AND o.status='SENDING' AND o.claim_until>?",(old['id'],stamp(instant))).fetchone()
        if sending:
            return False
        sid = validate_spec(chosen)
        c.execute('INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?)',(sid,canonical(chosen),stamp(instant)))
        c.execute('UPDATE assignments SET active=0 WHERE id=?',(old['id'],))
        aid = c.execute('INSERT INTO assignments(user_id,code,strategy_id,stage,catchup,created_at) VALUES(?,?,?,?,?,?)',
                        (row['user_id'],row['code'],sid,old['stage'],old['catchup'],stamp(instant))).lastrowid
        c.execute("UPDATE outbox SET status='EXPIRED' WHERE decision_id IN (SELECT d.id FROM personal_decisions d JOIN signal_runs s ON s.id=d.signal_id WHERE s.assignment_id=?) AND status IN ('PENDING','RETRY','SENDING')",(old['id'],))
        audit = {'reason':'用户点击进化并应用，历史分段、成本与延迟检验通过；尚无后续行情验证',
                 'mode':MODE,'historical_only':True,'repeated_history':True,'evidence':rid,
                 'requested_at':config['requested_at'],'old_strategy':old['strategy_id'],'new_strategy':sid,
                 'old_spec':config['base_spec'],'new_spec':chosen,'basis':'INDEX_PROXY','execution':'USER_CONFIRMATION_REQUIRED'}
        c.execute('INSERT INTO strategy_promotions(candidate_id,user_id,code,old_assignment,new_assignment,payload,created_at) VALUES(?,?,?,?,?,?,?)',
                  (rid,row['user_id'],row['code'],old['id'],aid,canonical(audit),stamp(instant)))
        c.execute("UPDATE evolution_candidates SET status='ACTIVE',updated_at=? WHERE id=?",(stamp(instant),rid))
        c.execute("INSERT INTO jobs(id,assignment_id,status,created_at,updated_at) VALUES(?,?,'PENDING',?,?)",(digest({'daily_promotion':rid}),aid,stamp(instant),stamp(instant)))
    store.health('evolution','PROMOTED','每日进化已更新现行规则，正在生成新判断；依据历史检验，尚无后续行情验证。',row['user_id'])
    return True
