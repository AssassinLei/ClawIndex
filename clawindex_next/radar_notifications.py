"""Opt-in discovery notices, isolated from personal BUY/SELL notification filters."""
import json
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from .radar import expected, latest, preferences, read, write
from .radar_rules import RULE_ID, select
from .storage import canonical, digest


MODES = {'OFF':'关闭机会提醒','ALL':'当前观察机会与条件满足','READY':'仅当前条件满足'}


def config_key(uid, cid):
    return f'radar:channel:{uid}:{cid}'


def configure(store, uid, cid, mode):
    if mode not in MODES:
        raise ValueError('机会提醒方式无效')
    with store.connect(True) as c:
        channel = c.execute("SELECT url FROM channels WHERE id=? AND user_id=? AND url<>''",(cid,uid)).fetchone()
        if not channel:
            raise PermissionError('不能配置其他账号的机会提醒')
        if mode!='OFF':
            from .webhooks import validate_url
            validate_url(channel['url'])
        c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(config_key(uid,cid),canonical(mode)))
        if mode=='OFF':
            cancel_channel(c,uid,cid)
    return True


def cancel_channel(c, uid, cid):
    c.execute("UPDATE settings SET value=json_set(value,'$.status','CANCELLED','$.claim_owner',NULL) WHERE key LIKE 'radar:notice:%' AND json_extract(value,'$.user_id')=? AND json_extract(value,'$.channel_id')=? AND json_extract(value,'$.status') IN ('PENDING','RETRY','SENDING')",(uid,cid))


def selection(store, uid, report):
    if not report:
        return {}
    prefs = preferences(store,uid)
    excluded = set(prefs['ignored'])
    if prefs['hide_watched']:
        excluded.update(r['code'] for r in store.rows('SELECT code FROM assignments WHERE user_id=? AND active=1',(uid,)))
    chosen,_ = select(report['rows'],report['correlations'],excluded)
    return {r['code']:r for r in chosen}


def already_delivered(c, item, state):
    """Also recognize successful legacy attempts whose queue was later cancelled."""
    rows = c.execute("""SELECT n.value,(SELECT a.value FROM settings a
        WHERE a.key LIKE 'radar:delivery:'||json_extract(n.value,'$.id')||':%'
        AND json_extract(a.value,'$.status')='SENT' ORDER BY json_extract(a.value,'$.created_at') DESC LIMIT 1)
        FROM settings n WHERE n.key LIKE 'radar:notice:%'
        AND json_extract(n.value,'$.user_id')=? AND json_extract(n.value,'$.channel_id')=?
        AND json_extract(n.value,'$.code')=? AND json_extract(n.value,'$.as_of')=?
        AND json_extract(n.value,'$.kind') IN ('NEW','READY')
        AND (json_extract(n.value,'$.status')='SENT' OR EXISTS (
            SELECT 1 FROM settings a WHERE a.key LIKE 'radar:delivery:'||json_extract(n.value,'$.id')||':%'
            AND json_extract(a.value,'$.status')='SENT'))""",
        (item['user_id'],item['channel_id'],item['code'],item['as_of']))
    for row in rows:
        sent = json.loads(row[0])
        receipt = json.loads(row[1]) if row[1] else {}
        delivered = sent.get('delivered_state') or receipt.get('delivered_state') or sent['event_state']
        if delivered=='READY' or delivered==state:
            return True
    return False


def other_claim(c, item, instant):
    return c.execute("""SELECT 1 FROM settings WHERE key LIKE 'radar:notice:%'
        AND json_extract(value,'$.user_id')=? AND json_extract(value,'$.channel_id')=?
        AND json_extract(value,'$.code')=? AND json_extract(value,'$.as_of')=?
        AND json_extract(value,'$.id')<>? AND json_extract(value,'$.kind') IN ('NEW','READY')
        AND json_extract(value,'$.status')='SENDING' AND json_extract(value,'$.claim_until')>? LIMIT 1""",
        (item['user_id'],item['channel_id'],item['code'],item['as_of'],item['id'],instant.isoformat())).fetchone()


