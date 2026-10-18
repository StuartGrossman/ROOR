"""Audited Telegram wording, through the live adapter's observation path.

Fixtures reproduce the 2026-10-15 parser audit. These tests only observe text;
they never execute an event, obtain a quote, or submit an order.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from matrix_execution import MatrixEngine


NOW = datetime(2026, 10, 13, 17, 0, tzinfo=timezone.utc)
RULES = {'entry_mode': 'signal_limit', 'entry_discount': 0}


class ObservationOnlyBroker:
    """Fail immediately if parsing crosses the brokerage boundary."""

    def __getattr__(self, name):
        raise AssertionError(f'Observation must not access broker.{name}')


class ParserHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = MatrixEngine(
            Path(self.tmp.name) / 'state.json', ObservationOnlyBroker(), RULES)
        self.next_id = 0

    def tearDown(self):
        self.tmp.cleanup()

    def observe(self, text, *, stamp=NOW, now=NOW, edited=False):
        self.next_id += 1
        self.engine.observe(self.next_id, text, stamp, now, edited=edited)
        self.assertEqual(self.engine.state['orders'], {})
        return self.engine.state['events'][str(self.next_id)]

    def test_callout_two_misplaced_space_is_pending_entry(self):
        event = self.observe('2. let sbuy 770 spy puts for 46 cents.')
        self.assertEqual(event['signal']['kind'], 'entry')
        self.assertTrue(event['entry_like'])
        self.assertEqual(event['signal']['strike'], '770')
        self.assertEqual(event['signal']['premium'], '0.46')
        self.assertEqual(event['status'], 'pending')

    def test_historical_stop_confirmations_become_pending_exits(self):
        fixtures = [
            ('525', 'stopped out 😫'),
            ('1343', 'SL triggered at 22 cents.'),
            ('2279', 'SL triggered. out at 30 cents.'),
            ('2444', 'SL triggered at 26 cents.'),
            ('2481', 'got SL at 28 cents.'),
            ('7109', 'profit stopped'),
            ('7364', 'SL triggered at 26 cents'),
            ('10196', 'SL triggered at 22 cents.'),
        ]
        for source_id, text in fixtures:
            with self.subTest(source_id=source_id, text=text):
                event = self.observe(text)
                self.assertEqual(event['signal']['kind'], 'exit')
                self.assertEqual(event['status'], 'pending')
                self.assertIsNone(event['signal'].get('right'))

    def test_historical_typo_in_put_exit_preserves_side(self):
        # Telegram message 10872.
        event = self.observe('cut the puts att 11 cents.')
        self.assertEqual(event['signal']['kind'], 'exit')
        self.assertEqual(event['signal']['right'], 'put')
        self.assertEqual(event['status'], 'pending')

    def test_historical_singular_cent_add_is_pending_add(self):
        # Telegram message 9755. Source quantity is not the bot's order size.
        event = self.observe('added 50 more at 35 cent')
        self.assertEqual(event['signal']['kind'], 'add')
        self.assertEqual(event['status'], 'pending')
        self.assertIsNone(event['signal'].get('right'))
        self.assertNotIn('qty', event['signal'])

    def test_exact_historical_sale_confirmations_are_observed_now(self):
        fixtures = [
            ('2534', 'sold the top'),
            ('3185', 'sold130'),
            ('8033', 'sell the move guys'),
            ('8806', 'whats with the eyes, i been sold.'),
        ]
        for source_id, text in fixtures:
            with self.subTest(source_id=source_id, text=text):
                event = self.observe(text)
                self.assertEqual(event['signal']['kind'], 'exit')
                self.assertEqual(event['status'], 'pending')
                self.assertEqual(event['sent_at'], NOW.isoformat())
                # Neither "the top" nor the unlabelled number is a fill price.
                self.assertNotIn('premium', event['signal'])
                self.assertNotIn('mentioned_premium', event['signal'])

    def test_three_historical_malformed_entries_preserve_exact_contract_fields(self):
        fixtures = [
            ('6300', 'lets buy - 766 spy calls for 47 cents.',
             '766', 'call', '0.47'),
            ('7471', '4. let’s buy 762 spy puts for 48 bents.',
             '762', 'put', '0.48'),
            ('10869', '4. lets buy 778 spy calls for 29 centsz.',
             '778', 'call', '0.29'),
        ]
        for source_id, text, strike, right, premium in fixtures:
            with self.subTest(source_id=source_id, text=text):
                event = self.observe(text)
                actual = {key: event['signal'].get(key) for key in
                          ['kind', 'ticker', 'strike', 'right', 'premium', 'expiration']}
                self.assertEqual(actual, {
                    'kind': 'entry', 'ticker': 'SPY', 'strike': strike,
                    'right': right, 'premium': premium, 'expiration': '2026-10-13'})
                self.assertEqual(event['status'], 'pending')
                self.assertTrue(event['entry_like'])

    def test_hypothetical_conditional_questions_and_third_party_reports_are_not_actions(self):
        fixtures = [
            'if SL triggered at 22 cents.',
            'SL triggered at 22 cents?',
            'got SL at 28 cents?',
            'if stopped out 😫',
            'profit stopped?',
            'should we cut the puts att 11 cents?',
            'I would have added 50 more at 35 cent',
            'added 50 more at 35 cent?',
            'if it bounces, sell the move guys',
            'he sold the top',
            'I should have sold the top',
            'whats with the eyes, he been sold.',
            'sold130?',
            'what if lets buy - 766 spy calls for 47 cents.',
            'if we break out lets buy 778 spy calls for 29 centsz.',
            '4. lets buy 778 spy calls for 29 centsz?',
        ]
        for text in fixtures:
            with self.subTest(text=text):
                event = self.observe(text)
                self.assertNotIn(event['signal']['kind'], {'entry', 'add', 'exit'})
                self.assertNotEqual(event['status'], 'pending')

    def test_entry_typo_support_keeps_ticker_and_strike_ambiguity_guard(self):
        fixtures = [
            'lets buy - 766 qqq calls for 47 cents.',
            'lets buy 762 maybe puts for 48 bents.',
            'lets buy - 499 calls for 47 cents.',
            'lets buy 1000 puts for 48 bents.',
            'lets buy 7715 puts for 29 centsz.',
        ]
        for text in fixtures:
            with self.subTest(text=text):
                event = self.observe(text)
                self.assertEqual(event['signal']['kind'], 'review')
                self.assertEqual(event['status'], 'review')

    def test_new_templates_cannot_bypass_stale_or_first_seen_edited_guards(self):
        fixtures = [
            'SL triggered at 22 cents.',
            'cut the puts att 11 cents.',
            'added 50 more at 35 cent',
            'sold130',
            '4. lets buy 778 spy calls for 29 centsz.',
        ]
        for text in fixtures:
            with self.subTest(text=text, guard='edited'):
                event = self.observe(text, edited=True)
                self.assertEqual(event['status'], 'review')
                self.assertEqual(event['reason'], 'First seen after an edit.')
            with self.subTest(text=text, guard='stale'):
                event = self.observe(text, stamp=NOW - timedelta(seconds=61))
                self.assertEqual(event['status'], 'skipped')

    def test_duplicate_observation_cannot_release_first_seen_edited_message(self):
        for text in ['SL triggered at 22 cents.',
                     'added 50 more at 35 cent', 'sold130',
                     'target hit', 'added 50 more at 35 cents']:
            with self.subTest(text=text):
                self.next_id += 1
                mid = self.next_id
                self.engine.observe(mid, text, NOW, NOW, edited=True)
                # Retain the edit rejection even if a later delivery omits the flag.
                self.engine.observe(mid, text, NOW, NOW, edited=False)
                event = self.engine.state['events'][str(mid)]
                self.assertEqual(event['status'], 'review')
                self.assertEqual(event['reason'], 'First seen after an edit.')
                self.assertEqual(self.engine.state['orders'], {})

    def test_duplicate_observation_cannot_release_ownership_review(self):
        for text in ['target hit', 'SL triggered at 22 cents.', 'sold130']:
            with self.subTest(text=text):
                event = self.observe(text)
                mid = self.next_id
                # Emulate the durable result of the execution ownership guard;
                # exercising that guard's broker calls is outside parser tests.
                self.engine.finish(event, 'review', 'No single matching bridge-owned position.')
                self.engine.observe(mid, text, NOW, NOW + timedelta(seconds=30))
                self.assertEqual(event['status'], 'review')
                self.assertEqual(event['reason'], 'No single matching bridge-owned position.')
                self.assertEqual(self.engine.state['orders'], {})


if __name__ == '__main__':
    unittest.main()
