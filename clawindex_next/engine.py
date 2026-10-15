from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import numpy as np
import pandas as pd

from .storage import digest

ALGORITHM = "factor_features_v3"
DEFAULT_SPEC = {"name":"trend_confirm_v1", "ma":120, "confirmation":3, "slope_window":5, "valuation_window":1250, "annual_sessions":252}


def validate_spec(spec):
    from .factors import PROFILES
    supported = (set(spec) == set(DEFAULT_SPEC) and spec["name"] == "trend_confirm_v1") or (set(spec) == set(DEFAULT_SPEC)|{"factor_profile"} and spec["name"] == "trend_factor_v2" and spec["factor_profile"] in PROFILES)
    if not supported:
        raise ValueError("仅支持已登记的趋势规则与固定因子组合；提示词不能发布交易规则")
    bounds = {"ma":(20,250),"confirmation":(1,10),"slope_window":(1,20),"valuation_window":(20,2500),"annual_sessions":(200,366)}
    for k,(lo,hi) in bounds.items():
        if type(spec[k]) is not int or not lo <= spec[k] <= hi:
            raise ValueError(f"参数{k}必须是{lo}至{hi}的整数")
    return digest(spec)


def clean_number(value):
    try:
        return float(value) if value is not None and math.isfinite(float(value)) else None
    except (ValueError,TypeError):
        return None


def feature_frame(rows, sessions, spec=None):
    spec = spec or DEFAULT_SPEC
    validate_spec(spec)
    columns = ["date","open","high","low","close","amount","pe","pb"]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(index=pd.Index(sessions,name="date"))
    if frame["date"].duplicated().any():
        raise ValueError("同日重复行情；请先选择具体修订版本")
    frame = frame.set_index("date").reindex(sessions)
    from .factors import TECH_FIELDS, BASIC_FIELDS
    # Never forward fill factor values or join a future session onto an earlier date.
    factors = pd.DataFrame([r.get("factors",{}) for r in rows],index=[r['date'] for r in rows]).reindex(sessions)
    additions = {}
    for k in TECH_FIELDS+BASIC_FIELDS:
        values = factors[k] if k in factors else frame[k] if k in frame else pd.Series(np.nan,index=frame.index)
        additions[k] = pd.to_numeric(values,errors='coerce').replace([np.inf,-np.inf],np.nan)
    frame = pd.concat([frame.drop(columns=list(additions),errors='ignore'),pd.DataFrame(additions,index=frame.index)],axis=1).copy()
    for k in columns[1:]:
        if k not in frame:
            frame[k] = np.nan
        frame[k] = pd.to_numeric(frame[k],errors="coerce").replace([np.inf,-np.inf],np.nan)
    valid = (frame.close > 0) & (frame.open > 0) & (frame.low > 0) & (frame.high >= frame[["open","close","low"]].max(axis=1)) & (frame.low <= frame[["open","close","high"]].min(axis=1))
    if 'quality' in frame:
        valid &= frame['quality'].ne('LEGACY_PROVENANCE_UNKNOWN')
    frame["valid_price"] = valid
    frame.loc[~valid,["open","high","low","close"]] = np.nan
    frame["amount"] = frame.amount.where(frame.amount >= 0)
    frame["implicit_roe_pb_pe"] = frame.pb.where(frame.pb > 0)/frame.pe.where(frame.pe > 0)
    for n in sorted({20,60,120,250,spec["ma"]}):
        frame[f"ma{n}"] = frame.close.rolling(n,min_periods=n).mean()
        frame[f"ma{n}_slope"] = frame[f"ma{n}"]/frame[f"ma{n}"].shift(spec["slope_window"])-1
    for n in (5,20,60):
        frame[f"return{n}"] = frame.close/frame.close.shift(n)-1
    frame["amount_ma20"] = frame.amount.where(frame.amount >= 0).rolling(20,min_periods=20).mean()
    frame["amount_ratio"] = frame.amount/frame.amount_ma20.where(frame.amount_ma20 > 0)
    frame["volatility20"] = np.log(frame.close/frame.close.shift()).rolling(20,min_periods=20).std(ddof=1)*math.sqrt(spec["annual_sessions"])
    frame["drawdown120"] = frame.close/frame.close.rolling(120,min_periods=120).max()-1
    for field in ("pe","pb"):
        values = frame[field].where(frame[field] > 0)
        frame[f"{field}_samples"] = values.rolling(spec["valuation_window"],min_periods=1).count()
        frame[f"{field}_percentile"] = values.rolling(spec["valuation_window"],min_periods=20).apply(lambda a: float(np.sum(a <= a[-1])/np.sum(~np.isnan(a))) if not np.isnan(a[-1]) else np.nan,raw=True)
    ma = frame[f'ma{spec["ma"]}']
    for label, condition in (("above",frame.close > ma),("below",frame.close < ma)):
        # Missing sessions and equality reset counters rather than joining visible bars.
        count = 0
        counts = []
        for x in condition:
            count = count+1 if x else 0
            counts.append(count)
        frame[label] = counts
    frame["cross_up"] = (frame.close > ma) & (frame.close.shift() <= ma.shift())
    frame["cross_down"] = (frame.close < ma) & (frame.close.shift() >= ma.shift())
    # The slope window must also contain every required session, even if endpoints exist.
    frame["strategy_ready"] = frame.close.rolling(spec["ma"]+spec["slope_window"],min_periods=spec["ma"]+spec["slope_window"]).count() == spec["ma"]+spec["slope_window"]
    descriptive_ready = frame.close.rolling(60+spec["slope_window"],min_periods=60+spec["slope_window"]).count() == 60+spec["slope_window"]
    frame["regime"] = np.select([~descriptive_ready,(frame.close > frame.ma20) & (frame.ma20 > frame.ma60) & (frame.ma60_slope > 0),(frame.close < frame.ma20) & (frame.ma20 < frame.ma60) & (frame.ma60_slope < 0)],["UNCERTAIN","UP","DOWN"],default="RANGE")
    return frame


