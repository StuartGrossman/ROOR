"""Read captured messages and write private proposed interpretations, never orders."""

import json
import os
from collections import Counter
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from archive import Archive
from signals import parse_signal, size_entry

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / 'work' / 'private'
SOURCE_CHAT_ID = -1004384036660


def configured_budgets(folder: Path) -> tuple[Decimal, Decimal, Decimal]:
    settings = json.loads((folder / 'settings.json').read_text())
    if settings.get('strategy_profile'):
        rules = json.loads((folder / settings['strategy_profile']).read_text())['rules']
        return (Decimal(str(rules['entry_budget'])), Decimal(str(rules['addon_budget'])),
                Decimal(str(rules['entry_discount'])))
    return (Decimal(settings['entry_premium_budget_usd']),
            Decimal(settings['addon_premium_budget_usd']), Decimal('0'))


def main():
    os.umask(0o077)
    budget, addon_budget, entry_discount = configured_budgets(Path(__file__).parent)
    archive = Archive(PRIVATE / 'messages.sqlite3')
    try:
        rows = archive.rows(SOURCE_CHAT_ID)
    finally:
        archive.close()
    # Interpret only the latest stored revision of each message.
    latest = {r['message_id']: r for r in rows}
    results = []
    counts = Counter()
    for row in latest.values():
        signal = parse_signal(row['text'], row['sent_at'])
        if not row['text'].strip() and row['media_type']:
            signal = {'kind': 'review', 'reason': 'Media-only post needs inspection.'}
        counts[signal['kind']] += 1
        if signal['kind'] == 'ignore':
            continue
        result = {'message_id': row['message_id'], 'sent_at': row['sent_at'],
                  'historical': bool(row['historical']), 'interpretation': signal,
                  'execution_enabled': False}
        if signal['kind'] == 'entry':
            limit_price = (Decimal(signal['premium']) * (1 - entry_discount)).quantize(
                Decimal('.01'), rounding=ROUND_DOWN)
            qty, premium_cost = size_entry(budget, limit_price, 100)
            result.update(proposed_quantity=qty, proposed_premium_cost=str(premium_cost),
                          proposed_limit_price=str(limit_price),
                          multiplier_assumption=100,
                          note='Verify actual contract size and tradability before any order.')
        elif signal['kind'] == 'add':
            result.update(premium_budget=str(addon_budget),
                          note='Requires an identified position, a fresh quote, and the confirmed rule for repeated add instructions.')
        results.append(result)
    dest = PRIVATE / 'interpreted-alerts.json'
    dest.write_text(json.dumps(results, indent=2))
    dest.chmod(0o600)
    print(json.dumps({'messages_reviewed': len(latest), 'classifications': dict(counts),
                      'entry_budget': str(budget), 'orders_submitted': 0}))


if __name__ == '__main__':
    main()
