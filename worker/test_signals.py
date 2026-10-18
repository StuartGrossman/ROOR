import unittest
from decimal import Decimal
from signals import parse_signal, size_entry

class SignalTests(unittest.TestCase):
    def test_spy_entry_uses_market_date_and_cents(self):
        s = parse_signal("1. let’s buy 600 spy calls for 39 cents.", "2026-10-14T01:00:00+00:00")
        self.assertEqual(s['kind'], 'entry')
        self.assertEqual(s['expiration'], '2026-10-13')
        self.assertEqual(s['premium'], '0.39')
        self.assertEqual(s['right'], 'call')

    def test_observed_misplaced_space_in_callout_two(self):
        signal = parse_signal("2. let sbuy 770 spy puts for 46 cents.",
                              "2026-10-16T14:53:31+00:00")
        self.assertEqual(signal['kind'], 'entry')
        self.assertEqual((signal['ticker'], signal['strike'], signal['right'],
                          signal['premium'], signal['expiration']),
                         ('SPY', '770', 'put', '0.46', '2026-10-16'))

    def test_misplaced_space_does_not_relax_full_message_matching(self):
        for text in ["we might let sbuy 770 spy puts for 46 cents.",
                     "2. let sbuy 770 spy puts for 46 cents. if it drops",
                     "let sbuy", "let sbuy 770 spy puts for zero cents."]:
            self.assertNotEqual(parse_signal(text, "2026-10-16T14:53:31+00:00")['kind'],
                                'entry')

    def test_200_budget_rounds_down_to_whole_contracts(self):
        self.assertEqual(size_entry(Decimal('200'), Decimal('0.39'), 100), (5, Decimal('195')))
        self.assertEqual(size_entry(Decimal('200'), Decimal('0.42'), 100), (4, Decimal('168')))

    def test_expensive_contract_is_skipped_not_rounded_up(self):
        self.assertEqual(size_entry(Decimal('200'), Decimal('2.01'), 100)[0], 0)

    def test_nonstandard_multiplier_is_used(self):
        self.assertEqual(size_entry(Decimal('200'), Decimal('2'), 10)[0], 10)

    def test_invalid_prices_and_budgets_rejected(self):
        for budget, premium in [('0', '.40'), ('200', '0'), ('200', '-1'), ('NaN', '.4')]:
            with self.assertRaises(ValueError):
                size_entry(Decimal(budget), Decimal(premium), 100)

    def test_spx_is_known_0dte_but_outside_spy_only_profile(self):
        s = parse_signal("6. lets buy 7715 spx puts for 185 cents. yolo", "2026-10-14T19:56:27+00:00")
        self.assertEqual(s['kind'], 'review')
        self.assertEqual(s['expiration'], '2026-10-14')
        self.assertEqual(s['reason'], 'SPX 0DTE is outside the SPY-only paper strategy.')

    def test_tickerless_spy_range_entry_uses_same_0dte_mapping(self):
        for text in ["1. lets buy 761 puts for 51 cents.", "lets buy 761 puts for 51 cents."]:
            s = parse_signal(text, "2026-10-15T13:53:37+00:00")
            self.assertEqual((s['kind'], s['ticker'], s['strike'], s['right']),
                             ('entry', 'SPY', '761', 'put'))
            self.assertEqual((s['premium'], s['expiration']), ('0.51', '2026-10-15'))

    def test_tickerless_strikes_outside_spy_range_require_review(self):
        for strike in ['392.5', '499', '1000', '7715']:
            s = parse_signal(f'lets buy {strike} puts for 51 cents.', '2026-10-15T13:53:37+00:00')
            self.assertEqual(s['kind'], 'review')

    def test_observed_price_unit_typo_preserves_called_premium(self):
        s = parse_signal("4. lets buy 600 spy calls for 29 centsz.", "2026-10-13T15:00:00+00:00")
        self.assertEqual((s['kind'], s['premium']), ('entry', '0.29'))

    def test_recap_and_watchlist_do_not_create_entries(self):
        for text in ["I was looking at 600 calls under 60 cents", "risked $400 on calls", "BUY THE DIPS", "39"]:
            self.assertNotEqual(parse_signal(text, "2026-10-13T15:00:00+00:00")['kind'], 'entry')

    def test_cut_calls_retains_contract_side(self):
        s = parse_signal("cut the calls at 7 cents.", "2026-10-13T15:00:00+00:00")
        self.assertEqual(s['kind'], 'exit')
        self.assertEqual(s['right'], 'call')
        self.assertTrue(s['needs_position_context'])

    def test_generic_exit_and_add_are_not_new_entries(self):
        self.assertEqual(parse_signal('sold', '2026-10-13T15:00:00+00:00')['kind'], 'exit')
        self.assertEqual(parse_signal('add', '2026-10-13T15:00:00+00:00')['kind'], 'add')

    def test_add_commands_require_position_and_quote(self):
        for text in ['add', 'addd', 'double it', 'add to the puts']:
            s = parse_signal(text, '2026-10-13T15:00:00+00:00')
            self.assertEqual(s['kind'], 'add')
            self.assertTrue(s['needs_position_context'])
            self.assertTrue(s['needs_fresh_quote'])

    def test_fill_recap_does_not_trigger_another_add(self):
        self.assertNotEqual(parse_signal('added 50 more at 40 cents', '2026-10-13T15:00:00+00:00')['kind'], 'add')

    def test_100_addon_budget(self):
        self.assertEqual(size_entry(Decimal('100'), Decimal('0.4'), 100), (2, Decimal('80')))
        self.assertEqual(size_entry(Decimal('100'), Decimal('1.25'), 100)[0], 0)

if __name__ == '__main__':
    unittest.main()
