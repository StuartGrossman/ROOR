import unittest
from unittest.mock import Mock
import requests
from broker import PaperBroker
from execution import UncertainSubmission, RejectedOrder


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.b = PaperBroker({'key_id': 'test', 'secret_key': 'test'})
        self.b.session = Mock()
        self.b.session.request.return_value.text = ''

    def test_submit_uses_only_paper_endpoint_and_never_follows_redirects(self):
        self.b.session.request.return_value.status_code = 200
        self.b.session.request.return_value.json.return_value = {'id': 'x'}
        self.b.submit({'symbol': 'SPY', 'qty': 1})
        args, kwargs = self.b.session.request.call_args
        self.assertEqual(args[:2], ('POST', 'https://paper-api.alpaca.markets/v2/orders'))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], (3, 10))

    def test_timeout_never_retries_submission(self):
        self.b.session.request.side_effect = requests.Timeout()
        with self.assertRaises(UncertainSubmission):
            self.b.submit({})
        self.assertEqual(self.b.session.request.call_count, 1)

    def test_5xx_is_uncertain_but_422_is_rejected(self):
        for status, error in [(500, UncertainSubmission), (422, RejectedOrder), (302, UncertainSubmission)]:
            self.b.session.request.return_value.status_code = status
            with self.assertRaises(error):
                self.b.submit({})

    def test_lookup_404_means_absent(self):
        self.b.session.request.return_value.status_code = 404
        self.assertIsNone(self.b.lookup('test'))


if __name__ == '__main__':
    unittest.main()
