from __future__ import annotations

import math

import numpy as np

from .engine import DEFAULT_SPEC, feature_frame, replay, validate_spec
from .storage import canonical, digest, now


def metrics(values, annual_sessions=252):
    values = np.asarray(values,dtype=float)
    if len(values)<2:
        return {"return":0.0,"cagr":None,"max_drawdown":0.0,"volatility":None}
    changes = values[1:]/values[:-1]-1
    return {"return":float(values[-1]/values[0]-1),"cagr":float((values[-1]/values[0])**(annual_sessions/(len(values)-1))-1),"max_drawdown":float(np.min(values/np.maximum.accumulate(values)-1)),"volatility":float(np.std(changes,ddof=1)*math.sqrt(annual_sessions)) if len(changes)>1 else None}


def backtest(rows, sessions, spec=None, cost_bps=10, delay=1, evaluation_start=None):
    spec = spec or DEFAULT_SPEC
    validate_spec(spec)
    if not 0 <= cost_bps <= 500 or not 1 <= delay <= 10:
        raise ValueError("成本为每边0至500基点，执行至少延迟1个交易时段")
    frame = feature_frame(rows,sessions,spec)
    signals = replay(frame,spec)
    start = next((i for i,s in enumerate(signals) if s["data_status"] == "OK"),None)
    if start is not None and evaluation_start:
        start = max(start,next((i for i,d in enumerate(sessions) if d>=evaluation_start),len(sessions)))
    if start is None or start+1 >= len(sessions):
        raise ValueError("研究样本不足，不能生成收益比较")
    fee = cost_bps/10000
    cash,units,baseline_units,baseline_cash = 1.0,0.0,0.0,1.0
    pending = None
    equity,baseline,trades,dates,exposure,curve_exposure = [1.0],[1.0],[],[sessions[start]],[],[0.0]
    fees,turnover,last_mark = 0.0,0.0,None
    for i in range(start,len(sessions)):
        row = frame.iloc[i]
        valid_open = row["open"] if math.isfinite(row["open"]) and row["open"] > 0 else None
        valid_close = row["close"] if math.isfinite(row["close"]) and row["close"] > 0 else None
        if valid_close is not None:
            last_mark = valid_close
        # A pending signal waits for a valid later open; never executes at its own close.
        if pending and i >= pending["due"] and valid_open is not None:
            action = pending["action"]
            if action == "BUY" and not units:
                trade_value = cash/(1+fee)
                paid = cash-trade_value
                units,cash = trade_value/valid_open,0.0
            elif action == "SELL" and units:
                trade_value = units*valid_open
                paid = trade_value*fee
                cash,units = trade_value-paid,0.0
            else:
                trade_value,paid = 0.0,0.0
            if trade_value:
                fees += paid
                turnover += trade_value
                trades.append({"signal_date":pending["signal_date"],"execution_date":sessions[i],"action":action,"price":float(valid_open),"fee":paid})
            pending = None
        if i >= start+delay and not baseline_units and valid_open is not None:
            baseline_units = baseline_cash/(1+fee)/valid_open
            baseline_cash = 0.0
        if i > start:
            dates.append(sessions[i])
            equity.append(float(cash+units*(last_mark or 0)))
            baseline.append(float(baseline_cash+baseline_units*(last_mark or 0)))
            exposure.append(bool(units))
            curve_exposure.append(float(bool(units)))
        signal = signals[i]
        if signal["data_status"] == "OK":
            # A later exit cancels an unfilled buy; the reverse also applies. One desired model exposure.
            desired = signal["model_state"] == "LONG"
            actual = bool(units)
            if pending and (pending["action"] == "BUY") != desired:
                pending = None
            if desired != actual and not pending:
                pending = {"action":"BUY" if desired else "SELL","signal_date":sessions[i],"due":i+delay}
    run = 0
    longest = 0
    for a,b in zip(equity,baseline):
        run = run+1 if a < b else 0
        longest = max(longest,run)
    n = len(equity)
    boundaries = [0,int(n*.6),int(n*.8),n]
    splits = {}
    for j,label in enumerate(("development","validation","held_out_test")):
        lo,hi = boundaries[j],boundaries[j+1]
        # Include the preceding close to preserve boundary returns. No strategy reset at splits.
        splits[label] = {"start":dates[lo],"end":dates[hi-1],"strategy":metrics(equity[max(0,lo-1):hi],spec["annual_sessions"]),"buy_hold":metrics(baseline[max(0,lo-1):hi],spec["annual_sessions"])} if hi>lo else None
    missing = int((~frame.valid_price).sum())
    regime_stats = {}
    for j in range(1,len(dates)):
        # Classify each return using the preceding session's observed features.
        label = str(frame.loc[dates[j-1],"regime"])
        entry = regime_stats.setdefault(label,{"sessions":0,"strategy_daily_return_sum":0.0,"buy_hold_daily_return_sum":0.0})
        entry["sessions"] += 1
        entry["strategy_daily_return_sum"] += equity[j]/equity[j-1]-1
        entry["buy_hold_daily_return_sum"] += baseline[j]/baseline[j-1]-1
    for entry in regime_stats.values():
        entry["strategy_mean_daily_return"] = entry.pop("strategy_daily_return_sum")/entry["sessions"]
        entry["buy_hold_mean_daily_return"] = entry.pop("buy_hold_daily_return_sum")/entry["sessions"]
    return {"spec":spec,"cost_bps_per_side":cost_bps,"execution_delay_sessions":delay,"cash_return":0,"initial_state":"FLAT","replay_start":sessions[0],"evaluation_start":dates[0],"evaluation_end":dates[-1],"strategy":metrics(equity,spec["annual_sessions"]),"buy_hold":metrics(baseline,spec["annual_sessions"]),"turnover_initial_capital":turnover,"fees_initial_capital":fees,"empty_fraction":1-sum(exposure)/len(exposure) if exposure else 1,"longest_underperformance_sessions":longest,"trade_count":len(trades),"trades":trades,"pending_unexecuted":pending,"splits":splits,"regime_performance":regime_stats,"curve":{"date":dates,"strategy":equity,"buy_hold":baseline,"exposure":curve_exposure},"missing_session_count":missing,"comparison_status":"INVALID_DATA_GAPS" if missing else "SHORT_SAMPLE" if n<spec["annual_sessions"] else "EXPLORATORY","limitations":["价格指数研究，不是基金净值或账户收益；未计分红、汇率和申赎限制","缺失行情时估值暂用最后已知收盘价并报告缺口，不能视为真实可成交记录","历史发布时间/修订未知，采用盘后可知且下一时段执行假设","样本外区间已用于本报告；再次据此调参不再是未使用的测试集","行业指数可能包含回溯历史及幸存者偏差；UP/DOWN/RANGE只是描述，不是牛熊预测"],"stage":"RESEARCH"}


