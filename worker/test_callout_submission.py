"""Today's callouts through reconnect, capture, parsing and the paper HTTP adapter.

The transport is entirely simulated: no credentials, Telegram session or network.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from unittest.mock import Mock

import broker as paper
import test_service_recovery as recovery
from matrix_execution import MatrixEngine
from test_matrix_policy import DISCOUNT_RULES

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


class PaperTransport:
    """Model broker acceptance and resting orders, never manufacture a fill."""
    def __init__(self):
        self.orders = {}
        self.submissions = []

    def request(self, method, url, **kwargs):
        if not url.startswith(paper.PAPER + '/'):
            raise AssertionError(f'Unexpected destination: {url}')
        path = url.removeprefix(paper.PAPER)
        params = kwargs.get('params', {})
        status, result = 200, None
        if (method, path) == ('GET', '/v2/account'):
            result = dict(status='ACTIVE', options_trading_level=2,
                          trading_blocked=False, options_buying_power='5000')
        elif (method, path) == ('GET', '/v2/clock'):
            result = {'is_open': True}
        elif (method, path) == ('GET', '/v2/positions'):
            result = []
        elif method == 'GET' and path.startswith('/v2/positions/'):
            status = 404
        elif (method, path) == ('GET', '/v2/options/contracts'):
            assert params['strike_price_gte'] == params['strike_price_lte']
            strike = Decimal(params['strike_price_gte'])
            expiry = params['expiration_date']
            symbol = (params['underlying_symbols'] + expiry.replace('-', '')[2:]
                      + params['type'][0].upper() + f'{int(strike * 1000):08d}')
            result = {'option_contracts': [dict(
                symbol=symbol, underlying_symbol=params['underlying_symbols'],
                root_symbol=params['root_symbol'], expiration_date=expiry,
                type=params['type'], strike_price=str(strike), size='100',
                status='active', tradable=True)]}
        elif (method, path) == ('GET', '/v2/orders'):
            result = [o for o in self.orders.values() if o['status'] == 'new']
        elif (method, path) == ('GET', '/v2/orders:by_client_order_id'):
            result = self.orders.get(params['client_order_id'])
            if result is None:
                status = 404
        elif (method, path) == ('POST', '/v2/orders'):
            payload = kwargs['json'].copy()
            assert payload['client_order_id'] not in self.orders
            self.submissions.append(payload)
            result = payload | dict(id='paper-accepted-41', status='new',
                                    filled_qty='0', filled_avg_price=None,
                                    submitted_at=NOW.isoformat())
            self.orders[payload['client_order_id']] = result
        elif method == 'DELETE' and path.startswith('/v2/orders/'):
            order = next(o for o in self.orders.values() if o['id'] == path.rsplit('/', 1)[1])
            order['status'] = 'canceled'
            status = 204
        else:
            raise AssertionError(f'Unexpected request: {method} {path}')
        return Mock(status_code=status, text='', json=lambda: result)


class CalloutSubmissionTests(unittest.TestCase):
    def test_today_callouts_recover_capture_and_submit_exact_resting_paper_limits(self):
        fixtures = [
            ('1. lets buy 761 puts for 51 cents.', 'SPY261015P00761000', '0.51', 2, '0.35'),
            ('2. let’s buy 771 spy calls for 53 cents.', 'SPY261015C00771000', '0.53', 2, '0.37'),
            ('3. let’s buy 772 spy calls for 39 cents.', 'SPY261015C00772000', '0.39', 3, '0.27'),
        ]
        for text, symbol, premium, quantity, limit in fixtures:
            with self.subTest(text=text):
                transport = PaperTransport()
                broker = paper.PaperBroker({'key_id': 'test-only', 'secret_key': 'test-only'})
                broker.session = Mock()
                broker.session.request.side_effect = transport.request
                messages = [
                    recovery.DroppingTelegram.message(40, text, NOW - timedelta(seconds=61)),
                    recovery.DroppingTelegram.message(41, text, NOW),
                ]
                state, _, client, _, captured = recovery.ServiceRecoveryTests().run_worker(
                    broker=broker, messages=messages, now=NOW)
                self.assertEqual(captured, [40, 41])
                self.assertEqual(state['cursor'], 41)
                self.assertGreaterEqual(client.connects, 2)
                self.assertEqual(state['events']['40']['status'], 'skipped')
                self.assertEqual(state['events']['41']['status'], 'submitted')
                signal = state['events']['41']['signal']
                self.assertEqual(signal['premium'], premium)
                self.assertEqual(signal['expiration'], '2026-10-15')
                expected = dict(symbol=symbol, qty=quantity, side='buy', type='limit',
                                time_in_force='day', position_intent='buy_to_open',
                                client_order_id='geerawr-41-entry', limit_price=limit)
                self.assertEqual(transport.submissions, [expected])
                calculated = (Decimal(premium) * Decimal('0.70')).quantize(
                    Decimal('0.01'), rounding=ROUND_DOWN)
                self.assertEqual(Decimal(limit), calculated)
                self.assertEqual(quantity, int(Decimal('100') // (calculated * 100)))
                order = state['orders']['geerawr-41-entry']
                self.assertEqual((order['id'], order['status'], order['filled_qty']),
                                 ('paper-accepted-41', 'new', '0'))
                self.assertEqual(order['lifetime_seconds'], 600)
                # Reopening the durable journal cannot resubmit the same alert.
                with tempfile.TemporaryDirectory() as directory:
                    journal = Path(directory) / 'execution-state.json'
                    journal.write_text(json.dumps(state))
                    engine = MatrixEngine(journal, broker, DISCOUNT_RULES)
                    engine.observe(41, text, NOW, NOW)
                    engine.tick(NOW + timedelta(seconds=599))
                    self.assertEqual(transport.orders['geerawr-41-entry']['status'], 'new')
                    engine.tick(NOW + timedelta(seconds=600))
                    self.assertEqual(transport.orders['geerawr-41-entry']['status'], 'canceled')
                    self.assertEqual(len(transport.submissions), 1)


if __name__ == '__main__':
    unittest.main()
