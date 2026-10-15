"""Explicit user confirmations; never infer fills from model or index prices."""
import json

from .engine import money, quantity
from .storage import canonical, digest, now


def confirm(store, uid, did, outcome, note="", position=None, fields=None, request_key=None):
    from .evolution import require_v2
    require_v2(store)
    if outcome not in ("SUBMITTED","EXECUTED","PARTIAL","DEFERRED"):
        raise ValueError("执行状态无效")
    if outcome == "DEFERRED" and not note.strip():
        raise ValueError("暂缓请填写或选择原因")
    if position not in (None,"EMPTY","HELD") or (position is not None and outcome not in ("EXECUTED","PARTIAL")):
        raise ValueError("只有明确成交确认才能更新持仓")
    fields = dict(fields or {})
    if set(fields)-{"shares","market_value","currency"}:
        raise ValueError("成交后持仓仅接受份额、市值和币种")
    if fields and position is None:
        raise ValueError("填写数量时需要同时确认持仓状态")
    for k,normalizer in (("shares",quantity),("market_value",money)):
        if fields.get(k) is not None:
            fields[k] = str(normalizer(fields[k]))
            if position == "EMPTY" and float(fields[k]) != 0:
                raise ValueError("空仓不能有正持仓金额或份额")
    if fields.get("currency") not in (None,"CNY","HKD","USD") or (fields.get("market_value") is not None and not fields.get("currency")):
        raise ValueError("市值需要明确币种")
    payload = {"outcome":outcome,"note":note,"confirmed_position":position,"fields":fields,"position_changed":position is not None,"actual_fill_verified_by":"USER" if outcome in ("EXECUTED","PARTIAL") else None}
    request_key = request_key or digest({"decision":did,**payload})
    with store.connect(True) as c:
        row = c.execute("SELECT d.result,s.id signal_id,s.as_of,s.assignment_id,a.code,a.active FROM personal_decisions d JOIN signal_runs s ON s.id=d.signal_id JOIN assignments a ON a.id=s.assignment_id WHERE d.id=? AND d.user_id=?", (did,uid)).fetchone()
        if not row:
            raise PermissionError("不能确认其他用户的操作")
        existing = c.execute("SELECT id,payload,decision_id FROM execution_confirmations WHERE user_id=? AND request_key=?", (uid,request_key)).fetchone()
        if existing:
            if existing["decision_id"] != did or json.loads(existing["payload"]) != payload:
                raise ValueError("重复请求标识与原确认内容不一致")
            return existing["id"]
        decision = json.loads(row["result"])
        if decision["action"] not in ("BUY","SELL"):
            raise ValueError("这条记录没有可执行买卖动作")
        if position is not None:
            if (outcome == "PARTIAL" or decision["action"] == "BUY") and position != "HELD":
                raise ValueError("买入或部分执行后应明确确认仍持有")
            if outcome == "EXECUTED" and decision["action"] == "SELL" and position != "EMPTY":
                raise ValueError("全部卖出确认应为空仓；仍持有请选部分执行")
            latest = {name:c.execute(f"SELECT MAX(id) FROM {table} WHERE user_id=? AND code=?", (uid,row["code"])).fetchone()[0] for table,name in (("position_reports","report"),("budgets","budget"),("product_mappings","mapping"))}
            projection = c.execute("SELECT signal_id FROM current_signals WHERE assignment_id=?", (row["assignment_id"],)).fetchone()
            current_signal = c.execute("SELECT signal_id FROM personal_decisions WHERE id=?", (did,)).fetchone()[0]
            from .decision_lifecycle import evidence_current
            from datetime import datetime, timezone
            if not row["active"] or latest != decision.get("context_refs") or not projection or projection[0] != current_signal or not evidence_current(c,row['signal_id'],row['code'],row['as_of'],datetime.now(timezone.utc)):
                raise ValueError("持仓或策略已更新；反馈可单独记录，当前持仓请重新确认")
            report = {"status":position,"note":note,"basis":"USER_EXECUTION_CONFIRMATION",**fields}
            # Omitted numbers become unknown, rather than retaining pre-trade values.
            c.execute("INSERT INTO position_reports(user_id,code,status,payload,reported_at) VALUES(?,?,?,?,?)", (uid,row["code"],position,canonical(report),now()))
            budget = c.execute("SELECT payload FROM budgets WHERE user_id=? AND code=? ORDER BY id DESC LIMIT 1", (uid,row["code"])).fetchone()
            if budget:
                value = json.loads(budget[0])
                value.update(used=None,cash=None,needs_confirmation=True)
                c.execute("INSERT INTO budgets(user_id,code,payload,reported_at) VALUES(?,?,?,?)", (uid,row["code"],canonical(value),now()))
        cid = c.execute("INSERT INTO execution_confirmations(user_id,decision_id,request_key,payload,created_at) VALUES(?,?,?,?,?)", (uid,did,request_key,canonical(payload),now())).lastrowid
        c.execute("INSERT INTO feedback(user_id,decision_id,status,payload,created_at) VALUES(?,?,?,?,?)", (uid,did,outcome,canonical(payload),now()))
        return cid
