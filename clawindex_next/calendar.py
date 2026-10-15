"""Market sessions, including holidays, half days and US daylight saving time."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pandas as pd


def grace_for(item):
    # sw_daily documents 18:30 China time (15:00 close + 210 minutes).
    # Other endpoints do not promise a publication time: this is an explicit estimate.
    return 210 if item["code"].endswith(".SI") else 150


@lru_cache(maxsize=24)
def _calendar(name, start, end):
    import exchange_calendars as xc
    return xc.get_calendar(name, start=start, end=end)


class MarketCalendar:
    version = "exchange_calendars"

    def sessions(self, name, start, end):
        cal = _calendar(name, start[:4]+"-01-01", end[:4]+"-12-31")
        # Jan 1 / Dec 31 may be holidays outside the first/last *session* of
        # the requested calendar years, even though the dates are in range.
        first = max(start,cal.first_session.date().isoformat())
        last = min(end,cal.last_session.date().isoformat())
        if first>last:
            return []
        return [x.date().isoformat() for x in cal.sessions_in_range(first,last)]

    def published_at(self, name, day, grace_minutes=150):
        cal = _calendar(name, day[:4]+"-01-01", day[:4]+"-12-31")
        return cal.session_close(pd.Timestamp(day)).to_pydatetime() + timedelta(minutes=grace_minutes)

    def opened_at(self, name, day):
        cal = _calendar(name,day[:4]+'-01-01',day[:4]+'-12-31')
        return cal.session_open(pd.Timestamp(day)).to_pydatetime()

    def expected(self, name, instant=None, grace_minutes=150):
        instant = instant or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("检查时间必须带时区")
        end = instant.date().isoformat()
        start = (instant - timedelta(days=20)).date().isoformat()
        for day in reversed(self.sessions(name,start,end)):
            if self.published_at(name,day,grace_minutes) <= instant:
                return day
        raise ValueError("日历范围内没有已经发布的时段")