def duplicate_leader(c, item, instant):
    """Legacy row IDs share one retry budget and its existing backoff."""
    row = c.execute("""SELECT value FROM settings WHERE key LIKE 'radar:notice:%'
        AND json_extract(value,'$.user_id')=? AND json_extract(value,'$.channel_id')=?
        AND json_extract(value,'$.code')=? AND json_extract(value,'$.as_of')=?
        AND json_extract(value,'$.event_state')=? AND json_extract(value,'$.kind') IN ('NEW','READY')
        AND json_extract(value,'$.status') IN ('PENDING','RETRY','SENDING','FAILED')
        ORDER BY CASE WHEN json_extract(value,'$.status')='SENDING' AND json_extract(value,'$.claim_until')>?
            THEN 1 ELSE 0 END DESC,json_extract(value,'$.attempts') DESC,
            json_extract(value,'$.next_at') DESC,json_extract(value,'$.created_at'),key LIMIT 1""",
        (item['user_id'],item['channel_id'],item['code'],item['as_of'],item['event_state'],instant.isoformat())).fetchone()
    leader = json.loads(row[0]) if row else None
    return leader if leader and leader['id']!=item['id'] else None


def enqueue(store, report, connection=None):
    previous = read(store,'radar:snapshot:'+report['previous_complete']) if report.get('previous_complete') else None
    for channel in store.rows("SELECT * FROM channels WHERE enabled=1 AND url<>''"):
        uid,cid = channel['user_id'],channel['id']
        mode = read(store,config_key(uid,cid),'OFF')
        if mode=='OFF':
            continue
        current,old = selection(store,uid,report),selection(store,uid,previous)
        events = []
        for code,row in current.items():
            if code not in old:
                events.append((code,'NEW',row['state']))
            elif row['state']=='READY' and old[code]['state']!='READY':
                events.append((code,'READY','READY'))
        with (nullcontext(connection) if connection is not None else store.connect(True)) as c:
            # Cron may be days away. Refresh only never-delivered waiting
            # opportunities, rather than expiring Wednesday's queue on Thursday
            # and losing the unchanged but still eligible opportunity forever.
            carry = {}
            waiting = c.execute("""SELECT key,value FROM settings WHERE key LIKE 'radar:notice:%'
                AND json_extract(value,'$.user_id')=? AND json_extract(value,'$.channel_id')=?
                AND json_extract(value,'$.kind') IN ('NEW','READY') AND json_extract(value,'$.as_of')<?
                AND json_extract(value,'$.status') IN ('PENDING','RETRY','EXPIRED')
                AND (json_extract(value,'$.attempts')<3 OR json_extract(value,'$.status')='EXPIRED')
                AND json_extract(value,'$.superseded_by') IS NULL""",
                (uid,cid,report['as_of'])).fetchall()
            for pending in waiting:
                notice = json.loads(pending['value'])
                code = notice['code']
                if code not in current:
                    continue
                if duplicate_leader(c,notice,datetime.fromisoformat(report['created_at'])):
                    notice.update(status='CANCELLED',detail='旧重复提醒复用同一机会的既有退避与尝试上限。')
                    c.execute('UPDATE settings SET value=? WHERE key=?',(canonical(notice),pending['key']))
                    continue
                state = current[code]['state']
                if already_delivered(c,notice,state):
                    notice.update(status='CANCELLED',detail='该行情阶段已有成功投递记录，不跨日重发。')
                    c.execute('UPDATE settings SET value=? WHERE key=?',(canonical(notice),pending['key']))
                    continue
                if mode=='READY' and state!='READY':
                    continue
                attempts,next_at = notice['attempts'],notice['next_at']
                if notice['status']=='EXPIRED':
                    audit = c.execute('SELECT value FROM settings WHERE key=?',
                        (f"radar:delivery:{notice['id']}:{attempts}",)).fetchone()
                    if audit and json.loads(audit[0]).get('status')=='EXPIRED':
                        # The old claim did not reach HTTP. Do not turn a stale
                        # validation into a consumed retry or a fake backoff.
                        attempts,next_at = max(0,attempts-1),report['created_at']
                if attempts>=3:
                    continue
                carry.setdefault(code,[]).append((pending['key'],notice,attempts,next_at))
                if not any(event[0]==code for event in events):
                    events.append((code,notice['kind'],state))
            for code,kind,state in events:
                if mode=='READY' and state!='READY':
                    continue
                # Evidence revisions and ranking churn are not distinct messages.
                # A WATCH -> READY upgrade may notify once more on the same day.
                identity = digest({'user':uid,'channel':cid,'as_of':report['as_of'],'code':code,'state':state})
                value = {'id':identity,'user_id':uid,'channel_id':cid,'code':code,'kind':kind,'event_state':state,
                         'report_id':report['id'],'as_of':report['as_of'],'status':'PENDING','attempts':0,
                         'next_at':report['created_at'],'created_at':report['created_at']}
                carried = carry.get(code,[])
                if carried:
                    value.update(attempts=max(n[2] for n in carried),
                                 next_at=max([report['created_at'],*[n[3] for n in carried]]),
                                 carried_from=[n[1]['id'] for n in carried])
                    if value['attempts']:
                        value['status']='RETRY'
                    for key,notice,_,_ in carried:
                        notice.update(status='CANCELLED',superseded_by=identity,claim_owner=None,claim_until=None,
                                      detail='机会仍有效，已更新为最新行情的待发送提醒。')
                        c.execute('UPDATE settings SET value=? WHERE key=?',(canonical(notice),key))
                if already_delivered(c,value,state):
                    continue
                if state=='READY':
                    c.execute("""UPDATE settings SET value=json_set(value,'$.status','CANCELLED','$.detail',
                        '已合并到当前条件满足的机会提醒。') WHERE key LIKE 'radar:notice:%'
                        AND json_extract(value,'$.user_id')=? AND json_extract(value,'$.channel_id')=?
                        AND json_extract(value,'$.code')=? AND json_extract(value,'$.as_of')=?
                        AND json_extract(value,'$.event_state')='WATCH' AND json_extract(value,'$.status') IN ('PENDING','RETRY')""",
                        (uid,cid,code,report['as_of']))
                existing = c.execute('SELECT value FROM settings WHERE key=?',('radar:notice:'+identity,)).fetchone()
                if existing:
                    old_value = json.loads(existing[0])
                    if old_value['status'] in ('SENT','SENDING','FAILED') or old_value['attempts']>=3:
                        continue
                    value = {**old_value,'report_id':report['id'],'kind':kind}
                    if carried:
                        value.update(attempts=max([old_value['attempts'],*[n[2] for n in carried]]),
                                     next_at=max([old_value['next_at'],*[n[3] for n in carried]]),
                                     carried_from=list(dict.fromkeys(old_value.get('carried_from',[])+[n[1]['id'] for n in carried])))
                    if old_value['status'] in ('CANCELLED','EXPIRED'):
                        value.update(status='RETRY' if value['attempts'] else 'PENDING',
                                     next_at=max(report['created_at'],value['next_at']) if carried else report['created_at'],
                                     claim_owner=None,claim_until=None,detail=None)
                c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('radar:notice:'+identity,canonical(value)))