def study(store, user_id, code, manifest, spec=None, goal="控制回撤并记录收益代价", costs=(0,10,30), delays=(1,2)):
    spec = spec or DEFAULT_SPEC
    results = []
    candidates = sorted({max(20,spec["ma"]-30),spec["ma"],min(250,spec["ma"]+30)})
    common_start = manifest["sessions"][max(candidates)+spec["slope_window"]-1] if len(manifest["sessions"]) > max(candidates)+spec["slope_window"] else None
    if common_start is None:
        raise ValueError("样本不足以在同一评估区间比较邻近参数")
    for ma in candidates:
        for cost in costs:
            for delay in delays:
                variant = {**spec,"ma":ma}
                try:
                    results.append(backtest(manifest["data"],manifest["sessions"],variant,cost,delay,common_start))
                except ValueError:
                    continue
    if not results:
        raise ValueError("样本不足以进行策略研究")
    report = {"code":code,"dataset_id":digest(manifest),"strategy_id":validate_spec(spec),"goal":goal,"parameter_trials":len(candidates),"scenario_trials":len(candidates)*len(costs)*len(delays),"selection":"未自动挑选获利最高参数，未自动启用","historical_availability":manifest["historical_availability"],"research_usable":all(r["comparison_status"] == "EXPLORATORY" for r in results),"results":results}
    rid = digest({"report":report,"user":user_id})
    with store.connect(True) as c:
        c.execute("INSERT OR IGNORE INTO research_runs VALUES(?,?,?,?,?)", (rid,user_id,code,canonical(report),now()))
    return rid,report
