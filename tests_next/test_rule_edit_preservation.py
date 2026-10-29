"""Editing one visible rule field must preserve unrelated saved assumptions."""
import json

from clawindex_next.engine import DEFAULT_SPEC
from test_ui_redesign import button,login


def test_manual_rule_edit_preserves_unedited_assumptions_and_catchup(seeded,monkeypatch):
    db,a,*_=seeded
    spec={**DEFAULT_SPEC,'valuation_window':500,'annual_sessions':240}
    a.assign('HKTECH',spec,catchup=True)
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('策略与研究').run()
    catchup=next(x for x in app.checkbox if x.label=='空仓时允许追随当前仍有效的入场条件（非新信号）')
    assert catchup.value
    next(x for x in app.number_input if x.label=='均线交易日数').set_value(90).run()
    button(app,'发布新规则版本并应用').click().run()
    assert not app.exception
    saved=a.watchlist()[0]
    assert json.loads(saved['spec'])=={**spec,'ma':90}
    assert saved['catchup']==1
