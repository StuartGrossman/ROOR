"""Durable callout exit monitoring. Broker fills are the only closure evidence."""
import re
from datetime import datetime
from decimal import Decimal
from execution import NY, TERMINAL


def classify(text):
    text=' '.join(text.lower().replace('’',"'").split()).rstrip('.!')
    if '?' in text:return None
    if re.match(r'^(?:you can )?(?:sell when you see green|when you see green sell)\b',text):
        return 'conditional_profit'
    if re.search(r'\b(?:sell|cut|exit)\b',text) and re.search(r'\b(?:if|when|once|on any sign)\b',text):
        return 'conditional_exit_undefined'
    if re.search(r'\b(?:get ready to sell|prepare to sell|know the (?:fucking )?risk)\b',text):
        return 'exit_warning'
    if re.fullmatch(r'(?:sell|exit|cut)(?: it| them| all| everything)? now(?: guys)?',text):
        return 'actionable_exit'
    if text=='now':
        return 'contextual_now'
    return None


class ExitGuidanceMixin:
    def capture_guidance(self, event, text, now):
        category=classify(text)
        if category=='contextual_now':
            previous=[]
            for prior in self.state['events'].values():
                if prior is event:break
                previous.append(prior)
            if not previous:return False
            prior=previous[-1]
            age=(datetime.fromisoformat(event['sent_at'])-datetime.fromisoformat(prior['sent_at'])).total_seconds()
            if not 0<=age<=60 or prior.get('guidance_class')!='exit_warning':return False
            event['resolved_targets']=prior.get('resolved_targets',[])
            category='actionable_exit'
        if category is None:return False
        event['guidance_class']=category
        # Carry named contract selectors even when the base parser called this commentary.
        match=re.search(r'\b(\d+(?:\.\d+)?)\s*(?:spy\s*|spx\s*|tsla\s*)?(calls?|puts?)\b',text.lower())
        selector={'kind':'exit','right':None}
        if match:selector.update(strike=match[1],right=match[2].rstrip('s'))
        event['signal']=selector
        if category=='actionable_exit':
            event['status']='pending'
        else:
            targets=self.source_context(event) if not match else [selector]
            event['resolved_targets']=[dict(x) for x in targets]
            today=now.astimezone(NY).date().isoformat()
            event['matched_symbols']=self.resolve_followup(event,today)
            if not event['matched_symbols']:
                event.update(status='review',reason='Exit guidance has no unambiguous owned source target.',guidance_class='position_matching_miss')
            elif category=='conditional_profit':
                event.update(status='watching',reason='Waiting for a fresh full-position bid above actual remaining cost plus fee buffer.')
            else:
                event.update(status='review',reason='Exit preparation or undefined condition: no automatic execution.')
        if category in {'exit_warning','conditional_exit_undefined'}:
            event['signal']['kind']='guidance'
        self.save()
        return True

    def monitor_guidance(self, now):
        for mid,event in list(self.state['events'].items()):
            if event.get('status')!='watching' or event.get('guidance_class')!='conditional_profit':continue
            if event.get('edited') and not event.get('reduce_only_edited_exit'):
                self.finish(event,'review','Conditional instruction edited; manual review required.');continue
            if not self.broker.is_open():continue
            for symbol in event.get('matched_symbols',[]):
                qty=self.owned(symbol)
                if qty<=0:continue
                if any(o['payload']['side']=='sell' or o['status'] in {'submitting','unknown'} for o in self.pending(symbol)):continue
                if self.broker.position(symbol)!=qty:continue
                if self.state['contracts'][symbol]['expiration']<now.astimezone(NY).date().isoformat():
                    event['monitor_issue']='Expired contract requires disposition reconciliation.';continue
                try:
                    quote=self.broker.full_quote(symbol)
                    age=(now-datetime.fromisoformat(quote['timestamp'].replace('Z','+00:00'))).total_seconds()
                    bid,ask=Decimal(str(quote['bid'])),Decimal(str(quote['ask']))
                    if not bid.is_finite() or not ask.is_finite() or not -1<=age<=3 or not 0<bid<=ask or int(quote['bid_size'])<qty:continue
                    buys=self.filled_buys(symbol)
                    if any(not o.get('filled_avg_price') or not Decimal(o['filled_avg_price']).is_finite() or Decimal(o['filled_avg_price'])<=0 for _,o in buys):continue
                    cost=sum(Decimal(o['filled_avg_price'])*self.remaining(cid)*100 for cid,o in buys)
                    if bid*qty*100<=cost+Decimal('.20')*qty:continue
                except Exception as exc:
                    event['monitor_issue']=f'Conditional quote unavailable: {type(exc).__name__}';continue
                # Freeze the original target; a later source entry cannot redirect it.
                child=f'{mid}-condition-{symbol}'
                if child in self.state['events']:continue
                contract=self.state['contracts'][symbol]
                exit_event=dict(signal={'kind':'exit','strike':contract['strike'],'right':contract['right']},
                                sent_at=now.isoformat(),status='pending',exit_started=True,
                                source_event_id=mid,conditional_parent=mid)
                self.state['events'][child]=exit_event;self.save()
                self.execute(child,exit_event,now)
            if event.get('matched_symbols') and all(self.owned(s)<=0 for s in event['matched_symbols']):
                event['status']='completed'
        self.save()

    def refresh_exit_watch(self, now):
        watch={}
        # This is reporting only: never treat accepted/submitted as a closing fill.
        orders_by_event={}
        for order in self.state['orders'].values():
            if order['payload']['side']=='sell':
                orders_by_event.setdefault(str(order.get('event_id')),[]).append(order)
        for mid,event in self.state['events'].items():
            if event['signal']['kind'] not in {'exit','guidance'}:continue
            orders=orders_by_event.get(str(mid),[])
            if orders:
                failed=any(o['status'] in TERMINAL and Decimal(o['filled_qty'])<Decimal(o['payload']['qty']) for o in orders)
                event['execution_status']=('execution_failure' if failed else 'filled' if all(o['status']=='filled' and Decimal(o['filled_qty'])==Decimal(o['payload']['qty']) for o in orders) else 'fill_pending')
        for symbol in self.state['contracts']:
            qty=self.owned(symbol)
            if qty<=0:continue
            category='missing_exit_guidance';source=None
            for mid,event in self.state['events'].items():
                if symbol not in event.get('matched_symbols',[]) or event['signal']['kind'] not in {'exit','guidance'}:continue
                source=mid
                category=event.get('execution_status') or event.get('guidance_class') or ('actionable_exit' if event['signal']['kind']=='exit' else category)
            sells=[o for o in self.state['orders'].values() if o['payload']['symbol']==symbol and o['payload']['side']=='sell']
            if sells:
                last=sells[-1]
                if last['status'] not in TERMINAL:
                    category='partial_fill' if Decimal(last['filled_qty'])>0 else 'fill_pending'
                    if last['status'] in {'submitting','unknown'}:category='execution_uncertain'
                    elif (now-datetime.fromisoformat(last['created_at'])).total_seconds()>30:category='execution_delayed'
                elif Decimal(last['filled_qty'])<Decimal(last['payload']['qty']):category='execution_failure'
            if self.state['contracts'][symbol]['expiration']<now.astimezone(NY).date().isoformat():
                category='expired_disposition_unconfirmed'
            watch[symbol]=dict(remaining_qty=qty,classification=category,source_event=source,
                               requires_review=category not in {'conditional_profit','fill_pending','partial_fill'},updated_at=now.isoformat())
        self.state['exit_watch']=watch
        self.save()
