import json
import plistlib
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import service


class ServiceTests(unittest.TestCase):
    def test_configuration_cannot_enable_live_or_different_sizing(self):
        base = {'environment':'paper','execution_enabled':True,'entry_premium_budget_usd':'200.00','addon_premium_budget_usd':'100.00','stop_loss':None,'follow_add_alerts':True,'repeated_add_handling':'one_per_60_second_burst','add_burst_seconds':60,'maximum_alert_age_seconds':60,'buy_limit_lifetime_seconds':60,'maximum_quote_age_seconds':15}
        base['execution_enabled'] = True
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(service, 'HERE', folder):
                for key, bad in [('environment', 'live'), ('entry_premium_budget_usd', '2000.00'),
                                 ('addon_premium_budget_usd', '500.00'), ('stop_loss', '.1'),
                                 ('execution_enabled', False), ('add_burst_seconds', 0)]:
                    (folder / 'settings.json').write_text(json.dumps(base | {key: bad}))
                    with self.assertRaises(RuntimeError, msg=key):
                        service.check_settings()
                (folder / 'settings.json').write_text(json.dumps(base))
                service.check_settings()

    def test_lock_status_does_not_trust_a_pid_file(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lock'
            with patch.object(service, 'LOCK', path):
                self.assertFalse(service.running())
                with path.open('a') as held:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertTrue(service.running())
                self.assertFalse(service.running())

    def test_noninteractive_stop_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'stop-requested'
            with patch.object(service, 'STOP', marker), patch('sys.stdin.isatty', return_value=False):
                with self.assertRaises(RuntimeError):
                    service.request_stop(confirmed=False)
            self.assertFalse(marker.exists())

    def test_stop_refuses_to_abandon_an_open_paper_position(self):
        class Broker:
            def request(self, method, path, **kwargs):
                if path == '/v2/positions':
                    return [{'symbol': 'SPY261014C00772000', 'qty': '2'}]
                return []
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'stop-requested'
            with patch.object(service, 'STOP', marker), patch.object(service, 'paper_broker', return_value=Broker()):
                with self.assertRaises(RuntimeError):
                    service.request_stop(confirmed=True)
            self.assertFalse(marker.exists())

    def test_confirmed_stop_when_account_is_flat_creates_marker(self):
        class Broker:
            def request(self, method, path, **kwargs):
                return []
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'stop-requested'
            with patch.object(service, 'STOP', marker), patch.object(service, 'paper_broker', return_value=Broker()):
                service.request_stop(confirmed=True)
            self.assertTrue(marker.exists())

    def test_worker_does_not_clear_an_intentional_stop_on_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            marker = folder / 'stop-requested'
            marker.touch()
            with patch.object(service, 'STOP', marker), patch.object(service, 'LOCK', folder / 'lock'), \
                 patch.object(service, 'check_settings', side_effect=AssertionError('worker should stay paused')):
                asyncio.run(service.worker())
            self.assertTrue(marker.exists())

    def test_start_clears_stop_marker_and_uses_installed_supervisor(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            marker = folder / 'stop-requested'
            agent = folder / 'agent.plist'
            marker.touch()
            agent.touch()
            with patch.object(service, 'STOP', marker), patch.object(service, 'AGENT_PLIST', agent), \
                 patch.object(service, 'check_settings'), patch.object(service, 'running', return_value=False), \
                 patch.object(service.subprocess, 'run', return_value=Mock(returncode=0)) as run, \
                 patch.object(service.subprocess, 'Popen') as spawn:
                service.request_start()
            self.assertFalse(marker.exists())
            self.assertTrue(any('kickstart' in call.args[0] for call in run.call_args_list))
            spawn.assert_not_called()

    def test_supervisor_restarts_failures_and_runs_at_login(self):
        with tempfile.TemporaryDirectory() as directory:
            agent=Path(directory)/'bridge.plist'
            with patch.object(service,'AGENT_PLIST',agent):
                service.install_supervisor()
            config=plistlib.loads(agent.read_bytes())
            self.assertEqual(config['KeepAlive'],{'SuccessfulExit':False})
            self.assertTrue(config['RunAtLoad'])
            self.assertEqual(config['ProgramArguments'][-1],'worker')

    def test_start_does_not_interrupt_an_already_running_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);agent=folder/'agent.plist';agent.touch()
            with patch.object(service,'STOP',folder/'stop'),patch.object(service,'AGENT_PLIST',agent), \
                 patch.object(service,'check_settings'),patch.object(service,'running',return_value=True), \
                 patch.object(service.subprocess,'run',return_value=Mock(returncode=0)) as run:
                service.request_start()
            self.assertEqual(run.call_count,1)

    def test_position_audit_detects_unjournaled_option_position(self):
        broker=Mock()
        broker.request.return_value=[{'symbol':'SPY261014C00772000','asset_class':'us_option','qty':'2'}]
        engine=Mock(broker=broker,state={'orders':{},'contracts':{}})
        engine.owned.return_value=0
        with self.assertRaisesRegex(RuntimeError,'position/journal mismatch'):
            service.audit_positions(engine)

    def test_position_audit_accepts_reconciled_owned_position(self):
        broker=Mock()
        broker.request.return_value=[{'symbol':'SPY261014C00772000','asset_class':'us_option','qty':'2'}]
        engine=Mock(broker=broker,state={'orders':{'id':{'payload':{'symbol':'SPY261014C00772000'}}},'contracts':{}})
        engine.owned.return_value=2
        service.audit_positions(engine)
        engine.reconcile.assert_called_once()


if __name__ == '__main__':
    unittest.main()
