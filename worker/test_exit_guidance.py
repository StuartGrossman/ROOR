import unittest
from datetime import timedelta
from decimal import Decimal
from test_matrix_policy import MatrixPolicyTests, RULES
from matrix_execution import MatrixEngine
from test_execution import NOW

class ExitGuidanceTests(unittest.TestCase):
    tearDown=MatrixPolicyTests.tearDown
    alert=MatrixPolicyTests.alert
    # Only inherit setup/helpers, not the legacy strategy test cases.
    def setUp(self):
        MatrixPolicyTests.setUp(self)
        original=self.broker.contract
        def contract(signal):
            result=original(signal)
            result['symbol']=f"SPY261013{signal['right'][0].upper()}{int(Decimal(signal['strike'])*1000):08d}"
            return result
        self.broker.contract=contract
        self.rules={**RULES,'callout_exits_only':True,'stop_loss':None,'addon_stop_loss':None}
        self.engine=MatrixEngine(self.engine.path,self.broker,self.rules)
    def test_no_automatic_stop_or_time_exit(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.broker.bid='.01';self.engine.tick(NOW)
        self.engine.tick(NOW+timedelta(hours=3))
        self.assertEqual([x['side'] for x in self.broker.calls],['buy'])
        self.assertEqual(next(iter(self.engine.state['exit_watch'].values()))['classification'],'missing_exit_guidance')
    def test_green_guidance_waits_then_sells_during_risk_only_tick(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'when you see green sell, don\'t wait for me',1)
        self.assertEqual(len(self.broker.calls),1)
        self.engine=MatrixEngine(self.engine.path,self.broker,self.rules)
        self.broker.bid='.45';self.broker.at=NOW+timedelta(seconds=90)
        self.engine.tick(self.broker.at,process_pending=False)
        self.assertEqual(self.broker.calls[-1]['side'],'sell')
        self.assertEqual(self.engine.state['events']['2']['execution_status'],'filled')
    def test_stale_or_insufficient_bid_does_not_trigger(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'sell when you see green',1)
        self.broker.bid='.50';self.engine.tick(NOW+timedelta(seconds=10))
        self.assertEqual(len(self.broker.calls),1)
    def test_undefined_condition_flagged(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'get ready to sell 600 calls on any sign of pullback',1)
        self.assertEqual(self.engine.state['events']['2']['guidance_class'],'conditional_exit_undefined')
        self.assertEqual(len(self.broker.calls),1)
    def test_reply_can_target_older_position(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'lets buy 601 spy calls for 39 cents.',1)
        now=NOW+timedelta(seconds=2);self.broker.at=now
        self.engine.observe(3,'sold',now,now,reply_to=1);self.engine.tick(now)
        self.assertTrue(self.broker.calls[-1]['symbol'].endswith('00600000'))
    def test_conditional_does_not_retarget_new_entry(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'sell when you see green',1)
        self.alert(3,'lets buy 601 spy calls for 39 cents.',2)
        self.broker.bid='.50';self.broker.at=NOW+timedelta(seconds=3);self.engine.tick(self.broker.at)
        self.assertTrue(self.broker.calls[-1]['symbol'].endswith('00600000'))
    def test_rejected_sell_flagged_no_duplicate(self):
        from execution import RejectedOrder
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.broker.submit=lambda _:(_ for _ in ()).throw(RejectedOrder())
        self.alert(2,'sold',1);self.engine.tick(self.broker.at)
        self.assertEqual(self.engine.state['events']['2']['execution_status'],'execution_failure')
        self.assertEqual(next(iter(self.engine.state['exit_watch'].values()))['classification'],'execution_failure')

    def test_partial_fill_stays_open_until_confirmed_after_restart(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        original=self.broker.submit
        def partial(payload):
            result=original(payload)
            if payload['side']=='sell':result.update(status='partially_filled',filled_qty='1')
            return result
        self.broker.submit=partial
        self.alert(2,'sold',1)
        self.assertEqual(self.engine.state['events']['2']['execution_status'],'fill_pending')
        self.assertEqual(next(iter(self.engine.state['exit_watch'].values()))['classification'],'partial_fill')
        self.engine=MatrixEngine(self.engine.path,self.broker,self.rules)
        order=self.broker.orders['geerawr-2-exit'];order.update(status='filled',filled_qty=str(order['qty']))
        self.engine.tick(self.broker.at)
        self.assertEqual(self.engine.state['events']['2']['execution_status'],'filled')
        self.assertEqual(self.engine.state['exit_watch'],{})
        self.assertEqual(len([x for x in self.broker.calls if x['side']=='sell']),1)
    def test_quote_size_must_cover_whole_position(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'sell when you see green',1)
        original=self.broker.full_quote
        self.broker.full_quote=lambda symbol:{**original(symbol),'bid_size':1}
        self.broker.bid='.50';self.engine.tick(self.broker.at)
        self.assertEqual(len(self.broker.calls),1)
    def test_choice_sequence_under_new_rules(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        for i,text in enumerate(['get ready to sell','or','risk it all and hold','your choice'],2):
            self.alert(i,text,i)
        self.assertEqual(self.broker.calls[-1]['side'],'sell')
    def test_warning_then_now_executes_but_now_alone_does_not(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'now',1);self.assertEqual(len(self.broker.calls),1)
        self.alert(3,'get ready to sell',2);self.assertEqual(len(self.broker.calls),1)
        self.alert(4,'now',3);self.assertEqual(self.broker.calls[-1]['side'],'sell')
    def test_uncopied_latest_source_is_reviewed(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'lets buy 601 spy calls for 999 cents.',1)
        self.alert(3,'sell when you see green',2)
        self.assertEqual(self.engine.state['events']['3']['guidance_class'],'position_matching_miss')
        self.assertEqual(len(self.broker.calls),1)
    def test_old_or_edited_guidance_does_not_arm(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.engine.observe(2,'sell when you see green',NOW,NOW+timedelta(seconds=100))
        self.engine.observe(3,'sell when you see green',NOW,NOW,edited=True)
        self.broker.bid='.50';self.engine.tick(NOW)
        self.assertEqual(len(self.broker.calls),1)
    def test_unrecognized_sell_wording_is_visible_for_review(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'sell half of the calls please',1)
        event=self.engine.state['events']['2']
        self.assertEqual(event['guidance_class'],'parser_miss')
        self.assertEqual(event['status'],'review')
        self.assertEqual(len(self.broker.calls),1)
    def test_edited_condition_is_disarmed(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.alert(2,'sell when you see green',1)
        self.engine.observe(2,'do not sell',NOW+timedelta(seconds=1),NOW+timedelta(seconds=2),edited=True)
        self.broker.bid='.50';self.broker.at=NOW+timedelta(seconds=2);self.engine.tick(self.broker.at)
        self.assertEqual(len(self.broker.calls),1)
    def test_questions_and_third_party_green_reports_never_arm(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        for i,text in enumerate(['sell when you see green?', 'he said sell when you see green', 'do not sell when you see green'],2):
            self.alert(i,text,i)
        self.broker.bid='.50';self.engine.tick(self.broker.at)
        self.assertEqual(len(self.broker.calls),1)
    def test_invalid_fill_cost_never_triggers_profit_condition(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        self.engine.state['orders']['geerawr-1-entry']['filled_avg_price']='0'
        self.alert(2,'sell when you see green',1)
        self.broker.bid='.50';self.engine.tick(self.broker.at)
        self.assertEqual(len(self.broker.calls),1)
    def test_green_cancels_pending_buy_before_exit(self):
        self.alert(1,'lets buy 600 spy calls for 39 cents.')
        order=self.broker.orders['geerawr-1-entry']
        order.update(status='partially_filled',filled_qty='1')
        self.engine.state['orders']['geerawr-1-entry'].update(status='partially_filled',filled_qty='1')
        self.alert(2,'sell when you see green',1)
        self.broker.bid='.50';self.engine.tick(self.broker.at)
        self.engine.tick(self.broker.at)
        self.assertEqual(order['status'],'canceled')
        self.assertEqual(self.broker.calls[-1]['side'],'sell')
        self.assertEqual(self.broker.calls[-1]['qty'],1)
