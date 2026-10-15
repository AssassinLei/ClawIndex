from datetime import datetime,timezone

import pandas as pd
import pytest

from clawindex_next.adapters import ProviderError,TushareAdapter,send_feishu
from clawindex_next.calendar import MarketCalendar,grace_for
from conftest import FixtureCalendar


def test_country_holidays_and_not_yet_closed_not_local_today():
    cal = MarketCalendar()
    assert "2026-10-01" not in cal.sessions("XSHG","2026-09-29","2026-10-09")
    assert "2026-10-02" in cal.sessions("XHKG","2026-09-29","2026-10-09")
    instant = datetime(2026,10,12,12,tzinfo=timezone.utc)
    assert cal.expected("XSHG",instant) == "2026-10-12"
    assert cal.expected("XNYS",instant) == "2026-10-09"


def test_dst_and_half_day_close():
    cal = MarketCalendar()
    assert cal.published_at("XNYS","2026-03-06",0).hour == 21
    assert cal.published_at("XNYS","2026-03-09",0).hour == 20
    assert cal.published_at("XNYS","2026-11-27",0).hour == 18


def test_sw_vendor_release_not_before_1830():
    cal = MarketCalendar()
    grace = grace_for({"code":"801737.SI"})
    assert cal.expected("XSHG",datetime(2026,10,12,10,tzinfo=timezone.utc),grace) == "2026-10-09"
    assert cal.expected("XSHG",datetime(2026,10,12,11,tzinfo=timezone.utc),grace) == "2026-10-12"


def test_calendar_range_can_begin_and_end_on_year_boundary_holidays():
    cal = MarketCalendar()
    assert cal.sessions("XHKG","2025-01-01","2025-01-03") == ["2025-01-02","2025-01-03"]
    assert cal.sessions("XHKG","2025-01-01","2025-01-01") == []
    assert "2023-12-31" not in cal.sessions("XNYS","2023-12-27","2023-12-31")


def test_endpoint_units_retry_permission_and_missing_international_amount():
    class Client:
        calls=[]
        def sw_daily(self,**kwargs):
            self.calls.append(("sw_daily",kwargs))
            return pd.DataFrame([{"trade_date":"20260812","open":100,"close":100,"high":101,"low":99,"amount":2.5}])
        def index_global(self,**kwargs):
            self.calls.append(("index_global",kwargs))
            return pd.DataFrame([{"trade_date":"20260812","open":100,"close":100,"high":101,"low":99}])
    client = Client()
    provider = TushareAdapter(client,FixtureCalendar(),pause=lambda _:None,minimum_interval=0)
    sw = provider.fetch({"code":"850531.SI","asset_kind":"EQUITY_SECTOR_INDEX","market":"CN"},"2026-08-01","2026-08-12")
    assert sw[0]["amount"] == 25000 and client.calls[0][1]["ts_code"] == "850531.SI"
    glob = provider.fetch({"code":"IXIC","asset_kind":"EQUITY_INDEX","market":"US"},"2026-08-01","2026-08-12")
    assert glob[0]["amount"] is None
    class Forbidden:
        def index_global(self,**kwargs): raise RuntimeError("没有权限 token=SECRET")
    with pytest.raises(ProviderError,match="NO_PERMISSION") as e:
        TushareAdapter(Forbidden(),pause=lambda _:None,minimum_interval=0).fetch({"code":"IXIC","asset_kind":"EQUITY_INDEX","market":"US"},"2026-08-01","2026-08-12")
    assert "SECRET" not in str(e.value)


def test_empty_data_is_delayed_not_closed(seeded):
    db,a,b,aid,baid = seeded
    with db.connect(True) as c:
        c.execute("DELETE FROM observations WHERE trade_date='2026-08-12'")
    class Empty:
        def index_global(self,**kwargs): return pd.DataFrame()
    with pytest.raises(ProviderError,match="DELAYED"):
        TushareAdapter(Empty(),FixtureCalendar(),pause=lambda _:None,minimum_interval=0).sync(db,"HKTECH")


def test_feishu_http_success_business_failure():
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"code":19001,"msg":"failure"}
    with pytest.raises(ProviderError,match="BUSINESS_ERROR"):
        send_feishu("https://example.invalid",{"text":"test"},lambda *a,**k:Response())


def test_provider_retry_is_bounded():
    class Broken:
        count=0
        def index_global(self,**kwargs):
            self.count+=1
            raise ConnectionError("secret internals")
    client = Broken()
    with pytest.raises(ProviderError,match="FAILED"):
        TushareAdapter(client,pause=lambda _:None,minimum_interval=0).fetch({"code":"IXIC","asset_kind":"EQUITY_INDEX","market":"US"},"2026-08-01","2026-08-12")
    assert client.count == 3
