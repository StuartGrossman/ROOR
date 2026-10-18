import tempfile,unittest
from pathlib import Path
from archive import Archive

class UpdateInboxTests(unittest.TestCase):
    def test_revisions_survive_restart_and_ack_does_not_requeue(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'archive.db';a=Archive(p)
            row=dict(chat_id=7,message_id=1,sent_at='2026-10-13T13:30:00+00:00',edited_at=None,text='buy',reply_to=None,media_type=None,historical=False,enqueue=True)
            a.record(**row)
            a.record(**dict(row,edited_at='2026-10-13T13:30:05+00:00',text='buy edited'))
            first=a.pending_updates(7);self.assertEqual([r['text'] for r in first],['buy','buy edited'])
            a.ack_update(first[0]['cursor']);a.close();a=Archive(p)
            self.assertEqual([r['text'] for r in a.pending_updates(7)],['buy edited'])
            a.record(**row)
            self.assertEqual(len(a.pending_updates(7)),1)
            a.ack_update(first[1]['cursor']);self.assertEqual(a.pending_updates(7),[]);a.close()
