"""Observed weekly gaps: exact grammar, per-contract actions, and restart safety."""
import json,tempfile,unittest
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from signals import parse_signal
from matrix_execution import MatrixEngine
from test_execution import NOW
from test_matrix_policy import QuotedBroker,DISCOUNT_RULES

TEXT='added 100 more to the 600 puts at 21 cents.\nadded 50 more to the 601 puts at 56 cents.'
class MultiBroker(QuotedBroker):
    def buying_power(self):return Decimal('5000')
    def contract(self,s):
        return dict(symbol=f"SPY261013{s['right'][0].upper()}{int(Decimal(s['strike'])*1000):08d}",size=100,right=s['right'],strike=s['strike'],expiration=s['expiration'])

class ThreeGapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.b=MultiBroker();self.rules={**DISCOUNT_RULES,'entry_mode':'signal_limit','entry_discount':0};self.path=Path(self.tmp.name)/'state.json';self.e=MatrixEngine(self.path,self.b,self.rules)
    def tearDown(self):self.tmp.cleanup()
    def observe(self,mid,text,sec=0,**kw):
        now=NOW+timedelta(seconds=sec);self.b.at=now;self.e.observe(mid,text,now,now,**kw);return now
    def entries(self):
        for mid,strike in [(1,600),(2,601)]:
            now=self.observe(mid,f'lets buy {strike} spy puts for 39 cents.',mid);self.e.tick(now)
    def test_bare_cut_is_a_contextual_exit(self):
        self.assertEqual(parse_signal('cut',NOW.isoformat())['kind'],'exit')
        now=self.observe(1,'lets buy 600 spy calls for 39 cents.');self.e.tick(now)
        now=self.observe(2,'cut',1);self.e.tick(now)
        self.assertEqual(self.b.calls[-1]['side'],'sell')
    def test_missing_cent_unit_add_uses_own_budget(self):
        now=self.observe(1,'lets buy 600 spy puts for 39 cents.');self.e.tick(now)
        now=self.observe(2,'added 50 more at 59',1);self.e.tick(now)
        self.assertEqual(self.e.state['events']['2']['signal']['kind'],'add')
        self.assertEqual((self.b.calls[-1]['side'],self.b.calls[-1]['qty']),('buy',10))
    def test_batch_observation_is_two_directed_adds(self):
        self.observe(10,TEXT);event=self.e.state['events']['10']
        self.assertEqual(event['signal']['kind'],'add_batch')
        self.assertEqual([(x['strike'],x['right']) for x in event['signal']['legs']],[('600','put'),('601','put')])
        self.assertEqual(self.b.calls,[])
    def test_batch_executes_once_for_each_owned_contract(self):
        self.entries();now=self.observe(10,TEXT,3);self.e.tick(now)
        adds=[x for x in self.b.calls if '-leg-' in x['client_order_id']]
        self.assertEqual(len(adds),2);self.assertEqual([x['qty'] for x in adds],[10,10]);self.assertEqual(len({x['symbol'] for x in adds}),2)
        self.assertTrue(all(o['event_id']=='10' for o in self.e.state['orders'].values() if '-leg-' in o['payload']['client_order_id']))
    def test_batch_restart_and_redelivery_cannot_duplicate(self):
        self.entries();now=self.observe(10,TEXT,3);self.e.tick(now);count=len(self.b.calls)
        self.e=MatrixEngine(self.path,self.b,self.rules);self.e.observe(10,TEXT,now,now);self.e.tick(now)
        self.assertEqual(len(self.b.calls),count)
    def test_unknown_first_leg_blocks_second_until_reconciled(self):
        self.entries();self.b.uncertain=True;now=self.observe(10,TEXT,3);self.e.tick(now)
        self.assertEqual(len([x for x in self.b.calls if '-leg-' in x['client_order_id']]),1)
        # The subsequent broker lookup includes the accepted fill details even
        # though the original submission acknowledgement was lost.
        for order in self.b.orders.values():
            order.setdefault('filled_avg_price',order.get('limit_price','.40'))
            order.setdefault('filled_at',now.isoformat())
        self.b.uncertain=False;self.e=MatrixEngine(self.path,self.b,self.rules);self.e.tick(now)
        self.assertEqual(len([x for x in self.b.calls if '-leg-' in x['client_order_id']]),2)
    def test_batch_retains_add_cap(self):
        self.entries();now=self.observe(10,TEXT,3);self.e.tick(now);count=len(self.b.calls)
        now=self.observe(11,TEXT,200);self.e.tick(now);self.assertEqual(len(self.b.calls),count)
    def test_no_owned_contract_does_not_create_standalone_batch_buy(self):
        now=self.observe(10,TEXT);self.e.tick(now);self.assertEqual(self.b.calls,[])
    def test_bare_cut_uses_latest_source_context_with_two_owned_contracts(self):
        self.entries();now=self.observe(10,'cut',3);self.e.tick(now)
        self.assertEqual(self.b.calls[-1]['side'],'sell')
        self.assertTrue(self.b.calls[-1]['symbol'].endswith('00601000'))
        self.assertGreater(self.e.owned(list(self.e.state['contracts'])[0]),0)
    def test_missing_unit_add_uses_latest_source_context_with_two_owned_contracts(self):
        self.entries();now=self.observe(10,'added 50 more at 59',3);self.e.tick(now)
        self.assertEqual(self.b.calls[-1]['client_order_id'],'geerawr-10-add')
        self.assertTrue(self.b.calls[-1]['symbol'].endswith('00601000'))
    def test_single_owned_batch_leg_cannot_buy_the_unowned_contract(self):
        now=self.observe(1,'lets buy 600 spy puts for 39 cents.');self.e.tick(now)
        now=self.observe(10,TEXT,3);self.e.tick(now)
        adds=[x for x in self.b.calls if '-leg-' in x['client_order_id']]
        self.assertEqual(len(adds),1)
        self.assertTrue(adds[0]['symbol'].endswith('00600000'))
    def test_duplicate_delivery_before_processing_retains_same_batch(self):
        self.entries();now=self.observe(10,TEXT,3);self.e.observe(10,TEXT,now,now);self.e.tick(now)
        self.assertEqual(len([x for x in self.b.calls if '-leg-' in x['client_order_id']]),2)

    def test_observed_out_confirmations_are_exits(self):
        for mid,text in enumerate(['im outz','im outs','im outz guys',"I'm outz",'outz'],50):
            self.observe(mid,text)
            self.assertEqual(self.e.state['events'][str(mid)]['signal']['kind'],'exit')
            self.assertEqual(self.e.state['events'][str(mid)]['status'],'pending')
    def test_milestones_preparations_and_ambiguous_now_are_not_sells(self):
        for mid,text in enumerate(['target reached','target hit','🎯','get ready to sell',
                                   'get ready to sell the move','now.',
                                   'I want no bitchin, that you didn’t know when to sell',
                                   'calls printing','im outz?'],70):
            self.observe(mid,text)
            self.assertNotEqual(self.e.state['events'][str(mid)]['signal']['kind'],'exit')
            self.assertNotEqual(self.e.state['events'][str(mid)]['status'],'pending')

    def test_exact_short_duplicate_entry_does_not_block_following_sell(self):
        text='lets buy 600 spy puts for 39 cents.'
        now=self.observe(1,text);self.e.tick(now)
        now=self.observe(2,text,30);self.e.tick(now)
        self.assertEqual(self.e.state['events']['2'].get('duplicate_of'),'1')
        now=self.observe(3,'sold',31);self.e.tick(now)
        self.assertEqual(self.b.calls[-1]['side'],'sell')
    def test_changed_entry_premium_remains_an_uncopied_entry_guard(self):
        now=self.observe(1,'lets buy 600 spy puts for 39 cents.');self.e.tick(now)
        now=self.observe(2,'lets buy 600 spy puts for 40 cents.',1);self.e.tick(now)
        self.assertNotIn('duplicate_of',self.e.state['events']['2'])
        self.assertTrue(self.e.state['events']['2']['entry_like'])
    def test_later_same_text_is_not_assumed_a_duplicate(self):
        text='lets buy 600 spy puts for 39 cents.'
        now=self.observe(1,text);self.e.tick(now)
        now=self.observe(2,text,31);self.e.tick(now)
        self.assertNotIn('duplicate_of',self.e.state['events']['2'])
        self.assertTrue(self.e.state['events']['2']['entry_like'])

    def test_partial_or_question_batch_is_not_executable(self):
        for i,text in enumerate([TEXT+'?',TEXT+'\nmaybe add later',TEXT.splitlines()[0]+'\nwhat about the other one?',TEXT.splitlines()[0]+'\n'+TEXT.splitlines()[0]],10):
            self.observe(i,text);self.assertNotEqual(self.e.state['events'][str(i)]['signal']['kind'],'add_batch')
    def test_stale_and_edited_new_formats_cannot_become_pending(self):
        for i,text in enumerate(['cut','added 50 more at 59',TEXT],10):
            self.e.observe(i,text,NOW,NOW+timedelta(seconds=61));self.assertNotEqual(self.e.state['events'][str(i)]['status'],'pending')
            self.e.observe(i+10,text,NOW,NOW,edited=True);self.assertNotEqual(self.e.state['events'][str(i+10)]['status'],'pending')
    def test_commentary_and_invalid_add_values_are_not_actions(self):
        for i,text in enumerate(['should we cut?','if it drops cut','added 50 more at 59?','I added 50 more at 59','added 50 more at 0','added 0 more at 59'],30):
            self.observe(i,text);self.assertNotIn(self.e.state['events'][str(i)]['signal']['kind'],{'add','exit','add_batch'})
    def test_edit_cancels_unfilled_batch_orders_through_parent(self):
        self.entries();original=self.b.submit
        def unfilled(payload):
            r=original(payload)
            if '-leg-' in payload['client_order_id']:r.update(status='new',filled_qty='0');self.b.orders[payload['client_order_id']].update(status='new',filled_qty='0')
            return r
        self.b.submit=unfilled;now=self.observe(10,TEXT,3);self.e.tick(now)
        self.e.observe(10,TEXT+' changed',now,now,edited=True);self.e.tick(now)
        pending=[o for cid,o in self.b.orders.items() if '-leg-' in cid]
        self.assertTrue(pending);self.assertTrue(all(o['status']=='canceled' for o in pending))

if __name__=='__main__':unittest.main()
