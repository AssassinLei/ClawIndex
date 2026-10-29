"""Deterministic risk-first gates. No LLM scores or future-data parameter selection."""
import math
import numpy as np

from .research import metrics


DEFAULT_POLICY = {
    "enabled":True,"auto_promote":False,"auto_inspect":True,
    "ma_min":90,"ma_max":150,"research_days":30,"annual_trial_budget":180,
    "max_drawdown":0.20,"max_return_sacrifice":0.10,"min_improvement":0.01,
    "min_exposure":0.15,"max_trades_year":12,"shadow_sessions":63,
    "min_shadow_trades":2,"promotion_days":90,"cost_bps":10,"delay":1,
    "factor_research":True,"factor_profiles":["price_risk_v1","technical_v1","valuation_liquidity_v1","volatility_channel_v1"],
}


def policy_config(values, spec):
    from .engine import validate_spec
    validate_spec(spec)
    config = {**DEFAULT_POLICY,"ma_min":max(20,spec["ma"]-30),"ma_max":min(250,spec["ma"]+30),**values}
    if set(config) != set(DEFAULT_POLICY):
        raise ValueError("存在未支持的自动进化配置")
    from .factors import PROFILES
    if not isinstance(config['factor_profiles'],list) or not config['factor_profiles'] or any(not isinstance(x,str) or x not in PROFILES for x in config['factor_profiles']) or len(config['factor_profiles']) != len(set(config['factor_profiles'])):
        raise ValueError("因子范围必须来自已登记组合且不能重复")
    for name in ("enabled","auto_promote","auto_inspect","factor_research"):
        if type(config[name]) is not bool:
            raise ValueError("自动进化开关必须为布尔值")
    bounds = {"ma_min":(20,250),"ma_max":(20,250),"research_days":(1,365),
              "annual_trial_budget":(18,360),"max_trades_year":(1,60),
              "shadow_sessions":(63,504),"min_shadow_trades":(2,30),
              "promotion_days":(30,730),"cost_bps":(0,500),"delay":(1,10)}
    for k,(lo,hi) in bounds.items():
        if type(config[k]) is not int or not lo <= config[k] <= hi:
            raise ValueError(f"{k}不在允许范围内")
    for k,lo,hi in (("max_drawdown",.01,.60),("max_return_sacrifice",0,.50),
                    ("min_improvement",.001,.20),("min_exposure",.05,.95)):
        if isinstance(config[k],bool) or not isinstance(config[k],(int,float)) or not math.isfinite(config[k]) or not lo <= config[k] <= hi:
            raise ValueError(f"{k}不在允许范围内")
    if not config["ma_min"] <= spec["ma"] <= config["ma_max"]:
        raise ValueError("当前均线必须在授权范围内")
    return config


def curve_summary(curve, trades, exposure, annual=252):
    result = metrics(curve,annual)
    result.update(trade_count=trades,exposure=exposure,trades_year=trades*annual/max(1,len(curve)-1))
    return result


def gates(candidate, incumbent, hold, matched, policy, require_trades=False):
    """All limits are empirical evaluation gates, not guaranteed future loss limits."""
    reasons = []
    if abs(candidate["max_drawdown"]) > policy["max_drawdown"]:
        reasons.append("观察回撤超过设定门槛")
    if abs(incumbent["max_drawdown"])-abs(candidate["max_drawdown"]) < policy["min_improvement"]:
        reasons.append("相对现行策略的回撤改善不足")
    if hold["return"]-candidate["return"] > policy["max_return_sacrifice"]:
        reasons.append("相对持续持有少赚过多")
    if candidate["exposure"] < policy["min_exposure"]:
        reasons.append("持有时间过少，不能仅靠空仓获得晋升")
    if candidate["trades_year"] > policy["max_trades_year"]:
        reasons.append("交易频率超过上限")
    if candidate["return"] < matched["return"]-policy["max_return_sacrifice"] or abs(candidate["max_drawdown"]) > abs(matched["max_drawdown"])+policy["min_improvement"]:
        reasons.append("没有通过相似敞口加现金的对照检查")
    if require_trades and candidate["trade_count"] < policy["min_shadow_trades"]:
        reasons.append("真实前向观察中的模拟交易次数不足")
    return reasons


