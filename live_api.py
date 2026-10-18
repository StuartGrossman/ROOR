"""Password-protected read-only API. No trading or Telegram client imports."""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from signals import parse_signal

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRIVATE = ROOT/'work/private'
CHAT_ID = -1004384036660
MAX_CURSOR = 2**63 - 1


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def utc(value):
    if value is None:
        return None
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('Missing source timezone')
    return stamp.astimezone(timezone.utc).isoformat()


def password_digest(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt),
                          n=16384, r=8, p=1, dklen=32, maxmem=64*1024*1024).hex()


def make_password_record(password):
    if not password or len(password.encode('utf-8')) > 128:
        raise ValueError('Password must contain 1–128 UTF-8 bytes')
    salt = secrets.token_hex(16)
    return {'algorithm': 'scrypt-v1', 'salt': salt, 'digest': password_digest(password, salt)}


def load_password_record(path):
    if path.stat().st_mode & 0o077:
        raise ValueError('Password file must be owner-only')
    record = json.loads(path.read_text())
    if (record.get('algorithm') != 'scrypt-v1' or len(bytes.fromhex(record['salt'])) != 16
            or len(bytes.fromhex(record['digest'])) != 32):
        raise ValueError('Invalid password file')
    return record


class Callout(BaseModel):
    cursor: int
    messageId: int
    revision: str
    sentAt: str
    editedAt: str | None
    capturedAt: str
    text: str
    replyToMessageId: int | None
    mediaType: str | None
    isHistorical: bool
    isEdited: bool
    signal: dict


class Page(BaseModel):
    items: list[Callout]
    nextCursor: int
    hasMore: bool
    generatedAt: str


class ReaderStatus(BaseModel):
    state: str
    updatedAt: str | None
    isConnected: bool


class FeedStatus(BaseModel):
    latestCursor: int
    lastCapturedAt: str | None
    reader: ReaderStatus
    generatedAt: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class APIError(BaseModel):
    error: ErrorDetail


class ReadArchive:
    def __init__(self, path, chat_id):
        self.path = Path(path).resolve()
        self.chat_id = chat_id

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=2)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            yield db
        finally:
            db.close()

    def page(self, after, limit):
        with self.connection() as db:
            if after is None:
                rows = list(db.execute(
                    'SELECT * FROM (SELECT rowid AS cursor, * FROM messages WHERE chat_id=? '
                    'ORDER BY rowid DESC LIMIT ?) ORDER BY cursor', (self.chat_id, limit)))
                more = False
            else:
                rows = list(db.execute('SELECT rowid AS cursor, * FROM messages WHERE chat_id=? '
                    'AND rowid>? ORDER BY rowid LIMIT ?', (self.chat_id, after, limit+1)))
                more = len(rows) > limit
                rows = rows[:limit]
        items = []
        for r in rows:
            items.append(dict(cursor=r['cursor'], messageId=r['message_id'], revision=r['revision'],
                              sentAt=utc(r['sent_at']), editedAt=utc(r['edited_at']), capturedAt=utc(r['captured_at']),
                              text=r['text'], replyToMessageId=r['reply_to'], mediaType=r['media_type'],
                              isHistorical=bool(r['historical']), isEdited=bool(r['edited_at']),
                              signal=parse_signal(r['text'], r['sent_at'])))
        return dict(items=items, nextCursor=items[-1]['cursor'] if items else (after or 0),
                    hasMore=more, generatedAt=utcnow())

    def status(self):
        with self.connection() as db:
            row = db.execute('SELECT rowid AS cursor, captured_at FROM messages WHERE chat_id=? '
                             'ORDER BY rowid DESC LIMIT 1', (self.chat_id,)).fetchone()
        return dict(latestCursor=row['cursor'] if row else 0, lastCapturedAt=utc(row['captured_at']) if row else None)


class RateLimiter:
    """One ASGI worker only. Keys use trusted connection addresses, not headers."""
    request_limit = 120
    failure_limit = 10

    def __init__(self):
        self.clients = {}

    def check(self, address):
        now = time.monotonic()
        self.clients = {k:v for k,v in self.clients.items() if now-v['seen'] < 900}
        if address not in self.clients and len(self.clients) >= 1024:
            raise HTTPException(429, 'RATE_LIMITED', headers={'Retry-After': '60'})
        record = self.clients.setdefault(address, dict(requests=[], failures=[], seen=now))
        record['seen'] = now
        record['requests'] = [t for t in record['requests'] if now-t < 60]
        record['failures'] = [t for t in record['failures'] if now-t < 900]
        if len(record['requests']) >= self.request_limit or len(record['failures']) >= self.failure_limit:
            raise HTTPException(429, 'RATE_LIMITED', headers={'Retry-After': '900' if len(record['failures']) >= self.failure_limit else '60'})
        record['requests'].append(now)
        return record


def reader_status(path):
    unavailable = dict(state='unavailable', updatedAt=None, isConnected=False)
    try:
        if Path(path).stat().st_size > 8192:
            return unavailable
        value = json.loads(Path(path).read_text())
        stamp = datetime.fromisoformat(value['updated_at'])
        if stamp.tzinfo is None:
            return unavailable
        age = (datetime.now(timezone.utc)-stamp).total_seconds()
        return dict(state=value['status'], updatedAt=utc(value['updated_at']),
                    isConnected=value['status'] == 'running' and 0 <= age <= 45)
    except (OSError, ValueError, KeyError, TypeError):
        return unavailable


