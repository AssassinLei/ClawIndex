from concurrent.futures import ThreadPoolExecutor

import pytest

from clawindex_next.auth import Auth
from clawindex_next.__main__ import main
from clawindex_next.storage import Store,now


@pytest.mark.parametrize("name",["","   ",None,"x"*65,"ali\nce","ali\tce","al\x00ice"])
def test_invalid_account_names_rejected(tmp_path,name):
    db = Store.create(tmp_path/"next.db")
    with pytest.raises(ValueError): Auth(db).login(name)
    assert not db.rows("SELECT id FROM users")


def test_new_account_created_once_and_spaces_trimmed(tmp_path):
    db = Store.create(tmp_path/"next.db")
    auth = Auth(db)
    with ThreadPoolExecutor(4) as pool:
        tokens = list(pool.map(lambda _:auth.login("  朋友甲  "),range(8)))
    assert len(db.rows("SELECT * FROM users")) == 1
    assert len(set(tokens)) == 8
    for token in tokens:
        user = auth.user(token)
        assert user["username"] == "朋友甲" and user["admin"] == 0
        assert "password_hash" not in user


def test_imported_and_former_password_accounts_both_accessible(seeded):
    db,a,b,aid,baid = seeded
    with db.connect(True) as c:
        # An intentionally invalid old hash proves username login never parses it.
        c.execute("UPDATE users SET password_hash='obsolete-hash' WHERE username='alice'")
    auth = Auth(db)
    alice = auth.user(auth.login("alice"))
    bob = auth.user(auth.login("bob"))
    assert alice["id"] == a.user["id"] and alice["admin"] == 1
    assert bob["id"] == b.user["id"] and bob["admin"] == 0
    assert len(db.rows("SELECT * FROM users")) == 2
    assert len(a.watchlist()) == 1


def test_logout_and_expiry_still_end_sessions(seeded):
    db,a,b,aid,baid = seeded
    auth = Auth(db)
    token = auth.login("alice")
    auth.logout(token)
    with pytest.raises(PermissionError): auth.user(token)
    token = auth.login("alice")
    with db.connect(True) as c:
        c.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
    with pytest.raises(PermissionError): auth.user(token)


def test_cli_account_role_without_password_or_input_prompt(tmp_path,capsys):
    path = tmp_path/"next.db"
    Store.create(path)
    main(["--db",str(path),"create-user","朋友甲","--admin"])
    db = Store(path)
    auth = Auth(db)
    assert auth.user(auth.login("朋友甲"),admin=True)["admin"] == 1
    assert "账号权限已保存" in capsys.readouterr().out
