"""Probe the configured AI with synthetic data; never open any database."""
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from dotenv import load_dotenv
from clawindex_next.adapters import llm_explanation
from clawindex_next.explanations import validate_explanation
from clawindex_next.llm_diagnostics import failure_diagnostic


def main():
    load_dotenv(ROOT/'.env.next')
    payload = {
        'decision':{'status':'AVAILABLE','action':'WAIT','reasons':[]},
        'signal':{'state_label':'WAIT','data_status':'OK','rule_trace':['合成样本：入场条件尚未确认']},
        'features':{'close':100.0,'ma120':101.0,'above':0,'below':3,'ma120_slope':-0.001},
    }
    report = {'sample':'SYNTHETIC_NO_ACCOUNT_DATA',
              'endpoint_host':urlsplit(os.environ.get('VNEXT_LLM_BASE_URL','https://api.deepseek.com')).hostname,
              'model':os.environ.get('VNEXT_LLM_MODEL','deepseek-chat'),
              'key_configured':bool(os.environ.get('VNEXT_LLM_API_KEY'))}
    start,phase = time.monotonic(),'provider'
    try:
        raw = llm_explanation(payload)
        phase = 'validation'
        validate_explanation(raw,payload['features'])
        report.update(status='MODEL_OK',response_characters=len(raw))
    except Exception as exc:
        report.update(status='FAILED',diagnostic=failure_diagnostic(exc,phase))
    report['elapsed_seconds'] = round(time.monotonic()-start,2)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='MODEL_OK' else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
