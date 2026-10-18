"""Start, stop, or inspect the local geerawr paper bridge."""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, errors, utils, events
from interpretation import PARSER_VERSION
from archive import Archive
from broker import PaperBroker
from connect import PRIVATE, ROOT, private_json
from execution import Engine, atomic_json

HERE = Path(__file__).resolve().parent
CHAT_ID = -1004384036660
STOP = PRIVATE / 'stop-requested'
STATUS = PRIVATE / 'service-status.json'
LOCK = PRIVATE / 'service.lock'
REPORT = HERE / 'STATUS.md'
AGENT_LABEL = 'com.teamror.geerawr-paper-bridge'
AGENT_PLIST = Path.home() / 'Library' / 'LaunchAgents' / f'{AGENT_LABEL}.plist'
TELEGRAM_TIMEOUT = 15
RISK_INTERVAL = 1


def utcnow():
    return datetime.now(timezone.utc)


def check_settings(*, allow_disabled=False):
    settings = json.loads((HERE / 'settings.json').read_text())
    if settings.get('strategy_profile') == 'matrix-profile.json':
        source = (HERE / 'matrix-profile.json').read_bytes()
        profile = json.loads(source)
        if settings.get('environment') != 'paper' or (settings.get('execution_enabled') is not True and not (allow_disabled and settings.get('execution_enabled') is False)) or profile.get('environment') != 'paper' or settings.get('profile_sha256') != hashlib.sha256(source).hexdigest():
            raise RuntimeError('Prepared paper profile is inactive or its verified fingerprint changed.')
        r = profile['rules']
        expected = {'entry_budget': 200, 'addon_budget': 400, 'max_adds': 1,
                    'stop_loss': None, 'addon_stop_loss': None, 'callout_exits_only': True,
                    'targets': [.10, .25], 'target_fractions': [0, 0],
                    'lock_arm': .75, 'lock_retention': .6,
                    'lock_confirmations': 2, 'lock_interval_ms': 10000,
                    'burst_seconds': 120, 'close_time': '15:15:00',
                    'entry_mode': 'market', 'entry_discount': 0,
                    'entry_lifetime_seconds': 600}
        if (profile.get('candidate') != 'entry200_add400_callout_exits'
                or profile.get('starting_capital') != 5000 or r != expected):
            raise RuntimeError('Paper profile differs from the verified callout-exit configuration.')
        return {**r, 'starting_capital': profile['starting_capital']}
    required = {'environment': 'paper', 'execution_enabled': True,
                'entry_premium_budget_usd': '200.00', 'addon_premium_budget_usd': '100.00',
                'stop_loss': None, 'follow_add_alerts': True,
                'repeated_add_handling': 'one_per_60_second_burst', 'add_burst_seconds': 60,
                'maximum_alert_age_seconds': 60, 'buy_limit_lifetime_seconds': 60,
                'maximum_quote_age_seconds': 15}
    if any(settings.get(k) != v for k, v in required.items()):
        raise RuntimeError('Settings differ from the verified $200/$100 paper configuration.')


def running():
    with LOCK.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def paper_broker():
    return PaperBroker(private_json('alpaca-paper.json'))


def install_supervisor():
    """Run at login and restart unexpected worker exits on this Mac."""
    if AGENT_PLIST.exists():
        return
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    config = {'Label': AGENT_LABEL,
              'ProgramArguments': [sys.executable, str(Path(__file__).resolve()), 'worker'],
              'RunAtLoad': True,
              'KeepAlive': {'SuccessfulExit': False},
              'ThrottleInterval': 5,
              'StandardOutPath': str(Path.home() / 'Library' / 'Logs' / 'geerawr-paper-bridge.log'),
              'StandardErrorPath': str(Path.home() / 'Library' / 'Logs' / 'geerawr-paper-bridge.log')}
    AGENT_PLIST.write_bytes(plistlib.dumps(config))
    AGENT_PLIST.chmod(0o600)


