"""Independent regressions for the chronological market-order callout rules."""
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from matrix_execution import MatrixEngine
from test_matrix_policy import DISCOUNT_RULES, NOW, QuotedBroker


class UnifiedCalloutRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broker = QuotedBroker()
        self.broker.ask = '.40'
        self.broker.bid = '.39'
        self.broker.buying_power = lambda: Decimal('5000')
        original_contract = self.broker.contract
        def contract(signal):
            result = original_contract(signal)
            result['symbol'] = f"SPY261013{signal['right'][0].upper()}{int(Decimal(signal['strike']) * 1000):08d}"
            return result
        self.broker.contract = contract
        original_submit = self.broker.submit
        def submit(payload):
            order = original_submit(payload)
            order['filled_avg_price'] = self.broker.ask if payload['side'] == 'buy' else self.broker.bid
            return order
        self.broker.submit = submit
        self.rules = dict(DISCOUNT_RULES, entry_mode='market', entry_discount=0,
                          entry_budget=200, addon_budget=400, max_adds=1,
                          stop_loss=None, addon_stop_loss=None, callout_exits_only=True)
        self.engine = MatrixEngine(Path(self.tmp.name) / 'state.json', self.broker, self.rules)

    def observe(self, mid, text, seconds=0, posted=None, **kwargs):
        now = NOW + timedelta(seconds=seconds)
        self.broker.at = now
        self.engine.observe(mid, text, posted or now, now, **kwargs)
        self.engine.tick(now)

    def entry(self, mid=1, strike=600, seconds=0):
        self.observe(mid, f'lets buy {strike} spy calls for 40 cents.', seconds)

    def sells(self):
        return [order for order in self.broker.calls if order['side'] == 'sell']

    def test_price_only_edit_preserves_filled_root_for_later_sold(self):
        self.entry()
        root = self.engine.state['orders']['geerawr-1-entry']['root_entry']
        self.observe(1, 'lets buy 600 spy calls for 46 cents.', 20, posted=NOW,
                     edited=True, edited_at=NOW + timedelta(seconds=19))
        self.observe(2, 'sold', 25)
        self.assertEqual(self.engine.state['orders']['geerawr-1-entry']['root_entry'], root)
        self.assertEqual([(o['symbol'], o['qty']) for o in self.sells()], [('SPY261013C00600000', 5)])
        self.assertEqual(self.engine.owned('SPY261013C00600000'), 0)

    def test_strike_edit_does_not_sell_the_previously_bought_contract(self):
        self.entry()
        self.observe(1, 'lets buy 601 spy calls for 40 cents.', 20, posted=NOW,
                     edited=True, edited_at=NOW + timedelta(seconds=19))
        self.observe(2, 'sold', 25)
        self.assertEqual(self.sells(), [])
        self.assertEqual(self.engine.state['events']['1']['status'], 'review')
        self.assertEqual(self.engine.owned('SPY261013C00600000'), 5)

    def test_side_edit_does_not_sell_the_previously_bought_contract(self):
        self.entry()
        self.observe(1, 'lets buy 600 spy puts for 40 cents.', 20, posted=NOW,
                     edited=True, edited_at=NOW + timedelta(seconds=19))
        self.observe(2, 'sold', 25)
        self.assertEqual(self.sells(), [])
        self.assertEqual(self.engine.state['events']['1']['status'], 'review')

    def test_fresh_first_seen_edited_entry_buys_once_at_observation(self):
        edit_at = NOW + timedelta(seconds=19)
        self.observe(1, 'lets buy 600 spy calls for 40 cents.', 20, posted=NOW,
                     edited=True, edited_at=edit_at)
        self.engine = MatrixEngine(self.engine.path, self.broker, self.rules)
        self.observe(1, 'lets buy 600 spy calls for 40 cents.', 21, posted=NOW,
                     edited=True, edited_at=edit_at)
        self.assertEqual(len(self.broker.calls), 1)
        self.assertEqual(self.broker.calls[0]['type'], 'market')
        self.assertEqual(self.engine.owned('SPY261013C00600000'), 5)

    def test_old_post_with_recent_edit_does_not_open_a_position(self):
        self.observe(1, 'lets buy 600 spy calls for 40 cents.', 120, posted=NOW,
                     edited=True, edited_at=NOW + timedelta(seconds=119))
        self.assertEqual(self.broker.calls, [])

    def test_start_selling_is_an_executable_exit(self):
        self.entry()
        self.observe(2, 'start selling', 5)
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(self.engine.owned('SPY261013C00600000'), 0)

    def test_warning_quote_update_now_exits_same_source(self):
        self.entry()
        self.observe(2, 'get ready to sell', 5)
        self.observe(3, '55 cents now', 20)
        self.observe(4, 'now', 80)
        self.assertEqual([(o['symbol'], o['qty']) for o in self.sells()], [('SPY261013C00600000', 5)])

    def test_intervening_entry_prevents_old_warning_exiting_new_trade(self):
        self.entry()
        self.observe(2, 'get ready to sell', 5)
        self.entry(3, 601, 10)
        self.observe(4, 'now', 15)
        self.assertEqual(self.sells(), [])
        self.assertEqual(self.engine.owned('SPY261013C00601000'), 5)

    def test_cancel_warning_prevents_later_now_exit(self):
        self.entry()
        self.observe(2, 'get ready to sell', 5)
        self.observe(3, 'do not sell', 10)
        self.observe(4, 'now', 15)
        self.assertEqual(self.sells(), [])

    def test_half_trim_sells_floor_of_actual_qty_once_and_keeps_remainder(self):
        self.entry()
        self.observe(2, 'sell half', 5)
        self.engine = MatrixEngine(self.engine.path, self.broker, self.rules)
        self.observe(2, 'sell half', 6, posted=NOW + timedelta(seconds=5))
        self.assertEqual([o['qty'] for o in self.sells()], [2])
        self.assertEqual(self.engine.owned('SPY261013C00600000'), 3)

    def test_ambiguous_trim_amount_requires_review_without_full_exit(self):
        self.entry()
        self.observe(2, 'sell some', 5)
        self.assertEqual(self.sells(), [])
        self.assertEqual(self.engine.state['events']['2']['status'], 'review')
        self.assertEqual(self.engine.owned('SPY261013C00600000'), 5)

    def test_recovered_edited_entry_pending_order_survives_until_broker_fill(self):
        original_submit = self.broker.submit
        def delayed(payload):
            order = original_submit(payload)
            order.update(status='new', filled_qty='0')
            return order
        self.broker.submit = delayed
        self.observe(1, 'lets buy 600 spy calls for 40 cents.', 20, posted=NOW,
                     edited=True, edited_at=NOW + timedelta(seconds=19))
        order = self.broker.orders['geerawr-1-entry']
        self.engine = MatrixEngine(self.engine.path, self.broker, self.rules)
        self.broker.at = NOW + timedelta(seconds=25)
        self.engine.tick(self.broker.at)
        self.assertEqual(order['status'], 'new', 'A known edited origin is not a subsequent revision.')
        order.update(status='filled', filled_qty=str(order['qty']), filled_at=self.broker.at.isoformat())
        self.engine.tick(self.broker.at)
        self.assertEqual(self.engine.owned('SPY261013C00600000'), 5)
        self.assertEqual(len(self.broker.calls), 1)

    def test_subsequent_edit_cancels_pending_remainder_of_recovered_entry(self):
        original_submit = self.broker.submit
        def partial(payload):
            order = original_submit(payload)
            order.update(status='partially_filled', filled_qty='2')
            return order
        self.broker.submit = partial
        self.observe(1, 'lets buy 600 spy calls for 40 cents.', 20, posted=NOW,
                     edited=True, edited_at=NOW + timedelta(seconds=19))
        self.observe(1, 'lets buy 600 spy calls for 46 cents.', 25, posted=NOW,
                     edited=True, edited_at=NOW + timedelta(seconds=24))
        self.assertEqual(self.broker.orders['geerawr-1-entry']['status'], 'canceled')
        self.assertEqual(self.engine.owned('SPY261013C00600000'), 2)
        self.assertEqual(len(self.broker.calls), 1)

    def test_conditional_profit_exit_keeps_original_trade_after_new_entry(self):
        self.entry()
        self.observe(2, 'sell when you see green', 5)
        self.entry(3, 601, 10)
        self.broker.bid = '.50'
        self.broker.at = NOW + timedelta(seconds=15)
        self.engine.tick(self.broker.at, process_pending=False)
        self.assertEqual([(o['symbol'], o['qty']) for o in self.sells()], [('SPY261013C00600000', 5)])
        self.assertEqual(self.engine.owned('SPY261013C00601000'), 5)

    def test_replayed_original_cannot_overwrite_newer_contract_edit(self):
        original = 'lets buy 600 spy calls for 40 cents.'
        latest = 'lets buy 601 spy calls for 40 cents.'
        self.observe(1, original)
        self.observe(1, latest, 20, posted=NOW, edited=True,
                     edited_at=NOW + timedelta(seconds=19))
        self.observe(1, original, 30, posted=NOW)
        self.assertEqual(self.engine.state['events']['1']['signal']['strike'], '601')
        self.assertEqual(self.engine.state['events']['1']['raw_source_text'], latest)
        self.observe(2, 'sold', 35)
        self.assertEqual(self.sells(), [])

    def test_out_of_order_old_edit_cannot_overwrite_newer_contract_edit(self):
        original = 'lets buy 600 spy calls for 40 cents.'
        older = 'lets buy 600 spy calls for 42 cents.'
        latest = 'lets buy 601 spy calls for 44 cents.'
        self.observe(1, original)
        self.observe(1, older, 10, posted=NOW, edited=True,
                     edited_at=NOW + timedelta(seconds=9))
        self.observe(1, latest, 20, posted=NOW, edited=True,
                     edited_at=NOW + timedelta(seconds=19))
        self.observe(1, older, 30, posted=NOW, edited=True,
                     edited_at=NOW + timedelta(seconds=9))
        self.assertEqual(self.engine.state['events']['1']['signal']['strike'], '601')
        self.assertEqual(self.engine.state['events']['1']['raw_source_text'], latest)
        self.observe(2, 'sold', 35)
        self.assertEqual(self.sells(), [])


if __name__ == '__main__':
    unittest.main()
