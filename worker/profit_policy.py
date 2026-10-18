"""Isolated historical experiment: immediate, profitable guidance exits.
No trailing stop, loss stop, hindsight price selection, or deployment side effect.
"""
import re
from datetime import datetime
from decimal import Decimal as D
from execution import NY


from interpretation import protection_kind


def install(e,targets=False):
    original_observe=e.observe;original_monitor=e.monitor_guidance
    goals={}
    def close(mid,event,symbol,now):
        for order in list(e.pending(symbol)):
            if order['payload']['side']=='buy' and order.get('id'):e.broker.cancel(order['id'])
        e.reconcile()
        qty=e.owned(symbol)
        if qty<=0:
            event['profit_result']='pending_entry_canceled_or_no_position';return
        if not e.exit_market_open(symbol,now) or e.broker.position(symbol)!=qty:return
        if any(o['payload']['side']=='sell' for o in e.pending(symbol)):return
        try:
            q=e.broker.full_quote(symbol);bid,ask=D(str(q['bid'])),D(str(q['ask']))
            age=(now-datetime.fromisoformat(q['timestamp'].replace('Z','+00:00'))).total_seconds()
            if not q.get('firm',True) or not bid.is_finite() or not ask.is_finite() or not -1<=age<=3 or not 0<bid<=ask or int(q['bid_size'])<qty:
                event['profit_result']='quote_unavailable_at_guidance';return
            buys=e.filled_buys(symbol)
            cost=sum(D(o['filled_avg_price'])*e.remaining(cid)*100 for cid,o in buys)
            event['profit_bid']=str(bid);event['profit_cost']=str(cost)
            if bid*qty*100<=cost+D('.20')*qty:
                event['profit_result']='not_green_at_guidance';return
        except (ValueError,KeyError,TypeError,ArithmeticError):
            event['profit_result']='quote_or_cost_unavailable_at_guidance';return
        child=f'{mid}-profit-{symbol}'
        if child in e.state['events']:return
        c=e.state['contracts'][symbol]
        out=dict(strategy_generated=True,signal={'kind':'exit','strike':c['strike'],'right':c['right']},sent_at=now.isoformat(),status='pending',exit_started=True,source_event_id=str(mid))
        e.state['events'][child]=out;e.execute(child,out,now)
        event['profit_result']='exit_requested';event['profit_exit_event']=child
    def observe(mid,text,stamp,now,**kw):
        original_observe(mid,text,stamp,now,**kw)
        kind=protection_kind(text);norm=' '.join(text.lower().split())
        observed_at=kw.get('edited_at') if kw.get('edited') else stamp
        if observed_at is None or not 0 <= (now-observed_at).total_seconds() <= 60:
            return
        target=targets and (re.search(r'(?:🎯|\btarget)\s+is\s+vwap',norm) or re.fullmatch(r'target\s+\d+(?:\.\d+)?[.!]*',norm) or re.fullmatch(r'(?:🎯|target)\s+reached[.!]*',norm))
        if not kind and not target:return
        event=e.state['events'].get(str(mid))
        if not event or event.get('raw_source_text')!=text or event.get('profit_policy_observed'):return
        if kw.get('edited') and not event.get('reduce_only_edited_exit'):return
        event['profit_policy_observed']=True
        # An old edited generic instruction cannot attach to an intervening trade.
        if kw.get('edited') and any(v.get('entry_like') and stamp<datetime.fromisoformat(v['sent_at'])<=now for k,v in e.state['events'].items() if k!=str(mid)):
            event['profit_result']='intervening_entry_before_edit';return
        event['signal']={'kind':'guidance','right':None}
        event.pop('resolved_targets',None)
        context=e.source_context(event);event['resolved_targets']=context
        symbols=e.resolve_followup(event,now.astimezone(NY).date().isoformat())
        event.update(status='review',profit_policy=kind or 'source_target',matched_symbols=symbols,profit_available_at=now.isoformat())
        if not symbols:event['profit_result']='no_owned_or_pending_source_match'
        for symbol in symbols:
            root=tuple(str(x.get('source_entry')) for x in context)
            key=(symbol,root)
            if kind:close(mid,event,symbol,now)
            elif 'reached' in norm:
                if key in goals:
                    close(mid,event,symbol,now);goals.pop(key,None)
                else:event['profit_result']='no_previous_source_target'
            else:
                number=re.fullmatch(r'target\s+(\d+(?:\.\d+)?)[.!]*',norm)
                goals[key]=(str(mid),event,D(number[1]) if number else None)
                event['profit_result']='target_waiting'
        e.save()
    def monitor(now):
        original_monitor(now)
        for key,(mid,event,level) in list(goals.items()):
            symbol,_=key
            if e.owned(symbol)<=0:goals.pop(key,None);continue
            if level is None:continue
            try:stock=e.broker.stock_reference()
            except (AttributeError,ValueError,TypeError):continue
            if stock is None:continue
            stock=D(str(stock));right=e.state['contracts'][symbol]['right']
            if (right=='put' and stock<=level) or (right=='call' and stock>=level):
                close(mid,event,symbol,now);goals.pop(key,None)
    e.observe=observe;e.monitor_guidance=monitor