def audit_positions(engine):
    """Do not take new alerts when paper holdings differ from durable order history."""
    engine.reconcile()
    actual = {p['symbol']: int(p['qty']) for p in engine.broker.request('GET', '/v2/positions')
              if p.get('asset_class') == 'us_option' or p['symbol'] in engine.state['contracts']}
    if hasattr(engine,'reconcile_broker_events'):
        engine.reconcile_broker_events(actual,utcnow())
    if engine.state.get('broker_lifecycle_reviews'):
        raise RuntimeError('Broker exercise exposure requires reconciliation; entries paused.')
    symbols = set(actual) | {o['payload']['symbol'] for o in engine.state['orders'].values()}
    mismatches = {symbol: (engine.owned(symbol), actual.get(symbol, 0)) for symbol in symbols
                  if engine.owned(symbol) != actual.get(symbol, 0)}
    if mismatches:
        raise RuntimeError(f'Paper position/journal mismatch: {mismatches}')


def request_stop(*, confirmed=False):
    if not confirmed:
        if not sys.stdin.isatty():
            raise RuntimeError('Stopping paper execution requires an explicit interactive confirmation.')
        if input('Type STOP to pause paper trading: ').strip() != 'STOP':
            print('Paper bridge remains active.')
            return
    broker = paper_broker()
    positions = broker.request('GET', '/v2/positions')
    open_orders = broker.request('GET', '/v2/orders', params={'status': 'open', 'limit': 500})
    if positions or open_orders:
        raise RuntimeError('Paper positions or open orders exist. Close or cancel them before stopping the worker.')
    STOP.touch(mode=0o600)
    print('Stop requested. No paper positions or open orders were present.')


