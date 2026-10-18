"""Keep the archive reader supervised while serving the authenticated API."""
import os, signal, subprocess, sys, time
from pathlib import Path

os.umask(0o077)
data=Path('/data'); data.chmod(0o700)
env=os.environ | {'TRADE_FEED_PRIVATE':str(data),'TRADE_FEED_DB':str(data/'messages.sqlite3'),'TRADE_FEED_READER_STATUS':str(data/'feed-reader-status.json')}
api=subprocess.Popen([sys.executable,'cloud_api.py'],env=env)
reader=None
stopping=False
def stop(signum, frame):
    global stopping
    stopping=True
signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
try:
    while not stopping and api.poll() is None:
        if (data/'feed-telegram.session').exists() and (data/'feed-telegram.json').exists() and (reader is None or reader.poll() is not None):
            reader=subprocess.Popen([sys.executable,'live_reader.py','worker'],env=env)
        time.sleep(5)
finally:
    for child in (reader,api):
        if child and child.poll() is None: child.terminate()
    for child in (reader,api):
        if child and child.poll() is None:
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
if not stopping: sys.exit(api.returncode or 1)
