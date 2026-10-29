import json
import time
import uuid
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .adapters import send_feishu, webhook_failure_detail
from .calendar import MarketCalendar, grace_for
from .service import Evaluator
from .storage import canonical, digest, now
from .syncing import MarketSync, error_message
from .tasks import enqueue, progress, request_for, reconcile, inspection_ai_enabled
from .background import activity
from .webhooks import allows, channel_mode, operation_payload


class Worker:
    def __init__(self, store, evaluator=None, sender=None, provider=None, owner=None):
        self.store,self.evaluator = store,evaluator or Evaluator(store)
        self.sender,self.provider = sender or send_feishu,provider
        self.owner = owner or uuid.uuid4().hex
        self.stopping = threading.Event()
        self.last_progress = time.monotonic()
        self.last_notice_check = None
        self.syncer = MarketSync(store,getattr(self.evaluator,"calendar",None) or MarketCalendar(),provider)

    def lease(self, name, seconds=300, instant=None):
        instant = instant or datetime.now(timezone.utc)
        with self.store.connect(True) as c:
            old = c.execute("SELECT * FROM leases WHERE name=?", (name,)).fetchone()
            if old and old["expires_at"] > instant.isoformat() and old["owner"] != self.owner:
                return False
            c.execute("INSERT INTO leases VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at", (name,self.owner,(instant+timedelta(seconds=seconds)).isoformat()))
        return True

    def release(self, name):
        with self.store.connect(True) as c:
            c.execute("DELETE FROM leases WHERE name=? AND owner=?", (name,self.owner))

    def pulse(self):
        self.lease("worker:"+self.owner,seconds=600)

    def mark_progress(self):
        self.last_progress = time.monotonic()
        with self.store.connect(True) as c:
            c.execute("INSERT INTO settings VALUES('worker_loop_completed',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(now(),))

    def enqueue_inspections(self, assignments, code, expected, instant):
        from .engine import ALGORITHM
        from .decision_lifecycle import current_revision
        with self.store.connect() as c:
            revision = current_revision(c,code,expected,instant)
        for assignment in (a for a in assignments if a['code']==code):
            include_ai = inspection_ai_enabled(self.store, assignment['user_id'])
            context = [self.store.rows(f'SELECT MAX(id) n FROM {table} WHERE user_id=? AND code=?',(assignment['user_id'],code))[0]['n'] for table in ('position_reports','budgets','product_mappings')]
            local_day = instant.astimezone(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
            value = digest({'revision':revision,'context':context,'expected':expected,'algorithm':ALGORITHM,'evaluation_day':local_day,'include_ai':include_ai})
            key = 'auto_inspect:'+str(assignment['id'])
            previous = self.store.rows('SELECT value FROM settings WHERE key=?',(key,))
            if previous and previous[0]['value']==value:
                continue
            # Reuse current manual results too, rather than having an independent
            # automatic receipt run the same rule/AI again later that day.
            current = self.store.rows('SELECT d.id decision_id,d.result decision,s.id signal_id,s.result signal,f.algorithm FROM current_signals v JOIN signal_runs s ON s.id=v.signal_id JOIN feature_snapshots f ON f.id=s.feature_id JOIN personal_decisions d ON d.signal_id=s.id WHERE v.assignment_id=? AND d.user_id=? ORDER BY d.created_at DESC,d.id DESC LIMIT 1',(assignment['id'],assignment['user_id']))
            reusable = False
            decision_id = None
            if current:
                row = current[0]
                signal,decision = json.loads(row['signal']),json.loads(row['decision'])
                evaluated_day = datetime.fromisoformat(signal['evaluated_at']).astimezone(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
                refs = decision.get('context_refs',{})
                reusable = (evaluated_day==local_day and signal['as_of']==expected and row['algorithm']==ALGORITHM and
                            signal['data_status'] in ('OK','INSUFFICIENT_DATA') and decision['expires_at']>instant.isoformat() and
                            [refs.get(k) for k in ('report','budget','mapping')]==context)
                if reusable:
                    from .decision_lifecycle import evidence_current
                    with self.store.connect() as c:
                        reusable = evidence_current(c,row['signal_id'],code,expected,instant)
                decision_id = row['decision_id']
            if reusable and include_ai:
                from .explanations import model_identity
                ai = self.store.rows("SELECT id FROM explanations WHERE decision_id=? AND model=? AND status='MODEL_OK'",(decision_id,model_identity()))
                if not ai:
                    enqueue(self.store,assignment['id'],refresh=False,include_ai=True,kind='explanation',decision_id=decision_id)
            elif not reusable:
                enqueue(self.store,assignment['id'],refresh=False,include_ai=include_ai)
            with self.store.connect(True) as c:
                c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))

    def schedule(self, instant=None, interleave=False):
        instant = instant or datetime.now(timezone.utc)
        local = instant.astimezone(ZoneInfo("Asia/Hong_Kong"))
        settings = {r["key"]:r["value"] for r in self.store.rows("SELECT key,value FROM settings WHERE key IN ('scheduler_enabled','daily_sync_enabled')")}
        inspect_enabled = settings.get("scheduler_enabled") == "true"
        processed = 0
        if settings.get("daily_sync_enabled","true") != "true" and not inspect_enabled:
            return
        if not self.lease("scheduler"):
            return
        try:
            if settings.get('daily_sync_enabled','true') == 'true':
                from .catalog import refresh, refresh_due
                if refresh_due(self.store,instant):
                    with activity(self.store,self.owner,'catalog'):
                        refresh(self.store,self.syncer.provider,instant)
            assignments = self.store.rows("SELECT * FROM assignments WHERE active=1")
            for code in sorted({a["code"] for a in assignments}):
                if self.stopping.is_set():
                    break
                self.pulse()
                if interleave:
                    self.notification_checkpoint()
                try:
                    raw_item = self.store.rows('SELECT metadata FROM instruments WHERE code=?',(code,))[0]
                    if not json.loads(raw_item['metadata']).get('identity_verified'):
                        identity_key = 'identity_attempt:'+code
                        previous = self.store.rows('SELECT value FROM settings WHERE key=?',(identity_key,))
                        if previous and instant-datetime.fromisoformat(previous[0]['value']) < timedelta(minutes=30):
                            continue
                        with self.store.connect(True) as c:
                            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(identity_key,instant.isoformat()))
                    item = self.syncer.ensure_identity(code)
                    expected = self.syncer.calendar.expected(item["calendar"],instant,grace_for(item))
                    # Weekdays refresh all watches; a Friday US session published on
                    # Saturday local time is also eligible. Local holidays stay calendar-aware.
                    published = self.syncer.calendar.published_at(item["calendar"],expected,grace_for(item)).astimezone(ZoneInfo("Asia/Hong_Kong"))
                    daily_key = "auto_sync:"+code
                    completed = self.store.rows("SELECT value FROM settings WHERE key=?", (daily_key,))
                    repairs = self.store.rows('SELECT value FROM settings WHERE key=?',('history_repair:'+code,))
                    repair = json.loads(repairs[0]['value']) if repairs else {}
                    repair_due = bool(repair.get('pending') or repair.get('missing_sessions',0)>0) and repair.get('next_due','') <= instant.isoformat()
                    if local.weekday() >= 5 and published.date() != local.date() and not repair_due:
                        self.enqueue_inspections(assignments,code,expected,instant)
                        continue
                    if completed and json.loads(completed[0]["value"]) == {"day":local.date().isoformat(),"expected":expected} and not repair_due:
                        self.enqueue_inspections(assignments,code,expected,instant)
                        continue
                    attempt_key = "sync_attempt:"+code
                    old = self.store.rows("SELECT value FROM settings WHERE key=?", (attempt_key,))
                    if old and instant-datetime.fromisoformat(old[0]["value"]) < timedelta(minutes=2 if repair_due else 30):
                        continue
                    with self.store.connect(True) as c:
                        c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (attempt_key,instant.isoformat()))
                    with activity(self.store,self.owner,'sync',code=code,reason='repair' if repair_due else 'daily'):
                        detail = self.syncer.refresh(code,instant=instant)
                    with self.store.connect(True) as c:
                        c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (daily_key,canonical({"day":local.date().isoformat(),"expected":expected})))
                    self.store.health("sync","OK",f"{code}: {detail}")
                    if inspect_enabled or settings.get('daily_sync_enabled','true') == 'true':
                        self.enqueue_inspections(assignments,code,expected,instant)
                except Exception as exc:
                    self.store.health("sync","FAILED",f"{code}: {error_message(exc)}")
                finally:
                    self.mark_progress()
                if interleave:
                    # Do not leave completed-index inspections behind an entire
                    # multi-index history backfill. Bounded batches keep sync moving.
                    for _ in range(4):
                        if self.stopping.is_set() or not self.run_job():
                            break
                        processed += 1
                        self.notification_checkpoint()
        finally:
            self.release("scheduler")
        return processed

    def complete_job(self, job, evaluated):
        """Consume late AI upgrades and complete atomically with enqueue()."""
        handled = False
        while True:
            with self.store.connect(True) as c:
                live = c.execute("SELECT j.id,a.active FROM jobs j JOIN assignments a ON a.id=j.assignment_id WHERE j.id=? AND j.status='RUNNING' AND j.claim_owner=?",(job['id'],self.owner)).fetchone()
                if not live:
                    return
                if not live['active']:
                    c.execute("UPDATE jobs SET status='CANCELLED',error='对应监控或规则已停用',claim_until=NULL,updated_at=? WHERE id=?",(now(),job['id']))
                    return
                stored = c.execute('SELECT value FROM settings WHERE key=?',('request:'+job['id'],)).fetchone()
                request = json.loads(stored[0]) if stored else {}
                need_ai = bool(evaluated and request.get('include_ai') and not handled)
                if not need_ai:
                    detail = '处理完成，请查看数据与解读'
                    if request.get('force_repair'):
                        repair = c.execute("SELECT r.value FROM settings r JOIN assignments a ON r.key='history_repair:'||a.code WHERE a.id=?",(job['assignment_id'],)).fetchone()
                        if repair:
                            state = json.loads(repair[0])
                            detail = f"本轮补回或更新 {state.get('changed_sessions',0)} 条行情；历史仍有 {state.get('missing_sessions',0)} 个缺口，"+('后台将继续分批补齐' if state.get('pending') else '请查看最新判断')
                    if evaluated and request.get('include_ai'):
                        from .explanations import model_identity
                        ai = c.execute('SELECT status FROM explanations WHERE decision_id=? AND model=? ORDER BY id DESC LIMIT 1',(evaluated[1],model_identity())).fetchone()
                        if not ai or ai[0] != 'MODEL_OK':
                            detail = '巡检已完成；AI未成功，已保留程序解读，可单独重试AI'
                    stamp = now()
                    c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",('progress:'+job['id'],canonical({'phase':'done','detail':detail,'updated_at':stamp})))
                    c.execute("UPDATE jobs SET status='SUCCEEDED',error=NULL,claim_until=NULL,updated_at=? WHERE id=?",(stamp,job['id']))
                    return
            from .adapters import llm_explanation
            from .explanations import explain, model_identity, ExplanationBusy
            progress(self.store,job['id'],self.owner,'explaining','巡检已完成，正在生成AI解读')
            try:
                explain(self.store,evaluated[1],llm_explanation,model_identity())
            except ExplanationBusy:
                raise
            except Exception:
                self.store.health('explanation','FAILED','AI解读未完成，已保留程序解读')
            handled = True

    def run_job(self, user_id=None):
        instant = datetime.now(timezone.utc)
        reconcile(self.store,user_id,instant)
        with self.store.connect(True) as c:
            c.execute("UPDATE jobs SET status='FAILED',error='工作进程在最后一次尝试中终止；租约已过期',updated_at=? WHERE status='RUNNING' AND attempts>=3 AND claim_until<? AND (? IS NULL OR assignment_id IN (SELECT id FROM assignments WHERE user_id=?))", (instant.isoformat(),instant.isoformat(),user_id,user_id))
            retry_before = (instant-timedelta(seconds=30)).isoformat()
            job = c.execute("SELECT j.*,a.user_id,a.code FROM jobs j JOIN assignments a ON a.id=j.assignment_id WHERE a.active=1 AND (? IS NULL OR a.user_id=?) AND j.attempts<3 AND (j.status='PENDING' OR (j.status='RETRY' AND j.updated_at<=?) OR (j.status='RUNNING' AND COALESCE(j.claim_until,'')<=?)) ORDER BY j.created_at,j.id LIMIT 1", (user_id,user_id,retry_before,instant.isoformat())).fetchone()
            if not job:
                return False
            c.execute("UPDATE jobs SET status='RUNNING',attempts=attempts+1,claim_owner=?,claim_until=?,updated_at=? WHERE id=?", (self.owner,(instant+timedelta(minutes=5)).isoformat(),now(),job["id"]))
            c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('progress:'+job['id'],canonical({'phase':'starting','detail':'后台已接收，正在准备本次任务','updated_at':now()})))
        with activity(self.store,self.owner,'job',job_id=job['id'],user_id=job['user_id']):
            return self.execute_job(job)

    def execute_job(self, job):
        try:
            stop = threading.Event()
            def keepalive():
                while not stop.wait(10):
                    try:
                        self.pulse()
                        with self.store.connect(True) as c:
                            c.execute("UPDATE jobs SET claim_until=? WHERE id=? AND claim_owner=? AND status='RUNNING'", ((datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),job["id"],self.owner))
                    except Exception:
                        return
            heartbeat = threading.Thread(target=keepalive,daemon=True)
            heartbeat.start()
            request = request_for(self.store,job["id"])
            assignment = self.store.rows("SELECT code,user_id FROM assignments WHERE id=?", (job["assignment_id"],))[0]
            if request.get("refresh"):
                progress(self.store,job["id"],self.owner,"syncing","正在更新到该市场最近已发布的交易日")
                options = {'force_repair':True} if request.get('force_repair') else {}
                self.syncer.refresh(assignment["code"],start=request.get("start"),**options)
            evaluated = None
            if request.get('kind') == 'explanation':
                owned = self.store.rows('SELECT d.id,s.id signal_id FROM personal_decisions d JOIN signal_runs s ON s.id=d.signal_id WHERE d.id=? AND d.user_id=? AND s.assignment_id=?',(request.get('decision_id'),assignment['user_id'],job['assignment_id']))
                if not owned:
                    raise PermissionError('不能生成其他用户或规则的解读')
                evaluated = (owned[0]['signal_id'],owned[0]['id'])
            elif request.get("kind") != "sync":
                progress(self.store,job["id"],self.owner,"analyzing","校验行情并计算指数信号与个人判断")
                evaluated = self.evaluator.evaluate(job["assignment_id"])
            self.complete_job(job,evaluated)
            status,error = "SUCCEEDED",None
        except Exception as exc:
            status = "FAILED" if job["attempts"]+1 >= 3 else "RETRY"
            error = error_message(exc)
            progress(self.store,job["id"],self.owner,"failed",error)
            uid = self.store.rows("SELECT user_id FROM assignments WHERE id=?", (job["assignment_id"],))[0]["user_id"]
            self.store.health("inspection","FAILED",error,uid)
        finally:
            if 'stop' in locals():
                stop.set()
                heartbeat.join(timeout=2)
        with self.store.connect(True) as c:
            c.execute("UPDATE jobs SET status=?,error=?,claim_until=NULL,updated_at=? WHERE id=? AND claim_owner=? AND status='RUNNING'", (status,error,now(),job["id"],self.owner))
        self.mark_progress()
        return True

    def deliver(self, instant=None):
        instant = instant or datetime.now(timezone.utc)
        with self.store.connect(True) as c:
            c.execute("UPDATE outbox SET status='FAILED',claim_owner=NULL,claim_until=NULL WHERE attempts>=3 AND (status IN ('PENDING','RETRY') OR (status='SENDING' AND COALESCE(claim_until,'')<=?))",(instant.isoformat(),))
            from .webhook_schedule import allowed as schedule_allowed
            schedules = {}
            def due(candidate):
                key = (candidate['user_id'],candidate['channel_id'])
                if key not in schedules:
                    schedules[key] = schedule_allowed(c,*key,instant)
                return schedules[key]
            candidates = c.execute("SELECT o.*,c.url FROM outbox o JOIN channels c ON c.id=o.channel_id JOIN personal_decisions d ON d.id=o.decision_id WHERE c.enabled=1 AND c.user_id=o.user_id AND d.user_id=o.user_id AND o.attempts<3 AND o.next_at<=? AND (o.status IN ('PENDING','RETRY') OR (o.status='SENDING' AND o.claim_until<?)) ORDER BY o.id", (instant.isoformat(),instant.isoformat()))
            row = next((candidate for candidate in candidates if due(candidate)),None)
            if not row:
                return False
            decision = json.loads(c.execute("SELECT result FROM personal_decisions WHERE id=?", (row["decision_id"],)).fetchone()[0])
            signal = c.execute("SELECT a.*,s.as_of,s.id signal_id FROM personal_decisions d JOIN signal_runs s ON s.id=d.signal_id JOIN assignments a ON a.id=s.assignment_id WHERE d.id=?", (row["decision_id"],)).fetchone()
            refs = {name:c.execute(f"SELECT MAX(id) FROM {table} WHERE user_id=? AND code=?", (row["user_id"],signal["code"])).fetchone()[0] for table,name in (("position_reports","report"),("budgets","budget"),("product_mappings","mapping"))}
            from .decision_lifecycle import evidence_current, feedback as episode_feedback
            handled = episode_feedback(c,row['user_id'],row['decision_id'])
            feedback = handled[0] if handled else None
            projection = c.execute("SELECT signal_id FROM current_signals WHERE assignment_id=?", (signal["id"],)).fetchone()
            item = dict(c.execute("SELECT * FROM instruments WHERE code=?", (signal["code"],)).fetchone())
            try:
                fresh = self.evaluator.calendar.expected(item["calendar"],instant,grace_for(item)) == signal["as_of"] and evidence_current(c,signal['signal_id'],signal['code'],signal['as_of'],instant)
            except Exception:
                fresh = False
            mode = channel_mode(c,row['channel_id'],row['user_id'],True)
            if not allows(mode,decision.get('action')) or decision['status']!='AVAILABLE' or signal['stage']!='ENABLED' or signal['user_id']!=row['user_id'] or not fresh or decision["expires_at"] <= instant.isoformat() or not signal["active"] or refs != decision.get("context_refs") or not projection or projection[0] != signal["signal_id"] or (feedback and feedback['status'] in ("SUBMITTED","EXECUTED","PARTIAL","DEFERRED","EXPIRED","CANCELLED")):
                c.execute("UPDATE outbox SET status='EXPIRED' WHERE id=?", (row["id"],))
                return True
            c.execute("UPDATE outbox SET status='SENDING',attempts=attempts+1,claim_until=?,claim_owner=? WHERE id=?", ((instant+timedelta(seconds=45)).isoformat(),self.owner,row["id"]))
        try:
            # Rebuild even old queued messages in the new minimal format.
            with activity(self.store,self.owner,'notification'):
                self.sender(row["url"],operation_payload(item['name'],decision['action']))
            status,detail = "SENT","HTTP及飞书业务码均成功"
        except Exception as exc:
            status = "FAILED" if row["attempts"]+1 >= 3 else "RETRY"
            detail = '投递失败：'+webhook_failure_detail(exc)
            self.store.health("notification",status,detail,row["user_id"])
        with self.store.connect(True) as c:
            c.execute("UPDATE outbox SET status=?,next_at=?,claim_until=NULL WHERE id=? AND claim_owner=? AND status='SENDING'", (status,(instant+timedelta(seconds=30*2**row["attempts"])).isoformat(),row["id"],self.owner))
            c.execute("INSERT INTO delivery_attempts(outbox_id,status,detail,created_at) VALUES(?,?,?,?)", (row["id"],status,detail,now()))
        self.mark_progress()
        return True

    def notification_checkpoint(self, force=False, maximum=4):
        """Bounded time-sensitive work, including opportunity Cron windows."""
        point = time.monotonic()
        if not force and self.last_notice_check is not None and point-self.last_notice_check<10:
            return 0
        self.last_notice_check = point
        processed = 0
        for _ in range(maximum):
            if self.stopping.is_set() or not self.deliver():
                break
            processed += 1
        if not self.stopping.is_set() and self.lease('radar_notices',seconds=120):
            try:
                from .radar_notifications import deliver
                for _ in range(min(maximum,4)):
                    if self.stopping.is_set():
                        break
                    with activity(self.store,self.owner,'notification'):
                        if not deliver(self.store,self.sender,self.syncer.calendar):
                            break
                    processed += 1
                    self.pulse()
            finally:
                self.release('radar_notices')
        return processed

    def once(self):
        self.pulse()
        jobs = 0
        deliveries = self.notification_checkpoint(force=True)
        # User requests already queued take priority over periodic refresh.
        for _ in range(20):
            if self.stopping.is_set(): break
            if not self.run_job():
                break
            jobs += 1
            deliveries += self.notification_checkpoint()
        jobs += self.schedule(interleave=True) or 0
        # Bounded batches keep scheduling and operator shutdown responsive.
        for _ in range(100):
            if self.stopping.is_set(): break
            self.pulse()
            if not self.run_job():
                break
            jobs += 1
            deliveries += self.notification_checkpoint()
        for _ in range(100):
            if self.stopping.is_set(): break
            self.pulse()
            if not self.deliver():
                break
            deliveries += 1
        deliveries += self.notification_checkpoint(force=True)
        if not self.stopping.is_set() and self.lease("evolution",seconds=300):
            try:
                from .evolution import Evolution
                with activity(self.store,self.owner,'evolution'):
                    Evolution(self.store,self.evaluator,self.owner).tick()
            finally:
                self.release("evolution")
        if not self.stopping.is_set() and self.lease('opportunity_radar',seconds=180):
            try:
                from .radar import Radar
                def heartbeat():
                    self.pulse()
                    self.lease('opportunity_radar',seconds=180)
                    self.mark_progress()
                with activity(self.store,self.owner,'radar'):
                    Radar(self.store,self.syncer.calendar,self.syncer.provider).tick(heartbeat=heartbeat)
            except Exception:
                self.store.health('radar','WAITING','机会雷达本轮暂未完成，后台将自动重试，已有监控继续运行。')
            finally:
                self.release('opportunity_radar')
        deliveries += self.notification_checkpoint(force=True)
        return {"jobs_processed":jobs,"deliveries_processed":deliveries}

    def wait_for_work(self, interval):
        """Poll only the lightweight queue while idle, without rerunning periodic work."""
        deadline = time.monotonic()+interval
        while not self.stopping.is_set():
            instant = datetime.now(timezone.utc)
            ready = self.store.rows("SELECT j.id FROM jobs j JOIN assignments a ON a.id=j.assignment_id WHERE a.active=1 AND j.attempts<3 AND (j.status='PENDING' OR (j.status='RETRY' AND j.updated_at<=?) OR (j.status='RUNNING' AND COALESCE(j.claim_until,'')<=?)) LIMIT 1", ((instant-timedelta(seconds=30)).isoformat(),instant.isoformat()))
            remaining = deadline-time.monotonic()
            if ready or remaining<=0:
                return
            self.stopping.wait(min(2,remaining))

    def run(self, interval=30):
        failures = 0
        self.mark_progress()
        watchdog_stop = threading.Event()
        def watchdog():
            while not watchdog_stop.wait(30):
                if time.monotonic()-self.last_progress > 1200:
                    # Terminate only this process; Docker restarts it. Persisted
                    # tasks resume after their existing bounded leases expire.
                    import os
                    os._exit(1)
        guardian = threading.Thread(target=watchdog,daemon=True)
        guardian.start()
        try:
            while not self.stopping.is_set():
                try:
                    self.once()
                    failures = 0
                    self.mark_progress()
                except Exception:
                    failures += 1
                    self.store.health("worker","FAILED","后台循环异常；30秒后重试，查看任务状态及配置")
                    if failures >= 3:
                        raise RuntimeError('后台连续异常，退出以便云端重启') from None
                if failures:
                    self.stopping.wait(interval)
                else:
                    with activity(self.store,self.owner,'idle'):
                        self.wait_for_work(interval)
        finally:
            watchdog_stop.set()
            guardian.join(timeout=2)
            self.release("worker:"+self.owner)


def worker_status(store):
    heartbeats = store.rows("SELECT expires_at FROM leases WHERE name LIKE 'worker:%' AND expires_at>?", (now(),))
    return bool(heartbeats)
