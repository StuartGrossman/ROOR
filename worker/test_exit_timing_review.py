import unittest
from datetime import datetime, timedelta
import test_unified_regressions as fixtures
from test_matrix_policy import NOW
from broker import PaperBroker


class ExitTimingReview(unittest.TestCase):
    setUp = fixtures.UnifiedCalloutRegressions.setUp
    observe = fixtures.UnifiedCalloutRegressions.observe
    entry = fixtures.UnifiedCalloutRegressions.entry
    sells = fixtures.UnifiedCalloutRegressions.sells

    def test_fresh_sell_edit_on_older_post_closes_unchanged_source_context(self):
        self.entry()
        self.observe(2, 'sell the move guys', 100, posted=NOW+timedelta(seconds=5),
                     edited=True, edited_at=NOW+timedelta(seconds=99))
        self.assertEqual(len(self.sells()), 1)

    def test_fresh_sell_edit_cannot_attach_to_intervening_trade(self):
        self.entry()
        self.entry(3, 601, 50)
        self.observe(2, 'sell the move guys', 100, posted=NOW+timedelta(seconds=5),
                     edited=True, edited_at=NOW+timedelta(seconds=99))
        self.assertEqual(self.sells(), [])

    def test_delayed_delivery_of_an_old_edit_does_not_sell(self):
        self.entry()
        self.observe(2, 'sold', 200, posted=NOW+timedelta(seconds=5),
                     edited=True, edited_at=NOW+timedelta(seconds=99))
        self.assertEqual(self.sells(), [])

    def test_old_generic_edit_cannot_follow_intervening_named_focus(self):
        self.entry(1,600)
        self.entry(2,601,2)
        self.observe(4,'holding the 600 calls',50)
        self.observe(3,'sold',100,posted=NOW+timedelta(seconds=5),
                     edited=True,edited_at=NOW+timedelta(seconds=99))
        self.assertEqual(self.sells(),[])

    def test_spy_exit_uses_option_session_instead_of_stock_clock(self):
        self.entry()
        self.broker.is_open = lambda: False
        self.broker.option_exit_open = lambda symbol, now: symbol.startswith('SPY')
        self.observe(2, 'sold', 10)
        self.assertEqual(len(self.sells()), 1)

    def test_explicit_expire_worthless_is_recorded_as_intent_without_selling(self):
        self.entry()
        self.observe(2, 'im letting the 600 calls exp worthless', 5)
        self.assertEqual(self.engine.state['events']['2'].get('source_terminal_intent'),'hold_to_expiry')
        self.assertEqual(self.sells(),[])

    def test_profit_stop_typo_is_recognized_but_does_not_arm_a_loss_stop(self):
        from profit_policy import install
        install(self.engine,targets=False)
        self.entry()
        self.broker.bid='.60';self.broker.ask='.61'
        self.observe(2,'set prfit stops',5)
        self.assertEqual(len(self.sells()),1)


class OptionExitCalendarReview(unittest.TestCase):
    def broker(self, day, close='16:00'):
        b=object.__new__(PaperBroker)
        b.is_open=lambda:False
        b.request=lambda *a,**kw: [{'date':day,'open':'09:30','close':close}] if day else []
        return b

    def test_spy_sell_window_extends_fifteen_minutes_on_a_trading_date(self):
        b=self.broker('2026-09-02')
        self.assertTrue(b.option_exit_open('SPY260902C00766000',datetime.fromisoformat('2026-09-02T16:03:00-04:00')))
        self.assertFalse(b.option_exit_open('SPY260902C00766000',datetime.fromisoformat('2026-09-02T16:15:00-04:00')))

    def test_holiday_and_other_underlyings_do_not_get_spy_extension(self):
        now=datetime.fromisoformat('2026-09-07T16:03:00-04:00')
        self.assertFalse(self.broker(None).option_exit_open('SPY260907C00766000',now))
        self.assertFalse(self.broker('2026-09-07').option_exit_open('TSLA260907C00350000',now))

    def test_early_close_uses_calendar_not_fixed_four_pm(self):
        b=self.broker('2026-11-27','13:00')
        self.assertTrue(b.option_exit_open('SPY261127C00766000',datetime.fromisoformat('2026-11-27T13:05:00-05:00')))
        self.assertFalse(b.option_exit_open('SPY261127C00766000',datetime.fromisoformat('2026-11-27T16:03:00-05:00')))
