import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution import Engine, UncertainSubmission

NOW = datetime(2026, 10, 13, 17, 0, tzinfo=timezone.utc)


class Broker:
    def __init__(self):
        self.orders = {}
        self.calls = []
        self.ask = '.40'
        self.uncertain = False
        self.external_qty = 0

    def is_open(self):
        return True

    def contract(self, signal):
        return {'symbol': 'SPY261013C00600000' if signal['right'] == 'call' else 'SPY261013P00600000',
                'size': 100, 'strike': signal['strike'], 'right': signal['right'], 'expiration': signal['expiration']}

    def quote(self, symbol):
        return {'ask': self.ask, 'timestamp': NOW.isoformat()}

    def position(self, symbol):
        return self.external_qty + sum(int(o['filled_qty']) * (1 if o['side'] == 'buy' else -1)
            for o in self.orders.values() if o['symbol'] == symbol)

    def lookup(self, client_id):
        return self.orders.get(client_id)

    def submit(self, payload):
        self.calls.append(payload)
        o = payload | {'id': payload['client_order_id'], 'status': 'filled',
                       'filled_qty': str(payload['qty'])}
        self.orders[payload['client_order_id']] = o
        if self.uncertain:
            raise UncertainSubmission()
        return o

    def cancel(self, order_id):
        self.orders[order_id]['status'] = 'canceled'


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.broker = Broker()
        self.engine = Engine(Path(self.tmp.name) / 'state.json', self.broker)

    def tearDown(self):
        self.tmp.cleanup()

    def observe(self, mid, text, stamp=NOW, now=NOW):
        self.engine.observe(mid, text, stamp, now)
        self.engine.tick(now)

    def entry(self, mid=1, right='calls'):
        self.observe(mid, f"1. lets buy 600 spy {right} for 39 cents.")

    def test_entry_honors_200_budget_and_limit(self):
        self.entry()
        self.assertEqual(self.broker.calls[0]['qty'], 5)
        self.assertEqual(self.broker.calls[0]['limit_price'], '0.39')
        self.assertEqual(self.broker.calls[0]['position_intent'], 'buy_to_open')

    def test_replay_and_restart_do_not_duplicate_order(self):
        self.entry()
        self.engine = Engine(self.engine.path, self.broker)
        self.entry()
        self.assertEqual(len(self.broker.calls), 1)

    def test_old_entry_is_ignored(self):
        self.observe(1, 'lets buy 600 spy calls for 39 cents.', stamp=NOW-timedelta(minutes=5))
        self.assertEqual(self.broker.calls, [])

    def test_add_burst_is_one_100_order_and_recap_is_not_an_order(self):
        self.entry()
        self.observe(2, 'add')
        self.observe(3, 'add', stamp=NOW+timedelta(seconds=2), now=NOW+timedelta(seconds=2))
        self.observe(4, 'double it', stamp=NOW+timedelta(seconds=4), now=NOW+timedelta(seconds=4))
        self.observe(5, 'added 50 more at 40 cents')
        self.assertEqual(len(self.broker.calls), 2)
        self.assertEqual(self.broker.calls[1]['qty'], 2)

    def test_sell_closes_only_owned_filled_contracts(self):
        self.entry()
        self.observe(2, 'sold')
        self.assertEqual(self.broker.calls[-1]['side'], 'sell')
        self.assertEqual(self.broker.calls[-1]['qty'], 5)
        self.assertEqual(self.broker.calls[-1]['position_intent'], 'sell_to_close')

    def test_generic_sold_with_two_positions_is_reviewed(self):
        self.entry()
        self.entry(2, 'puts')
        self.observe(3, 'sold')
        self.assertEqual(len(self.broker.calls), 2)
        self.assertEqual(self.engine.state['events']['3']['status'], 'review')

    def test_cut_calls_can_select_one_of_two_positions(self):
        self.entry()
        self.entry(2, 'puts')
        self.observe(3, 'cut the calls at 7 cents.')
        self.assertEqual(self.broker.calls[-1]['symbol'], 'SPY261013C00600000')
        self.assertEqual(self.broker.calls[-1]['side'], 'sell')

    def test_unrelated_position_blocks_entry(self):
        self.broker.external_qty = 3
        self.entry()
        self.assertEqual(self.broker.calls, [])

    def test_unknown_submission_is_reconciled_without_resubmission(self):
        self.broker.uncertain = True
        self.entry()
        self.broker.uncertain = False
        self.engine = Engine(self.engine.path, self.broker)
        self.engine.tick(NOW)
        self.assertEqual(len(self.broker.calls), 1)
        self.assertEqual(next(iter(self.engine.state['orders'].values()))['status'], 'filled')

    def test_edited_alert_does_not_create_a_second_order(self):
        self.entry()
        self.observe(1, 'lets buy 601 spy calls for 39 cents.')
        self.assertEqual(len(self.broker.calls), 1)
        self.assertEqual(self.engine.state['events']['1']['status'], 'review')

    def test_price_drop_does_not_create_a_stop_loss(self):
        self.entry()
        self.broker.ask = '.01'
        self.engine.tick(NOW)
        self.assertEqual(len(self.broker.calls), 1)

    def test_add_without_our_position_is_reviewed(self):
        self.observe(1, 'add')
        self.assertEqual(self.broker.calls, [])

    def test_stale_quote_prevents_add(self):
        self.entry()
        self.observe(2, 'add', NOW + timedelta(seconds=20), NOW + timedelta(seconds=20))
        self.assertEqual(len(self.broker.calls), 1)

    def test_temporary_quote_failure_does_not_consume_add_burst(self):
        self.entry()
        quote = self.broker.quote
        def failed(symbol):
            raise RuntimeError('Data temporarily unavailable')
        self.broker.quote = failed
        with self.assertRaises(RuntimeError):
            self.observe(2, 'add')
        self.broker.quote = quote
        self.engine.tick(NOW)
        self.assertEqual(len(self.broker.calls), 2)

    def test_new_burst_after_60_seconds_can_add_again(self):
        self.entry()
        self.observe(2, 'add')
        later = NOW + timedelta(seconds=61)
        self.broker.quote = lambda symbol: {'ask': '.4', 'timestamp': later.isoformat()}
        self.observe(3, 'add', later, later)
        self.assertEqual(len(self.broker.calls), 3)

    def test_unknown_not_found_blocks_new_orders(self):
        def timeout(payload):
            self.broker.calls.append(payload)
            raise UncertainSubmission()
        self.broker.submit = timeout
        self.entry()
        self.entry(2, 'puts')
        self.assertEqual(len(self.broker.calls), 1)

    def test_exit_cancels_unfilled_remainder_and_sells_only_partial_fill(self):
        def partial(payload):
            self.broker.calls.append(payload)
            result = payload | {'id': payload['client_order_id'], 'status': 'partially_filled', 'filled_qty': '2'}
            self.broker.orders[payload['client_order_id']] = result
            return result
        self.broker.submit = partial
        self.entry()
        self.observe(2, 'sold')
        self.assertEqual(self.broker.calls[-1]['qty'], 2)
        self.assertEqual(self.broker.calls[-1]['side'], 'sell')
        self.assertEqual(self.broker.orders['geerawr-1-entry']['status'], 'canceled')

    def test_pending_cancel_prevents_early_exit(self):
        self.entry()
        self.broker.orders['geerawr-1-entry'].update(status='partially_filled', filled_qty='2')
        self.engine.state['orders']['geerawr-1-entry'].update(status='partially_filled', filled_qty='2')
        self.broker.cancel = lambda oid: None
        self.observe(2, 'sold')
        self.assertEqual(len(self.broker.calls), 1)
        self.broker.orders['geerawr-1-entry']['status'] = 'canceled'
        self.engine.tick(NOW)
        self.assertEqual(self.broker.calls[-1]['qty'], 2)

    def test_external_position_change_prevents_exit(self):
        self.entry()
        self.broker.external_qty = 1
        self.observe(2, 'sold')
        self.assertEqual(len(self.broker.calls), 1)

    def test_skipped_other_entry_makes_generic_exit_ambiguous(self):
        self.entry()
        self.observe(2, 'lets buy 6000 spx calls for 100 cents.')
        self.observe(3, 'sold')
        self.assertEqual(len(self.broker.calls), 1)
        self.assertEqual(self.engine.state['events']['3']['status'], 'review')

    def test_sell_cancels_completely_unfilled_entry(self):
        def accepted(payload):
            self.broker.calls.append(payload)
            result = payload | {'id': payload['client_order_id'], 'status': 'new', 'filled_qty': '0'}
            self.broker.orders[payload['client_order_id']] = result
            return result
        self.broker.submit = accepted
        self.entry()
        self.observe(2, 'sold')
        self.assertEqual(self.broker.orders['geerawr-1-entry']['status'], 'canceled')
        self.assertEqual(len(self.broker.calls), 1)

    def test_first_seen_edited_entry_cannot_trade(self):
        self.engine.observe(1, 'lets buy 600 spy calls for 39 cents.', NOW, NOW, edited=True)
        self.engine.tick(NOW)
        self.assertEqual(self.broker.calls, [])

    def test_unfilled_buy_expires_after_60_seconds_without_selling_position(self):
        self.entry()
        for o in (self.broker.orders['geerawr-1-entry'], self.engine.state['orders']['geerawr-1-entry']):
            o.update(status='new', filled_qty='0')
        self.engine.tick(NOW + timedelta(seconds=61))
        self.assertEqual(self.broker.orders['geerawr-1-entry']['status'], 'canceled')
        self.assertEqual(len(self.broker.calls), 1)


if __name__ == '__main__':
    unittest.main()
