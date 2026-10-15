from datetime import datetime,timezone,timedelta

import pytest

from clawindex_next.engine import DEFAULT_SPEC, feature_frame, features_at, personal_decision, replay, step, validate_spec
from conftest import price_rows


def test_warmup_and_per_feature_dependencies():
    dates,rows = price_rows(80)
    f = feature_frame(rows,dates)
    x = features_at(f,dates[-1])
    assert x["ma60"] is not None and x["ma120"] is None
    assert replay(f)[-1]["data_status"] == "INSUFFICIENT_DATA"


def test_low_valuation_cannot_veto_exit_and_three_day_confirmation():
    feature = {"strategy_ready":True,"above":0,"below":3,"ma120_slope":-.01,"pe_percentile":.01}
    s = step(feature,"LONG",DEFAULT_SPEC,"2026-08-12")
    assert s["event"] == "SELL" and s["model_state"] == "FLAT"
    feature["below"] = 2
    assert step(feature,"LONG",DEFAULT_SPEC,"2026-08-11")["event"] == "NONE"


def test_missing_session_is_not_three_consecutive_days():
    dates,rows = price_rows(160)
    rows[-4]["close"] = rows[-4]["open"] = 70
    rows[-4]["low"],rows[-4]["high"] = 69,71
    rows[-2]["close"] = rows[-2]["open"] = 70
    rows[-2]["low"],rows[-2]["high"] = 69,71
    rows[-1]["close"] = rows[-1]["open"] = 70
    rows[-1]["low"],rows[-1]["high"] = 69,71
    rows.pop(-3)
    f = feature_frame(rows,dates)
    assert features_at(f,dates[-1])["below"] <= 2
    assert replay(f)[-1]["state_label"] is None


def test_equality_and_recovery_reset_counter():
    dates,rows = price_rows(160,prices=[100.]*160)
    f = feature_frame(rows,dates)
    assert f.iloc[-1]["above"] == f.iloc[-1]["below"] == 0
    assert replay(f)[-1]["state_label"] == "WAIT"
    prices = [100+i*.2 for i in range(160)]
    prices[-5:] = [70,70,150,70,70]
    dates,rows = price_rows(160,prices=prices)
    f = feature_frame(rows,dates)
    assert f.iloc[-1]["below"] == 2
    assert replay(f)[-1]["event"] == "NONE"


def test_no_future_features_or_valuation_percentiles():
    dates,rows = price_rows(160)
    before = features_at(feature_frame(rows[:140],dates[:140]),dates[139])
    rows[-1].update(close=99999,high=100000,pe=99999,pb=99999)
    after = features_at(feature_frame(rows,dates),dates[139])
    assert before == after


def test_persistent_episode_is_hold_and_stale_is_not_hold():
    dates,rows = price_rows()
    runs = replay(feature_frame(rows,dates))
    assert sum(s["event"] == "BUY" for s in runs) == 1
    assert runs[-1]["state_label"] == "HOLD"
    stale = step(features_at(feature_frame(rows,dates),dates[-1]),"LONG",DEFAULT_SPEC,dates[-1],quality="STALE")
    assert stale["state_label"] is None and stale["event"] == "NONE"


def test_default_empty_persists_but_explicit_unknown_remains_unknown():
    s = {"data_status":"OK","event":"BUY","entry_active":True,"exit_active":False,"model_state":"LONG"}
    instant = datetime.now(timezone.utc)
    default = personal_decision(s,instant=instant)
    assert default['action'] == 'BUY' and default['position_basis'] == 'DEFAULT_EMPTY'
    report = {"status":"EMPTY","reported_at":instant.isoformat()}
    assert personal_decision(s,report,instant=instant)["action"] == "BUY"
    report["reported_at"] = (instant-timedelta(days=8)).isoformat()
    assert personal_decision(s,report,instant=instant)["action"] == 'BUY'
    report['status'] = 'UNKNOWN'
    assert personal_decision(s,report,instant=instant)['status'] == 'CONTEXT_REQUIRED'


def test_budget_decimal_currency_and_catchup():
    instant = datetime.now(timezone.utc)
    report = {"status":"EMPTY","reported_at":instant.isoformat()}
    s = {"data_status":"OK","event":"NONE","entry_active":True,"exit_active":False,"model_state":"LONG"}
    assert personal_decision(s,report,instant=instant)["action"] == "WAIT"
    budget = {"limit":"100.01","used":"90.00","cash":"100.00","currency":"CNY","reported_at":instant.isoformat()}
    mapping = {"verified":True,"currency":"CNY","source_url":"https://example.org/official","product_code":"000001"}
    p = personal_decision(s,report,budget,mapping,instant,True)
    assert p["amount"] == "10.01" and p["exposure_basis"] != "PORTFOLIO_WEIGHT"
    mapping["currency"] = "HKD"
    assert personal_decision(s,report,budget,mapping,instant,True)["amount"] is None
    budget["used"] = "100.01"
    assert personal_decision(s,report,budget,mapping,instant,True)["status"] == "BLOCKED"


@pytest.mark.parametrize("value",[0,500,float('nan'),"120",True])
def test_invalid_parameters_rejected(value):
    with pytest.raises(ValueError): validate_spec({**DEFAULT_SPEC,"ma":value})


def test_impossible_ohlc_and_zero_amount_not_numeric_signals():
    dates,rows = price_rows()
    rows[-1].update(high=1,amount=0)
    f = feature_frame(rows,dates)
    assert features_at(f,dates[-1])["close"] is None
    assert replay(f)[-1]["state_label"] is None
