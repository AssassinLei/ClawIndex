from pathlib import Path

from streamlit.testing.v1 import AppTest


def button(app,label):
    return next(b for b in app.button if b.label == label)


def test_ui_login_all_pages_manual_unknown_and_user_history(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    from clawindex_next.service import Evaluator
    from conftest import FixtureCalendar
    Evaluator(db,FixtureCalendar()).evaluate(aid)
    monkeypatch.setenv("VNEXT_DB_PATH",str(db.path))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app_next.py"),default_timeout=30).run()
    assert not app.exception
    assert len(app.text_input) == 1 and app.text_input[0].label == "账号名"
    app.text_input[0].set_value("alice")
    button(app,"登录").click().run()
    assert not app.exception
    for page in ("监控与数据","持仓与预算","策略与研究","历史与反馈","通知与运行","健康","信号与待执行"):
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, (page,app.exception)
    button(app,"提交全部巡检").click().run()
    assert len(db.rows("SELECT * FROM jobs")) == 1
    app.sidebar.radio[0].set_value("持仓与预算").run()
    next(s for s in app.selectbox if s.label == "确认当前持仓状态").set_value("HELD")
    button(app,"确认并追加持仓报告").click().run()
    assert not app.exception
    assert a.position("HKTECH")["status"] == "HELD"
    assert b.position("HKTECH")['basis'] == 'DEFAULT_EMPTY'


def test_ui_missing_db_does_not_create_or_open_legacy(tmp_path,monkeypatch):
    path = tmp_path/"does-not-exist.db"
    monkeypatch.setenv("VNEXT_DB_PATH",str(path))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app_next.py")).run()
    assert app.error and not path.exists() and not app.exception


def test_ui_first_use_creates_account_without_password(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    monkeypatch.setenv("VNEXT_DB_PATH",str(db.path))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app_next.py")).run()
    assert len(app.text_input) == 1
    app.text_input[0].set_value("朋友甲")
    button(app,"登录").click().run()
    assert not app.exception
    assert db.rows("SELECT username,admin FROM users WHERE username='朋友甲'")[0] == {"username":"朋友甲","admin":0}
    assert any("当前用户：朋友甲" in x.value for x in app.sidebar.markdown)
    button(app,"退出登录").click().run()
    assert len(app.text_input) == 1 and app.text_input[0].label == "账号名"


def test_health_explains_pending_and_manual_rescue(seeded,monkeypatch):
    db,a,b,aid,baid = seeded
    monkeypatch.setenv("VNEXT_DB_PATH",str(db.path))
    a.submit(aid)
    monkeypatch.setattr('clawindex_next.service.Evaluator.evaluate',lambda *args:None)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app_next.py")).run()
    app.text_input[0].set_value("alice")
    button(app,"登录").click().run()
    app.sidebar.radio[0].set_value("健康").run()
    assert any("Pending＝等待执行" in x.value for x in app.warning)
    button(app,"立即处理我的待执行巡检").click().run()
    assert not app.exception
    assert db.rows("SELECT status FROM jobs")[0]["status"] == "SUCCEEDED"
