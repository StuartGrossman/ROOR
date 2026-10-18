import test_parser_three_gaps as helpers
import unittest
TEXT=helpers.TEXT
from matrix_execution import MatrixEngine
from decimal import Decimal

class SellContextTests(unittest.TestCase):
    setUp=helpers.ThreeGapTests.setUp
    tearDown=helpers.ThreeGapTests.tearDown
    observe=helpers.ThreeGapTests.observe
    entries=helpers.ThreeGapTests.entries
    def test_joint_context_sold_closes_both_once_after_restart(self):
        self.entries();now=self.observe(10,TEXT,3);self.e.tick(now)
        self.e=MatrixEngine(self.path,self.b,self.rules)
        now=self.observe(11,'sold',4);self.e.tick(now)
        sells=[o for o in self.b.calls if o['side']=='sell']
        self.assertEqual(len(sells),2)
        self.assertTrue(all(self.e.owned(o['symbol'])==0 for o in sells))
        now=self.observe(12,'im outz',5);self.e.tick(now)
        self.assertEqual(len([o for o in self.b.calls if o['side']=='sell']),2)
    def test_latest_source_exit_leaves_older_unsold_trade(self):
        self.entries();symbols=list(self.e.state['contracts'])
        now=self.observe(10,'sold',3);self.e.tick(now)
        self.assertGreater(self.e.owned(symbols[0]),0)
        self.assertEqual(self.e.owned(symbols[1]),0)
        now=self.observe(11,'im outz',4);self.e.tick(now)
        self.assertGreater(self.e.owned(symbols[0]),0)
    def test_generic_add_follows_latest_source_entry(self):
        self.entries();now=self.observe(10,'added 50 more at 59',3);self.e.tick(now)
        self.assertEqual(self.b.calls[-1]['client_order_id'],'geerawr-10-add')
        self.assertTrue(self.b.calls[-1]['symbol'].endswith('00601000'))
    def test_uncopied_latest_source_never_falls_back_to_old_position(self):
        now=self.observe(1,'lets buy 600 spy puts for 39 cents.');self.e.tick(now)
        now=self.observe(2,'lets buy 601 spy puts for 999 cents.',1);self.e.tick(now)
        count=len(self.b.calls);now=self.observe(3,'sold',2);self.e.tick(now)
        self.assertEqual(len(self.b.calls),count)
        self.assertEqual(self.e.state['events']['3']['status'],'review')
    def test_right_specific_exit_finds_latest_of_that_right(self):
        now=self.observe(1,'lets buy 600 spy calls for 39 cents.');self.e.tick(now)
        now=self.observe(2,'lets buy 601 spy puts for 39 cents.',1);self.e.tick(now)
        now=self.observe(3,'cut calls',2);self.e.tick(now)
        self.assertTrue(self.b.calls[-1]['symbol'].endswith('C00600000'))
    def test_single_leg_joint_exit_does_not_repeat_other_leg_after_restart(self):
        self.entries();now=self.observe(10,TEXT,3);self.e.tick(now)
        self.b.uncertain=True;now=self.observe(11,'sold',4);self.e.tick(now)
        self.assertEqual(len([o for o in self.b.calls if o['side']=='sell']),1)
        for o in self.b.orders.values():
            o.setdefault('filled_avg_price',o.get('limit_price','.40'));o.setdefault('filled_at',now.isoformat())
        self.b.uncertain=False;self.e=MatrixEngine(self.path,self.b,self.rules);self.e.tick(now)
        self.assertEqual(len([o for o in self.b.calls if o['side']=='sell']),2)

    def test_edited_latest_entry_blocks_fallback_to_an_older_trade(self):
        now=self.observe(1,'lets buy 600 spy puts for 39 cents.');self.e.tick(now)
        now=self.observe(2,'lets buy 601 spy puts for 39 cents.',1,edited=True);self.e.tick(now)
        count=len(self.b.calls);now=self.observe(3,'sold',2);self.e.tick(now)
        self.assertEqual(len(self.b.calls),count)
    def test_joint_exit_preserves_broker_ownership_check_per_leg(self):
        self.entries();now=self.observe(10,TEXT,3);self.e.tick(now)
        original=self.b.position;bad=list(self.e.state['contracts'])[1]
        self.b.position=lambda symbol: original(symbol)+1 if symbol==bad else original(symbol)
        now=self.observe(11,'sold',4);self.e.tick(now)
        sells=[o for o in self.b.calls if o['side']=='sell']
        self.assertEqual(len(sells),1);self.assertNotEqual(sells[0]['symbol'],bad)