def request_start():
    check_settings()
    install_supervisor()
    STOP.unlink(missing_ok=True)
    try:
        domain = f'gui/{os.getuid()}'
        target = f'{domain}/{AGENT_LABEL}'
        loaded = subprocess.run(['/bin/launchctl', 'print', target],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if loaded and running():
            print('Paper bridge is already running; the supervisor is installed.')
            return
        command = (['/bin/launchctl', 'kickstart', '-k', target] if loaded else
                   ['/bin/launchctl', 'bootstrap', domain, str(AGENT_PLIST)])
        subprocess.run(command, check=True)
    except Exception:
        STOP.touch(mode=0o600)
        raise
    print('Paper bridge launch requested. Use Check Status to verify it connected.')


def publish(engine, status, error=None):
    events = engine.state['events']
    orders = engine.state['orders']
    recent = [{'message_id': mid, 'action': e['signal']['kind'], 'status': e['status'],
               'reason': e.get('reason', '')} for mid, e in events.items()
              if e['signal']['kind'] != 'ignore'][-8:]
    summary = {'parser_version': PARSER_VERSION, 'status': status, 'updated_at': utcnow().isoformat(), 'environment': 'paper',
               'strategy_profile': 'entry200_add400_callout_exits' if hasattr(engine, 'rules') else 'legacy',
               'source': 'geerawr', 'cursor': engine.state.get('cursor'),
               'order_intents': len(orders), 'orders_by_status': dict(Counter(o['status'] for o in orders.values())),
               'events_by_status': dict(Counter(e['status'] for e in events.values())),
               'recent': recent, 'error': error, 'worker_pid': os.getpid(),
               'telegram': getattr(engine, 'telegram_health', None),
               'exit_monitor_version': '2026-10-17.2',
               'broker_dispositions': len(engine.state.get('broker_dispositions',{})),
               'broker_lifecycle_reviews': engine.state.get('broker_lifecycle_reviews',{}),
               'exit_watch': engine.state.get('exit_watch', {}),
               'exit_reviews': [{'message_id':mid,'classification':e.get('guidance_class','position_matching_miss'),
                                 'reason':e.get('reason','')} for mid,e in events.items()
                                if e.get('status')=='review' and e['signal']['kind'] in {'exit','guidance'}][-20:]}
    atomic_json(STATUS, summary)
    lines = ['# geerawr → Alpaca paper', '', f'**Status: {status}**',
             f'Last check: {summary["updated_at"]}', '',
             f'- Strategy profile: {summary["strategy_profile"]}.',
             '- Entry: market order sized from a fresh ask against a $200 budget; actual cost can exceed it.',
             '- Add: one filled channel add, up to $400.',
             '- Exits: channel sell guidance and defined conditional exits. No automatic stops, profit locks or timed liquidation.',
             '- No fixed +10% or +25% profit sales. Virtual account cap: $5,000.',
             '- Fresh, clear SPY same-day alerts only. Other alerts are recorded for review.',
             '- Historical messages never create trades.', '',
             f'Order intents: {len(orders)}. Broker status: {json.dumps(summary["orders_by_status"])}.', '',
             'The Mac supervisor restarts unexpected exits at login. Keep the Mac awake and connected; the timestamp must keep advancing.', '',
             'Use **Start Paper Bridge.command**, **Stop Paper Bridge.command**, or **Check Status.command** in this folder.',
             'Stop requires confirmation and is refused while the paper account has an open position or order.', '', '## Recent messages', '']
    lines.extend(['', '## Positions and exit flags', ''])
    lines.extend(f'- {symbol}: {item["remaining_qty"]} contracts — {item["classification"]}.' for symbol,item in summary['exit_watch'].items())
    lines.extend(f'- Exit review, message {item["message_id"]}: {item["classification"]}. {item["reason"]}' for item in summary['exit_reviews'])
    lines.extend(f'- Broker lifecycle review: {review["symbol"]}. {review["reason"]}' for review in summary['broker_lifecycle_reviews'].values())
    health = summary['telegram']
    if health:
        lines[4:4] = [f'Telegram connected: {health["connected"]}.',
                      f'Last successful Telegram read: {health["last_successful_read_at"] or "none since startup"}.',
                      f'Reconnection attempts: {health["reconnect_attempts"]}. Consecutive read/connection failures: {health["consecutive_failures"]}.',
                      'The last-check timestamp advances on failures too; successful reads confirm capture health.', '']
    lines.extend(f'- Message {e["message_id"]}: {e["action"]} — {e["status"]}. {e["reason"]}' for e in recent)
    if error:
        lines.extend(['', f'Connection/check issue: {error}. The worker will retry reads.'])
    temp = REPORT.with_suffix('.tmp')
    temp.write_text('\n'.join(lines) + '\n')
    os.replace(temp, REPORT)
    print(json.dumps({k: v for k, v in summary.items() if k != 'recent'}), flush=True)


async def telegram_request(engine, request):
    """Bound Telegram I/O without suspending paper-position risk checks."""
    task = asyncio.ensure_future(asyncio.wait_for(request, timeout=TELEGRAM_TIMEOUT))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=RISK_INTERVAL)
            if done:
                return task.result()
            try:
                engine.tick(utcnow(), process_pending=False)
                audit_positions(engine)
            except Exception as exc:
                publish(engine, 'risk management error', type(exc).__name__)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def read_messages(client, entity, **kwargs):
    return [msg async for msg in client.iter_messages(entity, **kwargs)]


async def wait_with_risk(engine, seconds):
    for _ in range(seconds):
        if STOP.exists():
            break
        try:
            engine.tick(utcnow(), process_pending=False)
            audit_positions(engine)
        except Exception as exc:
            publish(engine, 'risk management error', type(exc).__name__)
        await asyncio.sleep(1)


