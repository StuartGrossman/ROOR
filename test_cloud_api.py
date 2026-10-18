import tempfile, unittest, json
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
    def test_execution_status_reports_missing_or_present_worker(self):
        response=self.client.get('/api/v1/execution',headers=self.auth)
        self.assertEqual(response.json()['state'],'not_configured')
        (self.path.parent/'service-status.json').write_text('{"status":"running","updated_at":"2026-10-13T14:00:00+00:00","environment":"paper","orders_by_status":{"filled":2}}')
        self.assertEqual(self.client.get('/api/v1/execution',headers=self.auth).json()['environment'],'paper')
    def test_worker_health_is_the_status_source_in_paper_mode(self):
        import os
        from unittest.mock import patch
        from datetime import datetime, timezone
        (self.path.parent/'service-status.json').write_text(json.dumps({'status':'running','updated_at':datetime.now(timezone.utc).isoformat(),'telegram':{'connected':True}}))
        with patch.dict(os.environ,{'FLY_PAPER_EXECUTION':'1'}):
            app=create_cloud_app(self.path,make_password_record('secret'),123)
        self.assertTrue(TestClient(app).get('/api/v1/status',headers=self.auth).json()['reader']['isConnected'])
    def test_message_revisions_and_auth(self):
        self.assertEqual(self.client.get('/api/v1/messages/1').status_code,401)
        self.assertEqual(self.client.get('/api/v1/messages/1',headers=self.auth).json()['items'][0]['text'],'sold')
        self.assertEqual(self.client.get('/api/v1/messages/900',headers=self.auth).status_code,404)

    def test_worker_interpretations_are_exposed_with_version(self):
        from interpretation import interpret_signal, PARSER_VERSION
        cases=['always set profit stops to be safe','start selling','added 50 more at 35 cents','sell half','sell or hold','holding both','now.']
        stamp='2026-10-13T16:00:00+00:00'
        for mid,text in enumerate(cases,100):
            self.db.record(chat_id=123,message_id=mid,sent_at=stamp,edited_at=None,text=text,reply_to=None,media_type=None,historical=False)
        page=self.client.get('/api/v1/signals?date=2026-10-13',headers=self.auth).json()
        self.assertEqual(page['parserVersion'],PARSER_VERSION)
        by_id={x['messageId']:x['signal'] for x in page['items']}
        for mid,text in enumerate(cases,100):self.assertEqual(by_id[mid],interpret_signal(text,stamp))
