import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from .storage import canonical, digest, now
from .presentation import label
from .llm_diagnostics import failure_diagnostic

PROMPT_VERSION = "explain_zh_v3"


class ExplanationBusy(RuntimeError):
    pass


class ExplanationValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def model_identity():
    return os.environ.get("VNEXT_LLM_MODEL","deepseek-chat")+":"+digest({"endpoint":os.environ.get("VNEXT_LLM_BASE_URL","https://api.deepseek.com"),"prompt_version":PROMPT_VERSION,"thinking":os.environ.get('VNEXT_LLM_THINKING','disabled'),"max_tokens":os.environ.get('VNEXT_LLM_MAX_TOKENS','1800')})[:16]


def validate_explanation(raw, features):
    result = json.loads(raw)
    keys = {"summary","evidence_refs","limitations","counterarguments","scenarios"}
    if not isinstance(result,dict) or set(result) != keys or not isinstance(result["summary"],str) or len(result["summary"]) > 4000:
        raise ExplanationValidationError('INVALID_STRUCTURE',"模型输出结构不符")
    for k in ("evidence_refs","limitations","counterarguments"):
        if not isinstance(result[k],list) or not all(isinstance(v,str) and len(v)<2000 for v in result[k]):
            raise ExplanationValidationError('INVALID_STRUCTURE',"模型列表结构不符")
    if not set(result["evidence_refs"]) <= set(features):
        raise ExplanationValidationError('UNKNOWN_EVIDENCE',"模型引用了未提供的特征")
    if not isinstance(result["scenarios"],list) or len(result["scenarios"])>10:
        raise ExplanationValidationError('INVALID_STRUCTURE',"模型情景结构不符")
    for s in result["scenarios"]:
        if not isinstance(s,dict) or set(s) != {"assumption","implication"} or not all(isinstance(v,str) for v in s.values()):
            raise ExplanationValidationError('INVALID_STRUCTURE',"模型情景字段不符")
    # Free prose cannot be fully verified; prohibit imperative buy/sell claims from reaching UI.
    text = canonical(result).lower()
    # These exact disclaimers deny a guarantee. Remove only the negative phrase,
    # leaving any separate positive promise in the same sentence detectable.
    text = re.sub(r"(?<!不)(?<!非)(?<!不是)(?<!不能)(?<!无法)(?:不能|无法|不予|不作|不做|不提供|不构成|不代表|不意味着|并非|并不|不)保证收益",'',text)
    if any(x in text for x in ("立即买入","立即卖出","必须买","必须卖","保证收益","确定上涨","action",'"buy"','"sell"',"建议买入","建议卖出","建议持有")):
        raise ExplanationValidationError('UNSAFE_CLAIM',"模型解释包含交易指令或保证")
    return result


def explain(store, decision_id, provider=None, model="TEMPLATE"):
    row = store.rows("SELECT d.result decision,s.result signal,f.payload features,d.user_id,a.code FROM personal_decisions d JOIN signal_runs s ON s.id=d.signal_id JOIN feature_snapshots f ON f.id=s.feature_id JOIN assignments a ON a.id=s.assignment_id WHERE d.id=?", (decision_id,))[0]
    payload = {k:json.loads(row[k]) for k in ('decision','signal','features')}
    # Audit IDs and timestamps change during old-history repair even when the
    # current evidence and action are identical. They are not prose evidence.
    payload['signal'] = {k:v for k,v in payload['signal'].items() if k not in ('dataset_id','feature_id','evaluated_at','replay_start')}
    payload['decision'] = {k:v for k,v in payload['decision'].items() if k not in ('context_refs','expires_at')}
    input_hash = digest({"payload":payload,"prompt_version":PROMPT_VERSION})
    old = store.rows("SELECT * FROM explanations WHERE decision_id=? AND input_hash=? AND model=?", (decision_id,input_hash,model))
    if old and (not provider or old[0]["status"] == "MODEL_OK"):
        return json.loads(old[0]["payload"])
    signal = payload["signal"]
    template = {"summary":f"指数状态：{label(signal['state_label']) if signal['state_label'] else '数据未通过'}；个人状态：{label(payload['decision']['action'] or payload['decision']['status'])}。", "evidence_refs":[],"limitations":["这是指数规则研究，不能承诺收益；指数表现不等于基金或账户收益","旧数据发布时间和历史修订无法完整重建"],"counterarguments":["趋势策略可能在震荡行情反复交易或错过反弹"],"scenarios":[]}
    reused = None
    if provider:
        cached = store.rows("SELECT e.payload,e.created_at FROM explanations e JOIN personal_decisions d ON d.id=e.decision_id JOIN signal_runs s ON s.id=d.signal_id JOIN assignments a ON a.id=s.assignment_id WHERE e.input_hash=? AND e.model=? AND e.status='MODEL_OK' AND d.user_id=? AND a.code=? ORDER BY e.id DESC LIMIT 1",(input_hash,model,row['user_id'],row['code']))
        if cached:
            reused = json.loads(cached[0]['payload'])
            reused['reuse'] = {'original_generated_at':reused.get('reuse',{}).get('original_generated_at',cached[0]['created_at'])}
    status,result = ("MODEL_OK",reused) if reused is not None else ("TEMPLATE",template)
    lease,owner = None,uuid.uuid4().hex
    if provider and reused is None:
        lease = 'explanation:'+digest({'decision':decision_id,'input':input_hash,'model':model})
        instant = datetime.now(timezone.utc)
        with store.connect(True) as c:
            cached = c.execute("SELECT payload FROM explanations WHERE decision_id=? AND input_hash=? AND model=? AND status='MODEL_OK'",(decision_id,input_hash,model)).fetchone()
            if cached:
                return json.loads(cached[0])
            active = c.execute('SELECT expires_at FROM leases WHERE name=?',(lease,)).fetchone()
            if active and active[0] > instant.isoformat():
                raise ExplanationBusy('AI解读正在生成，请稍候，结果会自动保存')
            c.execute('INSERT INTO leases VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at',(lease,owner,(instant+timedelta(seconds=90)).isoformat()))
    try:
        if provider and reused is None:
            phase,started = 'provider',time.monotonic()
            try:
                raw = provider(payload)
                phase = 'validation'
                result = validate_explanation(raw,payload["features"])
                status = "MODEL_OK"
            except Exception as exc:
                status = "FAILED_TEMPLATE"
                diagnostic = failure_diagnostic(exc,phase)
                diagnostic['elapsed_ms'] = round((time.monotonic()-started)*1000)
                result = {**template,'diagnostic':diagnostic}
                uid = store.rows('SELECT user_id FROM personal_decisions WHERE id=?',(decision_id,))[0]['user_id']
                store.health("explanation","FAILED",canonical({'decision_id':decision_id,**diagnostic}),user_id=uid)
        with store.connect(True) as c:
            if lease:
                held = c.execute('SELECT owner,expires_at FROM leases WHERE name=?',(lease,)).fetchone()
                if not held or held[0] != owner or held[1] <= now():
                    raise ExplanationBusy('AI生成租约已过期，等待有效生成结果')
            c.execute("INSERT INTO explanations(decision_id,input_hash,model,status,payload,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(decision_id,input_hash,model) DO UPDATE SET status=excluded.status,payload=excluded.payload,created_at=excluded.created_at WHERE explanations.status='FAILED_TEMPLATE'", (decision_id,input_hash,model,status,canonical(result),now()))
    finally:
        if lease:
            with store.connect(True) as c:
                c.execute('DELETE FROM leases WHERE name=? AND owner=?',(lease,owner))
    return result
