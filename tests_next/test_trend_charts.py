"""Historical evidence must stay faithful to the inspected data and UI state."""
import copy
import json

import altair as alt
import pandas as pd
import pytest

from clawindex_next.engine import DEFAULT_SPEC,feature_frame,features_at
from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical,digest,now
from clawindex_next.trend_charts import (METRICS,chart_rows,chart_spec,display_range,history_frame,
                                        professional_fields,series_for)
from conftest import FixtureCalendar,price_rows
from test_ui_redesign import button,login


def sample():
    dates,rows = price_rows(400)
    for i,row in enumerate(rows):
        row.update(amount=100_000_000+i*1000,amount_unit='CNY',pe=10+i*.03,pb=1+i*.005,
                   factors={'rsi_bfq_12':45+i%20,'macd_dif_bfq':i*.001,'turnover_rate':.5+i*.001})
    return {'code':'HKTECH','sessions':dates,'as_of':dates[-1],'data':rows}


def test_display_ranges_preserve_warmup_and_match_inspection_values():
    manifest = sample()
    frame = history_frame(manifest,DEFAULT_SPEC)
    actual = feature_frame(manifest['data'],manifest['sessions'],DEFAULT_SPEC)
    for field in ('ma120','ma120_slope','volatility20','pe_percentile','pb_percentile','above'):
        pd.testing.assert_series_equal(frame[field],actual[field])
    data = series_for(frame,DEFAULT_SPEC,'price')
    recent = display_range(data,'近1月',manifest['as_of'])
    assert len(recent)<30 and recent.iloc[0]['120日规则均线']>0
    assert recent.iloc[-1].equals(data.iloc[-1])
    assert recent.index[0]>manifest['sessions'][0]


def test_chart_does_not_invent_prices_or_bridge_missing_sessions():
    manifest = sample()
    missing = manifest['sessions'][-15]
    manifest['data'] = [r for r in manifest['data'] if r['date']!=missing]
    frame = history_frame(manifest,DEFAULT_SPEC)
    assert pd.isna(frame.loc[missing,'close']) and pd.isna(frame.iloc[-1]['ma120'])
    data = series_for(frame,DEFAULT_SPEC,'price',selected=['close'])
    rows = chart_rows(data)
    before = rows[rows['日期']<pd.Timestamp(missing)].iloc[-1]
    after = rows[rows['日期']>pd.Timestamp(missing)].iloc[0]
    assert before['分段']!=after['分段']
    assert len(rows)==len(data)-1
    assert chart_spec('点位')['encoding']['detail']['field']=='分段'


def test_missing_factor_breaks_only_its_series_and_infinity_is_not_plotted():
    data = pd.DataFrame({'甲':[1,None,2,float('inf'),3],'乙':[5,6,7,8,9]},index=pd.bdate_range('2026-01-01',periods=5))
    rows = chart_rows(data)
    assert rows[rows['曲线']=='甲']['分段'].nunique()==3
    assert rows[rows['曲线']=='乙']['分段'].nunique()==1
    assert len(rows)==8


def test_ranges_use_analysis_date_and_future_prices_do_not_rewrite_past():
    manifest = sample()
    frame = history_frame(manifest,DEFAULT_SPEC)
    old = features_at(frame,manifest['sessions'][-30])
    changed = copy.deepcopy(manifest)
    changed['data'][-1]['close']*=.5
    assert features_at(history_frame(changed,DEFAULT_SPEC),manifest['sessions'][-30])==old
    future = copy.deepcopy(manifest['data'][-1])
    future.update(date='2099-01-01',close=999999)
    changed = copy.deepcopy(manifest)
    changed['sessions'].append(future['date'])
    changed['data'].append(future)
    pd.testing.assert_frame_equal(history_frame(changed,DEFAULT_SPEC),frame)
    recent = display_range(series_for(frame,DEFAULT_SPEC,'valuation'),'近1月',manifest['as_of'])
    assert len(recent)>10 and recent.index[-1]==manifest['as_of']


def test_valuation_is_past_only_and_amount_requires_confirmed_units():
    manifest = sample()
    manifest['data'][-1].update(amount_unit='UNKNOWN',pe=-1,pb=0)
    frame = history_frame(manifest,DEFAULT_SPEC)
    assert frame['amount_cny'].iloc[0]==1.
    assert pd.isna(frame['amount_cny'].iloc[-1])
    assert pd.isna(frame['pe'].iloc[-1]) and pd.isna(frame['pb'].iloc[-1])
    assert pd.isna(frame['implicit_roe_pb_pe'].iloc[-1])
    assert frame['price_percentile'].iloc[19]==1.
    assert pd.isna(frame['price_percentile'].iloc[18])
    chart = series_for(frame,DEFAULT_SPEC,'valuation')
    assert chart.iloc[-2]['市盈率历史分位']==frame.iloc[-2]['pe_percentile']*100
    assert 'rsi_bfq_12' in professional_fields(frame)
    assert 'turnover_rate' in professional_fields(frame)
    assert 'rsi_bfq_24' not in professional_fields(frame)


