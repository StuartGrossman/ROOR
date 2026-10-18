"""Start/stop this Mac's protected feed and HTTPS tunnel as one service."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PRIVATE = ROOT/'work/private'
LOCK = PRIVATE/'feed-share.lock'
STOP = PRIVATE/'feed-share-stop'
STATUS = PRIVATE/'feed-share-status.json'
TUNNEL_LOG = PRIVATE/'feed-tunnel.log'
SOURCE_STATUS = PRIVATE/'feed-share-source-status.json'
URL_PATTERN = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com\b')


def lock_held(path):
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def running():
    return lock_held(LOCK)


def tunnel_url(text):
    found = URL_PATTERN.findall(text)
    return found[0] if found else None


def publish(status, url=None, reason=None):
    payload = dict(status=status, url=url, updated_at=datetime.now(timezone.utc).isoformat(), reason=reason)
    with tempfile.NamedTemporaryFile(mode='w', dir=PRIVATE, delete=False) as f:
        json.dump(payload, f)
        temporary = Path(f.name)
    temporary.chmod(0o600)
    os.replace(temporary, STATUS)


def source_health(value):
    status = value['status']
    # Trading can pause orders while the source reader continues capturing.
    if status.startswith('paused:'):
        status = 'running'
    return {'status':status,'updated_at':value['updated_at']}


def publish_source(path):
    try:
        value = source_health(json.loads(path.read_text()))
    except (OSError,ValueError,KeyError,TypeError):
        value = {'status':'unavailable','updated_at':datetime.now(timezone.utc).isoformat()}
    with tempfile.NamedTemporaryFile(mode='w',dir=PRIVATE,delete=False) as f:
        json.dump(value,f)
        temporary = Path(f.name)
    temporary.chmod(0o600)
    os.replace(temporary,SOURCE_STATUS)


def api_ready():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8766/api/v1/trades', timeout=2):
            # Fail closed: the local API must require authentication.
            raise RuntimeError('API is not protected')
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return True
        raise RuntimeError('Unexpected API readiness response') from None
    except urllib.error.URLError:
        return False


def worker():
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    children = []
    with LOCK.open('a') as lock, ExitStack() as resources:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        STOP.unlink(missing_ok=True)
        publish('starting')
        env = os.environ | {'TRADE_FEED_HOST':'127.0.0.1','TRADE_FEED_PORT':'8766',
                             'TRADE_FEED_TRUST_CLOUDFLARE':'1',
                             'TRADE_FEED_READER_STATUS':str(SOURCE_STATUS)}
        commands = [
            ('feed-api.log',[str(ROOT/'work/api-venv/bin/python'), str(HERE/'live_api.py'),'serve']),
        ]
        reader = None
        reader_log = resources.enter_context((PRIVATE/'feed-reader.log').open('a'))
        url = None
        try:
            for log_name, command in commands:
                log = resources.enter_context((PRIVATE/log_name).open('a'))
                children.append(subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log))
            ready = False
            for _ in range(100):
                if stopped or STOP.exists():
                    return
                if any(p.poll() is not None for p in children):
                    raise RuntimeError('Feed startup failed')
                if api_ready():
                    ready = True
                    break
                time.sleep(.1)
            if not ready:
                raise RuntimeError('API startup timed out')
            log = resources.enter_context(TUNNEL_LOG.open('w'))
            children.append(subprocess.Popen([str(ROOT/'work/tools/cloudflared'), 'tunnel', '--no-autoupdate',
                '--url','http://127.0.0.1:8766', '--protocol','http2'], cwd=ROOT,
                stdin=subprocess.DEVNULL,stdout=log,stderr=log))
            while not stopped and not STOP.exists():
                bridge = lock_held(PRIVATE/'service.lock')
                if bridge and reader:
                    if reader.poll() is None:
                        reader.terminate()
                        reader.wait(timeout=10)
                    children.remove(reader)
                    reader = None
                elif not bridge and reader is None:
                    reader = subprocess.Popen([str(ROOT/'work/.venv/bin/python'),str(HERE/'live_reader.py')],
                        cwd=ROOT,stdin=subprocess.DEVNULL,stdout=reader_log,stderr=reader_log)
                    children.append(reader)
                publish_source(PRIVATE/('service-status.json' if bridge else 'feed-reader-status.json'))
                if any(p.poll() is not None for p in children):
                    raise RuntimeError('A feed process stopped')
                url = tunnel_url(TUNNEL_LOG.read_text(errors='replace'))
                publish('running' if url else 'starting tunnel', url)
                time.sleep(2)
        except Exception as exc:
            publish('failed', url, type(exc).__name__)
            raise
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
            # Only these service-owned children are stopped. Trading is separate.
            if stopped or STOP.exists():
                publish('stopped', url)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start','stop','status','worker'])
    command = parser.parse_args().command
    if command == 'worker':
        worker()
    elif command == 'start':
        if running():
            print('Shared feed is already running.')
        else:
            with (PRIVATE/'feed-share.log').open('a') as log:
                subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'worker'], cwd=ROOT,
                    stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
            print('Shared feed starting. Check status for the public address.')
    elif command == 'stop':
        STOP.touch(mode=0o600)
        print('Shared feed stop requested.')
    else:
        print('Worker running:',running())
        print(STATUS.read_text() if STATUS.exists() else 'Shared feed has not started.')


if __name__ == '__main__':
    main()
