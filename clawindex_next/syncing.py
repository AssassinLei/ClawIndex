"""Shared, leased refresh and API pacing across users and worker processes."""
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from .adapters import ProviderError, TushareAdapter
from .calendar import grace_for
from .storage import canonical, now


def rate_limit(store, pause=time.sleep, seconds=2.1):
    instant = datetime.now(timezone.utc)
    with store.connect(True) as c:
        row = c.execute("SELECT value FROM settings WHERE key='api_next_allowed:tushare'").fetchone()
        due = max(instant, datetime.fromisoformat(row[0])) if row else instant
        c.execute("INSERT INTO settings VALUES('api_next_allowed:tushare',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ((due+timedelta(seconds=seconds)).isoformat(),))
    pause(max(0, (due-instant).total_seconds()))


class MarketSync:
    def __init__(self, store, calendar, provider=None):
        self.store, self.calendar, self.provider = store, calendar, provider

    def ensure_identity(self, code):
        item = self.store.rows('SELECT * FROM instruments WHERE code=?',(code,))[0]
        if not json.loads(item['metadata']).get('identity_verified'):
            if self.provider is None:
                self.provider = TushareAdapter(rate_limiter=lambda:rate_limit(self.store))
            if hasattr(self.provider,'ensure_identity'):
                item = self.provider.ensure_identity(self.store,code)
        return item

    def refresh(self, code, start=None, instant=None, reuse_seconds=300):
        instant = instant or datetime.now(timezone.utc)
        item = self.ensure_identity(code)
        expected = self.calendar.expected(item["calendar"],instant,grace_for(item))
        key, owner = "market_sync:"+code, uuid.uuid4().hex
        # Reuse only verified, fresh successful refreshes for the same session.
        with self.store.connect(True) as c:
            last = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            saved = json.loads(last[0]) if last else {}
            repair = c.execute("SELECT value FROM settings WHERE key=?",('history_repair:'+code,)).fetchone()
            repair_due = json.loads(repair[0]) if repair else {}
            latest = c.execute("SELECT MAX(trade_date) FROM observations WHERE code=? AND trade_date<=? AND (available_at IS NULL OR available_at<=?)", (code,expected,instant.isoformat())).fetchone()[0]
            repair_ready = bool(repair_due.get('pending') or repair_due.get('missing_sessions',0)>0) and repair_due.get('next_due','') <= instant.isoformat()
            if not start and not repair_ready and saved.get("expected") == expected and saved.get("status") == "OK" and latest and latest >= expected and instant-datetime.fromisoformat(saved["checked_at"]) < timedelta(seconds=reuse_seconds):
                return "已复用刚更新的共享行情"
            lease = c.execute("SELECT * FROM leases WHERE name=?", (key,)).fetchone()
            if lease and lease["expires_at"] > now():
                raise ProviderError("SYNC_BUSY: 同一指数正在更新，请稍候，任务将自动重试")
            c.execute("INSERT INTO leases VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at", (key,owner,(datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat()))
        stop = threading.Event()
        def renew():
            while not stop.wait(10):
                try:
                    with self.store.connect(True) as c:
                        c.execute("UPDATE leases SET expires_at=? WHERE name=? AND owner=?", ((datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat(),key,owner))
                except Exception:
                    return
        thread = threading.Thread(target=renew,daemon=True)
        thread.start()
        try:
            if self.provider is None:
                self.provider = TushareAdapter(rate_limiter=lambda:rate_limit(self.store))
            self.provider.sync(self.store,code,start=start,instant=instant)
            latest = self.store.rows("SELECT MAX(trade_date) d FROM observations WHERE code=? AND trade_date<=? AND (available_at IS NULL OR available_at<=?)", (code,expected,instant.isoformat()))[0]["d"]
            if not latest or latest < expected:
                raise ProviderError("DELAYED: 预期交易日的数据尚未发布，请稍后重试")
            with self.store.connect(True) as c:
                if not c.execute("SELECT name FROM leases WHERE name=? AND owner=?", (key,owner)).fetchone():
                    raise ProviderError("SYNC_BUSY: 更新租约已失效，请重试")
                c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,canonical({"status":"OK","expected":expected,"checked_at":instant.isoformat()})))
            return "行情已更新至 "+expected
        finally:
            stop.set()
            thread.join(timeout=2)
            with self.store.connect(True) as c:
                c.execute("DELETE FROM leases WHERE name=? AND owner=?", (key,owner))


def error_message(exc):
    from .explanations import ExplanationBusy
    if isinstance(exc,ExplanationBusy):
        return 'AI解读正在由其他任务生成；稍后自动读取结果'
    # Provider messages from this application are sanitized; never expose raw SDK exceptions.
    code = str(exc).split(":",1)[0] if isinstance(exc,ProviderError) else ""
    return {
        "NO_PERMISSION":"行情接口未配置或权限不足，请检查Tushare配置及会员接口权限",
        "NETWORK_FAILED":"行情接口网络暂不可达，后台将自动重试；请检查服务器网络",
        "LLM_NOT_CONFIGURED":"尚未配置AI密钥，已使用程序解读",
        "DELAYED":"行情服务尚未提供预期交易日数据，请稍后重试",
        "SYNC_BUSY":"同一指数正在更新，稍后自动重试",
        "UNSUPPORTED":"指数资料尚未核实，暂不能更新",
    }.get(code,"处理失败，请检查网络、行情完整性或配置；详细信息见运行记录")
