"""Evidence-backed account dispositions, separate from submitted bot orders."""
from datetime import datetime
from decimal import Decimal,InvalidOperation
from execution import NY

class BrokerLifecycleMixin:
    def owned(self,symbol):
        return super().owned(symbol)-sum(d['qty'] for d in self.state.get('broker_dispositions',{}).values() if d['symbol']==symbol)

    def disposition_allocated(self,cid):
        return sum(n for d in self.state.get('broker_dispositions',{}).values() for buy,n in d['allocations'] if buy==cid)

    def lifecycle_cash(self):
        return sum((Decimal(d['cash_delta']) for d in self.state.get('broker_dispositions',{}).values()),Decimal(0))

    def reconcile_broker_events(self,actual,now):
        missing={s:self.owned(s)-int(actual.get(s,0)) for s in self.state['contracts'] if self.owned(s)>int(actual.get(s,0))>=0}
        if not missing or not hasattr(self.broker,'account_activities'):return
        first=min(o['created_at'][:10] for o in self.state['orders'].values() if o['payload']['symbol'] in missing and o['payload']['side']=='buy')
        activities=self.broker.account_activities(first)
        for a in activities:
            symbol=a.get('symbol');kind=a.get('activity_type');key=f"{kind}:{symbol}:{a.get('id')}"
            if symbol not in missing or missing[symbol]<=0 or not a.get('id') or key in self.state.get('broker_dispositions',{}):continue
            contract=self.state['contracts'][symbol]
            buys=self.filled_buys(symbol)
            if not buys or self.pending(symbol):continue
            first_fill=min((o.get('filled_at') or o['created_at']) for _,o in buys)
            day=(a.get('date') or a.get('transaction_time') or '')[:10]
            if not day or day<first_fill[:10] or day>now.astimezone(NY).date().isoformat():continue
            try:
                raw=Decimal(str(a.get('qty','0')))
                if not raw.is_finite() or raw!=raw.to_integral_value():continue
                cash=Decimal(0);price=None
                if kind in {'OPEXP','OPEXC'}:
                    if a.get('status')!='executed' or raw>=0 or Decimal(str(a.get('net_amount','NaN')))!=0:continue
                    if kind=='OPEXP' and day<contract['expiration']:continue
                    qty=int(-raw)
                elif kind=='FILL':
                    if a.get('side')!='sell' or raw<=0 or not a.get('order_id'):continue
                    order=self.broker.activity_order(a['order_id'])
                    if not order or order.get('symbol')!=symbol or order.get('side')!='sell' or order.get('client_order_id','').startswith('geerawr-'):continue
                    price=Decimal(str(a.get('price','NaN')))
                    if not price.is_finite() or price<=0:continue
                    qty=int(raw)
                    if Decimal(str(order.get('filled_qty','0')))<qty:continue
                    timestamp=a.get('transaction_time')
                    if not timestamp or datetime.fromisoformat(timestamp.replace('Z','+00:00'))<datetime.fromisoformat(first_fill.replace('Z','+00:00')):continue
                    cash=raw*(price*100-Decimal('.10'))
                else:continue
                if qty>missing[symbol]:continue
            except (InvalidOperation,ValueError,TypeError):continue
            left=qty;allocations=[]
            for cid,_ in buys:
                count=min(left,self.remaining(cid))
                if count:allocations.append([cid,count]);left-=count
                if not left:break
            if left:continue
            disposition=dict(symbol=symbol,qty=qty,kind=kind,activity_id=a['id'],date=day,
                             allocations=allocations,cash_delta=str(cash),price=str(price) if price is not None else None,
                             evidence=dict(a),reconciled_at=now.isoformat(),classification='broker_expired_worthless' if kind=='OPEXP' else 'broker_exercise_requires_review' if kind=='OPEXC' else 'broker_external_sell_fill')
            self.state.setdefault('broker_dispositions',{})[key]=disposition
            missing[symbol]-=qty
            if kind=='OPEXC':
                self.state.setdefault('broker_lifecycle_reviews',{})[key]=dict(symbol=symbol,reason='Broker confirms exercise. Underlying shares and cash must be reconciled before new entries.',activity_id=a['id'])
            self.save()