async def worker():
    # The OS releases this lock even after a crash. A stale PID is never trusted.
    with LOCK.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('The paper bridge is already running.') from None
        if STOP.exists():
            return
        rules = check_settings(allow_disabled=True)
        broker = paper_broker()
        broker.check_account()
        if rules:
            from matrix_execution import MatrixEngine
            engine = MatrixEngine(PRIVATE / 'execution-state.json', broker, rules)
            from profit_policy import install as install_profit_policy
            install_profit_policy(engine)
        else:
            engine = Engine(PRIVATE / 'execution-state.json', broker)
        # Recover risk management before depending on Telegram connectivity.
        engine.tick(utcnow(), process_pending=False)
        audit_positions(engine)
        config = private_json('telegram.json')
        client = TelegramClient(str(PRIVATE / ('feed-telegram' if os.environ.get('FLY_PAPER') == '1' else 'telegram')), config['api_id'], config['api_hash'],
            receive_updates=True, catch_up=True, sequential_updates=True, flood_sleep_threshold=0, request_retries=1, connection_retries=2)
        archive = Archive(PRIVATE / 'messages.sqlite3')
        async def incoming(event):
            # Durably enqueue all push revisions; brokerage mutations happen
            # only in the single consumer loop, never in Telegram callbacks.
            capture(archive,event.message,enqueue=True)
        client.add_event_handler(incoming,events.NewMessage(chats=CHAT_ID))
        client.add_event_handler(incoming,events.MessageEdited(chats=CHAT_ID))
        engine.telegram_health = dict(connected=False, last_successful_read_at=None,
                                      reconnect_attempts=0, consecutive_failures=0,
                                      last_error_stage=None)
        health = engine.telegram_health
        entity, attempted_connection = None, False
        try:
            last_report, last_edit_check = 0, 0
            while not STOP.exists():
                stage = 'broker/check'
                try:
                    try:
                        current_rules = check_settings()
                    except RuntimeError:
                        current_rules = None
                    if current_rules != rules:
                        engine.tick(utcnow(), process_pending=False)
                        audit_positions(engine)
                        publish(engine, 'entries paused: settings changed; open positions still managed')
                        await asyncio.sleep(5)
                        continue
                    # Broker risk checks continue even if Telegram polling fails.
                    engine.tick(utcnow(), process_pending=False)
                    audit_positions(engine)
                    if entity is None or not client.is_connected():
                        stage = 'telegram/connect'
                        health['connected'] = False
                        if attempted_connection:
                            health['reconnect_attempts'] += 1
                        attempted_connection = True
                        publish(engine, 'connecting Telegram')
                        await telegram_request(engine, client.connect())
                        stage = 'telegram/authorization'
                        if not await telegram_request(engine, client.is_user_authorized()):
                            raise RuntimeError('Telegram authorization expired.')
                        stage = 'telegram/channel'
                        entity = await telegram_request(engine, client.get_entity(CHAT_ID))
                        if utils.get_peer_id(entity) != CHAT_ID or not entity.broadcast:
                            raise RuntimeError('The configured source is not the confirmed broadcast channel.')
                        await telegram_request(engine,client.catch_up())
                        health['connected'] = True
                    drain_updates(archive,engine)
                    if 'cursor' not in engine.state:
                        stage = 'telegram/initial_cursor'
                        latest = await telegram_request(engine, client.get_messages(entity, limit=1))
                        engine.state['cursor'] = latest[0].id if latest else 0
                        engine.state['started_at'] = utcnow().isoformat()
                        engine.save()
                    stage = 'telegram/messages'
                    # Telegram returns oldest first above the durable cursor.
                    messages = await telegram_request(engine, read_messages(
                        client, entity, min_id=engine.state['cursor'], reverse=True, limit=100))
                    health['last_successful_read_at'] = utcnow().isoformat()
                    for msg in messages:
                        if STOP.exists():
                            break
                        if not msg.date:
                            continue
                        capture(archive, msg,enqueue=True)
                        engine.state['cursor'] = msg.id
                        engine.save()
                    drain_updates(archive,engine)
                    if STOP.exists():
                        break
                    if time.monotonic() - last_edit_check > 30:
                        stage = 'telegram/edits'
                        recent = await telegram_request(engine, read_messages(client, entity, limit=100))
                        for msg in recent:
                            capture(archive,msg,enqueue=str(msg.id) in engine.state['events'])
                        drain_updates(archive,engine)
                        last_edit_check = time.monotonic()
                    health.update(connected=True, consecutive_failures=0, last_error_stage=None)
                    stage = 'broker/check'
                    audit_positions(engine)
                    engine.tick(utcnow())
                    if time.monotonic() - last_report > 15:
                        pending_unknown = any(o['status'] in {'submitting', 'unknown'} for o in engine.state['orders'].values())
                        publish(engine, 'paused: uncertain order needs reconciliation' if pending_unknown else 'running')
                        last_report = time.monotonic()
                except errors.FloodWaitError as exc:
                    health['last_error_stage'] = stage
                    publish(engine, 'Telegram cooldown', f'Retry in {exc.seconds} seconds')
                    last_report = 0
                    await wait_with_risk(engine, exc.seconds)
                except Exception as exc:
                    # No raw requests, API keys, or message bodies in logs.
                    mismatch = str(exc) if str(exc).startswith('Paper position/journal mismatch:') else None
                    lifecycle_review = str(exc).startswith('Broker exercise exposure requires reconciliation;')
                    health['last_error_stage'] = stage
                    if stage.startswith('telegram/'):
                        health['consecutive_failures'] += 1
                        health['connected'] = False
                        entity = None
                        publish(engine, 'recovering Telegram connection', type(exc).__name__)
                        # Discard a dead or stalled transport before reconnecting.
                        # If cleanup itself hangs, exit so the supervisor restarts us.
                        await telegram_request(engine, client.disconnect())
                    else:
                        publish(engine, 'entries paused: broker exercise requires review' if lifecycle_review else 'position mismatch: entries paused' if mismatch else 'retrying broker/check',
                                str(exc) if lifecycle_review else mismatch or type(exc).__name__)
                    last_report = 0
                    await wait_with_risk(engine, 5)
                await asyncio.sleep(1 if rules else 3)
        finally:
            health['connected'] = False
            publish(engine, 'stopped')
            try:
                await telegram_request(engine, client.disconnect())
            finally:
                archive.close()


