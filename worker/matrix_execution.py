"""Prepared paper-only policy adapter; no startup or orders on import."""
import hashlib,re
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from execution import Engine,UncertainSubmission,RejectedOrder,NY,TERMINAL
from signals import size_entry, directed_add_legs, parse_signal
from managed_rules import decide
from exit_guidance import ExitGuidanceMixin, classify
from broker_lifecycle import BrokerLifecycleMixin

def direct_sell_choice(text):
    return bool(re.fullmatch(r'(?:you can )?(?:sell(?: now)? or hold|hold or sell(?: now)?)(?:,? (?:your choice|up to you))?[.!]?',text)
                or re.fullmatch(r'you can sell for meh gains or risk it all for itm[.!]?',text))

class MatrixEngine(BrokerLifecycleMixin,ExitGuidanceMixin,Engine):
    def __init__(self,path,broker,rules):
        super().__init__(path,broker)
        if rules['entry_mode'] not in {'signal_limit','discount_limit','market'} or not 0 <= rules['entry_discount'] < 1:
            raise ValueError('Unsupported entry limit design.')
        if rules['entry_mode'] in {'signal_limit','market'} and rules['entry_discount']!=0:
            raise ValueError('Stated-price entries cannot have a discount.')
        self.rules=rules
        if any('root_entry' not in o for o in self.state['orders'].values()):
            raise RuntimeError('Legacy order history needs reconciliation before activating this prepared profile.')

    @staticmethod
    def update_order(order,result):
        Engine.update_order(order,result)
        order['filled_avg_price']=result.get('filled_avg_price')
        order['filled_at']=result.get('filled_at') or order.get('filled_at')

    def allocations_filled(self,order):
        left=int(Decimal(order.get('filled_qty','0')));done={}
        for cid,n in order.get('allocations',[]):
            amount=min(left,n);done[cid]=amount;left-=amount
        return done

    def remaining(self,cid):
        buy=self.state['orders'][cid]
        sold=sum(self.allocations_filled(s).get(cid,0) for s in self.state['orders'].values() if s['payload']['side']=='sell')
        return int(Decimal(buy.get('filled_qty','0')))-sold-self.disposition_allocated(cid)

    def available(self):
        if self.state.get('broker_lifecycle_reviews'):return Decimal(0)
        cash=Decimal(str(self.rules.get('starting_capital',1000)))+self.lifecycle_cash()
        for o in self.state['orders'].values():
            n=Decimal(o.get('filled_qty','0'))
            if n:
                if o.get('filled_avg_price') is None:return Decimal(0)
                price=Decimal(o['filled_avg_price'])
                if not price.is_finite() or price<=0:return Decimal(0)
                value=n*price*100
                cash+=value if o['payload']['side']=='sell' else -value
                cash-=n*Decimal('.10')
            if o['payload']['side']=='buy' and o['status'] not in TERMINAL:
                pending=Decimal(o['payload']['qty'])-n
                reference=o.get('reservation_price',o['payload'].get('limit_price'))
                if reference is None:return Decimal(0)
                cash-=pending*(Decimal(reference)*100+Decimal('.10'))
        return min(cash,self.broker.buying_power())

    def observe(self,mid,text,stamp,now,**kw):
        edit_stamp=kw.pop('edited_at',None)
        reply_to=kw.pop('reply_to',None)
        already_observed=str(mid) in self.state['events']
        super().observe(mid,text,stamp,now,**kw)
        if reply_to is not None:self.state['events'][str(mid)]['reply_to']=str(reply_to);self.save()
        if kw.get('edited') and edit_stamp is not None:
            event=self.state['events'][str(mid)]
            if self.accept_fresh_edited_exit(event,text,stamp,edit_stamp,now):return
        if already_observed:return
        e=self.state['events'][str(mid)]
        if reply_to is not None:e['reply_to']=str(reply_to);self.save()
        if e['status'] not in ('ignore','review') or e.get('edited') or kw.get('edited') or not -5<=(now-stamp).total_seconds()<=60:return
        normalized=' '.join(text.replace('’',"'").lower().split())
        if self.observe_sell_choice(e,normalized):
            return
        if self.rules.get('callout_exits_only') and self.capture_guidance(e,normalized,now):return
        legs=directed_add_legs(text)
        if legs:
            e.update(signal=legs[0] if len(legs)==1 else {'kind':'add_batch','legs':legs},status='pending')
            self.save()
            return
        match=re.fullmatch(r'added (?P<qty>\d+) (?:more )?(?:at |for )?(?P<cents>\d+(?:\.\d+)?)(?: cents?)?[.!]?',normalized)
        if match and int(match['qty'])>0 and Decimal(match['cents'])>0:
            e.update(signal={'kind':'add','right':None},status='pending');self.save()
        elif re.fullmatch(r"(?:stop loss triggered|sl triggered|outz|i'?m (?:outz|outs)(?: guys)?)[.!]?",normalized):
            e.update(signal={'kind':'exit','right':None},status='pending');self.save()
        elif re.fullmatch(
            r"(?:(?:sl|stop loss) triggered(?:\.? (?:out )?at \d+(?:\.\d+)? cents?)?"
            r"|got sl at \d+(?:\.\d+)? cents?|stopped out(?: 😫)?|profit stopped"
            r"|sold the top|sold\d+(?:\.\d+)?|sell the move guys"
            r"|what'?s with the eyes, i been sold)[.!]?", normalized):
            # Confirmations act now, at an executable quote, never at a claimed
            # historical sale time or premium. Ownership guards still apply.
            e.update(signal={'kind':'exit','right':None},status='pending');self.save()
        else:
            exit_match=re.fullmatch(r'(?:sell|sold|cut|out)(?: the)? (?:(\d+(?:\.\d+)?) )?(calls?|puts?)(?: at \d+(?:\.\d+)? cents)?[.!]?',normalized)
            add_match=re.fullmatch(r'(?:double|triple|tripe|quadruple|quadriple)(?: the| to the)? (?:(\d+(?:\.\d+)?) )?(calls?|puts?)[.!]?',normalized)
            m=exit_match or add_match
            if m:
                e.update(signal={'kind':'exit' if exit_match else 'add','strike':m[1],'right':m[2].rstrip('s')},status='pending');self.save()
            elif re.fullmatch(r'cut at \d+(?:\.\d+)? cents[.!]?',normalized):
                e.update(signal={'kind':'exit','right':None},status='pending');self.save()

        if self.rules.get('callout_exits_only') and e['status'] in {'ignore','review'} and re.search(r'\b(?:sell|sold|cut|exit|outz|outs)\b',normalized):
            e.update(guidance_class='parser_miss',signal={'kind':'guidance','right':None},status='review',
                     reason='Unrecognized exit wording: review source before executing.')
            e['resolved_targets']=self.source_context(e)
            e['matched_symbols']=self.resolve_followup(e,now.astimezone(NY).date().isoformat())
            self.save()

    def accept_fresh_edited_exit(self,event,text,stamp,edit_stamp,now):
        # Current, explicitly executable text may reduce risk; missing originals
        # never authorize new or larger positions, old edits or inferred prices.
        if event.get('entry_like') or event.get('order_id'):return False
        if not (0<=(now-stamp).total_seconds()<=60 and
                0<=(edit_stamp-stamp).total_seconds()<=60 and
                0<=(now-edit_stamp).total_seconds()<=60):return False
        normalized=' '.join(text.lower().replace('’',"'").split())
        if classify(normalized)=='conditional_profit':
            event.update(reduce_only_edited_exit=True,edited_at=edit_stamp.isoformat(),digest=hashlib.sha256(text.encode()).hexdigest())
            return self.capture_guidance(event,normalized,now)
        signal={'kind':'exit','right':None} if direct_sell_choice(normalized) else parse_signal(text,stamp.isoformat())
        if signal['kind']!='exit':
            if re.fullmatch(r"(?:stopped out(?: 😫)?|stop loss triggered|sl triggered|outz|i'?m (?:outz|outs)(?: guys)?)[.!]?",normalized):
                signal={'kind':'exit','right':None}
            else:
                match=re.fullmatch(r'(?:sell|sold|cut|out)(?: the)? (?:(\d+(?:\.\d+)?) )?(calls?|puts?)(?: at \d+(?:\.\d+)? cents)?[.!]?',normalized)
                if not match:return False
                signal={'kind':'exit','strike':match[1],'right':match[2].rstrip('s')}
        event.setdefault('original_digest',event['digest'])
        event.update(digest=hashlib.sha256(text.encode()).hexdigest(),signal=signal,status='pending',
                     reduce_only_edited_exit=True,edited_at=edit_stamp.isoformat(),
                     reason='Fresh edited exit accepted as reduce-only at current observation time.')
        self.save();return True

    def observe_sell_choice(self,event,text):
        """Sell when an explicit choice is offered, never on preparation alone."""
        hints={'get ready to sell':'sell_preparation','get ready to sell the move':'sell_preparation',
               'or':'alternative','risk it all and hold':'hold_option'}
        hint=hints.get(text.rstrip('.!'))
        if hint:
            event['choice_hint']=hint;self.save()
        evidence=[]
        if text.rstrip('.!')=='your choice':
            at=datetime.fromisoformat(event['sent_at'])
            for mid,prior in reversed(list(self.state['events'].items())):
                if prior is event:continue
                age=(at-datetime.fromisoformat(prior['sent_at'])).total_seconds()
                if age>60:break
                if age<0:continue
                if prior.get('entry_like') or prior['signal']['kind'] in {'exit','add','add_batch'}:break
                if prior.get('edited'):continue
                if prior.get('choice_hint'):evidence.append((mid,prior['choice_hint']))
            evidence.reverse()
        direct=direct_sell_choice(text)
        if direct or [hint for _,hint in evidence]==['sell_preparation','alternative','hold_option']:
            event.update(signal={'kind':'exit','right':None},status='pending',
                         choice_exit=True,choice_evidence=[mid for mid,_ in evidence])
            self.save();return True
        return False

    def submit_policy(self,mid,event,symbol,qty,price,now,allocations=None,reason=None):
        cid=f'geerawr-{mid}-{event["signal"]["kind"]}'
        if cid in self.state['orders']:return
        buying=price is not None
        payload=dict(symbol=symbol,qty=qty,side='buy' if buying else 'sell',type=('market' if self.rules['entry_mode']=='market' else 'limit') if buying else 'market',
                     time_in_force='day',position_intent='buy_to_open' if buying else 'sell_to_close',client_order_id=cid)
        if buying and payload['type']=='limit':payload['limit_price']=str(price)
        order=dict(payload=payload,status='submitting',filled_qty='0',created_at=now.isoformat(),event_id=event.get('source_event_id',mid),
                   kind=event['signal']['kind'],root_entry=event.get('root_entry',mid),allocations=allocations or [],reason=reason)
        if buying:order['reservation_price']=str(price)
        if buying and event['signal']['kind']=='entry':
            order['lifetime_seconds']=self.rules['entry_lifetime_seconds']
        self.state['orders'][cid]=order;event.update(status='submitted',order_id=cid);self.save()
        try:self.update_order(order,self.broker.submit(payload))
        except UncertainSubmission:order['status']='unknown'
        except RejectedOrder:order['status']='rejected';event.update(status='review',reason='Paper order rejected.')
        self.save()

    def filled_buys(self,symbol):
        return [(cid,o) for cid,o in self.state['orders'].items() if o['payload']['symbol']==symbol and o['payload']['side']=='buy' and self.remaining(cid)>0]

    def source_context(self,event):
        """Resolve source intent independently of which entries we could fill.

        A new entry focuses subsequent generic follow-ups on that trade. An
        explicit multi-contract add focuses them on that named group. Right-
        specific instructions select the most recent context of that right.
        Closed or uncopied targets never fall back to an older owned position.
        """
        signal=event['signal'];day=datetime.fromisoformat(event['sent_at']).astimezone(NY).date()
        reply=event.get('reply_to')
        if reply:
            prior=self.state['events'].get(str(reply))
            if not prior or prior.get('edited'):return []
            ps=prior['signal']
            if prior.get('entry_like') and ps.get('strike') and ps.get('right'):
                return [dict(ps,source_entry=str(reply))]
            return prior.get('resolved_targets',[])
        contexts=[]
        for prior_mid,prior in self.state['events'].items():
            if prior is event:break
            if prior.get('strategy_generated'):continue
            if datetime.fromisoformat(prior['sent_at']).astimezone(NY).date()!=day:continue
            if prior.get('duplicate_of'):continue
            ps=prior['signal']
            if prior.get('entry_like'):
                # Even unsupported/edited entries are context barriers. They
                # must not redirect an unqualified follow-up to an older trade.
                contexts.append([dict(ps,source_edited=bool(prior.get('edited')),source_entry=prior_mid)])
            elif ps['kind']=='add_batch' and not prior.get('edited'):
                contexts.append(ps['legs'])
        for group in reversed(contexts):
            selected=[x for x in group if not signal.get('right') or not x.get('right') or x.get('right')==signal['right']]
            if not selected:continue
            if any(x.get('source_edited') or not x.get('strike') or not x.get('right') for x in selected):return []
            return selected
        return []

    def resolve_followup(self,event,today):
        signal=event['signal']
        if not signal.get('strike'):
            if 'resolved_targets' not in event:
                context=self.source_context(event)
                event['resolved_targets']=[dict(strike=x['strike'],right=x['right'],ticker=x.get('ticker'),source_entry=x.get('source_entry')) for x in context]
                self.save()
            targets=event['resolved_targets']
        else:targets=[signal]
        candidates=[]
        for target in targets:
            matches=[s for s,c in self.state['contracts'].items()
                     if c['expiration']==today
                     and Decimal(c['strike'])==Decimal(target['strike'])
                     and c['right']==target['right']
                     and (not target.get('ticker') or s.startswith(target['ticker']))
                     and (self.owned(s)>0 or self.pending(s))]
            if target.get('source_entry'):
                matches=[s for s in matches if any(str(o.get('root_entry'))==str(target['source_entry']) for _,o in self.filled_buys(s))
                         or any(str(o.get('root_entry'))==str(target['source_entry']) and o['payload']['side']=='buy' for o in self.pending(s))]
            if len(matches)>1:return []
            candidates.extend(matches)
        return list(dict.fromkeys(candidates))

    def execute_batch(self,mid,event,now):
        # Child state is saved within the source event, so restart/redelivery
        # cannot repeat a completed leg. Order event IDs reference the parent
        # so an edit cancels all still-unfilled buys from this message.
        if 'legs' not in event:
            event['legs']=[dict(signal=dict(signal),status='pending',
                                sent_at=event['sent_at'],source_event_id=str(mid))
                           for signal in event['signal']['legs']]
            self.save()
        for i,leg in enumerate(event['legs'],1):
            if leg['status']!='pending':
                continue
            if event['signal']['kind']=='exit':
                event['exit_started']=True;self.save()
            self.execute(f'{mid}-leg-{i}',leg,now)
            if any(o['status'] in {'submitting','unknown'} for o in self.state['orders'].values()):
                return
        if event['signal']['kind']=='exit' and any(leg.get('exit_started') for leg in event['legs']):
            event['exit_started']=True;self.save()
        if all(leg['status']!='pending' for leg in event['legs']):
            status=('submitted' if any(leg['status']=='submitted' for leg in event['legs'])
                    else 'review' if any(leg['status']=='review' for leg in event['legs'])
                    else 'skipped')
            self.finish(event,status,'Per-contract instructions processed individually.')

    def execute(self,mid,event,now):
        if event['signal']['kind']=='add_batch' or (event['signal']['kind']=='exit' and 'legs' in event):
            return self.execute_batch(mid,event,now)
        if not self.broker.is_open():return self.finish(event,'skipped','Market is closed.')
        signal=event['signal'];kind=signal['kind'];today=now.astimezone(NY).date().isoformat()
        if kind!='exit' and now.astimezone(NY).strftime('%H:%M:%S')>=self.rules['close_time']:
            return self.finish(event,'skipped','Strategy entry window is closed.')
        if kind=='entry':
            if signal['expiration']!=today:return self.finish(event,'review','Expiration is not today.')
            contract=self.broker.contract(signal)
            if not contract:return self.finish(event,'review','No unique tradable contract.')
            symbol=contract['symbol']
            if self.broker.position(symbol)!=0 or self.pending(symbol) or self.owned(symbol):
                # An exact repeat within 30 seconds is not an uncopied new
                # source position. Retain the original owned entry as its root.
                for prior_mid,prior in self.state['events'].items():
                    if prior is event:
                        break
                    order=self.state['orders'].get(prior.get('order_id'),{})
                    age=(datetime.fromisoformat(event['sent_at'])-datetime.fromisoformat(prior['sent_at'])).total_seconds()
                    if (prior.get('digest')==event.get('digest') and 0<=age<=30
                            and not prior.get('edited') and prior.get('entry_like')
                            and order.get('payload',{}).get('symbol')==symbol
                            and Decimal(order.get('filled_qty','0'))>0 and self.owned(symbol)>0):
                        event.update(duplicate_of=prior_mid,entry_like=False)
                        return self.finish(event,'skipped','Exact recent duplicate of an owned entry.')
                return self.finish(event,'review','Existing or pending contract ownership.')
            self.state['contracts'][symbol]=contract
            if self.rules['entry_mode']=='market':
                quote=self.broker.full_quote(symbol)
                age=(now-datetime.fromisoformat(quote['timestamp'].replace('Z','+00:00'))).total_seconds()
                if not -1<=age<=3:return self.finish(event,'review','Entry quote is stale.')
                price=Decimal(str(quote['ask']))
                if not price.is_finite() or price<=0:return self.finish(event,'review','Entry quote is invalid.')
            else:
                reference=Decimal(signal['premium'])
                discount=Decimal(str(self.rules['entry_discount']))
                price=(reference*(1-discount)).quantize(Decimal('.01'),rounding=ROUND_DOWN)
            budget=Decimal(str(self.rules['entry_budget']));event['root_entry']=mid
            if price<=0:return self.finish(event,'review','Discounted option limit is below one cent.')
        else:
            candidates=self.resolve_followup(event,today)
            if kind=='exit' and len(candidates)>1:
                event['legs']=[dict(signal={'kind':'exit','strike':self.state['contracts'][symbol]['strike'],
                                             'right':self.state['contracts'][symbol]['right']},
                                    status='pending',sent_at=event['sent_at'],source_event_id=str(mid))
                               for symbol in candidates]
                event['matched_symbols']=candidates;self.save()
                return self.execute_batch(mid,event,now)
            if len(candidates)!=1:return self.finish(event,'review','No unique owned target in the current source context.')
            symbol=candidates[0];contract=self.state['contracts'][symbol]
            event['matched_symbols']=[symbol]
            if kind=='exit':
                event['exit_started']=True;self.save()
                for o in self.pending(symbol):
                    if o['payload']['side']=='sell':return
                    if o.get('id'):self.broker.cancel(o['id'])
                self.reconcile()
                if self.pending(symbol):return
                qty=self.owned(symbol)
                if qty<=0 or self.broker.position(symbol)!=qty:return self.finish(event,'review','Owned quantity does not match broker.')
                allocations=[(cid,self.remaining(cid)) for cid,_ in self.filled_buys(symbol)]
                return self.submit_policy(mid,event,symbol,qty,None,now,allocations,'chat_exit')
            buys=self.filled_buys(symbol)
            if self.pending(symbol) or not buys or self.broker.position(symbol)!=self.owned(symbol):return self.finish(event,'review','Position is pending or changed outside bridge.')
            root=buys[0][1]['root_entry'];event['root_entry']=root
            count=sum(o.get('kind')=='add' and o.get('root_entry')==root and Decimal(o.get('filled_qty','0'))>0 for o in self.state['orders'].values())
            if self.rules['max_adds'] is not None and count>=self.rules['max_adds']:return self.finish(event,'skipped','Filled add limit reached.')
            last=self.state['bursts'].get(symbol)
            if last and 0<=(datetime.fromisoformat(event['sent_at'])-datetime.fromisoformat(last)).total_seconds()<=self.rules['burst_seconds']:
                self.state['bursts'][symbol]=event['sent_at'];return self.finish(event,'skipped','Same add burst.')
            quote=self.broker.full_quote(symbol)
            age=(now-datetime.fromisoformat(quote['timestamp'].replace('Z','+00:00'))).total_seconds()
            if not -1<=age<=3:return self.finish(event,'review','Add quote is stale.')
            price=Decimal(str(quote['ask']));budget=Decimal(str(self.rules['addon_budget']))
        qty,_=size_entry(budget,price,int(contract['size']))
        if qty<=0:return self.finish(event,'skipped','One contract exceeds budget.')
        if Decimal(qty)*(price*100+Decimal('.10'))>self.available():return self.finish(event,'skipped','Strategy cash or broker buying power is insufficient.')
        if kind=='add':self.state['bursts'][symbol]=event['sent_at']
        self.submit_policy(mid,event,symbol,qty,price,now)

    def tick(self,now,*,process_pending=True):
        after_close=now.astimezone(NY).strftime('%H:%M:%S')>=self.rules['close_time']
        if after_close and not self.rules.get('callout_exits_only'):
            self.reconcile()
            for o in list(self.state['orders'].values()):
                if o['payload']['side']=='buy' and o['status'] not in TERMINAL and o.get('id'):self.broker.cancel(o['id'])
            self.reconcile()
        super().tick(now,process_pending=process_pending)
        if self.rules.get('callout_exits_only'):
            self.monitor_guidance(now)
            self.refresh_exit_watch(now)
            return
        quotes={}
        for cid,buy in list(self.state['orders'].items()):
            if buy['payload']['side']!='buy' or not buy.get('root_entry') or self.remaining(cid)<=0:continue
            symbol=buy['payload']['symbol']
            if any(o['status'] in ('submitting','unknown') for o in self.pending(symbol)):continue
            if any(o['payload']['side']=='sell' for o in self.pending(symbol)):continue
            price=buy.get('filled_avg_price')
            valid_price=price is not None and Decimal(price).is_finite() and Decimal(price)>0
            if not valid_price and not after_close:continue
            qty=int(Decimal(buy['filled_qty']));lot=buy.setdefault('risk',{})
            sold={reason:sum(self.allocations_filled(s).get(cid,0) for s in self.state['orders'].values() if s.get('reason')==reason) for reason in ['target_1','target_2']}
            lot.update(qty=qty,remaining=self.remaining(cid),price=float(price) if valid_price else 0,kind=buy['kind'],filled_at=buy.get('filled_at') or buy['created_at'],sold_target_1=sold['target_1'],sold_target_2=sold['target_2'])
            if symbol not in quotes:quotes[symbol]=dict(bid=0,ask=0,bid_size=0,timestamp=now.isoformat()) if after_close else self.broker.full_quote(symbol)
            decision=decide(lot,quotes[symbol],now,self.rules);self.save()
            if not decision:continue
            if self.broker.position(symbol)!=self.owned(symbol):continue
            n,reason=decision
            pending_buys=[o for o in self.pending(symbol) if o['payload']['side']=='buy']
            if pending_buys:
                for o in pending_buys:
                    if o.get('id'):self.broker.cancel(o['id'])
                self.reconcile()
                # Re-evaluate the actual filled quantity after cancellation.
                continue
            digest=hashlib.sha256(f'{cid}:{reason}:{lot["remaining"]}'.encode()).hexdigest()[:12]
            mid='auto-'+digest
            if mid in self.state['events']:continue
            event=dict(signal={'kind':'exit'},status='pending',sent_at=now.isoformat(),root_entry=buy['root_entry'])
            self.state['events'][mid]=event;self.save()
            self.submit_policy(mid,event,symbol,n,None,now,[(cid,n)],reason)
            if any(o['status'] in ('submitting','unknown') for o in self.pending(symbol)):continue
