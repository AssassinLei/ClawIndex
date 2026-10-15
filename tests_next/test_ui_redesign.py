"""Daily workflows and decision prioritization in the redesigned interface."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from clawindex_next.service import Evaluator
from clawindex_next.storage import canonical
from clawindex_next.ui_evolution import today_items
from conftest import FixtureCalendar


def button(app, title):
    return next(x for x in app.button if x.label == title)


def login(db, monkeypatch, name='alice'):
    monkeypatch.setenv('VNEXT_DB_PATH',str(db.path))
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app_next.py'),default_timeout=30).run()
    app.text_input[0].set_value(name)
    button(app,'登录').click().run()
    assert not app.exception
    return app


def ready(seeded, monkeypatch, catchup=True):
    db,a,b,aid,baid = seeded
    monkeypatch.setattr('clawindex_next.service.MarketCalendar',FixtureCalendar)
    if catchup:
        aid = a.assign('HKTECH',catchup=True)
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    return db,a,aid


def test_today_prioritizes_action_before_quiet_results(seeded,monkeypatch):
    db,a,aid = ready(seeded,monkeypatch)
    from clawindex_next.migration import insert_instrument,instrument
    with db.connect(True) as c:
        insert_instrument(c,instrument('IXIC','纳斯达克'))
        c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,available_at,availability_basis,row_hash) SELECT 'IXIC',trade_date,payload,source,fetched_at,available_at,availability_basis,row_hash FROM observations WHERE code='HKTECH'")
    Evaluator(db,FixtureCalendar()).evaluate(a.assign('IXIC'))
    original = a.results()
    monkeypatch.setattr(a,'results',lambda:list(reversed(original)))
    items = today_items(a)
    assert [x['category'] for x in items] == ['action','quiet']
    assert items[0]['decision']['action'] == 'BUY'
    assert items[0]['row']['code'] == 'HKTECH'


@pytest.mark.parametrize('change',['expired','market','position'])
def test_old_or_changed_decision_never_enters_action_list(seeded,monkeypatch,change):
    db,a,aid = ready(seeded,monkeypatch)
    assert today_items(a)[0]['category']=='action'
    if change=='position':
        a.report_position('HKTECH','HELD')
    elif change=='market':
        monkeypatch.setattr(a,'market_status',lambda code:{'expected':'2026-08-13'})
    else:
        row=a.results()[0]
        decision=json.loads(row['decision'])
        decision['expires_at']=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        with db.connect(True) as c:
            c.execute('UPDATE personal_decisions SET result=? WHERE id=?',(canonical(decision),row['decision_id']))
    assert today_items(a)[0]['category']=='waiting'


def test_processed_decision_is_quiet_and_does_not_prompt_repeated_order(seeded,monkeypatch):
    db,a,aid=ready(seeded,monkeypatch)
    a.confirm_execution(a.results()[0]['decision_id'],'DEFERRED','等待实际可用资金')
    assert today_items(a)[0]['category']=='quiet'
    app=login(db,monkeypatch)
    assert any('请勿重复下单' in x.value for x in app.markdown)
    assert not any('建议操作：买入' in x.value for x in app.markdown)


def test_empty_account_has_direct_onboarding_and_disabled_inspection(seeded,monkeypatch):
    db,*_=seeded
    app=login(db,monkeypatch,'新朋友')
    assert button(app,'提交全部巡检').disabled
    button(app,'添加我的第一个指数').click().run()
    assert not app.exception
    assert app.title[0].value=='我的指数'
    assert any(x.label=='指数代码' for x in app.text_input)


def test_home_detail_can_be_closed_and_full_signals_have_separate_page(seeded,monkeypatch):
    db,a,aid=ready(seeded,monkeypatch,False)
    app=login(db,monkeypatch)
    assert not any(x.label=='显示指数状态' for x in app.multiselect)
    button(app,'查看数据').click().run()
    assert any(x.label=='指数收盘点位' for x in app.metric)
    button(app,'收起详情').click().run()
    assert not any(x.label=='指数收盘点位' for x in app.metric)
    button(app,'查看全部信号详情').click().run()
    assert not app.exception and app.title[0].value=='信号详情'


def test_unconfigured_evolution_is_not_claimed_as_running(seeded,monkeypatch):
    db,*_=seeded
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('策略与研究').run()
    assert not app.exception
    assert next(x for x in app.metric if x.label=='自动研究').value=='待初始化'
    assert next(x for x in app.expander if x.label=='调整自动进化设置').proto.expanded is False
    assert next(x for x in app.expander if x.label=='高级：手动研究与规则配置').proto.expanded is False


def test_history_keeps_records_after_unwatch(seeded,monkeypatch):
    db,a,aid=ready(seeded,monkeypatch)
    a.unwatch(aid)
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('历史与反馈').run()
    assert not app.exception
    assert next(x for x in app.selectbox if x.label=='筛选指数').options==['全部指数','HKTECH']
    assert any('HKTECH' in str(x.value) for x in app.dataframe)
