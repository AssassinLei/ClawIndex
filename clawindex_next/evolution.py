"""Bounded experiments, genuinely forward observations and audited promotions."""
import json
import math
import time
import uuid
from datetime import datetime, timedelta, timezone

from .engine import feature_frame, features_at, replay, step, validate_spec
from .research import backtest, metrics
from .storage import canonical, digest
from .validation import policy_config, select_candidate, curve_summary, gates
from .factors import authorized, candidates, coverage, profile, diagnostics


def stamp(instant=None):
    return (instant or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def require_v2(store):
    if store.version < 2:
        raise ValueError("自动进化需先执行 upgrade --backup，升级保留现有数据与开关")


def frozen_prefix(manifest, day):
    return digest([o for r,o in zip(manifest["data"],manifest["observations"]) if r["date"] <= day])


def forward_assessment(points, policy, annual=252):
    for point in points:
        if not all(isinstance(point[k],(float,int)) and math.isfinite(point[k]) and point[k]>0 for k in ("candidate","incumbent","hold")):
            raise ValueError("模拟净值记录无效")
    summaries = {}
    for name in ("candidate","incumbent"):
        summaries[name] = curve_summary([1.]+[p[name] for p in points],points[-1][name+"_trades"],sum(p[name+"_exposure"] for p in points)/len(points),annual)
    hold = metrics([1.]+[p["hold"] for p in points],annual)
    exposure = summaries["candidate"]["exposure"]
    matched = metrics([1.]+[1+exposure*(p["hold"]-1) for p in points],annual)
    reasons = gates(summaries["candidate"],summaries["incumbent"],hold,matched,policy,True)
    return {"reasons":reasons,"summaries":summaries,"hold":hold,"matched_exposure":matched,"fixed_horizon":True}


def save_policy(store, uid, code, values, only_if_missing=False):
    require_v2(store)
    active = store.rows("SELECT a.*,s.spec FROM assignments a JOIN strategy_versions s ON s.id=a.strategy_id WHERE user_id=? AND code=? AND active=1", (uid,code))
    if not active:
        raise PermissionError("请先添加自己的监控")
    spec = json.loads(active[0]["spec"])
    config = policy_config(values,spec)
    payload = {"config":config,"anchor_spec":spec,"objective":"DRAWDOWN_FIRST","basis":"INDEX_PROXY","version":"evolution_v2"}
    identity = digest({"user":uid,"code":code,"payload":payload,"at":stamp(),"nonce":uuid.uuid4().hex})
    with store.connect(True) as c:
        if only_if_missing:
            existing = c.execute('SELECT id FROM evolution_policies WHERE user_id=? AND code=? ORDER BY active DESC,created_at DESC,id DESC LIMIT 1',(uid,code)).fetchone()
            if existing:
                return existing[0]
        existing = c.execute('SELECT id,payload FROM evolution_policies WHERE user_id=? AND code=? AND active=1',(uid,code)).fetchone()
        if existing and canonical(json.loads(existing['payload'])) == canonical(payload):
            # Saving unchanged settings must not cancel months of observation.
            return existing['id']
        c.execute("UPDATE evolution_candidates SET status='QUARANTINED',updated_at=? WHERE id IN (SELECT r.id FROM evolution_runs r JOIN evolution_policies p ON p.id=r.policy_id WHERE p.user_id=? AND p.code=?) AND status IN ('FORWARD_SHADOW','ELIGIBLE','READY_TO_APPLY')", (stamp(),uid,code))
        c.execute("UPDATE evolution_runs SET status='CANCELLED',updated_at=? WHERE policy_id IN (SELECT id FROM evolution_policies WHERE user_id=? AND code=?) AND status IN ('PENDING','RUNNING','WAITING_DATA')", (stamp(),uid,code))
        c.execute("UPDATE evolution_policies SET active=0 WHERE user_id=? AND code=?", (uid,code))
        c.execute("INSERT INTO evolution_policies VALUES(?,?,?,?,?,?)", (identity,uid,code,1,canonical(payload),stamp()))
    return identity


def advance(portfolio, feature, spec, day, policy, executable=True):
    p = dict(portfolio)
    p["pending"] = dict(p["pending"]) if p["pending"] else None
    fee = policy["cost_bps"]/10000
    price,close = feature["open"],feature["close"]
    if p["pending"] and executable:
        p["pending"]["remaining"] -= 1
        if p["pending"]["remaining"] <= 0:
            if p["pending"]["action"] == "BUY" and not p["units"]:
                p["units"],p["cash"] = p["cash"]/(1+fee)/price,0.0
                p["trades"] += 1
            elif p["pending"]["action"] == "SELL" and p["units"]:
                p["cash"],p["units"] = p["units"]*price*(1-fee),0.0
                p["trades"] += 1
            p["pending"] = None
    signal = step(feature,p["model"],spec,day,p["trigger"])
    p["model"],p["trigger"] = signal["model_state"],signal["triggered_at"]
    desired = p["model"] == "LONG"
    if p["pending"] and (p["pending"]["action"] == "BUY") != desired:
        p["pending"] = None
    if desired != bool(p["units"]) and not p["pending"]:
        p["pending"] = {"action":"BUY" if desired else "SELL","remaining":policy["delay"]}
    p["long_sessions"] += int(bool(p["units"]))
    return p,p["cash"]+p["units"]*close,signal


class Evolution:
    def __init__(self, store, evaluator, owner=None):
        self.store,self.evaluator = store,evaluator
        self.owner = owner or uuid.uuid4().hex

    def plan(self, assignment, payload, instant):
        """One bounded, identical historical experiment for both entry points."""
        config = payload['config']
        base = json.loads(assignment['spec'])
        if not authorized(base,payload['anchor_spec'],config):
            raise ValueError('现行规则超出授权范围，请重新配置')
        manifest,quality = self.evaluator.inputs(assignment['code'],instant)
        if quality != 'OK':
            raise ValueError('行情尚未更新至已发布时段，后台会自动补齐后继续')
        mas = sorted({base['ma'],max(config['ma_min'],base['ma']-30),min(config['ma_max'],base['ma']+30)})
        costs = sorted({config['cost_bps'],min(500,config['cost_bps']+20)})
        delays = sorted({config['delay'],min(10,config['delay']+1)})
        common = max(max(mas),120 if config.get('factor_research') else 0)+base['slope_window']-1
        frame = feature_frame(manifest['data'],manifest['sessions'],base)
        if len(manifest['sessions'])-common < 504 or not frame.valid_price.all():
            raise ValueError('研究需完整预热和至少504个共同评估交易时段；后台会自动补齐可获取历史')
        variants,availability = candidates(base,config,frame,manifest['sessions'][common])
        if profile(base) and not availability[profile(base)]['eligible']:
            raise ValueError('现行因子规则历史暂未完整，后台补齐后自动重试')
        plan = [{'spec':spec,'cost':cost,'delay':delay} for spec in variants for cost in costs for delay in delays]
        result = {'base_spec':base,'plan':plan,'factor_coverage':availability,'evaluation_start':manifest['sessions'][common],
                  'policy':config,'data_cutoff':stamp(instant),'prefix_hash':frozen_prefix(manifest,manifest['as_of']),'test_consumed':False}
        if config.get('factor_research'):
            result['factor_diagnostics'] = diagnostics(frame,manifest['sessions'][common])
        return manifest,result

    def request(self, policy_id, instant=None, trigger='AUTOMATIC'):
        require_v2(self.store)
        instant = instant or datetime.now(timezone.utc)
        policies = self.store.rows("SELECT * FROM evolution_policies WHERE id=? AND active=1", (policy_id,))
        if not policies:
            raise ValueError("自动进化配置已失效")
        policy = policies[0]
        payload = json.loads(policy["payload"])
        config = payload["config"]
        if not config["enabled"]:
            raise ValueError("请先开启自动研究")
        outstanding = self.store.rows("SELECT r.id FROM evolution_runs r LEFT JOIN evolution_candidates c ON c.id=r.id WHERE r.policy_id=? AND (r.status IN ('PENDING','RUNNING') OR c.status IN ('FORWARD_SHADOW','ELIGIBLE'))", (policy_id,))
        if outstanding:
            return outstanding[0]["id"]
        assignment = self.store.rows("SELECT a.*,s.spec FROM assignments a JOIN strategy_versions s ON s.id=a.strategy_id WHERE a.user_id=? AND a.code=? AND active=1", (policy["user_id"],policy["code"]))
        if not assignment:
            raise ValueError("监控已停止")
        assignment = assignment[0]
        manifest,run_payload = self.plan(assignment,payload,instant)
        if trigger not in ('AUTOMATIC','MANUAL_RESEARCH'):
            raise ValueError('研究触发方式不正确')
        run_payload['trigger'] = trigger
        plan = run_payload['plan']
        rid = digest({"policy":policy_id,"assignment":assignment["id"],"cycle":instant.date().isoformat(),"dataset":digest(manifest)})
        with self.store.connect(True) as c:
            # Recheck under the writer lock: two requests cannot reserve two experiments.
            existing = c.execute("SELECT r.id FROM evolution_runs r LEFT JOIN evolution_candidates k ON k.id=r.id WHERE r.policy_id=? AND (r.status IN ('PENDING','RUNNING') OR k.status IN ('FORWARD_SHADOW','ELIGIBLE'))", (policy_id,)).fetchone()
            if existing:
                return existing[0]
            if not c.execute("SELECT id FROM evolution_policies WHERE id=? AND active=1", (policy_id,)).fetchone():
                raise ValueError("配置已更新，请刷新")
            reserved = c.execute("SELECT COALESCE(SUM(json_array_length(r.payload,'$.plan')),0) FROM evolution_runs r JOIN evolution_policies p ON p.id=r.policy_id WHERE p.user_id=? AND p.code=? AND r.created_at>=? AND json_extract(r.payload,'$.mode') IS NULL", (policy["user_id"],policy["code"],stamp(instant-timedelta(days=365)))).fetchone()[0]
            if reserved+len(plan) > config["annual_trial_budget"]:
                raise ValueError("已达年度试验预算，保留现行策略")
            last = c.execute("SELECT created_at FROM evolution_runs WHERE policy_id=? AND json_extract(payload,'$.mode') IS NULL ORDER BY created_at DESC LIMIT 1", (policy_id,)).fetchone()
            if last and instant-datetime.fromisoformat(last[0]) < timedelta(days=config["research_days"]):
                raise ValueError("本研究周期已使用，不能反复筛选同一测试区间")
            c.execute("INSERT INTO evolution_runs(id,policy_id,assignment_id,status,manifest,payload,created_at,updated_at) VALUES(?,?,?,'PENDING',?,?,?,?)", (rid,policy_id,assignment["id"],canonical(manifest),canonical(run_payload),stamp(instant),stamp(instant)))
        return rid

    def schedule(self, instant=None):
        if self.store.version < 2:
            return
        instant = instant or datetime.now(timezone.utc)
        from .tasks import enqueue
        daily = self.store.rows("SELECT value FROM settings WHERE key='daily_sync_enabled'")
        if not daily or daily[0]['value'] == 'true':
            # Start research for new/unconfigured watches without enabling trades
            # or notifications. A paused/retired historical policy is an opt-out,
            # not a missing configuration to silently recreate.
            defaults = self.store.rows('SELECT DISTINCT a.user_id,a.code FROM assignments a WHERE active=1 AND NOT EXISTS(SELECT 1 FROM evolution_policies p WHERE p.user_id=a.user_id AND p.code=a.code)')
            for default in defaults:
                save_policy(self.store,default['user_id'],default['code'],{'auto_promote':False},only_if_missing=True)
        policies = self.store.rows("SELECT * FROM evolution_policies WHERE active=1")
        for row in policies:
            config = json.loads(row["payload"])["config"]
            if not config["enabled"]:
                continue
            check_key = "evolution_check:"+row["id"]
            with self.store.connect(True) as c:
                previous = c.execute("SELECT value FROM settings WHERE key=?", (check_key,)).fetchone()
                if previous and instant-datetime.fromisoformat(previous[0]) < timedelta(minutes=30):
                    continue
                c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (check_key,stamp(instant)))
            try:
                manifest,quality = self.evaluator.inputs(row["code"],instant)
                if quality == "OK" and config["auto_inspect"]:
                    assignments = self.store.rows("SELECT id FROM assignments WHERE user_id=? AND code=? AND active=1", (row["user_id"],row["code"]))
                    if assignments:
                        aid = assignments[0]["id"]
                        context = [self.store.rows(f"SELECT MAX(id) v FROM {t} WHERE user_id=? AND code=?", (row["user_id"],row["code"]))[0]["v"] for t in ("position_reports","budgets","product_mappings")]
                        key = "evolution_inspect:"+row["id"]
                        value = digest({"aid":aid,"as_of":manifest["as_of"],"rev":manifest["observations"],"context":context,"evaluation_day":instant.date().isoformat()})
                        previous = self.store.rows("SELECT value FROM settings WHERE key=?", (key,))
                        if not previous or previous[0]["value"] != value:
                            enqueue(self.store,aid,refresh=False)
                            with self.store.connect(True) as c:
                                c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,value))
                manual = self.store.rows("SELECT r.id FROM evolution_runs r LEFT JOIN evolution_candidates k ON k.id=r.id WHERE r.policy_id=? AND json_extract(r.payload,'$.mode')='DAILY_APPLY' AND (r.status IN ('WAITING_DATA','PENDING','RUNNING') OR k.status='READY_TO_APPLY')",(row['id'],))
                if manual:
                    continue
                last = self.store.rows("SELECT created_at FROM evolution_runs WHERE policy_id=? AND json_extract(payload,'$.mode') IS NULL ORDER BY created_at DESC LIMIT 1", (row["id"],))
                if not last or instant-datetime.fromisoformat(last[0]["created_at"]) >= timedelta(days=config["research_days"]):
                    self.request(row["id"],instant)
            except (ValueError,RuntimeError):
                self.store.health("evolution","WAITING","自动研究等待新鲜且完整的历史、授权范围或研究预算",row["user_id"])

    def run_trial(self, instant=None):
        if self.store.version < 2:
            return False
        instant = instant or datetime.now(timezone.utc)
        started = time.monotonic()
        with self.store.connect(True) as c:
            row = c.execute("SELECT r.* FROM evolution_runs r JOIN evolution_policies p ON p.id=r.policy_id JOIN assignments a ON a.id=r.assignment_id WHERE p.active=1 AND a.active=1 AND (r.status='PENDING' OR (r.status='RUNNING' AND r.claim_until<?)) AND (json_extract(r.payload,'$.retry_after') IS NULL OR json_extract(r.payload,'$.retry_after')<=?) ORDER BY CASE WHEN json_extract(r.payload,'$.mode')='DAILY_APPLY' THEN 0 ELSE 1 END,r.created_at LIMIT 1", (stamp(instant),stamp(instant))).fetchone()
            if not row:
                return False
            run = dict(row)
            c.execute("UPDATE evolution_runs SET status='RUNNING',claim_owner=?,claim_until=?,attempts=attempts+1 WHERE id=?", (self.owner,stamp(instant+timedelta(minutes=5)),run["id"]))
        try:
            config = json.loads(run["payload"])
            manifest = json.loads(run["manifest"])
            if run["cursor"] >= len(config["plan"]):
                self.finish(run["id"],instant)
                return True
            trial = config["plan"][run["cursor"]]
            report = backtest(manifest["data"],manifest["sessions"],trial["spec"],trial["cost"],trial["delay"],config["evaluation_start"])
            with self.store.connect(True) as c:
                end_instant = instant+timedelta(seconds=time.monotonic()-started)
                current = c.execute("SELECT cursor FROM evolution_runs WHERE id=? AND status='RUNNING' AND claim_owner=? AND claim_until>=?", (run["id"],self.owner,stamp(end_instant))).fetchone()
                if not current or current[0] != run["cursor"]:
                    return False
                c.execute("INSERT OR IGNORE INTO evolution_trials VALUES(?,?,?)", (run["id"],run["cursor"],canonical(report)))
                c.execute("UPDATE evolution_runs SET cursor=cursor+1,status='PENDING',attempts=0,claim_until=NULL,updated_at=? WHERE id=?", (stamp(instant),run["id"]))
            if run["cursor"]+1 == len(config["plan"]):
                self.finish(run["id"],instant)
        except Exception as exc:
            with self.store.connect(True) as c:
                c.execute("UPDATE evolution_runs SET status=?,claim_until=NULL,error=?,updated_at=? WHERE id=? AND status IN ('RUNNING','PENDING') AND claim_owner=?", ("FAILED" if run["attempts"] >= 2 else "PENDING",f"研究失败：{type(exc).__name__}",stamp(instant),run["id"],self.owner))
        return True

    def finish(self, rid, instant=None):
        instant = instant or datetime.now(timezone.utc)
        run = self.store.rows("SELECT * FROM evolution_runs WHERE id=?", (rid,))[0]
        config,manifest = json.loads(run["payload"]),json.loads(run["manifest"])
        config.pop('retry_after',None)
        reports = [json.loads(r["payload"]) for r in self.store.rows("SELECT payload FROM evolution_trials WHERE run_id=? ORDER BY scenario", (rid,))]
        if len(reports) != len(config["plan"]):
            raise ValueError("试验记录尚未完整，不得冻结候选")
        if config.get('mode') == 'DAILY_APPLY':
            from .daily_evolution import finish
            finish(self,run,reports,instant)
            return
        chosen,assessment = select_candidate(reports,config["base_spec"],config["policy"])
        config.update(assessment=assessment,test_consumed=True)
        with self.store.connect(True) as c:
            if not c.execute("SELECT r.id FROM evolution_runs r JOIN evolution_policies p ON p.id=r.policy_id JOIN assignments a ON a.id=r.assignment_id WHERE r.id=? AND r.status IN ('PENDING','RUNNING') AND r.cursor=? AND p.active=1 AND a.active=1", (rid,len(config["plan"]))).fetchone():
                return
            if chosen:
                # Observation begins at the currently available day, never backfills
                # the days elapsed while historical experiments were running.
                current,quality = self.evaluator.inputs(manifest["code"],instant)
                if quality != "OK":
                    chosen = None
                    assessment["reasons"].append("完成研究时行情已过期，下周期重新检查")
                else:
                    from .factors import entry_filter
                    for checked_spec in (chosen,config['base_spec']):
                        if not profile(checked_spec):
                            continue
                        latest_feature = features_at(feature_frame(current['data'],current['sessions'],checked_spec),current['as_of'])
                        if not entry_filter(latest_feature,checked_spec)[1]:
                            config['retry_after'] = stamp(instant+timedelta(minutes=30))
                            config['waiting_reason'] = '等待最新专业因子发布，后台自动续接'
                            c.execute("UPDATE evolution_runs SET status='PENDING',claim_until=NULL,payload=?,updated_at=? WHERE id=?",(canonical(config),stamp(instant),rid))
                            return
                    portfolios = {}
                    for name,spec in (("candidate",chosen),("incumbent",config["base_spec"])):
                        signal = replay(feature_frame(current["data"],current["sessions"],spec),spec)[-1]
                        if signal["data_status"] != "OK":
                            raise ValueError("冻结候选缺少完整有效窗口")
                        portfolios[name] = {"cash":1.,"units":0.,"model":signal["model_state"],"trigger":signal["triggered_at"],"pending":{"action":"BUY","remaining":config["policy"]["delay"]} if signal["model_state"] == "LONG" else None,"trades":0,"long_sessions":0}
                    payload = {"spec":chosen,"base_spec":config["base_spec"],"policy":config["policy"],"frozen_as_of":current["as_of"],"frozen_at":stamp(instant),"prefix_hash":frozen_prefix(current,current["as_of"]),"assessment":assessment,"basis":"INDEX_PROXY"}
                    state = {**portfolios,"hold":{"cash":1.,"units":0.,"remaining":config["policy"]["delay"]},"last_as_of":current["as_of"],'last_observed_at':stamp(instant),"count":0}
                    c.execute("INSERT OR IGNORE INTO evolution_candidates VALUES(?,'FORWARD_SHADOW',?,?,?,?)", (rid,canonical(payload),canonical(state),stamp(instant),stamp(instant)))
            config["assessment"] = assessment
            c.execute("UPDATE evolution_runs SET status=?,payload=?,updated_at=? WHERE id=?", ("FROZEN" if chosen else "REJECTED",canonical(config),stamp(instant),rid))

    def observe(self, rid, instant=None):
        instant = instant or datetime.now(timezone.utc)
        rows = self.store.rows("SELECT k.*,r.policy_id,r.assignment_id,p.active,p.user_id,p.code FROM evolution_candidates k JOIN evolution_runs r ON r.id=k.id JOIN evolution_policies p ON p.id=r.policy_id WHERE k.id=?", (rid,))
        if not rows:
            return
        row = rows[0]
        if row["status"] != "FORWARD_SHADOW":
            return
        payload,state = json.loads(row["payload"]),json.loads(row["state"])
        if not payload.get('frozen_at'):
            self.quarantine(rid,'模拟成交时间校验已升级，旧观察保留但不再用于晋升；后续研究重新观察')
            return
        if not row["active"] or not self.store.rows("SELECT id FROM assignments WHERE id=? AND active=1", (row["assignment_id"],)):
            self.quarantine(rid,"策略或授权已更新")
            return
        manifest,quality = self.evaluator.inputs(row["code"],instant)
        if quality != "OK" or manifest["as_of"] <= state["last_as_of"]:
            return
        if frozen_prefix(manifest,payload["frozen_as_of"]) != payload["prefix_hash"]:
            self.quarantine(rid,"冻结前的行情发生修订，禁止改写前向观察")
            return
        from .factors import entry_filter
        for checked_spec in (payload['spec'],payload['base_spec']):
            if not profile(checked_spec):
                continue
            latest_feature = features_at(feature_frame(manifest['data'],manifest['sessions'],checked_spec),manifest['as_of'])
            if not entry_filter(latest_feature,checked_spec)[1]:
                # Same-session late publication can recover without fabricating
                # an observation. A skipped session still fails the gap check below.
                return
        days = self.evaluator.calendar.sessions(manifest["calendar"],state["last_as_of"],manifest["as_of"])
        if len(days) != 2:
            self.quarantine(rid,"后台错过观察交易日，不能用事后行情补写实时观察")
            return
        features = {name:features_at(feature_frame(manifest["data"],manifest["sessions"],spec),manifest["as_of"]) for name,spec in (("candidate",payload["spec"]),("incumbent",payload["base_spec"]))}
        if not all(f.get("strategy_ready") and f.get("valid_price") for f in features.values()):
            self.quarantine(rid,"前向观察遇到无效行情")
            return
        from .calendar import MarketCalendar
        calendar = self.evaluator.calendar if hasattr(self.evaluator.calendar,'opened_at') else MarketCalendar()
        opened = calendar.opened_at(manifest['calendar'],manifest['as_of'])
        executable = opened > max(datetime.fromisoformat(payload['frozen_at']),datetime.fromisoformat(state.get('last_observed_at',payload['frozen_at'])))
        point = {"observed_at":stamp(instant),"dataset":digest(manifest),"candidate":None,"incumbent":None,'open_after_freeze':executable}
        for name in ("candidate","incumbent"):
            state[name],point[name],signal = advance(state[name],features[name],payload["spec"] if name == "candidate" else payload["base_spec"],manifest["as_of"],payload["policy"],executable=executable)
            point[name+"_signal"] = signal["state_label"]
            point[name+"_trades"] = state[name]["trades"]
            point[name+"_exposure"] = float(bool(state[name]["units"]))
        hold = state["hold"]
        if not hold["units"] and executable:
            hold["remaining"] -= 1
            if hold["remaining"] <= 0:
                hold["units"],hold["cash"] = hold["cash"]/(1+payload["policy"]["cost_bps"]/10000)/features["candidate"]["open"],0.
        point["hold"] = hold["cash"]+hold["units"]*features["candidate"]["close"]
        state["count"] += 1
        state["last_as_of"] = manifest["as_of"]
        state['last_observed_at'] = stamp(instant)
        status = "FORWARD_SHADOW"
        if state["count"] == payload["policy"]["shadow_sessions"]:
            points = [json.loads(x["payload"]) for x in self.store.rows("SELECT payload FROM evolution_observations WHERE candidate_id=? ORDER BY as_of", (rid,))]+[point]
            payload["forward_assessment"] = forward_assessment(points,payload["policy"],payload["spec"]["annual_sessions"])
            status = "ELIGIBLE" if not payload["forward_assessment"]["reasons"] else "REJECTED"
        with self.store.connect(True) as c:
            existing = c.execute("SELECT state FROM evolution_candidates WHERE id=? AND status='FORWARD_SHADOW'", (rid,)).fetchone()
            if not existing or json.loads(existing[0])["count"] != state["count"]-1:
                return
            c.execute("INSERT INTO evolution_observations VALUES(?,?,?)", (rid,manifest["as_of"],canonical(point)))
            c.execute("INSERT OR IGNORE INTO datasets VALUES(?,?,?,?,?)", (point["dataset"],row["code"],manifest["as_of"],canonical(manifest),stamp(instant)))
            c.execute("UPDATE evolution_candidates SET status=?,state=?,payload=?,updated_at=? WHERE id=?", (status,canonical(state),canonical(payload),stamp(instant),rid))

    def quarantine(self, rid, reason):
        with self.store.connect(True) as c:
            c.execute("UPDATE evolution_candidates SET status='QUARANTINED',payload=json_set(payload,'$.quarantine_reason',?),updated_at=? WHERE id=? AND status IN ('FORWARD_SHADOW','ELIGIBLE')", (reason,stamp(),rid))

    def promote(self, rid, instant=None):
        instant = instant or datetime.now(timezone.utc)
        require_v2(self.store)
        with self.store.connect(True) as c:
            row = c.execute("SELECT k.*,r.policy_id,r.assignment_id,p.user_id,p.code,p.active,p.payload policy_payload FROM evolution_candidates k JOIN evolution_runs r ON r.id=k.id JOIN evolution_policies p ON p.id=r.policy_id WHERE k.id=?", (rid,)).fetchone()
            if not row or row["status"] != "ELIGIBLE" or not row["active"]:
                return False
            policy_data = json.loads(row["policy_payload"])
            config = policy_data["config"]
            payload,state = json.loads(row["payload"]),json.loads(row["state"])
            if not payload.get('frozen_at'):
                c.execute("UPDATE evolution_candidates SET status='QUARANTINED',payload=json_set(payload,'$.quarantine_reason','模拟成交时间校验已升级，旧观察不再用于晋升'),updated_at=? WHERE id=?",(stamp(instant),rid))
                return False
            validate_spec(payload["spec"])
            if not authorized(payload['spec'],policy_data['anchor_spec'],config):
                return False
            if not config["enabled"] or not config["auto_promote"]:
                return False
            assessment = payload.get("forward_assessment",{})
            if not assessment or assessment.get("reasons") or state["count"] != config["shadow_sessions"] or not config["ma_min"] <= payload["spec"]["ma"] <= config["ma_max"]:
                return False
            observations = c.execute("SELECT as_of,payload FROM evolution_observations WHERE candidate_id=? ORDER BY as_of", (rid,)).fetchall()
            if len(observations) != config["shadow_sessions"] or observations[-1]["as_of"] != state["last_as_of"] or observations[0]["as_of"] <= payload["frozen_as_of"]:
                return False
            computed = forward_assessment([json.loads(o["payload"]) for o in observations],config,payload["spec"]["annual_sessions"])
            if computed["reasons"]:
                return False
            old = c.execute("SELECT * FROM assignments WHERE id=? AND active=1", (row["assignment_id"],)).fetchone()
            if not old or old["strategy_id"] != validate_spec(payload["base_spec"]):
                return False
            sending = c.execute("SELECT o.id FROM outbox o JOIN personal_decisions d ON d.id=o.decision_id JOIN signal_runs s ON s.id=d.signal_id WHERE s.assignment_id=? AND o.status='SENDING' AND o.claim_until>? LIMIT 1", (old["id"],stamp(instant))).fetchone()
            if sending:
                return False
            last = c.execute("SELECT created_at FROM strategy_promotions WHERE user_id=? AND code=? ORDER BY id DESC LIMIT 1", (row["user_id"],row["code"])).fetchone()
            if last and instant-datetime.fromisoformat(last[0]) < timedelta(days=config["promotion_days"]):
                return False
            manifest,quality = self.evaluator.inputs(row["code"],instant)
            if quality != "OK" or state["last_as_of"] != manifest["as_of"] or frozen_prefix(manifest,payload["frozen_as_of"]) != payload["prefix_hash"]:
                return False
            sid = validate_spec(payload["spec"])
            c.execute("INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?)", (sid,canonical(payload["spec"]),stamp(instant)))
            c.execute("UPDATE assignments SET active=0 WHERE id=?", (old["id"],))
            aid = c.execute("INSERT INTO assignments(user_id,code,strategy_id,stage,catchup,created_at) VALUES(?,?,?,'ENABLED',?,?)", (row["user_id"],row["code"],sid,old["catchup"],stamp(instant))).lastrowid
            c.execute("UPDATE outbox SET status='EXPIRED' WHERE decision_id IN (SELECT d.id FROM personal_decisions d JOIN signal_runs s ON s.id=d.signal_id WHERE s.assignment_id=?) AND status IN ('PENDING','RETRY','SENDING')", (old["id"],))
            audit = {"reason":"历史检验与固定期限前向观察通过","evidence":rid,"old_strategy":old["strategy_id"],"new_strategy":sid,"basis":"INDEX_PROXY","execution":"USER_CONFIRMATION_REQUIRED"}
            c.execute("INSERT INTO strategy_promotions(candidate_id,user_id,code,old_assignment,new_assignment,payload,created_at) VALUES(?,?,?,?,?,?,?)", (rid,row["user_id"],row["code"],old["id"],aid,canonical(audit),stamp(instant)))
            c.execute("UPDATE evolution_candidates SET status='ACTIVE',updated_at=? WHERE id=?", (stamp(instant),rid))
            # Re-evaluate from the fixed history; preserve real holdings and cash.
            jid = digest({"promotion":rid})
            c.execute("INSERT INTO jobs(id,assignment_id,status,created_at,updated_at) VALUES(?,?,'PENDING',?,?)", (jid,aid,stamp(instant),stamp(instant)))
        self.store.health("evolution","PROMOTED","候选已按授权更新；持仓和资金未自动改变",row["user_id"])
        return True

    def rollback(self, rid, uid, instant=None):
        """Explicit rollback creates a new assignment and immutable audit event."""
        require_v2(self.store)
        instant = instant or datetime.now(timezone.utc)
        with self.store.connect(True) as c:
            p = c.execute("SELECT * FROM strategy_promotions WHERE candidate_id=? AND user_id=?", (rid,uid)).fetchone()
            if not p:
                raise PermissionError("没有本账号可回退的策略更新")
            current = c.execute("SELECT * FROM assignments WHERE id=? AND active=1", (p["new_assignment"],)).fetchone()
            if not current:
                raise ValueError("已回退或之后已手动改规则，不能覆盖当前规则")
            old = c.execute("SELECT * FROM assignments WHERE id=?", (p["old_assignment"],)).fetchone()
            sending = c.execute("SELECT o.id FROM outbox o JOIN personal_decisions d ON d.id=o.decision_id JOIN signal_runs s ON s.id=d.signal_id WHERE s.assignment_id=? AND o.status='SENDING' AND o.claim_until>?", (current["id"],stamp(instant))).fetchone()
            if sending:
                raise ValueError("旧提醒正在投递，请稍后回退")
            c.execute("UPDATE assignments SET active=0 WHERE id=?", (current["id"],))
            aid = c.execute("INSERT INTO assignments(user_id,code,strategy_id,stage,catchup,created_at) VALUES(?,?,?,?,?,?)", (uid,old["code"],old["strategy_id"],old["stage"],old["catchup"],stamp(instant))).lastrowid
            c.execute("UPDATE outbox SET status='EXPIRED' WHERE decision_id IN (SELECT d.id FROM personal_decisions d JOIN signal_runs s ON s.id=d.signal_id WHERE s.assignment_id=?) AND status IN ('PENDING','RETRY','SENDING')", (current["id"],))
            c.execute("INSERT INTO strategy_events(candidate_id,user_id,code,kind,payload,created_at) VALUES(?,?,?,'ROLLBACK',?,?)", (rid,uid,old["code"],canonical({"from_assignment":current["id"],"to_assignment":aid,"reason":"用户明确回退","holds_unchanged":True}),stamp(instant)))
            c.execute("UPDATE evolution_candidates SET status='RETIRED',updated_at=? WHERE id=?", (stamp(instant),rid))
            # Disable automatic promotion until the user reaffirms the policy.
            c.execute("UPDATE evolution_policies SET active=0 WHERE user_id=? AND code=?", (uid,old["code"]))
            c.execute("INSERT INTO jobs(id,assignment_id,status,created_at,updated_at) VALUES(?,?,'PENDING',?,?)", (digest({"rollback":rid}),aid,stamp(instant),stamp(instant)))
        return aid

    def tick(self, instant=None):
        if self.store.version < 2:
            return
        with self.store.connect(True) as c:
            c.execute("UPDATE evolution_runs SET status='CANCELLED',error='监控、规则或授权已更新',updated_at=? WHERE status IN ('PENDING','RUNNING','WAITING_DATA') AND id IN (SELECT r.id FROM evolution_runs r JOIN evolution_policies p ON p.id=r.policy_id JOIN assignments a ON a.id=r.assignment_id WHERE p.active=0 OR a.active=0)", (stamp(instant),))
            c.execute("UPDATE evolution_candidates SET status='QUARANTINED',payload=json_set(payload,'$.quarantine_reason','监控、规则或授权已更新'),updated_at=? WHERE status IN ('FORWARD_SHADOW','ELIGIBLE') AND id IN (SELECT r.id FROM evolution_runs r JOIN evolution_policies p ON p.id=r.policy_id JOIN assignments a ON a.id=r.assignment_id WHERE p.active=0 OR a.active=0)", (stamp(instant),))
        self.schedule(instant)
        from .daily_evolution import prepare, apply
        prepare(self,instant)
        for candidate in self.store.rows("SELECT id FROM evolution_candidates WHERE status='READY_TO_APPLY'"):
            apply(self,candidate['id'],instant)
        for row in self.store.rows("SELECT id,status FROM evolution_candidates WHERE status IN ('FORWARD_SHADOW','ELIGIBLE') ORDER BY updated_at"):
            try:
                if row["status"] == "FORWARD_SHADOW":
                    self.observe(row["id"],instant)
                self.promote(row["id"],instant)
            except (ValueError,RuntimeError):
                self.quarantine(row["id"],"前向数据或策略状态异常，保留现行策略")
        if not self.store.rows("SELECT id FROM jobs WHERE status IN ('PENDING','RUNNING','RETRY') LIMIT 1"):
            self.run_trial(instant)
