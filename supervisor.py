"""Run exactly one Telegram consumer alongside the authenticated API."""
import json, os, signal, subprocess, sys, time
from pathlib import Path

os.umask(0o077)
data=Path('/data'); data.chmod(0o700)
paper=os.environ.get('FLY_PAPER_EXECUTION')=='1'
env=os.environ | {'TRADE_FEED_PRIVATE':str(data),'TRADE_FEED_DB':str(data/'messages.sqlite3'),'TRADE_FEED_READER_STATUS':str(data/'feed-reader-status.json'),'PAPER_PRIVATE':str(data),'FLY_PAPER':'1'}
if paper:
    if not all((data/name).exists() for name in ('feed-telegram.session','feed-telegram.json','execution-state.json')):
        raise RuntimeError('Paper handoff files are missing; refusing to start.')
    key=os.environ.get('ALPACA_PAPER_CONFIG')
    if not key: raise RuntimeError('Paper credentials are missing; refusing to start.')
    for name, contents in [('alpaca-paper.json',key),('telegram.json',(data/'feed-telegram.json').read_text())]:
        p=data/name; p.write_text(contents); p.chmod(0o600)
api=subprocess.Popen([sys.executable,'cloud_api.py'],env=env)
consumer=None
stopping=False
def stop(signum, frame):
    global stopping
    stopping=True
signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
try:
    while not stopping and api.poll() is None:
        ready=(data/'feed-telegram.session').exists() and (data/'feed-telegram.json').exists()
        if ready and (consumer is None or consumer.poll() is not None):
            if paper:
                consumer=subprocess.Popen([sys.executable,'service.py','worker'],cwd='worker',env=env)
            else:
                consumer=subprocess.Popen([sys.executable,'live_reader.py','worker'],env=env)
        time.sleep(5)
finally:
    for child in (consumer,api):
        if child and child.poll() is None: child.terminate()
    for child in (consumer,api):
        if child and child.poll() is None:
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
if not stopping: sys.exit(api.returncode or 1)
