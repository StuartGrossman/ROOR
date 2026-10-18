import unittest,json
from datetime import timedelta
from decimal import Decimal
from test_matrix_policy import MatrixPolicyTests,RULES
from matrix_execution import MatrixEngine
from test_execution import NOW

class ExpiryTests(unittest.TestCase):
    tearDown=MatrixPolicyTests.tearDown
    alert=MatrixPolicyTests.alert
    def setUp(self):
        MatrixPolicyTests.setUp(self)
        self.rules={**RULES,'callout_exits_only':True,'stop_loss':None,'addon_stop_loss':None}
        self.engine=MatrixEngine(self.engine.path,self.broker,self.rules)
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.symbol=self.broker.calls[0]['symbol'];self.qty=self.engine.owned(self.symbol)
        self.activities=[];self.broker.account_activities=lambda after:self.activities
        self.broker.activity_order=lambda oid:{'id':oid,'symbol':self.symbol,'side':'sell','client_order_id':'broker-generated-order','filled_qty':str(self.qty)}
    def expiry(self,**kw):
        return dict(id='expiry-1',activity_type='OPEXP',date='2026-10-13',symbol=self.symbol,qty=str(-self.qty),net_amount='0',status='executed')|kw
    def reconcile(self):self.engine.reconcile_broker_events({},NOW+timedelta(days=1))
    def test_proven_zero_expiry_removes_remaining_without_order_or_proceeds(self):
        self.activities=[self.expiry()];cash=self.engine.available();self.reconcile()
        self.assertEqual(self.engine.owned(self.symbol),0)
        self.assertEqual(self.engine.remaining('geerawr-1-entry'),0)
        self.assertEqual(self.engine.available(),cash)
        self.assertEqual(len(self.broker.calls),1)
    def test_restart_does_not_double_count_expiry(self):
        self.activities=[self.expiry()];self.reconcile()
        self.engine=MatrixEngine(self.engine.path,self.broker,self.rules);self.reconcile()
        self.assertEqual(len(self.engine.state['broker_dispositions']),1)
        self.assertEqual(self.engine.owned(self.symbol),0)
    def test_absent_position_without_activity_is_not_assumed_zero(self):
        self.reconcile();self.assertEqual(self.engine.owned(self.symbol),self.qty)
    def test_pending_or_wrong_sign_or_wrong_symbol_expiry_rejected(self):
        for delta in [{'status':'pending'},{'qty':str(self.qty)},{'symbol':'OTHER'},{'net_amount':'10'},{'date':'2026-10-12'}]:
            self.activities=[self.expiry(**delta)];self.reconcile();self.assertEqual(self.engine.owned(self.symbol),self.qty)
    def test_exercise_removes_option_but_blocks_new_risk(self):
        self.activities=[self.expiry(activity_type='OPEXC')];self.reconcile()
        self.assertEqual(self.engine.owned(self.symbol),0)
        self.assertTrue(self.engine.state['broker_lifecycle_reviews'])
        self.assertEqual(self.engine.available(),0)
    def test_external_sell_uses_verified_fill_price(self):
        self.activities=[dict(id='fill-1',activity_type='FILL',symbol=self.symbol,qty=str(self.qty),price='.50',side='sell',order_id='broker-order',transaction_time=(NOW+timedelta(minutes=2)).isoformat())]
        self.broker.buying_power=lambda:Decimal('5000')
        before=self.engine.available();self.reconcile()
        self.assertEqual(self.engine.owned(self.symbol),0)
        self.assertEqual(self.engine.available()-before,Decimal(self.qty)*Decimal('49.90'))
    def test_fill_for_our_existing_order_is_not_double_counted(self):
        self.broker.activity_order=lambda oid:{'symbol':self.symbol,'side':'sell','client_order_id':'geerawr-2-exit','filled_qty':str(self.qty)}
        self.activities=[dict(id='fill-2',activity_type='FILL',symbol=self.symbol,qty=str(self.qty),price='.50',side='sell',order_id='owned',transaction_time=(NOW+timedelta(minutes=2)).isoformat())]
        self.reconcile();self.assertEqual(self.engine.owned(self.symbol),self.qty)
    def test_fresh_edited_sell_is_reduce_only_at_observation_time(self):
        now=NOW+timedelta(seconds=20);self.broker.at=now
        self.engine.observe(2,'sold',NOW+timedelta(seconds=10),now,edited=True,edited_at=now)
        self.engine.tick(now)
        self.assertEqual(self.broker.calls[-1]['side'],'sell')
        self.assertTrue(self.engine.state['events']['2']['reduce_only_edited_exit'])
    def test_stale_edited_sell_and_edited_entry_stay_in_review(self):
        now=NOW+timedelta(seconds=100);self.broker.at=now
        self.engine.observe(2,'sold',NOW,now,edited=True,edited_at=now)
        self.engine.observe(3,'lets buy 601 spy calls for 39 cents.',now,now,edited=True,edited_at=now)
        self.engine.tick(now)
        self.assertEqual(len(self.broker.calls),1)
    def test_missing_event_on_audit_still_pauses(self):
        import service
        from unittest.mock import patch
        self.broker.request=lambda method,path:[]
        with patch.object(service,'utcnow',return_value=NOW+timedelta(days=1)):
            with self.assertRaisesRegex(RuntimeError,'Paper position/journal mismatch'):service.audit_positions(self.engine)
    def test_proven_expiry_allows_audit_to_resume(self):
        import service
        from unittest.mock import patch
        self.broker.request=lambda method,path:[];self.activities=[self.expiry()]
        with patch.object(service,'utcnow',return_value=NOW+timedelta(days=1)):service.audit_positions(self.engine)
        self.assertEqual(self.engine.owned(self.symbol),0)
    def test_edited_comment_can_become_a_fresh_reduce_only_sell(self):
        self.engine.observe(2,'getting close',NOW+timedelta(seconds=1),NOW+timedelta(seconds=1))
        now=NOW+timedelta(seconds=20);self.broker.at=now
        self.engine.observe(2,'sold',NOW+timedelta(seconds=1),now,edited=True,edited_at=now);self.engine.tick(now)
        self.assertEqual(self.broker.calls[-1]['side'],'sell')
    def test_multiple_trade_quantities_cannot_be_overallocated(self):
        self.activities=[self.expiry(qty=str(-self.qty-1))];self.reconcile()
        self.assertEqual(self.engine.owned(self.symbol),self.qty)
    def test_fresh_edited_sell_preserves_reply_target(self):
        now=NOW+timedelta(seconds=20);self.broker.at=now
        self.engine.observe(2,'sold',NOW+timedelta(seconds=10),now,edited=True,edited_at=now,reply_to=999)
        self.engine.tick(now)
        self.assertEqual(len(self.broker.calls),1)
        self.assertEqual(self.engine.state['events']['2']['status'],'review')
    def test_activity_pagination_fetches_second_page(self):
        from broker import PaperBroker
        broker=PaperBroker.__new__(PaperBroker);calls=[]
        def request(method,path,params):
            calls.append(params)
            return [{'id':str(i)} for i in range(100)] if 'page_token' not in params else [{'id':'last'}]
        broker.request=request
        self.assertEqual(len(broker.account_activities('2026-10-13')),101)
        self.assertEqual(calls[1]['page_token'],'99')
    def test_activity_pagination_refuses_repeating_token(self):
        from broker import PaperBroker
        broker=PaperBroker.__new__(PaperBroker)
        broker.request=lambda *args,**kw:[{'id':'repeat'} for _ in range(100)]
        with self.assertRaisesRegex(RuntimeError,'pagination'):broker.account_activities('2026-10-13')
    def test_fresh_edited_green_condition_watches_until_profitable(self):
        now=NOW+timedelta(seconds=20);self.broker.at=now
        self.engine.observe(2,'sell when you see green',NOW+timedelta(seconds=10),now,edited=True,edited_at=now)
        self.engine.tick(now);self.assertEqual(len(self.broker.calls),1)
        self.broker.bid='.50';self.broker.at=NOW+timedelta(seconds=100);self.engine.tick(self.broker.at,process_pending=False)
        self.assertEqual(self.broker.calls[-1]['side'],'sell')
    def test_source_meh_gains_choice_is_an_exit(self):
        now=NOW+timedelta(seconds=20);self.broker.at=now
        self.engine.observe(2,'you can sell for meh gains or risk it all for itm.',NOW+timedelta(seconds=10),now,edited=True,edited_at=now)
        self.engine.tick(now);self.assertEqual(self.broker.calls[-1]['side'],'sell')
    def test_outz_with_stated_price_exits_at_current_market(self):
        now=NOW+timedelta(seconds=20);self.broker.at=now
        self.engine.observe(2,'outz at 59 cents',now,now);self.engine.tick(now)
        self.assertEqual(self.broker.calls[-1]['side'],'sell')
        self.assertEqual(self.engine.state['orders']['geerawr-2-exit']['filled_avg_price'],self.broker.bid)
