import json,tempfile,unittest
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from test_execution import Broker,NOW
from matrix_execution import MatrixEngine

RULES=dict(entry_budget=100,addon_budget=100,max_adds=2,stop_loss=.55,addon_stop_loss=.15,
           targets=[.1,.25],target_fractions=[.25,.25],lock_arm=1.,lock_retention=.9,
           lock_confirmations=2,lock_interval_ms=20000,burst_seconds=120,close_time='15:15:00',
           entry_mode='signal_limit',entry_discount=0,entry_lifetime_seconds=60)
DISCOUNT_RULES={**RULES, 'entry_budget':100, 'addon_budget':400, 'max_adds':1,
                'addon_stop_loss':.55, 'target_fractions':[0,0],
                'lock_arm':.75, 'lock_retention':.6, 'lock_interval_ms':10000,
                'entry_mode':'discount_limit', 'entry_discount':.30,
                'entry_lifetime_seconds':600, 'starting_capital':5000}

class QuotedBroker(Broker):
    def __init__(self):
        super().__init__();self.at=NOW;self.bid='.38'
    def buying_power(self):return Decimal('1000')
    def full_quote(self,symbol):return dict(bid=self.bid,ask=str(max(Decimal(self.bid)+Decimal('.01'),Decimal(self.ask))),bid_size=100,ask_size=100,timestamp=self.at.isoformat())
    def submit(self,payload):
        r=super().submit(payload)
        r.update(filled_avg_price=payload.get('limit_price',self.bid),filled_at=self.at.isoformat())
        return r

class MatrixPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.broker=QuotedBroker()
        self.engine=MatrixEngine(Path(self.tmp.name)/'state.json',self.broker,RULES)
    def tearDown(self):self.tmp.cleanup()
    def alert(self,i,text,seconds=0):
        now=NOW+timedelta(seconds=seconds);self.broker.at=now
        self.engine.observe(i,text,now,now);self.engine.tick(now)
    def test_entry_budget_is_100_and_two_distinct_adds_are_the_cap(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.assertEqual(self.broker.calls[0]['qty'],2)
        self.alert(2,'add',1);self.alert(3,'add',122);self.alert(4,'add',243)
        self.assertEqual(len([x for x in self.broker.calls if x['side']=='buy']),3)
    def test_add_stop_sells_only_add_lot(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.');self.alert(2,'add',1)
        self.broker.bid='.30';self.broker.at=NOW+timedelta(seconds=2);self.engine.tick(self.broker.at)
        self.assertEqual(self.broker.calls[-1]['side'],'sell')
        self.assertEqual(self.broker.calls[-1]['qty'],2)
        self.assertEqual(self.engine.owned('SPY261013C00600000'),2)
    def test_targets_then_two_profit_lock_confirmations_use_actual_fills(self):
        self.broker.bid='.09';self.alert(1,'lets buy 600 spy calls for 10 cents.')
        for seconds,bid in [(1,'.12'),(2,'.13'),(20,'.21'),(40,'.199'),(60,'.199')]:
            self.broker.bid=bid;self.broker.at=NOW+timedelta(seconds=seconds);self.engine.tick(self.broker.at)
        sales=[x['qty'] for x in self.broker.calls if x['side']=='sell']
        self.assertEqual(sales,[2,2,6])
    def test_restart_does_not_duplicate_a_completed_stop(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.broker.bid='.10';self.engine.tick(NOW)
        n=len(self.broker.calls)
        self.engine=MatrixEngine(self.engine.path,self.broker,RULES);self.engine.tick(NOW)
        self.assertEqual(len(self.broker.calls),n)
    def test_restart_manages_open_position_without_a_new_telegram_message(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.engine=MatrixEngine(self.engine.path,self.broker,RULES)
        self.broker.bid='.10';self.broker.at=NOW+timedelta(seconds=2)
        self.engine.tick(self.broker.at,process_pending=False)
        self.assertEqual((self.broker.calls[-1]['side'],self.broker.calls[-1]['qty']),('sell',2))
    def test_settings_pause_blocks_pending_entry_but_manages_open_position(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        now=NOW+timedelta(seconds=2);self.broker.at=now
        self.engine.observe(2,'lets buy 601 spy calls for 39 cents.',now,now)
        self.broker.bid='.10'
        self.engine.tick(now,process_pending=False)
        self.assertEqual(self.engine.state['events']['2']['status'],'pending')
        self.assertEqual([x['side'] for x in self.broker.calls],['buy','sell'])
    def test_uncertain_order_in_another_contract_does_not_block_open_lot_stop(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.engine.state['orders']['geerawr-unresolved-entry']={
            'payload':{'symbol':'SPY261013P00600000','side':'buy','qty':1,'limit_price':'0.30'},
            'status':'unknown','filled_qty':'0','created_at':NOW.isoformat(),
            'event_id':'unresolved','root_entry':'unresolved'}
        self.engine.save()
        self.broker.bid='.10';self.broker.at=NOW+timedelta(seconds=2)
        self.engine.tick(self.broker.at,process_pending=False)
        self.assertEqual((self.broker.calls[-1]['side'],self.broker.calls[-1]['qty']),('sell',2))
    def test_stale_quote_does_not_create_a_stop(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.broker.bid='.10';self.engine.tick(NOW+timedelta(seconds=10))
        self.assertEqual(len(self.broker.calls),1)
    def test_virtual_capital_blocks_an_unaffordable_buy(self):
        self.broker.buying_power=lambda:Decimal('20')
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.assertEqual(self.broker.calls,[])
    def test_partial_buy_is_canceled_before_stop_sale(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        o=self.broker.orders['geerawr-1-entry'];o.update(status='partially_filled',filled_qty='1')
        self.engine.state['orders']['geerawr-1-entry']['status']='partially_filled'
        self.broker.bid='.10';self.engine.tick(NOW)
        self.assertEqual(len(self.broker.calls),1)
        self.engine.tick(NOW)
        self.assertEqual((self.broker.calls[-1]['side'],self.broker.calls[-1]['qty']),('sell',1))
    def test_zero_bid_triggers_stop_but_does_not_supply_a_fill(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.broker.bid='0';self.engine.tick(NOW)
        self.assertEqual(len(self.broker.calls),1)
        self.broker.bid='.38';self.engine.tick(NOW)
        self.assertEqual(self.broker.calls[-1]['side'],'sell')
    def test_daily_close_works_during_quote_outage(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.broker.full_quote=lambda _:(_ for _ in ()).throw(RuntimeError('Quote outage'))
        self.broker.at=NOW+timedelta(seconds=8100);self.engine.tick(self.broker.at)
        self.assertEqual((self.broker.calls[-1]['side'],self.broker.calls[-1]['qty']),('sell',2))
    def test_daily_close_cancels_a_recent_unfilled_entry(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.',8090)
        o=self.broker.orders['geerawr-1-entry'];o.update(status='new',filled_qty='0')
        self.engine.state['orders']['geerawr-1-entry'].update(status='new',filled_qty='0')
        self.broker.at=NOW+timedelta(seconds=8100);self.engine.tick(self.broker.at)
        self.assertEqual(o['status'],'canceled')
        self.assertEqual(len(self.broker.calls),1)

    def test_discount_entry_uses_floor_cent_limit_and_small_opening(self):
        self.broker.buying_power=lambda:Decimal('5000')
        self.engine=MatrixEngine(self.engine.path,self.broker,DISCOUNT_RULES)
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        order=self.broker.calls[0]
        self.assertEqual((order['qty'],order['limit_price']),(3,'0.27'))
        self.assertEqual(self.engine.state['orders']['geerawr-1-entry']['lifetime_seconds'],600)
        self.assertEqual(self.engine.available(),Decimal('4918.70'))

    def test_discount_entry_rests_ten_minutes_then_cancels(self):
        self.broker.buying_power=lambda:Decimal('5000')
        self.engine=MatrixEngine(self.engine.path,self.broker,DISCOUNT_RULES)
        original_submit=self.broker.submit
        def unfilled(payload):
            order=original_submit(payload)
            order.update(status='new',filled_qty='0')
            return order
        self.broker.submit=unfilled
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.broker.at=NOW+timedelta(seconds=61)
        self.engine.tick(self.broker.at)
        self.assertEqual(self.broker.orders['geerawr-1-entry']['status'],'new')
        self.broker.at=NOW+timedelta(seconds=601)
        self.engine.tick(self.broker.at)
        self.assertEqual(self.broker.orders['geerawr-1-entry']['status'],'canceled')

    def test_discount_sequence_allows_one_400_dollar_add_without_fixed_targets(self):
        self.broker.buying_power=lambda:Decimal('5000')
        self.engine=MatrixEngine(self.engine.path,self.broker,DISCOUNT_RULES)
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'add',seconds=1)
        self.alert(3,'add',seconds=122)
        buys=[order for order in self.broker.calls if order['side']=='buy']
        self.assertEqual([(order['qty'],order['limit_price']) for order in buys],[(3,'0.27'),(10,'0.40')])
        self.assertEqual(self.engine.state['events']['3']['status'],'skipped')
        self.assertEqual(self.engine.state['events']['3']['reason'],'Filled add limit reached.')

if __name__=='__main__':unittest.main()
