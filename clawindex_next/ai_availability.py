"""Persist bounded provider pauses across web/worker restarts; store no secrets."""
import json
import os
from datetime import datetime, timedelta, timezone

from .storage import canonical, digest


PAUSE_SECONDS = {'INSUFFICIENT_BALANCE':21600,'AUTHENTICATION':21600,'PERMISSION':21600,
                 'MODEL_NOT_FOUND':1800,'BAD_REQUEST':1800,'INVALID_CONFIG':1800,
                 'NOT_CONFIGURED':1800,'RATE_LIMIT':60}


def config_key(model):
    # A changed key or request configuration must not inherit the old pause.
    config = {k:os.environ.get(k,'') for k in ('VNEXT_LLM_API_KEY','VNEXT_LLM_BASE_URL',
        'VNEXT_LLM_MODEL','VNEXT_LLM_TIMEOUT_SECONDS','VNEXT_LLM_MAX_TOKENS','VNEXT_LLM_THINKING')}
    return 'ai_pause:'+digest({'model':model,'config':config})


def paused(store, model, instant=None):
    instant = instant or datetime.now(timezone.utc)
    rows = store.rows('SELECT value FROM settings WHERE key=?',(config_key(model),))
    if not rows:
        return None
    try:
        value = json.loads(rows[0]['value'])
        if (value.get('code') in PAUSE_SECONDS and isinstance(value.get('message'),str)
                and datetime.fromisoformat(value['retry_at'])>instant):
            return {**value,'automatic_paused':True}
    except (KeyError,TypeError,ValueError):
        pass
    return None


def record_failure(store, model, diagnostic, instant=None):
    seconds = PAUSE_SECONDS.get(diagnostic['code'])
    if not seconds:
        return
    instant = instant or datetime.now(timezone.utc)
    value = {'code':diagnostic['code'],'message':diagnostic['message'],
             'started_at':instant.isoformat(),'retry_at':(instant+timedelta(seconds=seconds)).isoformat()}
    with store.connect(True) as c:
        c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  (config_key(model),canonical(value)))


def record_success(store, model, started_at):
    with store.connect(True) as c:
        # An older success must not clear a newer failure from another worker.
        c.execute("DELETE FROM settings WHERE key=? AND json_extract(value,'$.started_at')<=?",
                  (config_key(model),started_at.isoformat()))
