"""Per-channel delivery windows, independent of inspection and server timezone."""
import json
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from croniter import croniter


TIMEZONE = 'Asia/Shanghai'
DEFAULT_CRON = '30 19 * * mon-fri'
GRACE_MINUTES = 5


def config_key(uid, cid):
    return f'webhook_schedule:{uid}:{cid}'


def validate_expression(expression):
    if not isinstance(expression,str) or len(expression)>120:
        raise ValueError('Cron需填写标准五字段：分 时 日 月 周')
    expression = ' '.join(expression.strip().split())
    return _validate_expression(expression)


@lru_cache(maxsize=256)
def _validate_expression(expression):
    if len(expression.split())!=5 or re.search(r'\b[hr]\b',expression,re.I):
        raise ValueError('Cron需填写固定的标准五字段：分 时 日 月 周，不支持秒、年份或随机时间')
    try:
        croniter(expression,datetime(2024,1,1,tzinfo=ZoneInfo(TIMEZONE)),max_years_between_matches=8).get_next(datetime)
    except (ValueError,KeyError,OverflowError) as exc:
        raise ValueError('Cron表达式无效或没有可执行日期，请检查分钟、小时、日期和星期') from None
    return expression


def normalize(config):
    if config is None:
        return {'enabled':False,'cron':DEFAULT_CRON,'timezone':TIMEZONE}
    if not isinstance(config,dict) or type(config.get('enabled')) is not bool:
        raise ValueError('发送时间配置无效')
    return {'enabled':config['enabled'],'cron':validate_expression(config.get('cron',DEFAULT_CRON)),'timezone':TIMEZONE}


def read(connection, uid, cid):
    row = connection.execute('SELECT value FROM settings WHERE key=?',(config_key(uid,cid),)).fetchone()
    try:
        return normalize(json.loads(row[0]) if row else None)
    except (ValueError,TypeError):
        # Corrupt schedules must fail closed, never silently become immediate delivery.
        return {'enabled':True,'cron':'','timezone':TIMEZONE,'error':'已保存的发送时间无效，请重新保存Cron配置；正式推送暂缓。'}


def next_times(config, instant=None, count=3):
    if not config['enabled'] or config.get('error'):
        return []
    instant = (instant or datetime.now(timezone.utc)).astimezone(ZoneInfo(TIMEZONE))
    iterator = croniter(validate_expression(config['cron']),instant,max_years_between_matches=8)
    return [iterator.get_next(datetime).isoformat() for _ in range(count)]


def allowed(connection, uid, cid, instant):
    config = read(connection,uid,cid)
    if config.get('error'):
        return False
    if not config['enabled']:
        return True
    local = instant.astimezone(ZoneInfo(TIMEZONE))
    # Include a match at the current minute while retaining normal five-field semantics.
    base = local.replace(second=0,microsecond=0)+timedelta(minutes=1)
    try:
        slot = croniter(config['cron'],base,max_years_between_matches=8).get_prev(datetime)
    except (ValueError,KeyError,OverflowError):
        return False
    return slot.date()==local.date() and timedelta(0)<=local-slot<timedelta(minutes=GRACE_MINUTES)
