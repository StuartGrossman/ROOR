"""Exercise connection recovery through the real worker and paper state machine."""
import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import service
from matrix_execution import MatrixEngine
from test_execution import NOW
from test_matrix_policy import DISCOUNT_RULES, QuotedBroker


class RecoveryBroker(QuotedBroker):
    def __init__(self):
        super().__init__()
        self.risk_checks = 0

    def check_account(self):
        return {'environment': 'paper', 'status': 'ACTIVE'}

    def request(self, method, path, **kwargs):
        assert (method, path) == ('GET', '/v2/positions')
        self.risk_checks += 1
        symbols = {o['symbol'] for o in self.orders.values()}
        return [dict(symbol=s, qty=str(self.position(s)), asset_class='us_option')
                for s in symbols if self.position(s)]


class DroppingTelegram:
    def __init__(self, *, initial_failure=False, failed_reconnects=0, hang=False,
                 broker=None, messages=None, push_messages=None):
        self.connected = False
        self.handlers=[]
        self.push_messages=push_messages or []
        self.initial_failure = initial_failure
        self.failed_reconnects = failed_reconnects
        self.hang = hang
        self.broker = broker
        self.connects = 0
        self.polls = 0
        self.disconnect_risk_checks = []
        self.sales_during_hang = 0
        self.messages = messages or [
            self.message(40, '3. let’s buy 772 spy calls for 39 cents.', NOW-timedelta(minutes=2)),
            self.message(41, '4. let’s buy 773 spy calls for 39 cents.', NOW),
        ]

    @staticmethod
    def message(mid, text, stamp):
        return SimpleNamespace(id=mid, raw_text=text, date=stamp, edit_date=None,
                               media=None, reply_to_msg_id=None)

    def add_event_handler(self,handler,event):
        self.handlers.append(handler)

    async def catch_up(self):
        for msg in self.push_messages:
            await self.handlers[0](SimpleNamespace(message=msg))
        self.push_messages=[]

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connects += 1
        if self.initial_failure and self.connects == 1:
            raise OSError('Network unavailable')
        if 2 <= self.connects < 2+self.failed_reconnects:
            raise OSError('Network still unavailable')
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return True

    async def get_entity(self, chat_id):
        return SimpleNamespace(broadcast=True)

    async def iter_messages(self, entity, **kwargs):
        if not self.connected:
            raise ConnectionError('Cannot send requests while disconnected')
        if 'min_id' not in kwargs:
            return
        self.polls += 1
        # One successful poll, then lose transport. It only recovers on connect().
        if self.polls == 2:
            if self.hang:
                self.broker.bid = '.10'
                self.disconnect_risk_checks.append(self.broker.risk_checks)
                try:
                    await asyncio.Event().wait()
                finally:
                    self.sales_during_hang = sum(o['side'] == 'sell' for o in self.broker.calls)
            self.connected = False
            raise ConnectionError('Transport lost')
        if self.polls > 2 or self.initial_failure:
            for msg in self.messages:
                if msg.id > kwargs['min_id']:
                    yield msg


