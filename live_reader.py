"""Archive-only Telegram reader. Never imports broker or execution code."""
import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import time

from archive import Archive

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRIVATE = ROOT/'work/private'
CHAT_ID = -1004384036660


def private_json(path):
    if path.stat().st_mode & 0o077:
        raise ValueError('Configuration must be owner-only')
    return json.loads(path.read_text())


def prepare_session(original, destination):
    """Same-host session clone, using a consistent SQLite snapshot."""
    original, destination = Path(original), Path(destination)
    config = private_json(original/'telegram.json')
    source = original/'telegram.session'
    if source.stat().st_mode & 0o077:
        raise ValueError('Original session must be owner-only')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.stat().st_mode & 0o077:
        raise ValueError('Reader directory must be owner-only')
    target = destination/'feed-telegram.session'
    config_path = destination/'feed-telegram.json'
    if target.exists() or config_path.exists():
        raise FileExistsError('Reader session is already prepared')
    # Exclusive creation cannot overwrite a prior session or follow a symlink.
    fd = os.open(target, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True) as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst)
        with config_path.open('x') as output:
            json.dump({'api_id':config['api_id'], 'api_hash':config['api_hash']}, output)
        config_path.chmod(0o600)
    except Exception:
        # Target ownership is established by our exclusive creation above.
        target.unlink(missing_ok=True)
        raise


def capture_message(archive, message, chat_id=CHAT_ID, historical=False):
    if not message.date:
        return False
    return archive.record(chat_id=chat_id, message_id=message.id, sent_at=message.date.isoformat(),
        edited_at=message.edit_date.isoformat() if message.edit_date else None,
        text=message.raw_text or '', reply_to=message.reply_to_msg_id,
        media_type=type(message.media).__name__ if message.media else None, historical=historical)


def archive_cursor(archive, chat_id=CHAT_ID):
    row = archive.db.execute('SELECT MAX(message_id) FROM messages WHERE chat_id=?', (chat_id,)).fetchone()
    return row[0] or 0


def publish_status(path, status, last_message_id, error_type=None):
    payload = dict(status=status, updated_at=datetime.now(timezone.utc).isoformat(),
                   last_message_id=last_message_id, error_type=error_type)
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        json.dump(payload, f)
        temporary = Path(f.name)
    temporary.chmod(0o600)
    os.replace(temporary, path)


async def worker(private, db_path, status_path):
    from telethon import TelegramClient, errors, events, utils

    config = private_json(private/'feed-telegram.json')
    if (private/'feed-telegram.session').stat().st_mode & 0o077:
        raise ValueError('Reader session must be owner-only')
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    with (private/'feed-reader.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        archive = Archive(db_path)
        cursor = archive_cursor(archive)
        if status_path.exists():
            saved = private_json(status_path).get('last_message_id')
            if isinstance(saved, int) and 0 <= saved <= cursor:
                cursor = saved
        # Persist poll progress before live callbacks can capture newer IDs.
        publish_status(status_path, 'connecting', cursor)
        client = TelegramClient(str(private/'feed-telegram'), config['api_id'], config['api_hash'],
            receive_updates=True, catch_up=True, sequential_updates=True,
            flood_sleep_threshold=0, request_retries=1, connection_retries=2)

        async def incoming(event):
            # Do not advance poll progress here: doing so could skip an older
            # missed message between the durable cursor and this live update.
            capture_message(archive, event.message)

        client.add_event_handler(incoming, events.NewMessage(chats=CHAT_ID))
        client.add_event_handler(incoming, events.MessageEdited(chats=CHAT_ID))
        last_edit_check = 0
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError('Reader session is not authorized')
            entity = await client.get_entity(CHAT_ID)
            if utils.get_peer_id(entity) != CHAT_ID or not entity.broadcast:
                raise ValueError('Wrong source channel')
            await client.catch_up()
            if cursor == 0:
                # Initial context is explicitly historical, never a fresh alert.
                async for msg in client.iter_messages(entity, limit=100):
                    capture_message(archive, msg, historical=True)
                cursor = archive_cursor(archive)
            while not stopped.is_set():
                delay = 1
                try:
                    if not client.is_connected():
                        await client.connect()
                    async for msg in client.iter_messages(entity, min_id=cursor, reverse=True, limit=100):
                        if stopped.is_set():
                            break
                        capture_message(archive, msg)
                        cursor = max(cursor, msg.id)
                    if time.monotonic()-last_edit_check >= 30:
                        async for msg in client.iter_messages(entity, limit=25):
                            capture_message(archive, msg)
                        last_edit_check = time.monotonic()
                    publish_status(status_path, 'running', cursor)
                except errors.FloodWaitError as exc:
                    delay = max(1, exc.seconds)
                    publish_status(status_path, 'Telegram cooldown', cursor, type(exc).__name__)
                except Exception as exc:
                    delay = 5
                    publish_status(status_path, 'retrying connection', cursor, type(exc).__name__)
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            publish_status(status_path, 'stopped', cursor)
            archive.close()
            await client.disconnect()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='worker', choices=['worker','prepare-session'])
    args = parser.parse_args()
    private = Path(os.environ.get('TRADE_FEED_PRIVATE', DEFAULT_PRIVATE))
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    if private.stat().st_mode & 0o077:
        raise ValueError('Reader private directory must be owner-only')
    if args.command == 'prepare-session':
        prepare_session(DEFAULT_PRIVATE, private)
        print('Dedicated same-host reader session prepared. Trading remains unchanged.')
    else:
        asyncio.run(worker(private, Path(os.environ.get('TRADE_FEED_DB', private/'messages.sqlite3')),
            Path(os.environ.get('TRADE_FEED_READER_STATUS', private/'feed-reader-status.json'))))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print('Reader stopped: '+type(exc).__name__+'. Check private configuration.')
        raise SystemExit(1) from None
