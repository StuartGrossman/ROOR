import unittest
import test_parser_three_gaps as helpers
from matrix_execution import MatrixEngine

class SellChoiceTests(unittest.TestCase):
    setUp=helpers.ThreeGapTests.setUp
    tearDown=helpers.ThreeGapTests.tearDown
    observe=helpers.ThreeGapTests.observe
    def entry(self,mid=1,sec=0):
        now=self.observe(mid,'lets buy 600 spy puts for 39 cents.',sec);self.e.tick(now)
    def sequence(self):
        self.entry()
        for mid,text,sec in [(2,'get ready to sell',1),(3,'or',3),(4,'risk it all and hold.',9)]:
            now=self.observe(mid,text,sec);self.e.tick(now)
    def test_choice_sequence_sells_at_choice_not_at_preparation(self):
        self.sequence();self.assertEqual(len(self.b.calls),1)
        now=self.observe(5,'your choice',14);self.e.tick(now)
        self.assertEqual(self.b.calls[-1]['side'],'sell')
        self.assertEqual(self.e.state['events']['5'].get('choice_exit'),True)
    def test_choice_sequence_survives_restart_and_does_not_duplicate(self):
        self.sequence();self.e=MatrixEngine(self.path,self.b,self.rules)
        now=self.observe(5,'your choice',14);self.e.tick(now)
        self.e=MatrixEngine(self.path,self.b,self.rules);self.e.observe(5,'your choice',now,now);self.e.tick(now)
        self.assertEqual(len([o for o in self.b.calls if o['side']=='sell']),1)
    def test_choice_alone_and_expired_preparation_do_not_sell(self):
        self.entry();now=self.observe(2,'your choice',1);self.e.tick(now);self.assertEqual(len(self.b.calls),1)
        self.sequence();now=self.observe(6,'your choice',70);self.e.tick(now);self.assertEqual(len(self.b.calls),1)
    def test_new_entry_breaks_the_old_choice_context(self):
        self.sequence();now=self.observe(5,'lets buy 601 spy puts for 39 cents.',10);self.e.tick(now)
        before=len(self.b.calls);now=self.observe(6,'your choice',14);self.e.tick(now);self.assertEqual(len(self.b.calls),before)
    def test_direct_sell_or_hold_choice_sells(self):
        self.entry();now=self.observe(2,'sell or hold, your choice',1);self.e.tick(now)
        self.assertEqual(self.b.calls[-1]['side'],'sell')
    def test_questions_and_edited_choices_do_not_sell(self):
        self.entry();now=self.observe(2,'sell or hold?',1);self.e.tick(now)
        now=self.observe(3,'sell or hold',2,edited=True);self.e.tick(now)
        self.assertEqual(len(self.b.calls),1)

    def test_edited_new_entry_still_breaks_choice_context(self):
        self.sequence();now=self.observe(5,'lets buy 601 spy puts for 39 cents.',10,edited=True);self.e.tick(now)
        count=len(self.b.calls);now=self.observe(6,'your choice',14);self.e.tick(now)
        self.assertEqual(len(self.b.calls),count)
