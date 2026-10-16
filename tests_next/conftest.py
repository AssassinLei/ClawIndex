import json
from datetime import datetime,timezone

import pandas as pd
import pytest

from clawindex_next.auth import Auth
from clawindex_next.engine import DEFAULT_SPEC, validate_spec
from clawindex_next.migration import insert_instrument, instrument
from clawindex_next.service import UserService
from clawindex_next.storage import Store, canonical, digest, now


@pytest.fixture(autouse=True)
def isolated_provider_credentials(monkeypatch):
    # UI dotenv loading must never import real local credentials into regression tests.
    # Tests that exercise adapters explicitly provide their own fake credentials/provider.
    monkeypatch.setenv('VNEXT_LLM_API_KEY', '')
    monkeypatch.setenv('TUSHARE_TOKEN', '')


class FixtureCalendar:
    def sessions(self, name, start, end):
        return [d.date().isoformat() for d in pd.bdate_range(start,end)]

    def expected(self, name, instant=None, grace_minutes=150):
        return "2026-08-12"

    def published_at(self, name, day, grace_minutes=150):
        return datetime.fromisoformat(day+"T10:30:00+00:00")

    def opened_at(self, name, day):
        return datetime.fromisoformat(day+('T13:30:00+00:00' if name=='XNYS' else 'T01:30:00+00:00'))


def price_rows(count=160, end="2026-08-12", prices=None):
    dates = [d.date().isoformat() for d in pd.bdate_range(end=end,periods=count)]
    prices = prices or [100+i*.2 for i in range(count)]
    rows = [{"date":d,"open":p,"high":p+1,"low":p-1,"close":p,"amount":1000.,"pe":20.,"pb":2.} for d,p in zip(dates,prices)]
    return dates,rows


@pytest.fixture
def seeded(tmp_path):
    store = Store.create(tmp_path/"next.db")
    auth = Auth(store)
    uid = auth.provision("alice",True)
    bid = auth.provision("bob")
    token = auth.login("alice")
    btoken = auth.login("bob")
    with store.connect(True) as c:
        c.execute("INSERT INTO settings VALUES('daily_sync_enabled','false')")
        insert_instrument(c,instrument("HKTECH","恒生科技"))
        c.execute("INSERT INTO strategy_versions VALUES(?,?,?)", (validate_spec(DEFAULT_SPEC),canonical(DEFAULT_SPEC),now()))
        dates,rows = price_rows()
        for r in rows:
            c.execute("INSERT INTO observations(code,trade_date,payload,source,fetched_at,available_at,availability_basis,row_hash) VALUES(?,?,?,?,?,?,?,?)", ("HKTECH",r["date"],canonical(r),"fixture",now(),None,"FIXTURE",digest(r)))
    a = UserService(store,token)
    b = UserService(store,btoken)
    aid = a.assign("HKTECH")
    baid = b.assign("HKTECH")
    return store,a,b,aid,baid
