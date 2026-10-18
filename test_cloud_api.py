import tempfile, unittest
from pathlib import Path
from fastapi.testclient import TestClient
from archive import Archive
from cloud_api import create_cloud_app
from live_api import make_password_record

class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'a.sqlite3'; self.db=Archive(self.path)
        for mid, sent, text in [(1,'2026-10-13T03:30:00+00:00','sold'),(2,'2026-10-13T14:00:00+00:00','let’s buy 780 spy calls for 40 cents.'),(3,'2026-10-13T15:00:00+00:00','hello')]:
            self.db.record(chat_id=123,message_id=mid,sent_at=sent,edited_at=None,text=text,reply_to=None,media_type=None,historical=True)
        self.client=TestClient(create_cloud_app(self.path,make_password_record('secret'),123))
        self.auth={'Authorization':'Bearer secret'}
    def tearDown(self): self.db.close(); self.tmp.cleanup()
    def test_date_uses_new_york_trading_date_and_preserves_raw_text(self):
        data=self.client.get('/api/v1/callouts?date=2026-10-13',headers=self.auth).json()
        self.assertEqual([r['messageId'] for r in data['items']],[2,3])
        self.assertEqual(data['items'][0]['text'],'let’s buy 780 spy calls for 40 cents.')
    def test_signals_filters_non_signals_and_reports_review(self):
        data=self.client.get('/api/v1/signals?date=2026-10-13',headers=self.auth).json()
        self.assertEqual([r['messageId'] for r in data['items']],[2])
    def test_exit_guidance_directed_add_and_trim_are_visible_for_review(self):
        for mid,text in [(4,'get ready to sell'),(5,'added 2 more to the 780 calls at 35 cents'),(6,'trim some here')]:
            self.db.record(chat_id=123,message_id=mid,sent_at='2026-10-13T16:00:00+00:00',edited_at=None,text=text,reply_to=None,media_type=None,historical=False)
        items=self.client.get('/api/v1/signals?date=2026-10-13',headers=self.auth).json()['items']
        by_id={x['messageId']:x['signal'] for x in items}
        self.assertEqual(by_id[4]['guidance_class'],'exit_warning')
        self.assertEqual(by_id[5]['kind'],'add')
        self.assertEqual(by_id[6]['kind'],'review')
        self.assertEqual(by_id[6]['guidance_class'],'possible_trim')
    def test_message_revisions_and_auth(self):
        self.assertEqual(self.client.get('/api/v1/messages/1').status_code,401)
        self.assertEqual(self.client.get('/api/v1/messages/1',headers=self.auth).json()['items'][0]['text'],'sold')
        self.assertEqual(self.client.get('/api/v1/messages/900',headers=self.auth).status_code,404)
