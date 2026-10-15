import json

import pytest

from clawindex_next.ui_evolution import evolution_step
from clawindex_next.validation import DEFAULT_POLICY
from test_evolution import freeze
from test_ui_redesign import login


@pytest.mark.parametrize('status,expected',[
    ('PENDING','等待后台研究'),('RUNNING','正在比较新规则'),
    ('FORWARD_SHADOW','新规则正在模拟试用'),('REJECTED','本轮未找到合格改进'),
    ('ACTIVE','新规则已投入使用'),('FAILED','本轮研究未完成'),
])
def test_plain_language_stage_guidance(status,expected):
    latest={'status':'FROZEN','candidate_status':status,'payload':{}}
    title,next_step=evolution_step(latest,DEFAULT_POLICY)
    assert title==expected and next_step


def test_eligible_candidate_does_not_imply_automatic_application():
    latest={'status':'FROZEN','candidate_status':'ELIGIBLE','payload':{}}
    assert '未授权' in evolution_step(latest,{**DEFAULT_POLICY,'auto_promote':False})[1]
    assert '更换间隔' in evolution_step(latest,{**DEFAULT_POLICY,'auto_promote':True})[1]


def test_paused_research_does_not_claim_to_be_automatically_running():
    assert evolution_step(None,{**DEFAULT_POLICY,'enabled':False})[0]=='自动研究已暂停'


def test_unchanged_save_preserves_policy_candidate_and_research(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid=freeze(seeded,monkeypatch)
    before=db.rows('SELECT * FROM evolution_candidates')
    before_runs=db.rows('SELECT * FROM evolution_runs')
    assert a.set_evolution('HKTECH',{})==pid
    assert db.rows('SELECT * FROM evolution_candidates')==before
    assert db.rows('SELECT * FROM evolution_runs')==before_runs
    assert len(db.rows('SELECT * FROM evolution_policies'))==1
    # A real change still invalidates candidates tested under old conditions.
    assert a.set_evolution('HKTECH',{'research_days':31})!=pid
    assert db.rows('SELECT status FROM evolution_candidates')[0]['status']=='QUARANTINED'


def test_page_gives_daily_usage_and_preserves_progress_on_unchanged_save(seeded,monkeypatch):
    db,a,b,aid,baid=seeded
    pid=a.set_evolution('HKTECH',{})
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('策略与研究').run()
    assert not app.exception
    assert any('AI不决定如何改规则' in x.value for x in app.caption)
    assert any('你现在要做的' in x.value for x in app.info)
    assert any(x.label=='我该怎么用？' for x in app.expander)
    next(x for x in app.checkbox if x.label.startswith('我确认上述范围')).check()
    next(x for x in app.button if x.label=='保存自动进化配置').click().run()
    assert not app.exception
    assert a.evolution_policy('HKTECH')['id']==pid
    assert any('进度已保留' in x.value for x in app.success)


def test_old_policy_results_are_not_presented_as_current_work(seeded,monkeypatch):
    db,a,b,ev,calendar,rid,pid=freeze(seeded,monkeypatch)
    a.set_evolution('HKTECH',{'research_days':31})
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('策略与研究').run()
    assert not app.exception
    assert any('等待开始研究' in x.value for x in app.markdown)
    assert not any('新规则正在模拟试用' in x.value for x in app.markdown)
    assert any(x.label=='此前设置下的研究记录' for x in app.expander)