def drain_updates(archive,engine):
    for row in archive.pending_updates(CHAT_ID):
        if STOP.exists():break
        audit_positions(engine)
        engine.observe(row['message_id'],row['text'],datetime.fromisoformat(row['sent_at']),utcnow(),
                       edited=bool(row['edited_at']),edited_at=datetime.fromisoformat(row['edited_at']) if row['edited_at'] else None,
                       media=bool(row['media_type']),reply_to=row['reply_to'])
        engine.tick(utcnow())
        archive.ack_update(row['cursor'])


def capture(archive, msg, *, enqueue=False):
    if msg.date:
        archive.record(chat_id=CHAT_ID, message_id=msg.id, sent_at=msg.date.isoformat(),
            edited_at=msg.edit_date.isoformat() if msg.edit_date else None, text=msg.raw_text or '',
            reply_to=msg.reply_to_msg_id, media_type=type(msg.media).__name__ if msg.media else None,
            historical=False,enqueue=enqueue)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'stop', 'status', 'worker'])
    parser.add_argument('--confirm-stop', action='store_true', help='Acknowledge a deliberate stop request.')
    args = parser.parse_args()
    if args.command == 'start':
        request_start()
    elif args.command == 'stop':
        request_stop(confirmed=args.confirm_stop)
    elif args.command == 'status':
        print('Worker running:', running())
        print(STATUS.read_text() if STATUS.exists() else 'Not started yet.')
    else:
        asyncio.run(worker())


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f'Paper bridge stopped: {type(exc).__name__}. Check configuration and connectivity.', file=sys.stderr)
        sys.exit(1)
