"""Prepared paper-only policy adapter; no startup or orders on import."""
import hashlib,re
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from execution import Engine,UncertainSubmission,RejectedOrder,NY,TERMINAL
from signals import size_entry, directed_add_legs, parse_signal
from managed_rules import decide
from exit_guidance import ExitGuidanceMixin, classify
from broker_lifecycle import BrokerLifecycleMixin

from interpretation import interpret_signal, direct_sell_choice, PARSER_VERSION

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

    @staticmethod
    def source_identity(signal):
        return tuple(signal.get(k) for k in ('ticker','strike','right','expiration'))

    def observe(self,mid,text,stamp,now,**kw):
        edit_stamp=kw.pop('edited_at',None)
        edited=bool(kw.get('edited'))
        old=self.state['events'].get(str(mid))
        previous=dict(old) if old else None
        if previous and previous.get('edited_at'):
            latest_edit=datetime.fromisoformat(previous['edited_at'])
            if edit_stamp is None or edit_stamp<latest_edit:
                return  # Delayed original/older revision cannot undo known newer intent.
        signal=interpret_signal(text,stamp.isoformat())
        super().observe(mid,text,stamp,now,**kw)
        event=self.state['events'][str(mid)]
        event.update(raw_source_text=text,interpretation=signal,parser_version=PARSER_VERSION,
                     source_available_at=now.isoformat())
        if edit_stamp:event['edited_at']=edit_stamp.isoformat()
        digest=hashlib.sha256(text.encode()).hexdigest()
        if edited and edit_stamp is None:
            self.save();return
        if previous and previous['digest']==digest:
            self.save();return
        if previous and previous.get('entry_like') and previous['signal'].get('kind')=='entry':
            same=signal.get('kind')=='entry' and self.source_identity(previous['signal'])==self.source_identity(signal)
            event.update(digest=digest,signal=signal,source_identity_known=same,
                         actionable_edited_entry=False,identity_stable_edit=same)
            if same and previous.get('order_id'):
                # Filled lots keep their immutable order lineage. Remaining buys
                # are still cancelled on any change; an edit never adds size.
                event.update(status=previous['status'] if previous['status']!='pending' else 'review',
                             reason='Entry wording changed; owned contract identity preserved.')
            else:event.update(status='review',reason='Entry changed; no new order.')
            self.save();return
        event.update(signal=signal,digest=digest)
        source_fresh=-5<=(now-stamp).total_seconds()<=60
        edit_fresh=edit_stamp is not None and 0<=(now-edit_stamp).total_seconds()<=60
        if signal['kind']=='entry':
            if edited:
                allowed_prior=previous is None or (previous['signal'].get('kind') in {'review','ignore'} and not previous.get('order_id'))
                in_window=source_fresh and edit_fresh and 0<=(edit_stamp-stamp).total_seconds()<=60
                ordered=any(str(o.get('root_entry'))==str(mid) for o in self.state['orders'].values())
                intervening=any(v is not event and stamp<datetime.fromisoformat(v.get('source_available_at',v['sent_at']))<=now
                                and (v.get('entry_like') or v['signal']['kind'] in {'entry','exit','add','add_batch','guidance'})
                                for v in self.state['events'].values())
                if allowed_prior and in_window and not ordered and not intervening:
                    event.update(status='pending',entry_like=True,actionable_edited_entry=True,
                                 source_identity_known=True,reason='First actionable entry observed at its known edit time.')
                else:event.update(status='review',reason='Edited entry unavailable, stale, already processed, or superseded.')
            elif source_fresh and not previous:
                event.update(status='pending',source_identity_known=True)
            self.save();return
        if event.get('entry_like') and signal['kind']=='review':
            event.update(status='review',reason=signal.get('reason','Unsupported entry.'));self.save();return
        if previous and not edited:return
        if edited:
            # Current reductions may use a known edit; never apply an old
            # generic instruction to an intervening entry.
            intervening=any(v is not event and v.get('entry_like') and stamp<datetime.fromisoformat(v['sent_at'])<=now for v in self.state['events'].values())
            if not edit_fresh or not source_fresh or intervening or stamp.astimezone(NY).date()!=now.astimezone(NY).date():
                event.update(status='review',reason='Edited follow-up is stale or has intervening entry context.');self.save();return
            if signal['kind'] not in {'exit','guidance'}:
                event.update(status='review',reason='Edited adds cannot enlarge a position.');self.save();return
            event.update(reduce_only_edited_exit=True)
            if signal['kind']=='exit':event['exit_started']=True
        elif not source_fresh:
            self.save();return
        normalized=' '.join(text.replace('’',"'").lower().split())
        if self.observe_sell_choice(event,normalized):return
        if signal.get('guidance_class')=='position_focus':
            self.capture_source_focus(event,text,stamp,edit_stamp,now,edited)
            event.update(status='review');self.save();return
        if self.rules.get('callout_exits_only') and self.capture_guidance(event,normalized,now):return
        if signal['kind'] in {'exit','add','add_batch'}:
            event.update(status='pending');self.save();return
        if self.rules.get('callout_exits_only') and (signal.get('guidance_class')=='possible_trim' or re.search(r'\b(?:sell|sold|selling|cut|exit|outz|outs)\b',normalized)):
            event.update(guidance_class=signal.get('guidance_class','parser_miss'),signal={**signal,'kind':'guidance','right':signal.get('right')},status='review',
                         reason=signal.get('reason','Unrecognized exit wording: review source before executing.'))
            event['resolved_targets']=self.source_context(event)
            event['matched_symbols']=self.resolve_followup(event,now.astimezone(NY).date().isoformat())
        if event['signal']['kind']=='guidance' and event['status']=='ignore':event.update(status='review',reason='Context-dependent guidance requires a matching instruction sequence.')
        self.save()

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

    def capture_source_focus(self,event,text,stamp,edit_stamp,now,edited):
        """A named holding or explicit two-position comment changes focus only.

        This cannot submit orders. Unknown/uncopied targets remain barriers.
        """
        if not 0<=(now-stamp).total_seconds()<=60:return
        if edited and (edit_stamp is None or not 0<=(edit_stamp-stamp).total_seconds()<=60 or not 0<=(now-edit_stamp).total_seconds()<=60):return
        text=' '.join(text.lower().replace('’',"'").split()).rstrip('.!')
        named=re.fullmatch(r"(?:i'?m |im |still )?(?:holding|holdin|hodling|holdint)(?: the)? (\d+(?:\.\d+)?) (?:spy )?(calls?|puts?)",text)
        both=re.fullmatch(r'both (calls|puts)(?: (?:are )?(?:green|itm))(?: afff)?',text)
        pair=re.fullmatch(r"(?:i'?m |im |still )?holding (?:the )?(\d+(?:\.\d+)?) and (\d+(?:\.\d+)?) (calls?|puts?)",text)
        holding_both=re.fullmatch(r"(?:i'?m |im |still )?holding both(?: (calls|puts|sides))?(?: still)?",text)
        terminal=re.fullmatch(r"(?:i'?m |im )letting (?:the )?(\d+(?:\.\d+)?) (calls?|puts?) exp(?:ire)? worthless",text)
        if terminal:
            event.update(source_terminal_intent='hold_to_expiry',terminal_selector={'strike':terminal[1],'right':terminal[2].rstrip('s')});self.save();return
        premium_holding=re.fullmatch(r'(?:the )?(\d+(?:\.\d+)?) is holding at \d+(?:\.\d+)? cents?',text)
        if not named and not both and not pair and not holding_both and not premium_holding:return
        entries=[]
        for mid,prior in self.state['events'].items():
            if prior is event:break
            if datetime.fromisoformat(prior['sent_at']).astimezone(NY).date()!=stamp.astimezone(NY).date() or not prior.get('entry_like') or prior.get('duplicate_of'):continue
            ps=prior['signal']
            entries.append(dict(ps,source_entry=mid,source_edited=bool((prior.get('edited') and not prior.get('actionable_edited_entry') and not prior.get('source_identity_known')))))
        if named:
            matches=[x for x in entries if x.get('strike')==named[1] and x.get('right')==named[2].rstrip('s')]
            if matches:event['source_focus']=[matches[-1]]
        elif premium_holding:
            matches=[x for x in entries if x.get('strike')==premium_holding[1]]
            if matches and len({(x.get('ticker'),x.get('strike'),x.get('right')) for x in matches})==1:event['source_focus']=[matches[-1]]
        elif pair:
            selected=[]
            for strike in pair.group(1,2):
                matches=[x for x in entries if x.get('strike')==strike and x.get('right')==pair[3].rstrip('s')]
                if matches:selected.append(matches[-1])
            if len(selected)==2 and pair[1]!=pair[2]:event['source_focus']=selected
        else:
            side=both[1].rstrip('s') if both else holding_both[1].rstrip('s') if holding_both[1] in {'calls','puts'} else None
            matches=[x for x in entries if side is None or x.get('right')==side]
            if len(matches)>=2 and all(x.get('ticker') in {'SPY','SPX'} for x in matches[-2:]) and len({(x.get('ticker'),x.get('strike'),x.get('right')) for x in matches[-2:]})==2:event['source_focus']=matches[-2:]
        if event.get('source_focus'):
            if pair or holding_both:event['exit_focus_only']=True
            self.save()

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
            if not prior or (prior.get('edited') and not prior.get('actionable_edited_entry') and not prior.get('source_identity_known')):return []
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
            ps=prior.get('exit_scope_selector',prior['signal'])
            if prior.get('entry_like'):
                # Even unsupported/edited entries are context barriers. They
                # must not redirect an unqualified follow-up to an older trade.
                contexts.append([dict(ps,source_edited=bool((prior.get('edited') and not prior.get('actionable_edited_entry') and not prior.get('source_identity_known'))),source_entry=prior_mid)])
            elif ps['kind']=='exit' and ps.get('strike') and ps.get('right'):
                # An explicit singular selector replaces a previous group even
                # if it has no owned match. Never fall back to the old group.
                selectors=[x for group in contexts for x in group if x.get('strike')==ps['strike'] and x.get('right')==ps['right'] and (not ps.get('ticker') or ps['ticker']==x.get('ticker'))]
                contexts.append([selectors[-1] if selectors else dict(ps)])
            elif prior.get('source_focus'):
                if signal['kind']=='add' and prior.get('exit_focus_only'):continue
                contexts.append(prior['source_focus'])
            elif ps['kind']=='add_batch' and not (prior.get('edited') and not prior.get('actionable_edited_entry') and not prior.get('source_identity_known')):
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
                for leg in event['legs']:
                    for key in ('exit_fraction','exit_qty','action'):
                        if key in signal:leg['signal'][key]=signal[key]
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
                if 'exit_fraction' in signal:qty=int(Decimal(qty)*Decimal(signal['exit_fraction']))
                elif 'exit_qty' in signal:qty=min(qty,int(signal['exit_qty']))
                if qty<=0:return self.finish(event,'review','Trim rounds to zero whole contracts.')
                allocations=[];left=qty
                for cid,_ in self.filled_buys(symbol):
                    n=min(left,self.remaining(cid))
                    if n:allocations.append((cid,n));left-=n
                    if not left:break
                return self.submit_policy(mid,event,symbol,qty,None,now,allocations,'chat_trim' if signal.get('action')=='trim' else 'chat_exit')
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