def create_app(db_path, password_record, chat_id=CHAT_ID, status_path=None, trust_cloudflare=False):
    limiter = RateLimiter()
    source = ReadArchive(db_path, chat_id)
    scheme = HTTPBearer(auto_error=False)

    async def authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(scheme)):
        address = request.client.host if request.client else 'unknown'
        # This setting is used only for a Cloudflare tunnel into loopback.
        # External peers can never supply their own rate-limit identity.
        if trust_cloudflare and address in ('127.0.0.1','::1'):
            try:
                address = str(ipaddress.ip_address(request.headers.get('cf-connecting-ip','')))
            except ValueError:
                pass
        record = limiter.check(address)
        password = credentials.credentials if credentials else ''
        valid = (bool(password) and len(password.encode('utf-8')) <= 128
                 and hmac.compare_digest(password_digest(password, password_record['salt']), password_record['digest']))
        if not valid:
            record['failures'].append(time.monotonic())
            raise HTTPException(401, 'UNAUTHORIZED', headers={'WWW-Authenticate': 'Bearer'})

    app = FastAPI(title='Telegram callout feed', version='1.0.0', docs_url=None,
                  redoc_url=None, openapi_url=None, dependencies=[Depends(authenticate)],
                  responses={s:{'model':APIError} for s in (401,422,429,503)})
    app.state.limiter = limiter

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'"
        if request.url.scheme == 'https' or trust_cloudflare:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        codes = {401:('UNAUTHORIZED','A valid bearer password is required.'),
                 429:('RATE_LIMITED','Too many requests. Retry after the stated delay.'),
                 503:('SOURCE_UNAVAILABLE','The source archive is temporarily unavailable.'),
                 404:('NOT_FOUND','Endpoint not found.'), 405:('METHOD_NOT_ALLOWED','Only reads are supported.')}
        code, message = codes.get(exc.status_code, ('REQUEST_FAILED','Request failed.'))
        return JSONResponse({'error':dict(code=code,message=message)}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'error':dict(code='INVALID_REQUEST',message='Check cursor, limit and wait values.')}, status_code=422)

    async def read_page(after, limit):
        try:
            return await asyncio.to_thread(source.page, after, limit)
        except (sqlite3.Error, OSError, ValueError):
            raise HTTPException(503, 'SOURCE_UNAVAILABLE') from None

    @app.get('/api/v1/trades', response_model=Page)
    async def trades(request: Request, after: int | None = Query(None, ge=0, le=MAX_CURSOR),
                     limit: int = Query(100, ge=1, le=500), wait: int = Query(0, ge=0, le=25)):
        deadline = time.monotonic()+wait
        page = await read_page(after, limit)
        while after is not None and not page['items'] and time.monotonic() < deadline:
            if await request.is_disconnected():
                break
            await asyncio.sleep(min(.25, max(0, deadline-time.monotonic())))
            page = await read_page(after, limit)
        return page

    @app.get('/api/v1/status', response_model=FeedStatus)
    async def status():
        try:
            summary = await asyncio.to_thread(source.status)
        except (sqlite3.Error, OSError, ValueError):
            raise HTTPException(503, 'SOURCE_UNAVAILABLE') from None
        return {**summary, 'reader':reader_status(status_path or DEFAULT_PRIVATE/'feed-reader-status.json'), 'generatedAt':utcnow()}

    @app.get('/api/v1/schema', include_in_schema=False)
    async def schema():
        return app.openapi()

    return app


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve','set-password'])
    args = parser.parse_args()
    private = Path(os.environ.get('TRADE_FEED_PRIVATE', DEFAULT_PRIVATE))
    auth_path = Path(os.environ.get('TRADE_FEED_AUTH_FILE', private/'feed-auth.json'))
    if args.command == 'set-password':
        password = getpass.getpass('Feed password (hidden): ')
        if password != getpass.getpass('Confirm password (hidden): '):
            raise ValueError('Passwords do not match')
        record = make_password_record(password)
        auth_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=auth_path.parent, delete=False) as temp:
            json.dump(record, temp)
            temp_path = Path(temp.name)
        temp_path.chmod(0o600)
        os.replace(temp_path, auth_path)
        print('Private feed password hash saved.')
        return
    import uvicorn
    record = load_password_record(auth_path)
    app = create_app(Path(os.environ.get('TRADE_FEED_DB', private/'messages.sqlite3')), record,
                     status_path=Path(os.environ.get('TRADE_FEED_READER_STATUS', private/'feed-reader-status.json')),
                     trust_cloudflare=os.environ.get('TRADE_FEED_TRUST_CLOUDFLARE') == '1')
    uvicorn.run(app, host=os.environ.get('TRADE_FEED_HOST','127.0.0.1'),
                port=int(os.environ.get('TRADE_FEED_PORT','8766')), workers=1,
                proxy_headers=False, access_log=False, limit_concurrency=32, timeout_keep_alive=5)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print('Feed could not start: '+type(exc).__name__+'. Check private configuration.')
        raise SystemExit(1) from None
