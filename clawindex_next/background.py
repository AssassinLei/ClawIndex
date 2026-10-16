"""Durable worker activity and private, evidence-based descriptions of task waits."""
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .storage import canonical, now


@contextmanager
def activity(store, owner, kind, **fields):
    key, token = 'worker_activity:'+owner, uuid.uuid4().hex
    with store.connect(True) as c:
        previous = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        value = {'kind':kind, 'token':token, 'started_at':now(), **fields}
        c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, canonical(value)))
    try:
        yield
    finally:
        with store.connect(True) as c:
            current = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            if current and json.loads(current[0]).get('token') == token:
                if previous:
                    c.execute('UPDATE settings SET value=? WHERE key=?', (previous[0], key))
                else:
                    c.execute('DELETE FROM settings WHERE key=?', (key,))


def job_description(row, user_id):
    phase = row.get('phase') or 'starting'
    task = {'syncing':'更新行情', 'analyzing':'计算巡检信号', 'explaining':'生成AI解读'}.get(phase, '处理巡检任务')
    return (row['name']+'：'+task) if row['user_id'] == user_id else ('其他账号：'+task)


def snapshot(store, user_id, instant=None):
    instant = instant or datetime.now(timezone.utc)
    stamp = instant.isoformat()
    workers = store.rows("SELECT s.value FROM leases l LEFT JOIN settings s ON s.key='worker_activity:'||substr(l.name,8) WHERE l.name LIKE 'worker:%' AND l.expires_at>?", (stamp,))
    running = store.rows("SELECT j.id,j.claim_owner,a.user_id,i.name,json_extract(s.value,'$.phase') phase FROM jobs j JOIN assignments a ON a.id=j.assignment_id JOIN instruments i ON i.code=a.code LEFT JOIN settings s ON s.key='progress:'||j.id WHERE j.status='RUNNING' AND j.claim_until>?", (stamp,))
    names = {r['code']:r['name'] for r in store.rows('SELECT a.code,i.name FROM assignments a JOIN instruments i ON i.code=a.code WHERE a.user_id=? AND a.active=1', (user_id,))}
    activities, represented = [], set()
    for worker in workers:
        if not worker['value']:
            continue
        current = json.loads(worker['value'])
        kind = current.get('kind')
        if kind == 'job':
            job = next((r for r in running if r['id'] == current.get('job_id')), None)
            if not job:
                continue
            description = job_description(job, user_id)
            represented.add(job['id'])
        else:
            code = current.get('code')
            description = {
                'catalog':'更新共享指数目录',
                'sync':('更新'+names[code]+'的行情与历史') if code in names else '更新共享行情与历史',
                'evolution':'进行策略研究与进化',
                'radar':'扫描机会并补充行业历史',
                'notification':'发送通知',
            }.get(kind)
            if kind == 'sync' and current.get('reason'):
                description += '（历史或指标缺口到期补充）' if current['reason']=='repair' else '（检查最近已发布行情）'
        if description:
            activities.append({'kind':kind, 'description':description,
                               'job_id':current.get('job_id') if current.get('user_id') == user_id else None})
    # Older workers and the manual recovery tool may not write activity metadata.
    for job in running:
        if job['id'] not in represented:
            activities.append({'kind':'job', 'description':job_description(job, user_id),
                               'job_id':job['id'] if job['user_id'] == user_id else None})
    eligible = store.rows("SELECT j.id,j.created_at,a.user_id,i.name FROM jobs j JOIN assignments a ON a.id=j.assignment_id JOIN instruments i ON i.code=a.code WHERE a.active=1 AND j.attempts<3 AND (j.status='PENDING' OR (j.status='RETRY' AND j.updated_at<=?) OR (j.status='RUNNING' AND COALESCE(j.claim_until,'')<=?)) ORDER BY j.created_at,j.id", ((instant-timedelta(seconds=30)).isoformat(), stamp))
    return {'ready':bool(workers), 'activities':activities,
            'own_waiting':sum(r['user_id'] == user_id for r in eligible),
            'other_waiting':sum(r['user_id'] != user_id for r in eligible),
            '_eligible':eligible}


def public_snapshot(store, user_id):
    return {key:value for key,value in snapshot(store, user_id).items() if not key.startswith('_')}


def decorate_jobs(rows, state, instant=None):
    instant = instant or datetime.now(timezone.utc)
    titles = {'OFFLINE':'等待后台恢复', 'RECOVERING':'等待接续', 'RETRY':'等待自动重试',
              'STARTING':'准备开始', 'WAITING':'等待当前任务完成', 'DELAYED':'后台接收延迟'}
    for row in rows:
        row['progress'] = json.loads(row['progress']) if row['progress'] else {'phase':'queued', 'detail':'请求已保存'}
        elapsed = max(0, int((instant-datetime.fromisoformat(row['created_at'])).total_seconds()))
        row['wait_minutes'] = elapsed//60
        if row['status'] not in ('PENDING','RUNNING','RETRY'):
            continue
        ahead = [r for r in state['_eligible'] if (r['created_at'],r['id']) < (row['created_at'],row['id'])]
        row['queue_ahead'] = len(ahead)
        if not state['ready']:
            code, hint = 'OFFLINE', '后台当前离线；请求已保存，后台恢复后继续处理'
        elif row['status'] == 'RUNNING' and (not row['claim_until'] or row['claim_until'] <= instant.isoformat()):
            code, hint = 'RECOVERING', '上次执行已中断，等待后台接续'
        elif row['status'] == 'RETRY':
            delay = max(0, int((datetime.fromisoformat(row['updated_at'])+timedelta(seconds=30)-instant).total_seconds()))
            code, hint = 'RETRY', (row['error'] or '本次处理暂未完成')+('；约'+str(delay)+'秒后可自动重试' if delay else '；已到重试时间，后台将继续处理')
        elif row['status'] == 'RUNNING':
            code, hint = 'RUNNING', row['progress']['detail']
        else:
            busy = [a['description'] for a in state['activities'] if a.get('job_id') != row['id']]
            if busy:
                code, hint = 'WAITING', '后台当前：'+ '；'.join(busy[:3])+'。当前步骤完成后会继续处理本请求。'
            elif ahead:
                code, hint = 'WAITING', f'前面还有 {len(ahead)} 项可执行请求，后台会依次接收。'
            elif elapsed >= 60:
                code, hint = 'DELAYED', f'已等待 {elapsed//60} 分钟，后台有心跳但尚未接收；未发现其他可执行巡检，请查看系统状态。'
            else:
                code, hint = 'STARTING', '请求已保存，等待后台接收；未发现其他可执行巡检，无需重复点击。'
            if busy and ahead:
                hint += f' 前面另有 {len(ahead)} 项可执行请求。'
        row['queue_state'], row['queue_hint'] = code, hint
        row['queue_title'] = titles.get(code)
    return rows