@pytest.mark.parametrize('metric',list(METRICS))
def test_all_chart_types_generate_valid_vega_lite_schema(metric):
    manifest = sample()
    frame = history_frame(manifest,DEFAULT_SPEC)
    data = series_for(frame,DEFAULT_SPEC,metric,professional='rsi_bfq_12')
    assert not chart_rows(data).empty
    alt.Chart.from_dict({**chart_spec(METRICS[metric][1]),'data':{'values':[]}}).to_dict(validate=True)


def test_saved_inspection_chart_ignores_new_revisions_and_uses_original_rule(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    sid,_ = Evaluator(db,FixtureCalendar()).evaluate(aid)
    before = a.trend_source('HKTECH',sid)
    row = {**before['manifest']['data'][-1],'close':200.,'high':201.}
    with db.connect(True) as c:
        c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,availability_basis,row_hash) VALUES('HKTECH',?,?,'fixture',?,'FIXTURE',?)",(row['date'],canonical(row),now(),digest(row)))
    a.assign('HKTECH',{**DEFAULT_SPEC,'ma':90})
    assert a.trend_source('HKTECH',sid)==before
    current = a.trend_source('HKTECH')
    assert current['manifest']['data'][-1]['close']==200.
    assert current['spec']['ma']==90 and before['spec']['ma']==120
    with pytest.raises(PermissionError):
        b.trend_source('HKTECH',sid)
    with pytest.raises(PermissionError):
        a.trend_source('IXIC',sid)
    a.unwatch(a.watchlist()[0]['id'])
    assert a.trend_source('HKTECH',sid)==before # Archived analyses remain readable.


def test_latest_chart_without_prices_is_empty_and_does_not_enqueue_work(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    with db.connect(True) as c:
        c.execute('DELETE FROM observations')
    assert history_frame(**{k:a.trend_source('HKTECH')[k] for k in ('manifest','spec')}).empty
    assert db.rows('SELECT * FROM jobs')==[]
    with pytest.raises(PermissionError):
        a.trend_source('IXIC')


def charts(app):
    return app.get('vega_lite_chart')


def open_expander(app, label):
    expander = next(x for x in app.expander if x.label==label)
    app.session_state[expander.key] = True
    return app.run()


def test_today_chart_survives_refresh_and_retains_metric_and_range(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    sid,_ = Evaluator(db,FixtureCalendar()).evaluate(aid)
    explanations_before = db.rows('SELECT * FROM explanations')
    app = login(db,monkeypatch)
    assert not charts(app)
    button(app,'查看曲线').click().run()
    assert not app.exception and charts(app)
    assert any('本次巡检保存的数据' in x.value for x in app.caption)
    select = next(x for x in app.selectbox if x.label=='曲线指标')
    select.set_value('valuation').run()
    next(x for x in app.radio if x.label=='显示范围').set_value('近1月').run()
    assert not app.exception and charts(app)
    app.run()
    assert not app.exception and charts(app)
    assert next(x for x in app.selectbox if x.label=='曲线指标').value=='valuation'
    assert next(x for x in app.radio if x.label=='显示范围').value=='近1月'
    open_expander(app,'查看曲线数据并导出')
    assert not app.exception and app.get('download_button')
    assert any('市盈率历史分位' in x.value.columns for x in app.dataframe)
    button(app,'收起详情').click().run()
    assert not charts(app) and not app.exception
    assert not db.rows('SELECT * FROM jobs')
    assert len(db.rows('SELECT * FROM signal_runs'))==1
    assert db.rows('SELECT * FROM explanations')==explanations_before


def test_curves_available_in_data_evidence_watch_and_archived_history(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    app = login(db,monkeypatch)
    button(app,'查看数据').click().run()
    open_expander(app,'查看历史曲线')
    assert not app.exception and charts(app)
    app.sidebar.radio[0].set_value('信号与待执行').run()
    open_expander(app,'查看历史曲线')
    assert not app.exception and charts(app)
    app.sidebar.radio[0].set_value('监控与数据').run()
    assert not charts(app) # Collapsed chart is genuinely lazy.
    open_expander(app,'查看历史曲线')
    assert not app.exception and charts(app)
    assert any('后台已同步的最新可用数据' in x.value for x in app.caption)
    a.unwatch(aid)
    app.sidebar.radio[0].set_value('历史与反馈').run()
    open_expander(app,'查看某次巡检的历史曲线')
    assert not app.exception and charts(app)
    assert any('本次巡检保存的数据' in x.value for x in app.caption)


def test_missing_professional_values_and_empty_price_selection_have_clear_ui(seeded,monkeypatch):
    db,a,b,aid,_ = seeded
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    app = login(db,monkeypatch)
    button(app,'查看曲线').click().run()
    next(x for x in app.multiselect if x.label=='叠加曲线').set_value([]).run()
    assert not app.exception and not charts(app)
    assert any('暂无有效数值' in x.value for x in app.info)
    next(x for x in app.selectbox if x.label=='曲线指标').set_value('professional').run()
    assert not app.exception and not charts(app)
    assert any('海外接口可能不提供' in x.value for x in app.info)
