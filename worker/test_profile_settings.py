import tempfile,json,hashlib,unittest
from pathlib import Path
from unittest.mock import patch
import service

class ProfileSettingsTests(unittest.TestCase):
    def test_profile_requires_paper_activation_and_exact_fingerprint(self):
        original=Path(__file__).resolve().parent/'matrix-profile.json'
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);raw=original.read_bytes();(folder/'matrix-profile.json').write_bytes(raw)
            settings=dict(strategy_profile='matrix-profile.json',environment='paper',execution_enabled=True,profile_sha256=hashlib.sha256(raw).hexdigest())
            with patch.object(service,'HERE',folder):
                for changed in [dict(environment='live'),dict(execution_enabled=False),dict(profile_sha256='wrong')]:
                    (folder/'settings.json').write_text(json.dumps(settings|changed))
                    with self.assertRaises(RuntimeError):service.check_settings()
                (folder/'settings.json').write_text(json.dumps(settings|{'execution_enabled':False}))
                self.assertEqual(service.check_settings(allow_disabled=True)['entry_discount'],0)
                (folder/'settings.json').write_text(json.dumps(settings))
                selected=service.check_settings()
                self.assertEqual((selected['entry_budget'],selected['addon_budget'],selected['starting_capital']),
                                 (200,400,5000))
                self.assertEqual(selected['entry_mode'],'market')
                self.assertIsNone(selected['stop_loss'])
                self.assertIsNone(selected['addon_stop_loss'])
                self.assertTrue(selected['callout_exits_only'])
                altered=json.loads(raw)
                altered['rules']['entry_discount']=.25
                changed=json.dumps(altered).encode()
                (folder/'matrix-profile.json').write_bytes(changed)
                (folder/'settings.json').write_text(json.dumps(settings | {'profile_sha256':hashlib.sha256(changed).hexdigest()}))
                with self.assertRaises(RuntimeError):service.check_settings()

    def test_active_profile_submits_200_starter_without_discount_and_400_add(self):
        from datetime import timedelta
        from decimal import Decimal
        from matrix_execution import MatrixEngine
        from test_matrix_policy import QuotedBroker
        from test_execution import NOW
        with tempfile.TemporaryDirectory() as directory:
            broker=QuotedBroker();broker.buying_power=lambda:Decimal('5000')
            engine=MatrixEngine(Path(directory)/'state.json',broker,service.check_settings())
            engine.observe(1,'lets buy 600 spy calls for 39 cents.',NOW,NOW);engine.tick(NOW)
            self.assertEqual((broker.calls[0]['qty'],broker.calls[0]['type']),(5,'market'))
            now=NOW+timedelta(seconds=1);broker.at=now
            engine.observe(2,'add',now,now);engine.tick(now)
            self.assertEqual((broker.calls[1]['qty'],broker.calls[1]['type']),(10,'market'))
            broker.bid='.01';engine.tick(now)
            self.assertEqual([o['side'] for o in broker.calls],['buy','buy'])
