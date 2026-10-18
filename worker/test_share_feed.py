import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import fcntl
import share_feed


class ShareFeedTests(unittest.TestCase):
    def test_only_cloudflare_public_hostnames_are_extracted(self):
        self.assertEqual(share_feed.tunnel_url('Tunnel ready: https://example-word.trycloudflare.com'),
                         'https://example-word.trycloudflare.com')
        self.assertIsNone(share_feed.tunnel_url('https://example.com'))
        self.assertIsNone(share_feed.tunnel_url('http://example.trycloudflare.com'))

    def test_running_uses_a_real_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'lock'
            with patch.object(share_feed,'LOCK',path):
                self.assertFalse(share_feed.running())
                with path.open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    self.assertTrue(share_feed.running())

    def test_bridge_health_preserves_original_timestamp_and_omits_account_data(self):
        value = {'status':'running','updated_at':'2026-10-14T16:00:00+00:00','orders':{'private':'data'}}
        self.assertEqual(share_feed.source_health(value),
                         {'status':'running','updated_at':'2026-10-14T16:00:00+00:00'})
        self.assertEqual(share_feed.source_health(value | {'status':'paused: uncertain order needs reconciliation'})['status'],'running')
        self.assertEqual(share_feed.source_health(value | {'status':'Telegram cooldown'})['status'],'Telegram cooldown')


if __name__ == '__main__':
    unittest.main()
