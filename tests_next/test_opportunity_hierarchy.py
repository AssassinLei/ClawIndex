import copy

import pytest

from clawindex_next.industry_hierarchy import enrich_rows, parent_label
from clawindex_next.radar import latest, publish, view
from clawindex_next.radar_notifications import deliver
from clawindex_next.webhooks import message_body, opportunity_payload
from conftest import FixtureCalendar
from test_evolution import T0
from test_radar import public_item, radar_seed, report_for
from test_radar_notifications_tracking import notices
from test_ui_redesign import login, button
from test_webhooks import URL


@pytest.mark.parametrize('parent_id',['110000','801010.SI'])
def test_parent_matches_industry_identifier_or_index_identifier(parent_id):
    directory=[public_item('801010.SI','一级行业甲','L1',industry='110000')]
    rows=[{'code':'801011.SI','level':'L2','parent':parent_id}]
    row=enrich_rows(rows,directory)[0]
    assert row['parent_index']=={'code':'801010.SI','name':'一级行业甲'}
    assert '一级行业甲（801010.SI）' in parent_label(row)
    assert 'parent_index' not in rows[0]


def test_unknown_or_ambiguous_parent_is_not_guessed():
    row={'code':'801011.SI','level':'L2','parent':'110000'}
    directory=[public_item('801010.SI','一级甲','L1',industry='110000'),
               public_item('801020.SI','一级乙','L1',industry='110000')]
    result=enrich_rows([row],directory)[0]
    assert result['parent_index'] is None and '暂未确认' in parent_label(result)
    result=enrich_rows([{**row,'parent':'999999'}],directory)[0]
    assert result['parent_index'] is None
    assert not parent_label({'code':'801010.SI','level':'L1'})


def test_non_l1_or_other_classification_version_cannot_be_parent():
    item=public_item('801010.SI','错误父级','L2',industry='110000')
    row={'code':'801011.SI','level':'L2','parent':'110000'}
    assert enrich_rows([row],[item])[0]['parent_index'] is None
    item['metadata'].update(level='L1',source_version='SW2014')
    assert enrich_rows([row],[item])[0]['parent_index'] is None


def test_new_snapshot_captures_parent_and_legacy_display_does_not_rewrite_evidence(radar_seed):
    db,a,b,items=radar_seed
    report=report_for(radar_seed)
    row=next(r for r in report['rows'] if r['level']=='L2')
    assert row['parent_index']=={'code':'801010.SI','name':'测试行业甲'}
    for row in report['rows']:row.pop('parent_index')
    publish(db,report,T0)
    before=copy.deepcopy(latest(db))
    result=view(db,a.user['id'],a.watchlist(),FixtureCalendar(),T0)
    child=next(r for r in result['display_rows'] if r['level']=='L2')
    assert child['parent_index']['name']=='测试行业甲'
    assert latest(db)==before and result['report']==before


def test_captured_parent_name_is_not_overwritten_by_current_directory():
    row={'code':'801011.SI','level':'L2','parent':'110000','parent_index':{'code':'801010.SI','name':'证据时的一级行业'}}
    result=enrich_rows([row],[public_item('801010.SI','后来改名','L1',industry='110000')])[0]
    assert result['parent_index']['name']=='证据时的一级行业'


def test_only_enabled_webhook_receives_l2_opportunity_with_parent(radar_seed):
    db,a,b,items=radar_seed
    enabled=a.set_channel(URL,'机会开启',mode='BUY_ONLY',radar_mode='ALL')
    disabled=a.set_channel(URL+'2','机会关闭',mode='BUY_ONLY')
    other=b.set_channel(URL+'3','其他账号',mode='ALL')
    for item in items:
        if item['metadata']['level']=='L1':a.radar_preferences(ignore=item['code'])
    publish(db,report_for(radar_seed),T0)
    queue=notices(db)
    assert queue and {r['channel_id'] for r in queue}=={enabled}
    assert a.radar_channel_mode(disabled)==b.radar_channel_mode(other)=='OFF'
    sent=[]
    while deliver(db,lambda url,payload:sent.append((url,payload)),FixtureCalendar(),T0):pass
    assert len(sent)==1
    payload=sent[0][1]
    assert payload['radar_level']=='L2' and payload['radar_parent']['name']=='测试行业甲'
    card=message_body(payload)
    assert '所属一级行业：测试行业甲（801010.SI）' in card['card']['elements'][0]['text']['content']
    assert not db.rows('SELECT * FROM outbox')


