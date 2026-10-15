import json
from datetime import timedelta

import pandas as pd
import pytest

from clawindex_next.catalog import refresh, KEY
from clawindex_next.storage import canonical, digest
from test_evolution import T0
from test_ui_redesign import login, ready, button


def archive(db):
    with db.connect(True) as c:
        for code,name,level in [('801730.SI','电力设备','L1'),('801733.SI','电池','L2'),('801734.SI','机器人','L3')]:
            # The migration archive records shared public classifications only.
            payload = {'index_code':code,'industry_name':name,'level':level,'src':'SW2021'}
            c.execute("INSERT INTO legacy_archive(table_name,legacy_user,row_hash,payload) VALUES('industry_list',NULL,?,?)",(digest(payload),canonical(payload)))


def test_search_uses_names_codes_and_archive_without_network(seeded):
    db,a,b,*_ = seeded
    archive(db)
    assert a.search_indices('电池')[0]['code']=='801733.SI'
    assert a.search_indices('801734.si')[0]['name']=='机器人'
    assert a.search_indices('hktech')[0]['name']=='恒生科技'
    assert not a.search_indices('does-not-exist')
    assert len({r['code'] for r in a.search_indices()})==len(a.search_indices())
    cid = a.add_watch('801733.SI',provider=object())
    assert a.add_watch('801733.SI',provider=object())==cid
    assert b.watchlist()[0]['code']=='HKTECH'
    assert len(db.rows('SELECT * FROM jobs WHERE assignment_id=?',(cid,)))==1


def test_directory_refresh_is_cached_and_keeps_last_good_copy(seeded):
    db,a,*_ = seeded
    calls = []
    class Provider:
        def _call(self,endpoint,**kwargs):
            calls.append(kwargs['level'])
            return pd.DataFrame([{'index_code':'801733.SI','industry_name':'电池'}])
    refresh(db,Provider(),T0)
    refresh(db,Provider(),T0+timedelta(days=1))
    assert calls==['L1','L2','L3'] and a.search_indices('电池')
    class Failure:
        def _call(self,*args,**kwargs):
            raise RuntimeError('sensitive-provider-error')
    refresh(db,Failure(),T0+timedelta(days=8))
    assert a.search_indices('电池')
    assert 'sensitive' not in json.dumps(a.health())
    assert db.rows('SELECT value FROM settings WHERE key=?',(KEY,))


def test_search_ui_changes_results_and_adds_selected_index(seeded,monkeypatch):
    db,a,*_ = seeded
    archive(db)
    app = login(db,monkeypatch)
    app.sidebar.radio[0].set_value('监控与数据').run()
    next(x for x in app.text_input if x.label=='搜索指数').set_value('电池').run()
    result = next(x for x in app.selectbox if x.label=='搜索结果')
    assert result.options==['电池 · 801733.SI']
    result.select_index(0).run()
    button(app,'添加监控').click().run()
    assert not app.exception and any(w['code']=='801733.SI' for w in a.watchlist())
    next(x for x in app.text_input if x.label=='搜索指数').set_value('机器人').run()
    assert next(x for x in app.selectbox if x.label=='搜索结果').options==['机器人 · 801734.SI']


@pytest.mark.parametrize('reason',['我相信后面会涨','再观望下'])
def test_reason_choice_is_saved_without_requiring_free_text(seeded,monkeypatch,reason):
    db,a,aid = ready(seeded,monkeypatch)
    app = login(db,monkeypatch)
    assert not any(x.label=='其他说明' for x in app.text_input)
    next(x for x in app.selectbox if x.label=='处理结果').set_value('DEFERRED').run()
    next(x for x in app.selectbox if x.label=='执行说明或暂缓原因').set_value(reason).run()
    button(app,'保存处理结果').click().run()
    assert not app.exception
    row = db.rows('SELECT * FROM execution_confirmations')[0]
    assert json.loads(row['payload'])['note']==reason
    assert json.loads(row['payload'])['outcome']=='DEFERRED'
    assert a.position('HKTECH')['basis']=='DEFAULT_EMPTY'
    assert any('卡片保留在原位' in x.value for x in app.success)


def test_other_reason_requires_text_and_saves_it(seeded,monkeypatch):
    db,a,aid = ready(seeded,monkeypatch)
    app = login(db,monkeypatch)
    next(x for x in app.selectbox if x.label=='执行说明或暂缓原因').set_value('其他').run()
    assert any(x.label=='其他说明' for x in app.text_input)
    button(app,'保存处理结果').click().run()
    assert not app.exception and any('请填写说明' in x.value for x in app.error)
    assert not db.rows('SELECT * FROM execution_confirmations')
    next(x for x in app.text_input if x.label=='其他说明').set_value('今天已在基金平台处理')
    button(app,'保存处理结果').click().run()
    assert not app.exception
    assert json.loads(db.rows('SELECT payload FROM execution_confirmations')[0]['payload'])['note']=='今天已在基金平台处理'
