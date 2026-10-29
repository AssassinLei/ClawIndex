"""Daily decisions distinguish intent, order submission, fills, and evidence."""
import json
from datetime import timedelta

import pytest

from clawindex_next.service import Evaluator
from clawindex_next.today_guidance import handling,plan_context,summary_counts
from clawindex_next.today_guidance import display_priority,research_reuse_message
from clawindex_next.ui_evolution import today_items
from conftest import FixtureCalendar
from test_ui_redesign import button,login,ready
from test_evolution import T0


def item(status=None,action='BUY',category='quiet'):
    return {'category':category,'decision':{'action':action},'feedback':[{'status':status}] if status else []}


def test_summary_does_not_count_deferred_or_unfilled_orders_as_nothing_to_do():
    rows = [item(category='action'),item('DEFERRED','SELL'),item('SUBMITTED'),item('PARTIAL'),
            item('EXECUTED'),item(action='HOLD'),item(category='waiting')]
    counts = summary_counts(rows,awaiting=2)
    assert counts=={'action':2,'quiet':2,'waiting':5,'followup':3}
    assert counts['action']+counts['quiet']+counts['waiting']==len(rows)+2


@pytest.mark.parametrize('status,expected,followup',[
    ('DEFERRED','已暂缓，尚未成交',True),('SUBMITTED','已提交，待成交确认',True),
    ('PARTIAL','部分成交，待跟进',True),('EXECUTED','已确认成交',False),
    ('CANCELLED','已记录取消',False),('EXPIRED','已记录过期',False)])
def test_handling_labels_never_call_intent_a_verified_fill(status,expected,followup):
    result = handling(status,'SELL')
    assert result['title']==expected and result['followup']==followup
    if status=='DEFERRED':
        assert '退出条件仍成立' in result['text'] and '没有据此确认成交' in result['text']


def test_non_action_feedback_does_not_invent_buy_conditions_or_followups():
    assert '入场条件' not in handling('DEFERRED','WAIT')['text']
    assert not handling('DEFERRED','HOLD')['followup']
    assert summary_counts([item('DEFERRED','WAIT')])=={'action':0,'quiet':1,'waiting':0,'followup':0}


def test_plan_context_separates_catchup_from_new_events_and_shows_full_expiry():
    signal = {'event':'NONE','triggered_at':'2026-08-10'}
    decision = {'action':'BUY','expires_at':'2026-08-12T15:30:00+00:00'}
    context = plan_context(signal,decision)
    assert '不是今天又出现了新的交叉信号' in context['event']
    assert '2026-08-10' in context['event']
    assert '2026-08-12 23:30' in context['validity'] and '北京时间' in context['validity']
    assert '提前失效' in context['validity']
    assert '入场触发' in plan_context({**signal,'event':'BUY'},decision)['event']
    assert '已经卖出' in plan_context(signal,{**decision,'action':'SELL'})['event']


@pytest.mark.parametrize('status,fragment',[
    ('DEFERRED','你选择暂不执行'),('SUBMITTED','尚未确认成交'),
    ('PARTIAL','剩余部分尚未确认'),('EXECUTED','你已确认本次操作成交')])
def test_today_cards_explain_actual_execution_status_without_resending_orders(seeded,monkeypatch,status,fragment):
    db,a,aid = ready(seeded,monkeypatch)
    did = a.results()[0]['decision_id']
    a.confirm_execution(did,status,'人工反馈')
    assert today_items(a)[0]['category']=='quiet' # Existing notification dedup stays intact.
    app = login(db,monkeypatch)
    assert not app.exception
    assert any(fragment in x.value for x in app.markdown)
    assert not any('该操作已处理：' in x.value for x in app.markdown)
    assert not db.rows('SELECT * FROM jobs')
    assert a.position('HKTECH')['basis']=='DEFAULT_EMPTY' # A feedback never invents a position.
    if status!='EXECUTED':
        assert any('尚未确认全部成交' in x.value for x in app.caption)


def test_today_chart_stays_open_when_background_replaces_decision_id(seeded,monkeypatch):
    db,a,aid = ready(seeded,monkeypatch)
    old_id = a.results()[0]['decision_id']
    app = login(db,monkeypatch)
    button(app,'查看曲线').click().run()
    next(x for x in app.radio if x.label=='显示范围').set_value('近3月').run()
    assert app.get('vega_lite_chart')
    a.report_position('HKTECH','EMPTY')
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    assert a.results()[0]['decision_id']!=old_id
    app.run()
    assert not app.exception and app.get('vega_lite_chart')
    assert next(x for x in app.radio if x.label=='显示范围').value=='近3月'
    assert any('本次巡检保存的数据' in x.value for x in app.caption)
    assert len(db.rows('SELECT * FROM execution_confirmations'))==0


def test_feedback_requires_explicit_outcome_and_cannot_change_position_before_fill(seeded,monkeypatch):
    db,a,aid = ready(seeded,monkeypatch)
    app = login(db,monkeypatch)
    result = next(x for x in app.selectbox if x.label=='处理结果')
    assert result.value is None and button(app,'保存处理结果').disabled
    checkbox = lambda: next(x for x in app.checkbox if x.label=='同时确认成交后的真实持仓状态')
    assert checkbox().disabled
    result.set_value('EXECUTED').run()
    checkbox().check().run()
    assert checkbox().value
    next(x for x in app.selectbox if x.label=='处理结果').set_value('DEFERRED').run()
    assert checkbox().disabled and not checkbox().value
    button(app,'保存处理结果').click().run()
    assert not app.exception
    assert a.position('HKTECH')['basis']=='DEFAULT_EMPTY'
    assert json.loads(db.rows('SELECT payload FROM execution_confirmations')[0]['payload'])['outcome']=='DEFERRED'


def test_pending_exit_precedes_new_entry_but_submitted_exit_is_not_a_fresh_order():
    sell = {**item('DEFERRED','SELL'),'current':True,'handled':True}
    sell['decision']['status']='AVAILABLE'
    buy = item(category='action')
    submitted = {**sell,'feedback':[{'status':'SUBMITTED'}]}
    assert display_priority(sell)<display_priority(buy)<display_priority(submitted)


def test_only_current_click_receipt_can_claim_reused_research():
    receipt = {'status':'REUSED','run_id':'study','requested_at':T0.isoformat(),
               'original_requested_at':(T0-timedelta(days=1)).isoformat(),'as_of':'2026-08-12'}
    assert '未重新搜索' in research_reuse_message(receipt,'study',T0)
    assert research_reuse_message(receipt,'other',T0) is None
    assert research_reuse_message(receipt,'study',T0+timedelta(seconds=1)) is None
    assert research_reuse_message(receipt,'study',T0+timedelta(days=1)) is None
