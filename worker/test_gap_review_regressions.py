"""Archive-supported exit-gap regressions; all broker activity stays in a fake."""
import unittest
from datetime import timedelta

import test_unified_regressions as fixtures
from interpretation import interpret_signal, protection_kind
from profit_policy import install
from test_matrix_policy import NOW


class GapReviewRegressions(unittest.TestCase):
    setUp = fixtures.UnifiedCalloutRegressions.setUp
    observe = fixtures.UnifiedCalloutRegressions.observe
    entry = fixtures.UnifiedCalloutRegressions.entry
    sells = fixtures.UnifiedCalloutRegressions.sells

    def two_calls(self):
        # August 24: entries #5088 / #5111, followed by #5121.
        self.entry(5088, 765)
        self.entry(5111, 764, 5)

    def test_both_calls_green_is_focus_only_in_canonical_parser(self):
        signal = interpret_signal('both calls green', NOW)
        self.assertEqual(signal.get('guidance_class'), 'position_focus')
        self.assertEqual(signal['kind'], 'guidance')

    def test_both_calls_green_then_sold_closes_both_source_positions(self):
        self.two_calls()
        self.observe(5121, 'both calls green', 10)
        self.assertEqual(self.sells(), [], 'A green-status message alone is not an exit.')
        self.observe(5122, 'sold', 15)
        self.assertEqual({o['symbol'] for o in self.sells()},
                         {'SPY261013C00765000', 'SPY261013C00764000'})
        self.assertEqual([o['qty'] for o in self.sells()], [5, 5])

    def test_both_calls_green_then_protect_profit_closes_both_when_green(self):
        install(self.engine, targets=False)
        self.two_calls()
        self.observe(5121, 'both calls green', 10)
        self.broker.bid = '.60'
        self.broker.ask = '.61'
        self.observe(5122, 'set profit stops', 15)
        self.assertEqual({o['symbol'] for o in self.sells()},
                         {'SPY261013C00765000', 'SPY261013C00764000'})
        self.assertTrue(all(o['type'] == 'market' for o in self.sells()))

    def test_both_calls_green_does_not_expand_a_subsequent_add_to_both(self):
        self.two_calls()
        self.observe(5121, 'both calls green', 10)
        self.observe(5122, 'add', 15)
        buys = [o for o in self.broker.calls if o['side'] == 'buy']
        self.assertEqual([(o['symbol'], o['qty']) for o in buys], [
            ('SPY261013C00765000', 5), ('SPY261013C00764000', 5),
            ('SPY261013C00764000', 10)])

    def test_ambiguous_group_across_tickers_does_not_sell_older_spy(self):
        self.entry(5088, 765)
        self.observe(5111, 'lets buy 7640 spx calls for 40 cents.', 5)
        self.observe(5121, 'both calls green', 10)
        self.observe(5122, 'sold', 15)
        self.assertEqual(self.sells(), [])
        self.assertEqual(self.engine.owned('SPY261013C00765000'), 5)

    def test_tight_stop_in_current_sizing_statement_is_profit_guidance(self):
        # Exact wording of archive #4008, not an automatic loss-stop request.
        text = 'im in 150 contracts, so I have to have a tight stop'
        self.assertEqual(protection_kind(text), 'tight_profit_protection')
        self.assertEqual(interpret_signal(text, NOW).get('guidance_class'),
                         'tight_profit_protection')

    def test_tight_stop_sizing_statement_takes_current_green_profit(self):
        install(self.engine, targets=False)
        self.entry(4000)
        self.broker.bid = '.60'
        self.broker.ask = '.61'
        self.observe(4008, 'im in 150 contracts, so I have to have a tight stop', 5)
        self.assertEqual([(o['side'], o['type'], o['qty']) for o in self.sells()],
                         [('sell', 'market', 5)])

    def test_tight_stop_sizing_statement_when_red_does_not_create_delayed_stop(self):
        install(self.engine, targets=False)
        self.entry(4000)
        self.broker.bid = '.30'
        self.observe(4008, 'im in 150 contracts, so I have to have a tight stop', 5)
        self.assertEqual(self.sells(), [])
        self.assertEqual(self.engine.state['events']['4008'].get('profit_result'),
                         'not_green_at_guidance')
        for seconds, bid in [(10, '.10'), (15, '.60'), (20, '.30')]:
            self.broker.bid = bid
            self.broker.ask = str(float(bid) + .01)
            self.broker.at = NOW + timedelta(seconds=seconds)
            self.engine.tick(self.broker.at, process_pending=False)
        self.assertEqual(self.sells(), [])
        self.assertEqual(self.engine.owned('SPY261013C00600000'), 5)

    def test_negated_or_educational_tight_stop_wording_cannot_trigger_exit(self):
        install(self.engine, targets=False)
        self.entry()
        self.broker.bid = '.60'
        self.broker.ask = '.61'
        examples = [
            'im in 150 contracts, so I do not have to have a tight stop',
            "im in 150 contracts, but I don't have to have a tight stop",
            'im in 150 contracts, so should I have a tight stop?',
            'When trading options, I have to have a tight stop',
            'For example, im in 150 contracts, so I have to have a tight stop',
        ]
        for mid, text in enumerate(examples, 2):
            with self.subTest(text=text):
                self.assertIsNone(protection_kind(text))
                self.observe(mid, text, mid * 5)
                self.assertEqual(self.sells(), [])


if __name__ == '__main__':
    unittest.main()
