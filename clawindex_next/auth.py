import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .storage import now


class Auth:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def username(value):
        if not isinstance(value,str):
            raise ValueError("请输入账号名")
        value = value.strip()
        if not value or len(value)>64 or any(ord(ch)<32 or ord(ch)==127 for ch in value):
            raise ValueError("账号名须为1至64个字符，不能包含换行或控制字符")
        return value

    def provision(self, username, admin=False):
        """Local CLI account/role management; login itself never grants admin."""
        username = self.username(username)
        with self.store.connect(True) as c:
            c.execute("INSERT INTO users(username,admin,created_at) VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET admin=excluded.admin", (username,int(admin),now()))
            uid = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        return uid

    def login(self, username):
        """Trusted friends-only account selection, not identity verification."""
        username = self.username(username)
        token = secrets.token_urlsafe(32)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
        with self.store.connect(True) as c:
            c.execute("INSERT OR IGNORE INTO users(username,created_at) VALUES(?,?)", (username,now()))
            uid = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
            c.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(),uid,expiry))
        return token

    def user(self, token, admin=False):
        rows = self.store.rows("SELECT u.id,u.username,u.admin,u.created_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (hashlib.sha256(token.encode()).hexdigest(),now()))
        if not rows or (admin and not rows[0]["admin"]):
            raise PermissionError("登录已过期或没有管理员权限")
        return rows[0]

    def logout(self, token):
        with self.store.connect(True) as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