def features_at(frame, day):
    if day not in frame.index:
        return {}
    result = {}
    for k,v in frame.loc[day].items():
        if isinstance(v,(bool,np.bool_)):
            result[k] = bool(v)
        elif isinstance(v,(float,int,np.number)):
            result[k] = clean_number(v)
    result["date"] = day
    result["algorithm"] = ALGORITHM
    result["regime"] = str(frame.loc[day,"regime"])
    return result


def step(feature, state, spec, day, triggered_at=None, quality="OK"):
    previous = state
    ready = feature.get("strategy_ready",False)
    if quality != "OK" or not ready:
        return {"as_of":day,"previous_state":previous,"model_state":previous,"state_label":None,"event":"NONE","entry_active":False,"exit_active":False,"triggered_at":triggered_at,"data_status":quality if quality != "OK" else "INSUFFICIENT_DATA","rule_trace":["完整有效交易时段不足或数据质量未通过"],"research_stage":"RESEARCH"}
    exit_active = feature["below"] >= spec["confirmation"]
    from .factors import entry_filter, profile, PROFILES
    passed,factor_ready = entry_filter(feature,spec)
    entry_active = feature["above"] >= spec["confirmation"] and feature[f'ma{spec["ma"]}_slope'] > 0 and passed
    event = "NONE"
    if exit_active and state == "LONG":
        state,event,triggered_at = "FLAT","SELL",day
    elif entry_active and state == "FLAT":
        state,event,triggered_at = "LONG","BUY",day
    label = event if event != "NONE" else ("HOLD" if state == "LONG" else "WAIT")
    trace = [f"低于MA{spec['ma']}连续{int(feature['below'])}日；退出要求{spec['confirmation']}日",f"高于MA{spec['ma']}连续{int(feature['above'])}日；均线斜率{feature[f'ma{spec['ma']}_slope']:.6f}","退出优先；因子不否决退出"]
    if profile(spec):
        trace.append(PROFILES[profile(spec)]['label']+('：通过入场过滤' if passed else '：暂不入场' if factor_ready else '：后台等待补齐，暂停新增买入'))
    return {"as_of":day,"previous_state":previous,"model_state":state,"state_label":label,"event":event,"entry_active":bool(entry_active),"exit_active":bool(exit_active),"triggered_at":triggered_at,"data_status":"OK","factor_ready":factor_ready,"rule_trace":trace,"research_stage":"RESEARCH"}


