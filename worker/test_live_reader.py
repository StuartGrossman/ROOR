import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from archive import Archive
from live_reader import capture_message, archive_cursor, publish_status, prepare_session


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.archive = Archive(self.path/'messages.sqlite3')

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def message(self, **changes):
        return SimpleNamespace(**(dict(id=123, date=datetime(2026,10,13,14,tzinfo=timezone.utc),
            edit_date=None, raw_text='sold', reply_to_msg_id=None, media=None) | changes))

    def test_records_only_callout_data_and_deduplicates_polls(self):
        self.assertTrue(capture_message(self.archive, self.message(), 7))
        self.assertFalse(capture_message(self.archive, self.message(), 7))
        self.assertEqual(self.archive.count(7), 1)
        self.assertEqual(archive_cursor(self.archive, 7), 123)
        self.assertEqual(self.archive.rows(7)[0]['historical'], 0)

    def test_late_edit_is_preserved_as_an_additional_revision(self):
        capture_message(self.archive, self.message(), 7)
        capture_message(self.archive, self.message(edit_date=datetime.now(timezone.utc), raw_text='sold out'), 7)
        self.assertEqual(self.archive.count(7), 2)
        self.assertEqual(archive_cursor(self.archive, 7), 123)

    def test_skips_messages_without_a_source_date(self):
        self.assertFalse(capture_message(self.archive, self.message(date=None), 7))
        self.assertEqual(self.archive.count(7), 0)

    def test_status_has_no_message_text_or_connection_secrets(self):
        path = self.path/'status.json'
        publish_status(path, 'running', 123)
        value = json.loads(path.read_text())
        self.assertEqual(set(value), {'status','updated_at','last_message_id','error_type'})
        self.assertEqual(value['last_message_id'], 123)
        self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_session_clone_preserves_original_and_omits_phone(self):
        original = self.path/'source'
        destination = self.path/'reader'
        original.mkdir(mode=0o700)
        (original/'telegram.json').write_text(json.dumps({'api_id':111,'api_hash':'test-hash','phone':'private-phone'}))
        (original/'telegram.json').chmod(0o600)
        with sqlite3.connect(original/'telegram.session') as db:
            db.execute('CREATE TABLE session_data (value TEXT)')
            db.execute('INSERT INTO session_data VALUES (?)',('test-session',))
        (original/'telegram.session').chmod(0o600)
        before = (original/'telegram.session').read_bytes()
        prepare_session(original, destination)
        self.assertEqual((original/'telegram.session').read_bytes(), before)
        self.assertEqual(json.loads((destination/'feed-telegram.json').read_text()), {'api_id':111,'api_hash':'test-hash'})
        with sqlite3.connect(destination/'feed-telegram.session') as db:
            self.assertEqual(db.execute('SELECT value FROM session_data').fetchone()[0],'test-session')
        self.assertEqual((destination/'feed-telegram.session').stat().st_mode & 0o077,0)
        with self.assertRaises(FileExistsError):
            prepare_session(original, destination)


if __name__ == '__main__':
    unittest.main()
