"""Persistent, fail-closed paper-order state machine. No stop-loss strategy."""

import hashlib
import json
import os
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from signals import parse_signal, size_entry

TERMINAL = {'filled', 'canceled', 'expired', 'rejected', 'replaced'}
NY = ZoneInfo('America/New_York')


class UncertainSubmission(Exception):
    """The broker may have received the order. Never retry the POST."""


class RejectedOrder(Exception):
    """Broker explicitly rejected the request."""


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as out:
        os.chmod(temp, 0o600)
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Engine:
    def __init__(self, path: Path, broker):
        self.path, self.broker = path, broker
        self.state = json.loads(path.read_text()) if path.exists() else {
            'version': 1, 'events': {}, 'orders': {}, 'contracts': {}, 'bursts': {}}
        if self.state['version'] != 1:
            raise RuntimeError('Unsupported execution state version.')

    def save(self):
        atomic_json(self.path, self.state)

    def observe(self, mid, text, stamp, now, *, edited=False, media=False, reply_to=None, edited_at=None):
        key = str(mid)
        digest = hashlib.sha256(text.encode()).hexdigest()
        old = self.state['events'].get(key)
        if old:
            if old['digest'] != digest:
                old.update(status='review', reason='Message changed; no new order.', edited=True)
                self.save()
            return
        signal = parse_signal(text, stamp.isoformat())
        entry_like = bool(re.match(r"(?:\d+[.)]\s*)?(?:let'?s buy|let sbuy)\b", ' '.join(text.replace('’', "'").lower().split())))
        event = {'digest': digest, 'signal': signal, 'sent_at': stamp.isoformat(),
                 'status': 'pending', 'entry_like': entry_like}
        if reply_to is not None:event['reply_to']=str(reply_to)
        age = (now - stamp).total_seconds()
        if edited:
            event.update(status='review', reason='First seen after an edit.', edited=True)
        elif media and not text:
            event.update(status='review', reason='Image-only alert needs review.')
        elif not -5 <= age <= 60:
            event.update(status='skipped', reason='Alert is older than 60 seconds or clock differs.')
        elif signal['kind'] in {'ignore', 'review'}:
            event.update(status=signal['kind'], reason=signal.get('reason', 'Commentary.'))
        self.state['events'][key] = event
        self.save()

    def owned(self, symbol):
        return sum(int(Decimal(o.get('filled_qty', '0'))) * (1 if o['payload']['side'] == 'buy' else -1)
                   for o in self.state['orders'].values() if o['payload']['symbol'] == symbol)

    def pending(self, symbol):
        return [o for o in self.state['orders'].values()
                if o['payload']['symbol'] == symbol and o['status'] not in TERMINAL]

    def reconcile(self):
        for cid, order in self.state['orders'].items():
            if order['status'] not in TERMINAL:
                found = self.broker.lookup(cid)
                if found:
                    self.update_order(order, found)
                    self.save()

    @staticmethod
    def update_order(order, result):
        order.update(id=result['id'], status=result['status'], filled_qty=str(result['filled_qty']))

    def finish(self, event, status, reason):
        event.update(status=status, reason=reason)
        self.save()

    def tick(self, now, *, process_pending=True):
        self.reconcile()
        # Cancel still-open buys after alert edits or their saved lifetime. Filled
        # positions are never sold because of elapsed time or a price change.
        for order in self.state['orders'].values():
            if order['status'] in TERMINAL or order['payload']['side'] != 'buy' or not order.get('id'):
                continue
            event = self.state['events'][order['event_id']]
            lifetime = order.get('lifetime_seconds', 60)
            if (event.get('edited') and not event.get('actionable_edited_entry')) or (now - datetime.fromisoformat(order['created_at'])).total_seconds() >= lifetime:
                self.broker.cancel(order['id'])
        self.reconcile()
        if any(o['status'] in {'submitting', 'unknown'} for o in self.state['orders'].values()):
            return
        for mid, event in self.state['events'].items():
            if event['status'] != 'pending':
                continue
            # Risk-only checks still finish exits already awaiting buy cancellation.
            if not process_pending and not (event['signal']['kind'] == 'exit' and event.get('exit_started')):
                continue
            if (now - datetime.fromisoformat(event['sent_at'])).total_seconds() > 60 and not event.get('exit_started'):
                self.finish(event, 'skipped', 'Alert expired before execution.')
                continue
            self.execute(mid, event, now)
            if any(o['status'] in {'submitting', 'unknown'} for o in self.state['orders'].values()):
                break

    def execute(self, mid, event, now):
        signal, kind = event['signal'], event['signal']['kind']
        if not self.broker.is_open():
            return self.finish(event, 'skipped', 'Market is closed.')
        today = now.astimezone(NY).date().isoformat()
        if kind == 'entry':
            if signal['expiration'] != today:
                return self.finish(event, 'review', 'Expiration is not today.')
            contract = self.broker.contract(signal)
            if not contract:
                return self.finish(event, 'review', 'No unique tradable contract.')
            symbol = contract['symbol']
            if self.broker.position(symbol) != 0 or self.pending(symbol) or self.owned(symbol):
                return self.finish(event, 'review', 'Existing position or pending order in this contract.')
            self.state['contracts'][symbol] = contract
            price, budget = Decimal(signal['premium']), Decimal('200')
        else:
            # The channel may hold a trade we could not copy. A single position
            # in our account alone is not proof that a generic alert refers to it.
            for prior in self.state['events'].values():
                if prior is event:
                    break
                if not prior.get('entry_like') or datetime.fromisoformat(prior['sent_at']).astimezone(NY).date().isoformat() != today:
                    continue
                prior_order = self.state['orders'].get(prior.get('order_id'), {})
                copied = (Decimal(prior_order.get('filled_qty', '0')) > 0 or
                          bool(prior_order) and prior_order['status'] not in TERMINAL) and not prior.get('edited')
                right = prior['signal'].get('right')
                if not copied and (not signal.get('right') or not right or right == signal['right']):
                    return self.finish(event, 'review', 'Channel has an entry we could not copy; follow-up is ambiguous.')
            candidates = [symbol for symbol, c in self.state['contracts'].items()
                if c['expiration'] >= today and (self.owned(symbol) > 0 or self.pending(symbol))
                and (not signal.get('right') or c['right'] == signal['right'])
                and (not signal.get('strike') or Decimal(c['strike']) == Decimal(signal['strike']))]
            if len(candidates) != 1:
                return self.finish(event, 'review', 'No single matching position owned by this bridge.')
            symbol = candidates[0]
            contract = self.state['contracts'][symbol]
            if kind == 'exit':
                event['exit_started'] = True
                self.save()
                for order in self.pending(symbol):
                    if order['payload']['side'] == 'sell':
                        return self.finish(event, 'skipped', 'An exit is already pending.')
                    if order.get('id'):
                        self.broker.cancel(order['id'])
                self.reconcile()
                if self.pending(symbol):
                    return
                qty = self.owned(symbol)
                if self.broker.position(symbol) != qty or qty <= 0:
                    return self.finish(event, 'review', 'Position changed outside bridge, or entry never filled.')
                return self.submit(mid, event, symbol, qty, None, now)
            last = self.state['bursts'].get(symbol)
            stamp = datetime.fromisoformat(event['sent_at'])
            if last and 0 <= (stamp - datetime.fromisoformat(last)).total_seconds() <= 60:
                self.state['bursts'][symbol] = event['sent_at']
                return self.finish(event, 'skipped', 'Repeated instruction in the same add burst.')
            qty = self.owned(symbol)
            if self.pending(symbol) or qty <= 0 or self.broker.position(symbol) != qty:
                return self.finish(event, 'review', 'Position is unfilled, pending, or changed outside bridge.')
            quote = self.broker.quote(symbol)
            age = (now - datetime.fromisoformat(quote['timestamp'].replace('Z', '+00:00'))).total_seconds()
            if not -5 <= age <= 15:
                return self.finish(event, 'review', 'No fresh OPRA ask quote.')
            price, budget = Decimal(str(quote['ask'])), Decimal('100')
            self.state['bursts'][symbol] = event['sent_at']
        if not price.is_finite() or price <= 0 or price != price.quantize(Decimal('.01')):
            return self.finish(event, 'review', 'Invalid option premium.')
        qty, _ = size_entry(budget, price, int(contract['size']))
        if qty == 0:
            return self.finish(event, 'skipped', 'One whole contract exceeds the premium budget.')
        self.submit(mid, event, symbol, qty, price, now)

    def submit(self, mid, event, symbol, qty, price, now):
        cid = f'geerawr-{mid}-{event["signal"]["kind"]}'
        if cid in self.state['orders']:
            return self.finish(event, 'submitted', 'Order intent already exists; do not retry.')
        buying = price is not None
        payload = {'symbol': symbol, 'qty': qty, 'side': 'buy' if buying else 'sell',
                   'type': 'limit' if buying else 'market', 'time_in_force': 'day',
                   'position_intent': 'buy_to_open' if buying else 'sell_to_close',
                   'client_order_id': cid}
        if buying:
            payload['limit_price'] = str(price)
        order = {'payload': payload, 'status': 'submitting', 'filled_qty': '0',
                 'created_at': now.isoformat(), 'event_id': mid}
        self.state['orders'][cid] = order
        event.update(status='submitted', order_id=cid)
        self.save()  # Durable intent precedes the only possible POST.
        try:
            self.update_order(order, self.broker.submit(payload))
        except UncertainSubmission:
            order['status'] = 'unknown'
        except RejectedOrder:
            order['status'] = 'rejected'
            event.update(status='review', reason='Alpaca rejected the paper order.')
        self.save()
