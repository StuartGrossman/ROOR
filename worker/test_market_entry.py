import tempfile,unittest
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from matrix_execution import MatrixEngine
from test_matrix_policy import QuotedBroker, DISCOUNT_RULES, NOW

class MarketEntryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.broker=QuotedBroker();self.broker.buying_power=lambda:Decimal('5000')
  self.rules={**DISCOUNT_RULES,'entry_mode':'market','entry_discount':0,'entry_budget':200,'addon_budget':400,'stop_loss':None,'addon_stop_loss':None,'callout_exits_only':True}
  self.engine=MatrixEngine(Path(self.tmp.name)/'state.json',self.broker,self.rules)
 def tearDown(self):self.tmp.cleanup()
 def test_market_entry_sizes_from_ask_and_sends_no_price_limit(self):
  self.broker.ask='.70';self.broker.bid='.69'
  self.engine.observe(1,'lets buy 600 spy calls for 39 cents.',NOW,NOW);self.engine.tick(NOW)
  order=self.broker.calls[0]
  self.assertEqual((order['type'],order['qty']),('market',2));self.assertNotIn('limit_price',order)
 def test_stale_quote_never_creates_entry(self):
  self.engine.observe(1,'lets buy 600 spy calls for 39 cents.',NOW,NOW)
  self.engine.tick(NOW+timedelta(seconds=5));self.assertEqual(self.broker.calls,[])

 def test_add_and_exit_are_market_and_no_automatic_stop(self):
  self.broker.ask='.40';self.broker.bid='.39'
  self.engine.observe(1,'lets buy 600 spy calls for 39 cents.',NOW,NOW);self.engine.tick(NOW)
  at=NOW+timedelta(seconds=1);self.broker.at=at
  self.engine.observe(2,'add',at,at);self.engine.tick(at)
  self.assertEqual((self.broker.calls[1]['type'],self.broker.calls[1]['qty']),('market',10))
  self.broker.bid='.01';self.engine.tick(at)
  self.assertEqual([o['side'] for o in self.broker.calls],['buy','buy'])
  self.engine.observe(3,'sell',at,at);self.engine.tick(at)
  self.assertEqual((self.broker.calls[-1]['side'],self.broker.calls[-1]['type']),('sell','market'))
 def test_profit_protection_sells_if_profitable_at_guidance(self):
  import profit_policy
  profit_policy.install(self.engine)
  self.broker.ask='.40';self.broker.bid='.39'
  self.engine.observe(1,'lets buy 600 spy calls for 39 cents.',NOW,NOW);self.engine.tick(NOW)
  at=NOW+timedelta(seconds=1);self.broker.at=at;self.broker.bid='.60'
  self.engine.observe(2,'set profit stops',at,at);self.engine.tick(at)
  self.assertEqual([o['side'] for o in self.broker.calls],['buy','sell'])
 def test_profit_guidance_that_is_already_red_never_sells(self):
  import profit_policy
  profit_policy.install(self.engine)
  self.engine.observe(1,'lets buy 600 spy calls for 39 cents.',NOW,NOW);self.engine.tick(NOW)
  at=NOW+timedelta(seconds=1);self.broker.at=at;self.broker.bid='.10'
  self.engine.observe(2,'set profit stops',at,at);self.engine.tick(at)
  self.assertEqual([o['side'] for o in self.broker.calls],['buy'])
  self.assertEqual(self.engine.state['events']['2']['profit_result'],'not_green_at_guidance')