def replay(frame, spec=None):
    spec = spec or DEFAULT_SPEC
    state,trigger = "FLAT",None
    results = []
    for day in frame.index:
        result = step(features_at(frame,day),state,spec,day,trigger)
        state,trigger = result["model_state"],result["triggered_at"]
        results.append(result)
    return results


def money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError("金额须为有限非负数")
        return result.quantize(Decimal("0.01"),rounding=ROUND_DOWN)
    except (InvalidOperation,TypeError):
        raise ValueError("金额格式不正确") from None


def quantity(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError("份额须为有限非负数")
        return result.quantize(Decimal("0.000001"),rounding=ROUND_DOWN)
    except (InvalidOperation,TypeError):
        raise ValueError("份额格式不正确") from None


def personal_decision(signal, report=None, budget=None, mapping=None, instant=None, catchup=False, stale_days=7):
    instant = instant or datetime.now(timezone.utc)
    result = {"status":"AVAILABLE","action":None,"amount":None,"currency":None,"exposure_basis":None,"reasons":[],"required_context":[],"expires_at":(instant+timedelta(days=1)).isoformat(),"policy":"personal_policy_v1"}
    if signal["data_status"] != "OK":
        result.update(status="BLOCKED",reasons=["行情质量未通过，不能给出有效个人操作"])
        return result
    if not report or (report.get('status') == 'UNKNOWN' and report.get('basis') == 'LEGACY_MANUAL_UNCONFIRMED'):
        report = {"status":"EMPTY","basis":"DEFAULT_EMPTY","reported_at":instant.isoformat()}
    result["position_basis"] = report.get('basis','MANUAL_CONFIRMATION')
    if report.get("status") == "UNKNOWN":
        result.update(status="CONTEXT_REQUIRED",required_context=["你曾明确选择持仓未知，请改为空仓或持有"],reasons=["持仓被明确标为未知"])
        return result
    held = report["status"] == "HELD"
    if held and signal["exit_active"]:
        result["action"] = "SELL"
        result["reasons"] = ["退出条件仍成立；请反馈是否执行"]
    elif held and signal["model_state"] == "FLAT":
        result.update(status="CONTEXT_REQUIRED",required_context=["复核模型空仓与实际持有不一致的原因"],reasons=["模型与实际状态不一致"])
    elif held:
        result["action"] = "HOLD"
    elif signal["event"] == "BUY" or (catchup and signal["entry_active"]):
        result["action"] = "BUY"
        result["reasons"] = ["本日入场事件" if signal["event"] == "BUY" else "用户开启追随当前入场条件；这不是新的交叉信号"]
    else:
        result["action"] = "WAIT"
    # Index-only / status-only operation remains useful without made-up money instructions.
    if result["action"] == "BUY" and budget:
        if budget.get("needs_confirmation"):
            result.update(status="CONTEXT_REQUIRED",action=None,required_context=["交易后请确认剩余现金与已投入本金"])
            return result
        if instant-datetime.fromisoformat(budget["reported_at"]) > timedelta(days=stale_days):
            result.update(status="CONTEXT_REQUIRED",action=None,required_context=["更新现金与预算"])
            return result
        if all(budget.get(k) is not None for k in ("limit","used","cash")):
            remaining = max(Decimal(0),money(budget["limit"])-money(budget["used"]))
            amount = min(remaining,money(budget["cash"]))
            if amount <= 0:
                result.update(status="BLOCKED",action="WAIT",reasons=["预算或现金不足；指数信号仍保留"])
            elif mapping and mapping.get("verified") and mapping.get("source_url") and mapping.get("currency") == budget.get("currency") and mapping.get("product_code"):
                result.update(amount=str(amount),currency=budget["currency"],exposure_basis="USER_REPORTED_PRODUCT_BUDGET")
            else:
                result["required_context"].append("提供并核实真实基金映射和同币种资金，才能计算金额")
    if result["action"] == "SELL" and mapping and mapping.get("verified") and mapping.get("currency") == report.get("currency") and report.get("market_value") is not None and instant-datetime.fromisoformat(report['reported_at']) <= timedelta(days=stale_days):
        result.update(amount=str(money(report["market_value"])),currency=report["currency"],exposure_basis="USER_REPORTED_PRODUCT_VALUE")
    return result
