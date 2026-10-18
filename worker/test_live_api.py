import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from archive import Archive
from live_api import create_app, make_password_record, ReadArchive


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.db = self.folder/'messages.sqlite3'
        self.archive = Archive(self.db)
        self.auth = {'Authorization': 'Bearer testing-password'}
        self.app = create_app(self.db, make_password_record('testing-password'), 123,
                              self.folder/'reader-status.json')
        self.client = TestClient(self.app)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def record(self, mid=1, text="let’s buy 780 spy calls for 40 cents.", edited=None, chat=123):
        self.archive.record(chat_id=chat, message_id=mid, sent_at='2026-10-13T14:00:00+00:00',
                            edited_at=edited, text=text, reply_to=None, media_type=None, historical=False)

    def test_every_route_requires_auth_and_rejects_query_password(self):
        self.record()
        for path in ['/api/v1/trades', '/api/v1/status', '/api/v1/schema']:
            for headers in ({}, {'Authorization': 'Bearer wrong'}, {'Authorization': 'Basic testing-password'}):
                response = self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn('text', response.text)
        self.assertEqual(self.client.get('/api/v1/trades?password=testing-password').status_code, 401)

    def test_message_text_and_utc_timestamp_preserved_without_private_fields(self):
        self.record(text="let’s buy 780 spy calls for 40 cents.\n")
        self.record(mid=2, chat=999, text='another channel')
        response = self.client.get('/api/v1/trades?after=0', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        items = response.json()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['text'], "let’s buy 780 spy calls for 40 cents.\n")
        self.assertEqual(items[0]['sentAt'], '2026-10-13T14:00:00+00:00')
        self.assertEqual(items[0]['signal']['kind'], 'entry')
        self.assertNotIn('chat_id', response.text)
        self.assertNotIn('password', response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_cursor_pages_include_edits_of_older_messages(self):
        self.record(1)
        self.record(2)
        first = self.client.get('/api/v1/trades?after=0&limit=1', headers=self.auth).json()
        self.assertTrue(first['hasMore'])
        second = self.client.get(f"/api/v1/trades?after={first['nextCursor']}&limit=1", headers=self.auth).json()
        self.assertEqual(second['items'][0]['messageId'], 2)
        self.record(1, text='sold', edited='2026-10-13T14:01:00+00:00')
        edit = self.client.get(f"/api/v1/trades?after={second['nextCursor']}", headers=self.auth).json()
        self.assertEqual(edit['items'][0]['messageId'], 1)
        self.assertTrue(edit['items'][0]['isEdited'])
        self.assertEqual(edit['items'][0]['text'], 'sold')
        empty = self.client.get(f"/api/v1/trades?after={edit['nextCursor']}", headers=self.auth).json()
        self.assertEqual(empty['nextCursor'], edit['nextCursor'])
        self.assertEqual(empty['items'], [])

    def test_default_page_returns_latest_records_in_capture_order(self):
        for mid in range(1, 5):
            self.record(mid)
        page = self.client.get('/api/v1/trades?limit=2', headers=self.auth).json()
        self.assertEqual([r['messageId'] for r in page['items']], [3, 4])
        self.assertFalse(page['hasMore'])

    def test_inputs_are_bounded_and_have_structured_errors(self):
        for query in ['after=-1', 'after=9223372036854775808', 'after=abc', 'limit=0', 'limit=501', 'wait=26']:
            response = self.client.get('/api/v1/trades?'+query, headers=self.auth)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()['error']['code'], 'INVALID_REQUEST')

    def test_archive_is_read_only_and_missing_archive_is_not_created(self):
        source = ReadArchive(self.db, 123)
        with source.connection() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM messages')
        missing = self.folder/'missing.sqlite3'
        app = create_app(missing, make_password_record('testing-password'), 123, self.folder/'status.json')
        response = TestClient(app).get('/api/v1/trades', headers=self.auth)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(missing.exists())
        self.assertNotIn(str(self.folder), response.text)

    def test_health_exposes_disconnected_reader_without_source_secrets(self):
        self.record()
        result = self.client.get('/api/v1/status', headers=self.auth).json()
        self.assertFalse(result['reader']['isConnected'])
        self.assertEqual(result['latestCursor'], 1)

    def test_bridge_heartbeat_tolerates_its_15_second_publish_interval(self):
        from datetime import datetime,timedelta,timezone
        path = self.folder/'reader-status.json'
        for age,connected in [(30,True),(60,False)]:
            path.write_text(json.dumps({'status':'running','updated_at':(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()}))
            self.assertEqual(self.client.get('/api/v1/status',headers=self.auth).json()['reader']['isConnected'],connected)

    def test_failed_authentication_is_rate_limited(self):
        self.app.state.limiter.failure_limit = 2
        for _ in range(2):
            self.assertEqual(self.client.get('/api/v1/trades').status_code, 401)
        response = self.client.get('/api/v1/trades')
        self.assertEqual(response.status_code, 429)
        self.assertIn('retry-after', response.headers)

    def test_authenticated_requests_are_rate_limited(self):
        self.app.state.limiter.request_limit = 2
        for _ in range(2):
            self.assertEqual(self.client.get('/api/v1/trades', headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get('/api/v1/trades', headers=self.auth).status_code, 429)

    def test_long_poll_delivers_a_new_record_from_another_connection(self):
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
                pending = asyncio.create_task(client.get('/api/v1/trades?after=0&wait=2', headers=self.auth))
                await asyncio.sleep(.15)
                self.record(11, text='sold')
                response = await asyncio.wait_for(pending, timeout=3)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['items'][0]['messageId'], 11)
        asyncio.run(check())

    def test_cloudflare_clients_have_separate_rate_limits_only_via_local_tunnel(self):
        async def check():
            app = create_app(self.db, make_password_record('testing-password'), 123,
                             self.folder/'status.json', trust_cloudflare=True)
            app.state.limiter.failure_limit = 1
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',123)),base_url='http://test') as client:
                bad = await client.get('/api/v1/trades',headers={'CF-Connecting-IP':'198.51.100.1'})
                self.assertEqual(bad.status_code,401)
                good = await client.get('/api/v1/trades',headers=self.auth | {'CF-Connecting-IP':'198.51.100.2'})
                self.assertEqual(good.status_code,200)
            external = create_app(self.db,make_password_record('testing-password'),123,
                                  self.folder/'status.json',trust_cloudflare=True)
            external.state.limiter.failure_limit = 1
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=external,client=('198.51.100.99',123)),base_url='http://test') as client:
                await client.get('/api/v1/trades',headers={'CF-Connecting-IP':'198.51.100.1'})
                spoofed = await client.get('/api/v1/trades',headers=self.auth | {'CF-Connecting-IP':'198.51.100.2'})
                self.assertEqual(spoofed.status_code,429)
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
