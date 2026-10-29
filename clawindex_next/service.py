from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from .auth import Auth
from .calendar import MarketCalendar, grace_for
from .engine import ALGORITHM, DEFAULT_SPEC, feature_frame, features_at, money, quantity, personal_decision, replay, step, validate_spec
from .explanations import explain, model_identity
from .migration import insert_instrument, instrument
from .storage import canonical, digest, now
from .tasks import enqueue
from .presentation import label
from .webhooks import MODES, allows, channel_mode, filter_key, operation_payload, validate_url


class UserService:
    """Every user-facing read and mutation derives identity from a checked session."""
    def __init__(self, store, token):
        self.store,self.token = store,token

    @property
    def user(self):
        return Auth(self.store).user(self.token)

    def watchlist(self):
        return self.store.rows("SELECT a.*,i.name,i.market,i.asset_kind,i.metadata,s.spec FROM assignments a JOIN instruments i ON i.code=a.code JOIN strategy_versions s ON s.id=a.strategy_id WHERE a.user_id=? AND a.active=1 ORDER BY i.name", (self.user["id"],))

    def _assignment(self, assignment_id):
        rows = self.store.rows("SELECT * FROM assignments WHERE id=? AND user_id=?", (assignment_id,self.user["id"]))
        if not rows:
            raise PermissionError("不能访问其他用户的监控或历史")
        return rows[0]

    def _code(self, code):
        if not self.store.rows("SELECT id FROM assignments WHERE user_id=? AND code=?", (self.user["id"],code)):
            raise PermissionError("请先添加自己的监控")

    def assign(self, code, spec=None, stage="RESEARCH", catchup=False):
        if stage not in ("RESEARCH","SHADOW","ENABLED"):
            raise ValueError("策略阶段不正确")
        if not self.store.rows("SELECT code FROM instruments WHERE code=?", (code,)):
            raise ValueError("需要先核实并登记标的身份")
        spec = spec or DEFAULT_SPEC
        sid = validate_spec(spec)
        uid = self.user["id"]
        if stage == "ENABLED" and not self.store.rows("SELECT id FROM research_runs WHERE user_id=? AND code=? AND json_extract(payload,'$.strategy_id')=? AND json_extract(payload,'$.research_usable')=1", (uid,code,sid)):
            raise ValueError("开启前需审阅同规则的研究报告；历史缺口须修复且评估期至少一年。研究结果不是收益保证，仍可先用RESEARCH/SHADOW查看信号")
        with self.store.connect(True) as c:
            c.execute("INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?)", (sid,canonical(spec),now()))
            c.execute("UPDATE assignments SET active=0 WHERE user_id=? AND code=? AND active=1", (uid,code))
            return c.execute("INSERT INTO assignments(user_id,code,strategy_id,stage,catchup,created_at) VALUES(?,?,?,?,?,?)", (uid,code,sid,stage,int(catchup),now())).lastrowid

    def add_watch(self, code, provider=None, catchup=False):
        self.user
        code = code.strip().upper()
        existing = self.store.rows("SELECT code FROM instruments WHERE code=?", (code,))
        if not existing:
            from .catalog import entries
            known = next((item for item in entries(self.store) if item['code']==code),None)
            archived = self.store.rows("SELECT payload FROM legacy_archive WHERE table_name='industry_list' AND json_extract(payload,'$.index_code')=? LIMIT 1", (code,))
            industry = json.loads(archived[0]["payload"]) if archived else None
            item = known or instrument(code,code,industry)
            if not item["metadata"]["identity_verified"]:
                from .adapters import TushareAdapter
                item = (provider or TushareAdapter()).lookup(code)
            with self.store.connect(True) as c:
                insert_instrument(c,item)
        active = next((item for item in self.watchlist() if item['code']==code),None)
        if active:
            return active['id']
        aid = self.assign(code,catchup=catchup)
        enqueue(self.store,aid,refresh=True)
        return aid

    def search_indices(self, keyword=''):
        self.user
        from .catalog import search
        return search(self.store,keyword)

    def radar(self):
        from .radar import view
        return view(self.store,self.user['id'],self.watchlist(),MarketCalendar())

    def request_radar(self):
        from .radar import request
        return request(self.store,self.user['id'],MarketCalendar())

    def radar_preferences(self, hide_watched=None, ignore=None, restore=None):
        from .radar import preferences, write, universe
        uid = self.user['id']
        prefs = preferences(self.store,uid)
        if hide_watched is not None:
            if type(hide_watched) is not bool:
                raise ValueError('显示偏好必须为开关')
            prefs['hide_watched'] = hide_watched
        if ignore:
            if ignore not in {item['code'] for item in universe(self.store)}:
                raise ValueError('该指数不在L1、L2雷达范围内')
            prefs['ignored'] = sorted(set(prefs['ignored'])|{ignore})
        if restore:
            prefs['ignored'] = [code for code in prefs['ignored'] if code!=restore]
        write(self.store,'radar:preferences:'+str(uid),prefs)
        return True

    def add_radar_watch(self, code):
        state = self.radar()
        if code not in {r['code'] for r in state['candidates']}:
            raise ValueError('此机会已变化，请查看最新候选后再添加')
        return self.add_watch(code,catchup=True)

    def radar_history(self, limit=30):
        self.user
        limit = min(max(int(limit),1),100)
        return [json.loads(r['value']) for r in self.store.rows("SELECT value FROM settings WHERE key LIKE 'radar:snapshot:%' ORDER BY json_extract(value,'$.created_at') DESC,key DESC LIMIT ?",(limit,))]

    def radar_outcomes(self):
        self.user
        from .radar_tracking import outcomes
        return outcomes(self.store,MarketCalendar())

    def radar_channel_mode(self, channel_id):
        from .radar import read
        from .radar_notifications import config_key
        if channel_id not in {r['id'] for r in self.channels()}:
            raise PermissionError('不能查看其他账号的机会提醒设置')
        return read(self.store,config_key(self.user['id'],channel_id),'OFF')

    def set_radar_channel(self, channel_id, mode):
        from .radar_notifications import configure
        return configure(self.store,self.user['id'],channel_id,mode)

    def set_radar_enabled(self, enabled):
        Auth(self.store).user(self.token,admin=True)
        if type(enabled) is not bool:
            raise ValueError('自动扫描设置必须为开关')
        from .radar import write
        write(self.store,'radar:enabled',enabled)
        return True

    def sync(self, code, start=None, provider=None):
        from .adapters import TushareAdapter
        self._code(code)
        return (provider or TushareAdapter()).sync(self.store,code,start)

    def research(self, code, spec=None, goal="控制回撤并记录收益代价"):
        from .research import study
        self._code(code)
        manifest,_ = Evaluator(self.store).inputs(code)
        return study(self.store,self.user["id"],code,manifest,spec,goal=goal)

    def explanation(self, decision_id, use_ai=False):
        from .adapters import llm_explanation
        if not self.store.rows("SELECT id FROM personal_decisions WHERE id=? AND user_id=?", (decision_id,self.user["id"])):
            raise PermissionError("不能解释其他用户的记录")
        model = model_identity() if use_ai else "TEMPLATE"
        return explain(self.store,decision_id,llm_explanation if use_ai else None,model)

    def saved_explanation(self, decision_id):
        if not self.store.rows("SELECT id FROM personal_decisions WHERE id=? AND user_id=?", (decision_id,self.user["id"])):
            raise PermissionError("不能查看其他用户的解释")
        model = model_identity()
        rows = self.store.rows("SELECT status,payload,created_at FROM explanations WHERE decision_id=? AND (model=? OR model='TEMPLATE') ORDER BY CASE WHEN model=? THEN 0 ELSE 1 END,id DESC LIMIT 1", (decision_id,model,model))
        return {**rows[0],"payload":json.loads(rows[0]["payload"])} if rows else None

    def unwatch(self, assignment_id):
        self._assignment(assignment_id)
        with self.store.connect(True) as c:
            c.execute("UPDATE assignments SET active=0 WHERE id=?", (assignment_id,))

    def report_position(self, code, status, fields=None):
        self._code(code)
        if status not in ("UNKNOWN","EMPTY","HELD"):
            raise ValueError("必须明确选择未知、空仓或持有")
        fields = dict(fields or {})
        allowed = {"market_value","shares","currency","target_completion","unrealized_pnl_ratio","note"}
        if set(fields)-allowed:
            raise ValueError("未知持仓字段；不接受组合权重推断")
        for k in ("market_value","shares"):
            if fields.get(k) is not None:
                fields[k] = str(quantity(fields[k]) if k == "shares" else money(fields[k]))
        if fields.get("currency") not in (None,"CNY","HKD","USD"):
            raise ValueError("不支持的币种")
        if fields.get("market_value") is not None and not fields.get("currency"):
            raise ValueError("市值必须声明币种")
        if status != "HELD" and any(fields.get(k) is not None and quantity(fields[k])>0 for k in ("market_value","shares")):
            raise ValueError("未知或空仓不能同时填正持仓市值/份额")
        if fields.get("target_completion") is not None:
            ratio = float(fields["target_completion"])
            if not 0 <= ratio <= 1:
                raise ValueError("目标完成度必须在0至1之间")
        if fields.get("unrealized_pnl_ratio") is not None:
            ratio = float(fields["unrealized_pnl_ratio"])
            if not -1 <= ratio < 100:
                raise ValueError("手报浮盈亏比例无效")
        fields.update(status=status,basis="MANUAL_CONFIRMATION")
        with self.store.connect(True) as c:
            return c.execute("INSERT INTO position_reports(user_id,code,status,payload,reported_at) VALUES(?,?,?,?,?)", (self.user["id"],code,status,canonical(fields),now())).lastrowid

    def position(self, code):
        self._code(code)
        rows = self.store.rows("SELECT * FROM position_reports WHERE user_id=? AND code=? ORDER BY id DESC LIMIT 1", (self.user["id"],code))
        if not rows:
            return {"status":"EMPTY","basis":"DEFAULT_EMPTY","reported_at":now(),"id":None}
        report = {**json.loads(rows[0]["payload"]),"reported_at":rows[0]["reported_at"],"id":rows[0]["id"]}
        if report.get('basis') == 'LEGACY_MANUAL_UNCONFIRMED' and report['status'] == 'UNKNOWN':
            return {"status":"EMPTY","basis":"DEFAULT_EMPTY","reported_at":report['reported_at'],"id":report['id']}
        return report

    def set_budget(self, code, fields):
        self._code(code)
        if set(fields) != {"limit","used","cash","currency"} or fields["currency"] not in ("CNY","HKD","USD"):
            raise ValueError("预算需要上限、已投入、可用现金和币种，不能用指数推算")
        payload = {k:str(money(fields[k])) for k in ("limit","used","cash")}
        payload["currency"] = fields["currency"]
        with self.store.connect(True) as c:
            c.execute("INSERT INTO budgets(user_id,code,payload,reported_at) VALUES(?,?,?,?)", (self.user["id"],code,canonical(payload),now()))

    def set_mapping(self, code, fields):
        from urllib.parse import urlparse
        self._code(code)
        if set(fields) != {"product_code","currency","source_url","verified"} or not fields["product_code"] or fields["currency"] not in ("CNY","HKD","USD") or urlparse(fields["source_url"]).scheme != "https" or type(fields["verified"]) is not bool:
            raise ValueError("基金映射需要产品代码、币种、HTTPS官方资料链接和人工核实声明")
        with self.store.connect(True) as c:
            c.execute("INSERT INTO product_mappings(user_id,code,payload,created_at) VALUES(?,?,?,?)", (self.user["id"],code,canonical(fields),now()))

    def submit(self, assignment_id, instant=None, refresh=False, include_ai=False, kind="inspection", start=None):
        a = self._assignment(assignment_id)
        if not a["active"]:
            raise ValueError("监控已归档")
        if refresh:
            if kind not in ("inspection","sync"):
                raise ValueError("不支持的任务类型")
            if start:
                try:
                    datetime.strptime(start,"%Y-%m-%d")
                except ValueError:
                    raise ValueError("起始日期请填写年-月-日，例如2016-01-01") from None
            return enqueue(self.store,assignment_id,True,include_ai,kind,start)
        stamp = (instant or datetime.now(timezone.utc)).isoformat()
        # Snapshot revisions + personal context distinguish meaningful reruns.
        rows = self.store.rows("SELECT MAX(id) revision FROM observations WHERE code=?", (a["code"],))
        context = {t:self.store.rows(f"SELECT MAX(id) id FROM {t} WHERE user_id=? AND code=?", (a["user_id"],a["code"]))[0]["id"] for t in ("position_reports","budgets","product_mappings")}
        job_id = digest({"assignment":assignment_id,"revision":rows[0]["revision"],"context":context,"evaluation_day":stamp[:10]})
        with self.store.connect(True) as c:
            c.execute("INSERT OR IGNORE INTO jobs(id,assignment_id,status,created_at,updated_at) VALUES(?,?,'PENDING',?,?)", (job_id,assignment_id,stamp,stamp))
        return job_id

    def jobs(self, ids=None):
        from .tasks import reconcile
        from .background import snapshot, decorate_jobs
        uid = self.user['id']
        reconcile(self.store,uid)
        if ids is not None and not ids:
            return []
        condition = (' AND j.id IN ('+','.join('?' for _ in ids)+')') if ids is not None else ' AND a.active=1'
        params = (uid,*ids) if ids is not None else (uid,)
        rows = self.store.rows("SELECT j.*,a.code,i.name,s.value progress FROM jobs j JOIN assignments a ON a.id=j.assignment_id JOIN instruments i ON i.code=a.code LEFT JOIN settings s ON s.key='progress:'||j.id WHERE a.user_id=?"+condition+" ORDER BY CASE WHEN j.status IN ('PENDING','RUNNING','RETRY') THEN 0 ELSE 1 END,j.created_at DESC"+('' if ids is not None else ' LIMIT 100'),params)
        return decorate_jobs(rows, snapshot(self.store, uid))

    def background_status(self):
        from .background import public_snapshot
        return public_snapshot(self.store, self.user['id'])

    def inspection_ai_enabled(self):
        from .tasks import inspection_ai_enabled
        return inspection_ai_enabled(self.store, self.user['id'])

    def set_inspection_ai(self, enabled):
        uid = self.user['id']
        with self.store.connect(True) as c:
            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      ('inspection_ai:'+str(uid), canonical(bool(enabled))))

    def request_inspection(self, assignment_id, include_ai=None, instant=None):
        """User click: reuse today's current result, or request only missing AI."""
        from .tasks import enqueue, reconcile
        from zoneinfo import ZoneInfo
        include_ai = self.inspection_ai_enabled() if include_ai is None else bool(include_ai)
        instant = instant or datetime.now(timezone.utc)
        assignment = self._assignment(assignment_id)
        if not assignment['active']:
            raise ValueError('监控已归档')
        reconcile(self.store,self.user['id'],instant)
        day_start = instant.astimezone(ZoneInfo('Asia/Hong_Kong')).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
        rows = self.store.rows("SELECT d.id decision_id,d.result decision,d.created_at,s.id signal_id,s.result signal,s.as_of,s.dataset_id FROM current_signals v JOIN signal_runs s ON s.id=v.signal_id JOIN personal_decisions d ON d.signal_id=s.id WHERE v.assignment_id=? AND d.user_id=? ORDER BY d.created_at DESC,d.id DESC LIMIT 1",(assignment_id,self.user['id']))
        current = False
        row = rows[0] if rows else None
        if row:
            decision,signal = json.loads(row['decision']),json.loads(row['signal'])
            refs = {name:self.store.rows(f'SELECT MAX(id) v FROM {table} WHERE user_id=? AND code=?',(self.user['id'],assignment['code']))[0]['v'] for table,name in (('position_reports','report'),('budgets','budget'),('product_mappings','mapping'))}
            try:
                item = self.store.rows('SELECT * FROM instruments WHERE code=?',(assignment['code'],))[0]
                expected = MarketCalendar().expected(item['calendar'],instant,grace_for(item))
            except (ValueError,RuntimeError):
                expected = None
            current = signal['data_status'] in ('OK','INSUFFICIENT_DATA') and row['as_of'] == expected and decision.get('context_refs') == refs and decision['expires_at'] > instant.isoformat()
            if signal['data_status'] == 'INSUFFICIENT_DATA':
                repair = self.store.rows('SELECT value FROM settings WHERE key=?',('history_repair:'+assignment['code'],))
                state = json.loads(repair[0]['value']) if repair else {}
                # An incomplete result is reusable only until automatic repair
                # becomes due. A successful "today" receipt must not trap gaps.
                if not repair or state.get('pending') and state.get('next_due','')<=instant.isoformat():
                    current = False
            if current:
                from .decision_lifecycle import evidence_current
                with self.store.connect() as c:
                    current = evidence_current(c,row['signal_id'],assignment['code'],expected,instant)
        completed = self.store.rows("SELECT j.id FROM jobs j LEFT JOIN settings s ON s.key='request:'||j.id WHERE assignment_id=? AND status='SUCCEEDED' AND updated_at>=? AND updated_at<=? AND COALESCE(json_extract(s.value,'$.kind'),'inspection')='inspection' LIMIT 1",(assignment_id,day_start,instant.isoformat()))
        today = bool(row and ((day_start <= row['created_at'] <= instant.isoformat()) or completed))
        if current and today:
            saved = self.saved_explanation(row['decision_id'])
            if not include_ai or saved and saved['status'] == 'MODEL_OK':
                return {'status':'ALREADY_DONE','job_id':None,'decision_id':row['decision_id'],'message':'今天已完成巡检'+('和AI解读' if include_ai else '')+'，已保留现有结果，无需重复生成。'}
            in_flight = self.store.rows("SELECT j.id FROM jobs j LEFT JOIN settings s ON s.key='request:'||j.id WHERE assignment_id=? AND status IN ('PENDING','RUNNING','RETRY') AND COALESCE(json_extract(s.value,'$.kind'),'inspection')='inspection' LIMIT 1",(assignment_id,))
            if in_flight:
                jid = enqueue(self.store,assignment_id,refresh=False,include_ai=True)
                return {'status':'AI_JOINED','job_id':jid,'decision_id':row['decision_id'],'message':'巡检正在处理，已合并AI解读需求。'}
            jid = enqueue(self.store,assignment_id,refresh=False,include_ai=True,kind='explanation',decision_id=row['decision_id'])
            return {'status':'AI_ONLY','job_id':jid,'decision_id':row['decision_id'],'message':'今天已完成巡检，正在补生成AI解读，不重复巡检。'}
        # A queued automatic inspection can be upgraded rather than duplicated.
        jid = enqueue(self.store,assignment_id,refresh=not current,include_ai=include_ai)
        return {'status':'UPDATED' if today else 'QUEUED','job_id':jid,'decision_id':None,'message':'今天已巡检过，行情或持仓等输入发生变化，正在重新计算。' if today else '巡检请求已保存；相同的待执行任务会合并处理。'}

    def request_data_repair(self, assignment_id, include_ai=None):
        assignment = self._assignment(assignment_id)
        if not assignment['active']:
            raise ValueError('监控已归档')
        include_ai = self.inspection_ai_enabled() if include_ai is None else bool(include_ai)
        return enqueue(self.store,assignment_id,refresh=True,include_ai=include_ai,force_repair=True)

    def market_status(self, code):
        self._code(code)
        item = self.store.rows("SELECT * FROM instruments WHERE code=?", (code,))[0]
        instant = datetime.now(timezone.utc)
        expected = MarketCalendar().expected(item["calendar"],instant,grace_for(item))
        rows = self.store.rows("SELECT MAX(trade_date) latest FROM observations WHERE code=? AND trade_date<=? AND (available_at IS NULL OR available_at<=?)", (code,expected,instant.isoformat()))
        return {"expected":expected,"latest":rows[0]["latest"]}

    def results(self):
        return self.store.rows("SELECT a.code,i.name,s.id signal_id,s.as_of,s.result signal,d.id decision_id,d.result decision FROM current_signals v JOIN assignments a ON a.id=v.assignment_id JOIN instruments i ON i.code=a.code JOIN signal_runs s ON s.id=v.signal_id JOIN personal_decisions d ON d.signal_id=s.id WHERE a.user_id=? AND a.active=1 AND d.id=(SELECT id FROM personal_decisions WHERE signal_id=s.id ORDER BY created_at DESC,id DESC LIMIT 1)", (self.user["id"],))

    def strategy_assessment(self, code):
        self._code(code)
        from .strategy_assessment import assessment
        return assessment(self.store,self.user['id'],code)

    def trend_source(self, code, signal_id=None):
        """Inspect saved evidence or read synced prices; never start a sync/AI job."""
        if signal_id:
            rows = self.store.rows('''SELECT i.name,d.manifest,v.spec,f.algorithm FROM signal_runs s
                JOIN assignments a ON a.id=s.assignment_id JOIN instruments i ON i.code=a.code
                JOIN datasets d ON d.id=s.dataset_id JOIN strategy_versions v ON v.id=a.strategy_id
                JOIN feature_snapshots f ON f.id=s.feature_id
                WHERE s.id=? AND a.user_id=? AND a.code=?''',(signal_id,self.user['id'],code))
            if not rows:
                raise PermissionError('不能查看其他用户或其他指数的巡检曲线')
            row = rows[0]
            return {'name':row['name'],'manifest':json.loads(row['manifest']),
                    'spec':json.loads(row['spec']),'algorithm':row['algorithm']}
        self._code(code)
        row = self.store.rows('''SELECT i.name,v.spec FROM assignments a JOIN instruments i ON i.code=a.code
            JOIN strategy_versions v ON v.id=a.strategy_id WHERE a.user_id=? AND a.code=?
            ORDER BY a.active DESC,a.created_at DESC,a.id DESC LIMIT 1''',(self.user['id'],code))[0]
        try:
            manifest,_ = Evaluator(self.store).inputs(code)
        except ValueError as exc:
            if not str(exc).startswith('NO_DATA:'):
                raise
            manifest = {'code':code,'as_of':None,'sessions':[],'data':[]}
        return {'name':row['name'],'manifest':manifest,'spec':json.loads(row['spec']),'algorithm':ALGORITHM}

    def decision_state(self, decision_id):
        from .decision_lifecycle import evidence_current, feedback
        uid = self.user['id']
        row = self.store.rows("SELECT d.result,s.id signal_id,s.as_of,s.assignment_id,a.code,a.active FROM personal_decisions d JOIN signal_runs s ON s.id=d.signal_id JOIN assignments a ON a.id=s.assignment_id WHERE d.id=? AND d.user_id=?",(decision_id,uid))
        if not row:
            raise PermissionError('不能查看其他用户的判断状态')
        row = row[0]
        instant = datetime.now(timezone.utc)
        decision = json.loads(row['result'])
        expected = self.market_status(row['code'])['expected']
        with self.store.connect() as c:
            refs = {name:c.execute(f'SELECT MAX(id) FROM {table} WHERE user_id=? AND code=?',(uid,row['code'])).fetchone()[0] for table,name in (('position_reports','report'),('budgets','budget'),('product_mappings','mapping'))}
            projection = c.execute('SELECT signal_id FROM current_signals WHERE assignment_id=?',(row['assignment_id'],)).fetchone()
            current = bool(row['active'] and projection and projection[0]==row['signal_id'] and row['as_of']==expected and decision['context_refs']==refs and datetime.fromisoformat(decision['expires_at'])>instant and evidence_current(c,row['signal_id'],row['code'],expected,instant))
            return {'current':current,'feedback':feedback(c,uid,decision_id)}

    def history(self):
        return self.store.rows("SELECT s.as_of,a.code,s.id signal_id,s.result signal,d.id decision_id,d.result decision FROM signal_runs s JOIN assignments a ON a.id=s.assignment_id JOIN personal_decisions d ON d.signal_id=s.id WHERE a.user_id=? ORDER BY s.created_at DESC LIMIT 500", (self.user["id"],))

    def legacy(self):
        return self.store.rows("SELECT table_name,payload FROM legacy_archive WHERE legacy_user=? AND table_name IN ('inspection_log','fund_custom_prompts','user_position') ORDER BY id DESC", (self.user["username"],))

    def feedback(self, decision_id, status, note="", trade=None):
        allowed = {"ACKNOWLEDGED","EXECUTED","PARTIAL","DEFERRED","EXPIRED","CANCELLED"}
        if status not in allowed or (status == "DEFERRED" and not note.strip()):
            raise ValueError("反馈状态无效；暂缓需要写明原因")
        rows = self.store.rows("SELECT result FROM personal_decisions WHERE id=? AND user_id=?", (decision_id,self.user["id"]))
        if not rows:
            raise PermissionError("不能反馈其他用户的决策")
        payload = {"note":note,"trade":None,"position_changed":False}
        if trade:
            if set(trade) != {"price","quantity","fees","currency","product_code"}:
                raise ValueError("真实交易记录需要成交价、份额、费用、币种、基金代码")
            if trade["currency"] not in ("CNY","HKD","USD") or not trade["product_code"]:
                raise ValueError("交易需要真实基金代码与受支持的币种")
            payload["trade"] = {**trade,"price":str(quantity(trade["price"])),"quantity":str(quantity(trade["quantity"])),"fees":str(money(trade["fees"]))}
        with self.store.connect(True) as c:
            return c.execute("INSERT INTO feedback(user_id,decision_id,status,payload,created_at) VALUES(?,?,?,?,?)", (self.user["id"],decision_id,status,canonical(payload),now())).lastrowid

    def channels(self):
        # Never return URLs to general diagnostic/export paths.
        uid = self.user['id']
        with self.store.connect() as c:
            rows = [dict(r) for r in c.execute("SELECT id,label,enabled FROM channels WHERE user_id=? AND url<>'' ORDER BY id",(uid,))]
            return [{**r,'mode':channel_mode(c,r['id'],uid,r['enabled'])} for r in rows]

    def set_channel(self, url, label, enabled=False, mode=None, radar_mode='OFF', schedule=None):
        url = validate_url(url)
        from .webhook_schedule import normalize, config_key as schedule_key
        schedule = normalize(schedule)
        from .radar_notifications import MODES as RADAR_MODES, config_key
        if radar_mode not in RADAR_MODES:
            raise ValueError('机会提醒方式无效')
        mode = mode if mode is not None else 'BUY_SELL' if enabled else 'OFF'
        if mode not in MODES:
            raise ValueError('推送方式无效')
        uid = self.user['id']
        with self.store.connect(True) as c:
            cid = c.execute("INSERT INTO channels(user_id,url,label,enabled) VALUES(?,?,?,?)", (uid,url,label,int(mode!='OFF'))).lastrowid
            if mode!='OFF':
                c.execute('INSERT INTO settings VALUES(?,?)',(filter_key(uid,cid),mode))
            c.execute('INSERT INTO settings VALUES(?,?)',(config_key(uid,cid),canonical(radar_mode)))
            c.execute('INSERT INTO settings VALUES(?,?)',(schedule_key(uid,cid),canonical(schedule)))
            return cid

    def channel_schedule(self, channel_id):
        from .webhook_schedule import read, next_times
        uid = self.user['id']
        with self.store.connect() as c:
            if not c.execute("SELECT id FROM channels WHERE id=? AND user_id=? AND url<>''",(channel_id,uid)).fetchone():
                raise PermissionError('不能查看其他账号的Webhook发送时间')
            config = read(c,uid,channel_id)
        return {**config,'next_times':next_times(config)}

    def configure_channel_schedule(self, channel_id, enabled, expression):
        from .webhook_schedule import normalize, config_key
        uid = self.user['id']
        with self.store.connect(True) as c:
            if not c.execute("SELECT id FROM channels WHERE id=? AND user_id=? AND url<>''",(channel_id,uid)).fetchone():
                raise PermissionError('不能修改其他账号的Webhook发送时间')
            config = normalize({'enabled':enabled,'cron':expression})
            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(config_key(uid,channel_id),canonical(config)))
        return True

    def configure_channel(self, channel_id, mode):
        if mode not in MODES:
            raise ValueError('推送方式无效')
        uid = self.user['id']
        with self.store.connect(True) as c:
            if not c.execute("SELECT id FROM channels WHERE id=? AND user_id=? AND url<>''",(channel_id,uid)).fetchone():
                raise PermissionError('不能修改其他用户的Webhook')
            c.execute('UPDATE channels SET enabled=? WHERE id=? AND user_id=?',(int(mode!='OFF'),channel_id,uid))
            if mode!='OFF':
                c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(filter_key(uid,channel_id),mode))
        return True

    def toggle_channel(self, channel_id, enabled):
        uid = self.user['id']
        with self.store.connect(True) as c:
            if not c.execute("SELECT id FROM channels WHERE id=? AND user_id=? AND url<>''",(channel_id,uid)).fetchone():
                raise PermissionError('不能修改其他用户的Webhook')
            c.execute("UPDATE channels SET enabled=? WHERE id=? AND user_id=?", (int(enabled),channel_id,uid))

    def test_webhook(self, url, sender=None):
        uid = self.user['id']
        url = validate_url(url)
        if sender is None:
            from .adapters import send_feishu
            sender = send_feishu
        try:
            sender(url,{'text':'你好，世界'})
        except Exception as exc:
            from .adapters import webhook_failure_detail
            detail = '测试发送失败：'+webhook_failure_detail(exc)
            self.store.health('notification','FAILED',detail,uid)
            raise RuntimeError(detail) from None
        return True

    def test_channel(self, channel_id, sender=None):
        return self.test_webhook(self.channel_url(channel_id),sender)

    def channel_url(self, channel_id):
        """Explicit owner-only credential reveal; list/export paths omit URLs."""
        rows = self.store.rows("SELECT url FROM channels WHERE id=? AND user_id=? AND url<>''",(channel_id,self.user['id']))
        if not rows:
            raise PermissionError('Webhook不存在或不属于当前账号')
        return rows[0]['url']

    def delete_channel(self, channel_id):
        uid = self.user['id']
        with self.store.connect(True) as c:
            if not c.execute('SELECT id FROM channels WHERE id=? AND user_id=?',(channel_id,uid)).fetchone():
                raise PermissionError('Webhook不存在或不属于当前账号')
            # Keep the channel identity for outbox/attempt audit foreign keys,
            # but erase the credential and prevent re-enabling a deleted entry.
            c.execute("UPDATE channels SET enabled=0,url='' WHERE id=? AND user_id=?",(channel_id,uid))
            c.execute('DELETE FROM settings WHERE key=?',(filter_key(uid,channel_id),))
            from .webhook_schedule import config_key as schedule_key
            c.execute('DELETE FROM settings WHERE key=?',(schedule_key(uid,channel_id),))
            c.execute("UPDATE outbox SET status='CANCELLED',claim_until=NULL,claim_owner=NULL WHERE channel_id=? AND user_id=? AND status IN ('PENDING','RETRY','SENDING')",(channel_id,uid))
            from .radar_notifications import cancel_channel, config_key
            cancel_channel(c,uid,channel_id)
            c.execute('DELETE FROM settings WHERE key=?',(config_key(uid,channel_id),))
        return True

    def health(self):
        user = self.user
        return self.store.rows("SELECT component,status,detail,created_at FROM health WHERE user_id=? OR user_id IS NULL ORDER BY id DESC LIMIT 100", (user["id"],))

    def process_pending(self, limit=20):
        """Manual rescue: only this account's inspections, no scheduling or delivery."""
        from .worker import Worker
        uid = self.user["id"]
        worker = Worker(self.store)
        processed = 0
        try:
            for _ in range(min(max(int(limit),0),20)):
                if not worker.run_job(user_id=uid):
                    break
                processed += 1
        finally:
            worker.release("worker:"+worker.owner)
        return processed

    def set_scheduler(self, enabled):
        Auth(self.store).user(self.token,admin=True)
        with self.store.connect(True) as c:
            c.execute("UPDATE settings SET value=? WHERE key='scheduler_enabled'", (canonical(bool(enabled)),))

    def daily_sync_enabled(self):
        self.user
        rows = self.store.rows("SELECT value FROM settings WHERE key='daily_sync_enabled'")
        return not rows or rows[0]["value"] == "true"

    def set_daily_sync(self, enabled):
        Auth(self.store).user(self.token,admin=True)
        with self.store.connect(True) as c:
            c.execute("INSERT INTO settings VALUES('daily_sync_enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (canonical(bool(enabled)),))

    def export_feedback(self):
        uid = self.user["id"]
        result = {"positions":self.store.rows("SELECT code,status,payload,reported_at FROM position_reports WHERE user_id=?", (uid,)),"feedback":self.store.rows("SELECT decision_id,status,payload,created_at FROM feedback WHERE user_id=?", (uid,))}
        if self.store.version >= 2:
            result["executions"] = self.store.rows("SELECT decision_id,payload,created_at FROM execution_confirmations WHERE user_id=?", (uid,))
        return result

    def evolution_policy(self, code):
        self._code(code)
        if self.store.version < 2:
            return None
        rows = self.store.rows("SELECT * FROM evolution_policies WHERE user_id=? AND code=? AND active=1", (self.user["id"],code))
        return {**rows[0],"payload":json.loads(rows[0]["payload"])} if rows else None

    def set_evolution(self, code, values):
        self._code(code)
        from .evolution import save_policy
        return save_policy(self.store,self.user["id"],code,values)

    def request_evolution(self, code):
        policy = self.evolution_policy(code)
        if not policy:
            raise ValueError("请先保存自动进化配置")
        from .evolution import Evolution
        return Evolution(self.store,Evaluator(self.store)).request(policy["id"],trigger='MANUAL_RESEARCH')

    def evolve_daily(self, code):
        self._code(code)
        from .daily_evolution import request
        from .evolution import Evolution, save_policy
        policy = self.evolution_policy(code)
        pid = policy['id'] if policy else save_policy(self.store,self.user['id'],code,{},only_if_missing=True)
        # Retired policies need explicit configuration rather than silent revival.
        if not self.evolution_policy(code):
            raise ValueError('此前的进化设置已停用，请先在下方重新保存范围')
        return request(Evolution(self.store,Evaluator(self.store)),pid)

    def evolution_history(self, code):
        self._code(code)
        if self.store.version < 2:
            return []
        rows = self.store.rows("SELECT r.*,k.status candidate_status,k.payload candidate_payload,k.state candidate_state FROM evolution_runs r JOIN evolution_policies p ON p.id=r.policy_id LEFT JOIN evolution_candidates k ON k.id=r.id WHERE p.user_id=? AND p.code=? ORDER BY r.created_at DESC LIMIT 50", (self.user["id"],code))
        for row in rows:
            row.pop("manifest")
            for key in ("payload","candidate_payload","candidate_state"):
                row[key] = json.loads(row[key]) if row[key] else None
        return rows

    def evolution_records(self, code):
        self._code(code)
        from .evolution_records import records
        return records(self.store,self.user['id'],code) if self.store.version>=2 else []

    def confirm_execution(self, decision_id, outcome, note="", position=None, fields=None):
        from .execution import confirm
        return confirm(self.store,self.user["id"],decision_id,outcome,note,position,fields)

    def rollback_evolution(self, candidate_id):
        from .evolution import Evolution
        return Evolution(self.store,Evaluator(self.store)).rollback(candidate_id,self.user["id"])

    def confirm_unchanged(self, code):
        old = self.position(code)
        if not old or old["status"] == "UNKNOWN":
            raise ValueError("请先确认真实持仓")
        fields = {k:v for k,v in old.items() if k in ("market_value","shares","currency","target_completion","unrealized_pnl_ratio","note")}
        # Refresh status only; stale market values are not fresh valuations.
        for key in ("market_value","unrealized_pnl_ratio"):
            fields.pop(key,None)
        fields["note"] = "用户确认期间未交易；市值需另行更新"
        return self.report_position(code,old["status"],fields)


class Evaluator:
    """Trusted worker entry point. Never exposed as a user identity selector in the UI."""
    def __init__(self, store, calendar=None):
        self.store,self.calendar = store,calendar or MarketCalendar()

    def inputs(self, code, instant=None):
        instant = (instant or datetime.now(timezone.utc)).astimezone(timezone.utc)
        item = self.store.rows("SELECT * FROM instruments WHERE code=?", (code,))[0]
        rows = self.store.rows("SELECT o.* FROM observations o WHERE code=? AND id=(SELECT MAX(id) FROM observations WHERE code=o.code AND trade_date=o.trade_date AND (available_at IS NULL OR available_at<=?)) AND (available_at IS NULL OR available_at<=?) ORDER BY trade_date", (code,instant.isoformat(),instant.isoformat()))
        if not rows:
            raise ValueError("NO_DATA: 没有可用行情")
        expected = self.calendar.expected(item["calendar"],instant,grace_for(item))
        rows = [r for r in rows if r["trade_date"] <= expected]
        if code == 'HKTECH':
            from .data_maintenance import history_start
            rows = [r for r in rows if r['trade_date']>=history_start(item)]
        if not rows:
            raise ValueError("NO_DATA: 没有已完成时段行情")
        last = rows[-1]["trade_date"]
        sessions = self.calendar.sessions(item["calendar"],rows[0]["trade_date"],last)
        data = [json.loads(r["payload"]) for r in rows]
        manifest = {"code":code,"observations":[{"id":r["id"],"hash":r["row_hash"],"available_at":r["available_at"],"availability_basis":r["availability_basis"]} for r in rows],"calendar":item["calendar"],"sessions":sessions,"start":sessions[0],"as_of":last,"data":data,"historical_availability":"LEGACY_UNKNOWN_OR_ESTIMATED"}
        identity = json.loads(item["metadata"]).get("identity_verified")
        quality = "OK" if last == expected and identity else "STALE" if identity else "IDENTITY_UNVERIFIED"
        return manifest,quality

    def evaluate(self, assignment_id, instant=None):
        instant = instant or datetime.now(timezone.utc)
        assignment = self.store.rows("SELECT a.*,s.spec FROM assignments a JOIN strategy_versions s ON s.id=a.strategy_id WHERE a.id=?", (assignment_id,))[0]
        spec = json.loads(assignment["spec"])
        manifest,quality = self.inputs(assignment["code"],instant)
        frame = feature_frame(manifest["data"],manifest["sessions"],spec)
        results = replay(frame,spec)
        signal = results[-1]
        feature = features_at(frame,manifest["as_of"])
        if quality != "OK":
            signal = step(feature,signal["model_state"],spec,manifest["as_of"],signal["triggered_at"],quality)
        if signal['data_status'] == 'INSUFFICIENT_DATA':
            from .data_maintenance import readiness
            signal['data_readiness'] = readiness(frame,manifest['data'],spec)
        dataset_id = digest(manifest)
        feature_id = digest({"dataset":dataset_id,"algorithm":ALGORITHM,"spec":spec,"feature":feature})
        signal.update(code=assignment["code"],strategy_id=assignment["strategy_id"],dataset_id=dataset_id,feature_id=feature_id,evaluated_at=instant.isoformat(),initial_state="FLAT",replay_start=manifest["start"],research_stage=assignment["stage"])
        signal_id = digest({"assignment":assignment_id,"dataset":dataset_id,"feature":feature_id,"quality":signal["data_status"]})
        context = {}
        for table,name,timekey in (("position_reports","report","reported_at"),("budgets","budget","reported_at"),("product_mappings","mapping","created_at")):
            found = self.store.rows(f"SELECT * FROM {table} WHERE user_id=? AND code=? ORDER BY id DESC LIMIT 1", (assignment["user_id"],assignment["code"]))
            context[name] = {**json.loads(found[0]["payload"]),timekey:found[0][timekey],"id":found[0]["id"]} if found else None
        decision = personal_decision(signal,context["report"],context["budget"],context["mapping"],instant,bool(assignment["catchup"]))
        decision["context_refs"] = {k:v["id"] if v else None for k,v in context.items()}
        decision_id = digest({"signal":signal_id,"user":assignment["user_id"],"context":context,"status":decision["status"],"action":decision["action"],"evaluation_day":instant.date().isoformat()})
        stamp = now()
        with self.store.connect(True) as c:
            c.execute("INSERT OR IGNORE INTO datasets VALUES(?,?,?,?,?)", (dataset_id,assignment["code"],manifest["as_of"],canonical(manifest),stamp))
            c.execute("INSERT OR IGNORE INTO feature_snapshots VALUES(?,?,?,?,?)", (feature_id,dataset_id,ALGORITHM,canonical(feature),stamp))
            c.execute("INSERT OR IGNORE INTO signal_runs VALUES(?,?,?,?,?,?,?)", (signal_id,assignment_id,dataset_id,feature_id,manifest["as_of"],canonical(signal),stamp))
            c.execute("INSERT OR IGNORE INTO personal_decisions VALUES(?,?,?,?,?,?)", (decision_id,signal_id,assignment["user_id"],context["report"]["id"] if context["report"] else None,canonical(decision),stamp))
            current_context = {name:c.execute(f"SELECT MAX(id) FROM {table} WHERE user_id=? AND code=?", (assignment["user_id"],assignment["code"])).fetchone()[0] for table,name in (("position_reports","report"),("budgets","budget"),("product_mappings","mapping"))}
            still_current = current_context == decision["context_refs"] and bool(c.execute("SELECT active FROM assignments WHERE id=?", (assignment_id,)).fetchone()[0])
            from .decision_lifecycle import evidence_current, manifest_revision, observation_floor
            previous = c.execute('SELECT v.as_of,d.manifest FROM current_signals v JOIN signal_runs s ON s.id=v.signal_id JOIN datasets d ON d.id=s.dataset_id WHERE v.assignment_id=?',(assignment_id,)).fetchone()
            floor = observation_floor(c,assignment['code'])
            revision = manifest_revision(manifest,floor)
            previous_revision = manifest_revision(json.loads(previous['manifest']),floor) if previous else None
            can_replace = not previous or manifest['as_of']>previous['as_of'] or (manifest['as_of']==previous['as_of'] and revision>=(previous_revision or 0))
            # Both versions must be compared over the same analysis scope.
            # Also reject a computation overtaken by a real data correction.
            if still_current and can_replace and evidence_current(c,signal_id,assignment['code'],manifest['as_of'],instant):
                c.execute('INSERT INTO current_signals VALUES(?,?,?) ON CONFLICT(assignment_id) DO UPDATE SET signal_id=excluded.signal_id,as_of=excluded.as_of',(assignment_id,signal_id,manifest['as_of']))
            projection = c.execute("SELECT signal_id FROM current_signals WHERE assignment_id=?", (assignment_id,)).fetchone()
            if still_current and projection and projection[0] == signal_id and assignment["stage"] == "ENABLED" and decision["status"] == "AVAILABLE" and decision["action"] in ("BUY","SELL","HOLD","WAIT"):
                from .decision_lifecycle import feedback, HANDLED
                handled = feedback(c,assignment['user_id'],decision_id)
                if handled and handled[0]['status'] in HANDLED:
                    channels = []
                else:
                    channels = c.execute("SELECT id FROM channels WHERE user_id=? AND enabled=1", (assignment["user_id"],)).fetchall()
                index_name = c.execute('SELECT name FROM instruments WHERE code=?',(assignment['code'],)).fetchone()[0]
                payload = operation_payload(index_name,decision['action'])
                for channel in channels:
                    if not allows(channel_mode(c,channel['id'],assignment['user_id'],True),decision['action']):
                        continue
                    key = {"user":assignment["user_id"],"channel":channel["id"],"assignment":assignment_id,"episode":signal["triggered_at"],"action":decision["action"]}
                    if decision['action'] in ('HOLD','WAIT'):
                        key.pop('episode')
                        key['as_of'] = manifest['as_of']
                    dedup = digest(key)
                    c.execute("INSERT OR IGNORE INTO outbox(user_id,channel_id,decision_id,dedup_key,payload,next_at) VALUES(?,?,?,?,?,?)", (assignment["user_id"],channel["id"],decision_id,dedup,canonical(payload),stamp))
                    # Rebind unsent notices after a data/context revision. Never
                    # resurrect delivered, cancelled or exhausted instructions.
                    c.execute("UPDATE outbox SET decision_id=?,payload=?,next_at=CASE WHEN status='EXPIRED' THEN ? ELSE next_at END,status=CASE WHEN status='EXPIRED' THEN 'PENDING' ELSE status END WHERE dedup_key=? AND attempts<3 AND status IN ('PENDING','RETRY','EXPIRED')",(decision_id,canonical(payload),stamp,dedup))
        # Network/LLM calls are deliberately outside the investment decision transaction.
        try:
            explain(self.store,decision_id)
        except Exception:
            self.store.health("explanation","FAILED","解释流程异常；已提交的信号与决策仍有效",assignment["user_id"])
        return signal_id,decision_id
