"""Opt-in discovery notices, isolated from personal BUY/SELL notification filters."""
import json
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from .radar import expected, latest, preferences, read, write
from .radar_rules import RULE_ID, select
from .storage import canonical, digest


MODES = {'OFF':'关闭机会提醒','ALL':'新增机会、条件满足及撤销','READY':'仅条件满足及后续撤销'}


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
        # Ranking can hide an opportunity before its conditions actually fail.
        # Revoke every previously delivered active notice, not only yesterday's top three.
        for record in report['rows']:
            code = record['code']
            if record['state']=='REJECTED':
                sent = store.rows("SELECT value FROM settings WHERE key LIKE 'radar:notice:%' AND json_extract(value,'$.user_id')=? AND json_extract(value,'$.channel_id')=? AND json_extract(value,'$.code')=? AND json_extract(value,'$.status')='SENT' ORDER BY json_extract(value,'$.created_at') DESC LIMIT 1",(uid,cid,code))
                if sent and json.loads(sent[0]['value'])['kind']!='WITHDRAWN':
                    events.append((code,'WITHDRAWN',json.loads(sent[0]['value'])['event_state']))
        with (nullcontext(connection) if connection is not None else store.connect(True)) as c:
            for code,kind,state in events:
                if mode=='READY' and state!='READY':
                    continue
                identity = digest({'user':uid,'channel':cid,'report':report['id'],'code':code,'kind':kind})
                value = {'id':identity,'user_id':uid,'channel_id':cid,'code':code,'kind':kind,'event_state':state,
                         'report_id':report['id'],'as_of':report['as_of'],'status':'PENDING','attempts':0,
                         'next_at':report['created_at'],'created_at':report['created_at']}
                c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('radar:notice:'+identity,canonical(value)))


def deliver(store, sender, calendar, instant=None):
    from .adapters import webhook_failure_detail
    instant = instant or datetime.now(timezone.utc)
    owner = uuid.uuid4().hex
    with store.connect(True) as c:
        c.execute("UPDATE settings SET value=json_set(value,'$.status','FAILED','$.claim_owner',NULL,'$.claim_until',NULL,'$.detail','达到3次尝试上限，最后一次投递中断，未自动重发。') WHERE key LIKE 'radar:notice:%' AND json_extract(value,'$.attempts')>=3 AND (json_extract(value,'$.status') IN ('PENDING','RETRY') OR (json_extract(value,'$.status')='SENDING' AND COALESCE(json_extract(value,'$.claim_until'),'')<=?))",(instant.isoformat(),))
        row = c.execute("SELECT key,value FROM settings WHERE key LIKE 'radar:notice:%' AND json_extract(value,'$.attempts')<3 AND (json_extract(value,'$.status') IN ('PENDING','RETRY') OR (json_extract(value,'$.status')='SENDING' AND COALESCE(json_extract(value,'$.claim_until'),'')<=?)) AND json_extract(value,'$.next_at')<=? ORDER BY json_extract(value,'$.created_at'),key LIMIT 1",(instant.isoformat(),instant.isoformat())).fetchone()
        if not row:
            return False
        item = json.loads(row['value'])
        channel = c.execute("SELECT * FROM channels WHERE id=? AND user_id=? AND enabled=1 AND url<>''",(item['channel_id'],item['user_id'])).fetchone()
        mode_row = c.execute('SELECT value FROM settings WHERE key=?',(config_key(item['user_id'],item['channel_id']),)).fetchone()
        mode = json.loads(mode_row[0]) if mode_row else 'OFF'
        allowed = channel and mode!='OFF' and (mode!='READY' or item['event_state']=='READY')
        if allowed:
            item.update(status='SENDING',attempts=item['attempts']+1,claim_owner=owner,claim_until=(instant+timedelta(seconds=45)).isoformat())
        else:
            item['status'] = 'CANCELLED'
        c.execute('UPDATE settings SET value=? WHERE key=?',(canonical(item),row['key']))
    if not allowed:
        return True
    report = latest(store)
    selected = selection(store,item['user_id'],report) if report and report['status']=='COMPLETE' else {}
    current = next((r for r in report['rows'] if r['code']==item['code']),None) if report else None
    fresh = report and report['status']=='COMPLETE' and report['rule_id']==RULE_ID and report['as_of']==expected(calendar,instant)==item['as_of']
    valid = fresh and current and ((item['kind']=='WITHDRAWN' and current['state']=='REJECTED') or (item['kind']!='WITHDRAWN' and item['code'] in selected and (item['event_state']!='READY' or current['state']=='READY')))
    if not valid:
        status,detail = 'EXPIRED','机会已过期、被撤销或偏好已变化，未发送。'
    else:
        state = 'WITHDRAWN' if item['kind']=='WITHDRAWN' else current['state']
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
            c.execute('UPDATE settings SET value=? WHERE key=?',(canonical(value),row['key']))
        audit = {'status':status,'detail':detail,'user_id':item['user_id'],'channel_id':item['channel_id'],'created_at':instant.isoformat()}
        c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(f"radar:delivery:{item['id']}:{item['attempts']}",canonical(audit)))
    return True