def test_disabling_one_opportunity_channel_cancels_only_its_pending_notices(radar_seed):
    db,a,b,*_=radar_seed
    first=a.set_channel(URL,'第一个',mode='ALL',radar_mode='ALL')
    second=a.set_channel(URL+'2','第二个',mode='ALL',radar_mode='ALL')
    publish(db,report_for(radar_seed),T0)
    a.set_radar_channel(first,'OFF')
    assert all(r['status']=='CANCELLED' for r in notices(db) if r['channel_id']==first)
    assert all(r['status']=='PENDING' for r in notices(db) if r['channel_id']==second)
    with pytest.raises(PermissionError):b.set_radar_channel(second,'OFF')


def test_invalid_opportunity_mode_does_not_create_half_configured_webhook(radar_seed):
    db,a,*_=radar_seed
    with pytest.raises(ValueError,match='机会提醒方式无效'):
        a.set_channel(URL,'无效',mode='ALL',radar_mode='INVALID')
    assert not a.channels()


def test_unknown_parent_is_explicit_in_card_and_l1_card_does_not_get_extra_line():
    card=message_body(opportunity_payload({'name':'二级指数','level':'L2','parent_index':None},'READY'))
    assert '所属一级行业：暂未确认' in card['card']['elements'][0]['text']['content']
    card=message_body(opportunity_payload({'name':'一级指数','level':'L1'},'WATCH'))
    assert '所属一级行业' not in card['card']['elements'][0]['text']['content']
    with pytest.raises(ValueError,match='一级行业资料无效'):
        message_body({'radar_name':'测试','radar_state':'READY','radar_level':'L2','radar_parent':'invalid'})


def test_webhook_page_exposes_per_channel_switch_and_saves_it(radar_seed,monkeypatch):
    db,a,b,*_=radar_seed
    first=a.set_channel(URL,'第一条',mode='BUY_ONLY')
    second=a.set_channel(URL+'2','第二条',mode='BUY_ONLY')
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('通知与运行').run()
    next(x for x in app.checkbox if x.key=='radar_on_'+str(first)).check().run()
    assert not app.exception and a.radar_channel_mode(first)=='ALL'
    assert a.radar_channel_mode(second)=='OFF'
    next(x for x in app.selectbox if x.key=='radar_scope_'+str(first)).set_value('READY').run()
    assert a.radar_channel_mode(first)=='READY'
    next(x for x in app.checkbox if x.key=='radar_on_'+str(first)).uncheck().run()
    assert not app.exception and a.radar_channel_mode(first)=='OFF'


def test_discovery_page_displays_l2_parent_on_card_and_in_full_table(radar_seed,monkeypatch):
    db,a,b,items=radar_seed
    for item in items:
        if item['metadata']['level']=='L1':a.radar_preferences(ignore=item['code'])
    publish(db,report_for(radar_seed),T0)
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('发现机会').run()
    assert not app.exception
    assert any('所属一级行业：测试行业甲（801010.SI）' in x.value for x in app.caption)
    tables=[x.value for x in app.dataframe if '所属一级行业' in x.value.columns]
    assert tables and '测试行业甲（801010.SI）' in set(tables[0]['所属一级行业'])


def test_add_webhook_form_can_save_opportunity_preference(radar_seed,monkeypatch):
    db,a,b,*_=radar_seed
    app=login(db,monkeypatch)
    app.sidebar.radio[0].set_value('通知与运行').run()
    next(x for x in app.text_input if x.label=='飞书机器人通知地址').set_value(URL)
    next(x for x in app.text_input if x.label=='渠道名称').set_value('新增机会提醒')
    next(x for x in app.selectbox if x.label=='新Webhook推送方式').set_value('BUY_ONLY')
    next(x for x in app.checkbox if x.label=='新Webhook开启机会推送').check()
    next(x for x in app.selectbox if x.label=='新Webhook机会推送范围').set_value('READY')
    button(app,'保存Webhook').click().run()
    assert not app.exception
    assert a.radar_channel_mode(a.channels()[0]['id'])=='READY'
