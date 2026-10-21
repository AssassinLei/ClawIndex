"""Risk definitions, sector independence and persistent forward tracking."""
import copy
from datetime import timedelta

import pandas as pd

from clawindex_next.engine import features_at
from clawindex_next.radar import publish
from clawindex_next.radar_rules import analyze, classify, frame_for, market_state, pair_key, select
from clawindex_next.storage import digest
from conftest import price_rows
from test_evolution import T0
from test_radar import bars, public_item, radar_seed, report_for
from test_ui_redesign import login, button


def custom_frame(prices):
    days,rows = price_rows(len(prices),prices=prices)
    return frame_for(rows,days)


def test_recovered_crash_still_fails_true_window_maximum_drawdown():
    days,rows = bars(.001)
    prices = [r['close'] for r in rows]
    prices[-90] *= .7
    frame = custom_frame(prices)
    benchmark = frame_for(bars(.0001,seed=2)[1],days)
    assert frame.drawdown120.iloc[-1]>-.02 and frame.max_drawdown120.iloc[-1]<-.20
    row = classify(public_item('801010.SI','测试行业','L1'),frame,benchmark,market_state(benchmark))
    assert row['state']=='REJECTED'
    assert any('最大收盘回撤' in text for text in row['reasons'])


def test_recent_calm_does_not_hide_sixty_day_volatility():
    days,rows = bars(.001)
    prices = [r['close'] for r in rows]
    prices[-45] *= .84
    frame = custom_frame(prices)
    benchmark = frame_for(bars(.0001,seed=2)[1],days)
    assert frame.volatility20.iloc[-1]<.45<frame.volatility60.iloc[-1]
    assert frame.max_drawdown120.iloc[-1]>-.20
    row = classify(public_item('801010.SI','测试行业','L1'),frame,benchmark,market_state(benchmark))
    assert row['state']=='REJECTED' and any('60日' in text for text in row['reasons'])


def test_many_strong_children_cannot_outvote_weak_primary_sectors():
    days,up = bars(.0012)
    _,down = bars(falling=True)
    benchmark = frame_for(bars(.0001,seed=2)[1],days)
    items,frames = [],{}
    for i in range(4):
        item = public_item(f'8010{i}0.SI',str(i),'L1',industry=str(i))
        items.append(item); frames[item['code']] = frame_for(up if i==0 else down,days)
    for i in range(8):
        item = public_item(f'8020{i}0.SI','子行业'+str(i),'L2',parent='0')
        items.append(item); frames[item['code']] = frame_for(up,days)
    report = analyze(items,frames,benchmark)
    assert report['status']=='COMPLETE'
    assert report['market']['breadth']==.25 and report['market']['breadth_total']==4
    assert not report['market']['entry_allowed']
    assert all(r['state']!='READY' for r in report['rows'])


def test_primary_coverage_gate_is_separate_from_overall_coverage():
    days,rows = bars(.0012)
    frame = frame_for(rows,days)
    items = [public_item(f'8010{i}0.SI',str(i),'L1',industry=str(i)) for i in range(4)]
    items += [public_item(f'8020{i}0.SI',str(i),'L2',parent='0') for i in range(8)]
    frames = {r['code']:frame for r in items[1:]}
    report = analyze(items,frames,frame)
    assert report['coverage']>.8 and report['market']['breadth_coverage']==.75
    assert not report['market']['entry_allowed']


def test_sibling_sectors_share_one_slot_even_when_returns_are_uncorrelated():
    base = {'state':'READY','kind':'TREND','level':'L2','industry_code':None,
            'features':{'relative60':.1,'volatility20':.2},'parent_index':{'code':'801010.SI','name':'一级行业'}}
    a = {**base,'code':'801011.SI','name':'甲','parent':'110000'}
    b = {**base,'code':'801012.SI','name':'乙','parent':'801010.SI'}
    chosen,suppressed = select([a,b],{pair_key(a['code'],b['code']):.1})
    assert len(chosen)==1 and len(suppressed)==1


def test_valuation_distinctness_uses_the_same_window_as_percentiles():
    days,rows = bars(.0012,n=1500)
    for i,row in enumerate(rows):
        row.update(pe=10+i*.01 if i<100 else 20.,pb=1+i*.001 if i<100 else 2.)
    frame = frame_for(rows,days)
    benchmark = frame_for(bars(.0001,seed=2,n=1500)[1],days)
    row = classify(public_item('801010.SI','测试','L1'),frame,benchmark,market_state(benchmark))
    assert row['state'] in ('WATCH','READY')
    assert any('估值样本长度或有效变化不足' in text for text in row['risks'])


def test_new_risk_metrics_do_not_read_future_bars():
    days,rows = bars()
    before = features_at(frame_for(rows,days),days[-20])
    rows[-1]['close'] *= .5
    after = features_at(frame_for(rows,days),days[-20])
    assert before==after


def test_rule_upgrade_starts_distinct_tracking_cohort_without_rewriting_old(radar_seed):
    db,a,b,*_ = radar_seed
    report = report_for(radar_seed)
    publish(db,report,T0)
    initial = db.rows("SELECT key,value FROM settings WHERE key LIKE 'radar:discovery:%'")
    changed = copy.deepcopy(report)
    changed['rule']['version'] = 'future-rule-test'
    changed['rule_id'] = digest(changed['rule'])
    publish(db,changed,T0+timedelta(minutes=5))
    assert len(db.rows("SELECT key FROM settings WHERE key LIKE 'radar:discovery:%'"))==2*len(initial)
    for row in initial:
        assert db.rows('SELECT value FROM settings WHERE key=?',(row['key'],))[0]['value']==row['value']


def test_tracking_results_remain_visible_after_automatic_rerun(radar_seed,monkeypatch):
    db,a,b,*_ = radar_seed
    publish(db,report_for(radar_seed),T0)
    app = login(db,monkeypatch)
    app.sidebar.radio[0].set_value('发现机会').run()
    button(app,'查看展示后的跟踪结果').click().run()
    assert not app.exception
    def tracking_visible():
        return any('观察交易日' in x.value.columns for x in app.dataframe)
    assert tracking_visible()
    app.run() # Same path as the periodic fragment rerender: no button click.
    assert not app.exception and tracking_visible()
    button(app,'收起跟踪结果').click().run()
    assert not app.exception and not tracking_visible()
