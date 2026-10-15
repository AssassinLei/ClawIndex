import pytest
from streamlit.testing.v1 import AppTest

from clawindex_next.ui_components import explanation_title


@pytest.mark.parametrize('status,title,source',[
    ('MODEL_OK','AI生成的解读','内容来源：AI。'),
    ('TEMPLATE','程序生成的解读','内容来源：程序固定模板。'),
    ('FAILED_TEMPLATE','程序生成的解读（AI未成功）','内容来源：程序固定模板。'),
    ('UNRECOGNIZED','解读来源待确认','该记录的内容来源暂时无法确认。'),
])
def test_entire_explanation_has_authoritative_source_label(status,title,source):
    record={'status':status,'created_at':'2026-10-14T14:00:00+00:00',
            'payload':{'summary':'测试摘要','evidence_refs':[],'limitations':['测试局限'],
                       'counterarguments':['测试反面观点'],'scenarios':[]}}
    app=AppTest.from_string('from clawindex_next.ui_components import explanation_panel\nexplanation_panel('+repr(record)+')').run()
    assert not app.exception
    assert any(x.value=='#### '+title for x in app.markdown)
    assert any(x.value.startswith(source) for x in app.caption)
    assert any('买卖判断与参考金额由程序规则产生' in x.value for x in app.caption)
    if status!='MODEL_OK':
        assert not any(x.value=='#### AI生成的解读' for x in app.markdown)


def test_missing_explanation_is_not_labeled_as_ai_or_program():
    assert explanation_title(None)=='解读尚未生成'


def test_source_is_not_inferred_from_model_text():
    assert explanation_title({'status':'TEMPLATE','payload':{'summary':'我是AI生成的'}})=='程序生成的解读'