def segment(report, lo, hi, annual=252):
    dates = report["curve"]["date"][lo:hi]
    # Include the close before the first assessed return. Otherwise a sharp
    # loss on the validation/test boundary disappears from its risk metrics.
    anchor = max(0,lo-1)
    equity = report["curve"]["strategy"][anchor:hi]
    hold = report["curve"]["buy_hold"][anchor:hi]
    trades = [t for t in report["trades"] if dates[0] <= t["execution_date"] <= dates[-1]]
    # Exposure is explicitly stored by the simulator; do not infer it from returns.
    weights = report["curve"]["exposure"][max(1,lo):hi]
    exposure = float(np.mean(weights))
    matched = [exposure*x/hold[0]+1-exposure for x in hold]
    return curve_summary(equity,len(trades),exposure,annual),metrics(hold,annual),metrics(matched,annual)


def select_candidate(reports, base_spec, policy):
    """Choose using development only; validation/test never select a runner-up."""
    from .engine import validate_spec
    base_id = validate_spec(base_spec)
    annual = base_spec["annual_sessions"]
    primary = [r for r in reports if r["cost_bps_per_side"] == policy["cost_bps"] and r["execution_delay_sessions"] == policy["delay"]]
    baseline = next(r for r in primary if validate_spec(r["spec"]) == base_id)
    n = len(baseline["curve"]["date"])
    boundaries = [0,int(n*.6),int(n*.8),n]
    if n < 504 or min(boundaries[i+1]-boundaries[i] for i in range(3)) < 63:
        return None,{"reasons":["共同评估区间不足504个交易时段"],"trials":len(reports)}
    ranked = []
    for report in primary:
        if validate_spec(report["spec"]) == base_id:
            continue
        c,h,m = segment(report,0,boundaries[1],annual)
        incumbent = segment(baseline,0,boundaries[1],annual)[0]
        reasons = gates(c,incumbent,h,m,policy)
        if not reasons:
            ranked.append((abs(c["max_drawdown"]),-c["return"],validate_spec(report["spec"]),report))
    if not ranked:
        return None,{"reasons":["开发区间没有满足回撤与机会成本条件的候选"],"trials":len(reports)}
    chosen = min(ranked,key=lambda x:x[:3])[3]
    checks = []
    # Freeze this winner before opening remaining intervals. Cost/delay stress cannot
    # substitute a different parameter after a failed test.
    for r in reports:
        if r["spec"] != chosen["spec"]:
            continue
        reference = next(x for x in reports if x["spec"] == base_spec and x["cost_bps_per_side"] == r["cost_bps_per_side"] and x["execution_delay_sessions"] == r["execution_delay_sessions"])
        for label,lo,hi in (("validation",boundaries[1],boundaries[2]),("test",boundaries[2],n)):
            c,h,m = segment(r,lo,hi,annual)
            reasons = gates(c,segment(reference,lo,hi,annual)[0],h,m,policy)
            checks.append({"interval":label,"cost_bps":r["cost_bps_per_side"],"delay":r["execution_delay_sessions"],"candidate":c,"reasons":reasons})
    # Adjacent parameters must not imply a sharply isolated development optimum.
    from .factors import profile
    neighbours = [r for r in primary if r["spec"] != chosen["spec"] and r['spec']['name'] == chosen['spec']['name'] and profile(r['spec']) == profile(chosen['spec'])]
    # A neighbour that merely stays in cash or sacrifices excessive return is
    # not evidence of a stable investable region around the selected parameter.
    neighbour_ok = False
    incumbent = segment(baseline,0,boundaries[1],annual)[0]
    chosen_drawdown = abs(segment(chosen,0,boundaries[1],annual)[0]['max_drawdown'])
    for neighbour in neighbours:
        c,h,m = segment(neighbour,0,boundaries[1],annual)
        if not gates(c,incumbent,h,m,policy) and abs(c['max_drawdown'])-chosen_drawdown <= 2*policy['min_improvement']:
            neighbour_ok = True
            break
    reasons = sorted({v for check in checks for v in check["reasons"]})
    if not neighbour_ok:
        reasons.append("邻近参数稳定性不足")
    return (chosen["spec"] if not reasons else None),{"reasons":reasons,"checks":checks,"selected_spec":chosen["spec"],"trials":len(reports),"historical_only":True,"test_consumed":True}