def deliver(store, sender, calendar, instant=None):
    from .adapters import webhook_failure_detail
    instant = instant or datetime.now(timezone.utc)
    owner = uuid.uuid4().hex
    with store.connect(True) as c:
        # Retire pre-upgrade withdrawals even when a Cron window is hours away.
        retired = c.execute("""UPDATE settings SET value=json_set(value,'$.status','CANCELLED',
            '$.claim_owner',NULL,'$.claim_until',NULL,'$.detail','仅推送当前机会，撤销通知已停止。')
            WHERE key LIKE 'radar:notice:%' AND json_extract(value,'$.kind')='WITHDRAWN'
            AND json_extract(value,'$.status') IN ('PENDING','RETRY','SENDING')""").rowcount
        c.execute("UPDATE settings SET value=json_set(value,'$.status','FAILED','$.claim_owner',NULL,'$.claim_until',NULL,'$.detail','达到3次尝试上限，最后一次投递中断，未自动重发。') WHERE key LIKE 'radar:notice:%' AND json_extract(value,'$.attempts')>=3 AND (json_extract(value,'$.status') IN ('PENDING','RETRY') OR (json_extract(value,'$.status')='SENDING' AND COALESCE(json_extract(value,'$.claim_until'),'')<=?))",(instant.isoformat(),))
        from .webhook_schedule import allowed as schedule_allowed
        candidates = c.execute("SELECT key,value FROM settings WHERE key LIKE 'radar:notice:%' AND json_extract(value,'$.attempts')<3 AND (json_extract(value,'$.status') IN ('PENDING','RETRY') OR (json_extract(value,'$.status')='SENDING' AND COALESCE(json_extract(value,'$.claim_until'),'')<=?)) AND json_extract(value,'$.next_at')<=? ORDER BY json_extract(value,'$.created_at'),key",(instant.isoformat(),instant.isoformat()))
        row = None
        for candidate in candidates:
            notice = json.loads(candidate['value'])
            if other_claim(c,notice,instant):
                continue
            if duplicate_leader(c,notice,instant):
                notice.update(status='CANCELLED',detail='旧重复提醒复用同一机会的既有退避与尝试上限。')
                c.execute('UPDATE settings SET value=? WHERE key=?',(canonical(notice),candidate['key']))
                retired += 1
                continue
            active = c.execute("SELECT enabled FROM channels WHERE id=? AND user_id=? AND url<>''",(notice['channel_id'],notice['user_id'])).fetchone()
            setting = c.execute('SELECT value FROM settings WHERE key=?',(config_key(notice['user_id'],notice['channel_id']),)).fetchone()
            choice = json.loads(setting[0]) if setting else 'OFF'
            # Cancel disabled channels promptly, but do not let a future schedule block other channels.
            if not active or not active[0] or choice=='OFF' or (choice=='READY' and notice['event_state']!='READY') or already_delivered(c,notice,notice['event_state']) or schedule_allowed(c,notice['user_id'],notice['channel_id'],instant):
                row = candidate
                break
        if not row:
            return bool(retired)
        item = json.loads(row['value'])
        channel = c.execute("SELECT * FROM channels WHERE id=? AND user_id=? AND enabled=1 AND url<>''",(item['channel_id'],item['user_id'])).fetchone()
        mode_row = c.execute('SELECT value FROM settings WHERE key=?',(config_key(item['user_id'],item['channel_id']),)).fetchone()
        mode = json.loads(mode_row[0]) if mode_row else 'OFF'
        allowed = channel and mode!='OFF' and (mode!='READY' or item['event_state']=='READY') and not already_delivered(c,item,item['event_state'])
        if allowed:
            item.update(status='SENDING',attempts=item['attempts']+1,claim_owner=owner,claim_until=(instant+timedelta(seconds=45)).isoformat())
        else:
            item.update(status='CANCELLED',detail='渠道已关闭、筛选方式改变，或同一行情日的该机会已发送。')
        c.execute('UPDATE settings SET value=? WHERE key=?',(canonical(item),row['key']))
    if not allowed:
        return True
    report = latest(store)
    selected = selection(store,item['user_id'],report) if report and report['status']=='COMPLETE' else {}
    current = next((r for r in report['rows'] if r['code']==item['code']),None) if report else None
    fresh = report and report['status']=='COMPLETE' and report['rule_id']==RULE_ID and report['as_of']==expected(calendar,instant)==item['as_of']
    valid = fresh and current and item['kind'] in ('NEW','READY') and item['code'] in selected and (item['event_state']!='READY' or current['state']=='READY')
    if not valid:
        status,detail = 'EXPIRED','机会已过期、被撤销或偏好已变化，未发送。'
    else:
        state = current['state']
        try:
            from .industry_hierarchy import enrich_rows
            from .radar import universe
            from .webhooks import opportunity_payload
            current = enrich_rows([current],universe(store))[0]
            sender(channel['url'],opportunity_payload(current,state))
            status,detail = 'SENT','机会提醒已发送，HTTP与飞书业务码均成功。'
        except Exception as exc:
            status = 'FAILED' if item['attempts']>=3 else 'RETRY'
            detail = webhook_failure_detail(exc)
            store.health('radar_notification',status,detail,item['user_id'])
    with store.connect(True) as c:
        # A delete/disable callback during the request must not revive this queue.
        live = c.execute('SELECT value FROM settings WHERE key=?',(row['key'],)).fetchone()
        value = json.loads(live[0]) if live else None
        if value and value.get('claim_owner')==owner and value['status']=='SENDING':
            value.update(status=status,detail=detail,next_at=(instant+timedelta(seconds=30*2**(item['attempts']-1))).isoformat(),claim_owner=None)
            if status=='SENT':
                value.update(sent_at=instant.isoformat(),delivered_state=current['state'])
            c.execute('UPDATE settings SET value=? WHERE key=?',(canonical(value),row['key']))
        audit = {'status':status,'detail':detail,'user_id':item['user_id'],'channel_id':item['channel_id'],
                 'notice_id':item['id'],'code':item['code'],'as_of':item['as_of'],'kind':item['kind'],
                 'delivered_state':current['state'] if status=='SENT' else None,'created_at':instant.isoformat()}
        c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(f"radar:delivery:{item['id']}:{item['attempts']}",canonical(audit)))
    return True