class ServiceRecoveryTests(unittest.TestCase):
    def run_worker(self, *, initial_failure=False, failed_reconnects=0, hang=False,
                   open_position=False, broker=None, messages=None, now=NOW,push_messages=None):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            broker = broker or RecoveryBroker()
            engine = MatrixEngine(folder/'execution-state.json', broker, DISCOUNT_RULES)
            if open_position:
                engine.observe(30, 'lets buy 600 spy calls for 39 cents.', NOW, NOW)
                engine.tick(NOW)
            engine.state['cursor'] = 37
            engine.save()
            client = DroppingTelegram(initial_failure=initial_failure,
                                      failed_reconnects=failed_reconnects,
                                      hang=hang, broker=broker, messages=messages,push_messages=push_messages)
            stop = folder/'stop'
            snapshots = []
            sleeps = []
            real_publish = service.publish
            real_sleep = asyncio.sleep

            def publish(*args, **kwargs):
                real_publish(*args, **kwargs)
                snapshots.append(json.loads((folder/'status.json').read_text()))

            async def sleep(seconds):
                sleeps.append(seconds)
                state = json.loads(engine.path.read_text())
                if state['cursor'] >= 41 or len(sleeps) >= 30:
                    stop.touch()
                await real_sleep(0)

            with contextlib.ExitStack() as stack, contextlib.redirect_stdout(io.StringIO()):
                for name, value in {
                    'LOCK': folder/'lock', 'STOP': stop, 'PRIVATE': folder,
                    'STATUS': folder/'status.json', 'REPORT': folder/'STATUS.md',
                }.items():
                    stack.enter_context(patch.object(service, name, value))
                stack.enter_context(patch.object(service, 'utcnow', return_value=now))
                stack.enter_context(patch.object(service, 'check_settings', return_value=DISCOUNT_RULES))
                stack.enter_context(patch.object(service, 'paper_broker', return_value=broker))
                stack.enter_context(patch.object(service, 'private_json', return_value={'api_id': 12345, 'api_hash': '0'*32}))
                stack.enter_context(patch.object(service, 'TelegramClient', return_value=client))
                stack.enter_context(patch.object(service.utils, 'get_peer_id', return_value=service.CHAT_ID))
                stack.enter_context(patch.object(service, 'publish', side_effect=publish))
                stack.enter_context(patch.object(service.asyncio, 'sleep', side_effect=sleep))
                stack.enter_context(patch.object(service, 'TELEGRAM_TIMEOUT', .03, create=True))
                stack.enter_context(patch.object(service, 'RISK_INTERVAL', .002, create=True))
                async def bounded_worker():
                    await asyncio.wait_for(service.worker(), timeout=1)
                asyncio.run(bounded_worker())
            state = json.loads(engine.path.read_text())
            import sqlite3
            with sqlite3.connect(folder/'messages.sqlite3') as db:
                captured = [r[0] for r in db.execute('SELECT message_id FROM messages ORDER BY message_id')]
            return state, broker, client, snapshots, captured

    def test_disconnect_recovers_cursor_and_archives_missed_alert_without_late_order(self):
        state, broker, client, snapshots, captured = self.run_worker()
        self.assertEqual(captured, [40, 41])
        self.assertEqual(state['cursor'], 41)
        self.assertEqual(state['events']['40']['status'], 'skipped')
        self.assertEqual(state['events']['41']['status'], 'submitted')
        self.assertEqual([o['client_order_id'] for o in broker.calls], ['geerawr-41-entry'])
        self.assertGreaterEqual(client.connects, 2)

    def test_initial_network_failure_retries_and_captures(self):
        state, broker, client, snapshots, captured = self.run_worker(initial_failure=True)
        self.assertEqual(captured, [40, 41])
        self.assertEqual(len(broker.calls), 1)

    def test_failed_reconnects_keep_retrying_and_preserve_last_successful_read(self):
        state, broker, client, snapshots, captured = self.run_worker(failed_reconnects=2)
        self.assertEqual(captured, [40, 41])
        failures = [s for s in snapshots if s.get('error')]
        self.assertTrue(failures)
        self.assertTrue(any(s.get('telegram', {}).get('last_successful_read_at') for s in failures))
        recovered = [s for s in snapshots if s['status'] == 'running' and s['cursor'] == 41]
        self.assertTrue(recovered)
        self.assertEqual(recovered[-1]['telegram']['consecutive_failures'], 0)

    def test_hung_telegram_read_times_out_and_keeps_open_position_managed(self):
        state, broker, client, snapshots, captured = self.run_worker(hang=True, open_position=True)
        self.assertEqual(captured, [40, 41])
        exits = [o for o in broker.calls if o['side'] == 'sell']
        self.assertTrue(exits, 'Open position stop must still execute while Telegram is hung')
        self.assertGreater(client.sales_during_hang, 0)
        self.assertGreater(broker.risk_checks, client.disconnect_risk_checks[0])
        self.assertTrue(any(s.get('error') == 'TimeoutError' for s in snapshots))

    def test_cooldown_finishes_started_exit_without_submitting_pending_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            broker = RecoveryBroker()
            engine = MatrixEngine(folder/'state.json', broker, DISCOUNT_RULES)
            engine.observe(1, 'lets buy 600 spy calls for 39 cents.', NOW, NOW)
            engine.tick(NOW)
            order = broker.orders['geerawr-1-entry']
            order.update(status='partially_filled', filled_qty='1')
            engine.state['orders']['geerawr-1-entry'].update(status='partially_filled', filled_qty='1')
            # The broker acknowledges the cancel later, after the sell has started.
            broker.cancel = lambda order_id: None
            engine.observe(2, 'sell calls', NOW, NOW)
            engine.tick(NOW)
            self.assertTrue(engine.state['events']['2']['exit_started'])
            self.assertEqual(engine.state['events']['2']['status'], 'pending')
            engine.observe(3, 'lets buy 601 spy puts for 39 cents.', NOW, NOW)
            engine.observe(4, 'sell puts', NOW, NOW)
            order['status'] = 'canceled'
            with patch.object(service, 'utcnow', return_value=NOW), \
                 patch.object(service, 'STOP', folder/'stop'), \
                 patch.object(service.asyncio, 'sleep', new_callable=AsyncMock):
                asyncio.run(service.wait_with_risk(engine, 2))
            self.assertEqual([(o['side'], o['qty']) for o in broker.calls], [('buy', 3), ('sell', 1)])
            self.assertEqual(engine.state['events']['2']['status'], 'submitted')
            self.assertEqual(engine.state['events']['3']['status'], 'pending')
            self.assertEqual(engine.state['events']['4']['status'], 'pending')


if __name__ == '__main__':
    unittest.main()

    def test_push_original_and_intermediate_edit_are_archived_before_polled_final(self):
        original=DroppingTelegram.message(41,'lets buy 600 spy calls for 39 cents.',NOW-timedelta(seconds=20))
        middle=DroppingTelegram.message(41,'lets buy 600 spy calls for 41 cents.',original.date)
        middle.edit_date=NOW-timedelta(seconds=10)
        final=DroppingTelegram.message(41,'lets buy 600 spy calls for 43 cents.',original.date)
        final.edit_date=NOW-timedelta(seconds=5)
        state,broker,client,_,captured=self.run_worker(initial_failure=True,messages=[final],push_messages=[original,middle,final])
        self.assertEqual(captured,[41,41,41])
        self.assertEqual(state['events']['41']['raw_source_text'],final.raw_text)
        self.assertEqual(len([o for o in broker.calls if o['side']=='buy']),1)
        self.assertTrue(state['events']['41']['identity_stable_edit'])
