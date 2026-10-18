import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from analyze import configured_budgets


class AnalyzeTests(unittest.TestCase):
    def test_active_profile_budgets_are_used_for_historical_interpretations(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'settings.json').write_text(json.dumps({'strategy_profile': 'matrix-profile.json'}))
            (folder / 'matrix-profile.json').write_text(json.dumps({'rules': {'entry_budget': 100, 'addon_budget': 400, 'entry_discount': .30}}))
            self.assertEqual(configured_budgets(folder), (Decimal('100'), Decimal('400'), Decimal('0.3')))

    def test_legacy_budgets_remain_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'settings.json').write_text(json.dumps({'entry_premium_budget_usd': '200.00', 'addon_premium_budget_usd': '100.00'}))
            self.assertEqual(configured_budgets(folder), (Decimal('200.00'), Decimal('100.00'), Decimal('0')))


if __name__ == '__main__':
    unittest.main()
