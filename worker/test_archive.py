import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive import Archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "messages.sqlite3"
        self.archive = Archive(self.path)

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def message(self, **changes):
        value = dict(chat_id=-100123, message_id=7, sent_at="2026-10-13T16:00:00+00:00",
                     edited_at=None, text="Example entry", reply_to=None,
                     media_type=None, historical=True)
        return value | changes

    def test_duplicate_delivery_creates_one_revision(self):
        self.assertTrue(self.archive.record(**self.message()))
        self.assertFalse(self.archive.record(**self.message()))
        self.assertEqual(self.archive.count(-100123), 1)

    def test_edit_preserves_original_and_changed_text(self):
        self.archive.record(**self.message())
        self.archive.record(**self.message(text="Corrected entry", edited_at="2026-10-13T16:01:00+00:00"))
        rows = self.archive.rows(-100123)
        self.assertEqual([r["text"] for r in rows], ["Example entry", "Corrected entry"])

    def test_same_message_id_in_other_chat_does_not_collide(self):
        self.archive.record(**self.message())
        self.archive.record(**self.message(chat_id=-100456))
        self.assertEqual(self.archive.count(-100123), 1)
        self.assertEqual(self.archive.count(-100456), 1)

    def test_historical_message_never_becomes_fresh_on_replay(self):
        self.archive.record(**self.message())
        self.archive.record(**self.message(historical=False))
        self.assertEqual(self.archive.rows(-100123)[0]["historical"], 1)

    def test_text_is_stored_as_data(self):
        payload = "'); DROP TABLE messages; --"
        self.archive.record(**self.message(text=payload))
        self.assertEqual(self.archive.rows(-100123)[0]["text"], payload)

    def test_capture_persists_across_restart(self):
        self.archive.record(**self.message())
        self.archive.close()
        self.archive = Archive(self.path)
        self.assertFalse(self.archive.record(**self.message()))
        self.assertEqual(self.archive.count(-100123), 1)


if __name__ == "__main__":
    unittest.main()
