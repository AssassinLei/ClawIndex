"""Exec one foreground role. Docker owns restart and SIGTERM delivery."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    role = sys.argv[1] if len(sys.argv)>1 else 'web'
    db = os.environ.get('VNEXT_DB_PATH','/data/quant_system_next.db')
    if role in ('web','worker'):
        from clawindex_next.cloud import preflight
        preflight()
    if role == 'web':
        command = [sys.executable,'-m','streamlit','run',str(ROOT/'app_next.py'),
                   '--server.address=0.0.0.0','--server.port=8502','--server.headless=true','--browser.gatherUsageStats=false']
    elif role == 'worker':
        command = [sys.executable,'-m','clawindex_next','--db',db,'worker']
    else:
        command = [sys.executable,'-m','clawindex_next','--db',db,*sys.argv[1:]]
    os.execv(sys.executable,command)


if __name__ == '__main__':
    main()
