"""Pure decisions for filled, individually costed option buy lots."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo
NY=ZoneInfo('America/New_York')

def decide(lot,quote,now,rules):
    remaining=lot['remaining']
    if remaining<=0:return None
    if now.astimezone(NY).strftime('%H:%M:%S')>=rules['close_time']:
        return remaining,'risk_close'
    age=(now-datetime.fromisoformat(quote['timestamp'].replace('Z','+00:00'))).total_seconds()
    bid=float(quote['bid']);ask=float(quote['ask'])
    firm=-1<=age<=3 and 0<=bid<=ask and ask>0
    stop=rules['stop_loss'] if lot['kind']=='entry' else rules['addon_stop_loss']
    if firm and bid<=lot['price']*(1-stop)+1e-9:lot['stop_triggered']=True
    valid=firm and bid>0 and int(quote['bid_size'])>0
    if not valid:
        lot['breaches']=0
        return None
    price=lot['price'];ret=bid/price-1
    if lot.get('stop_triggered'):return remaining,'stop_loss'
    sample=(now-datetime.fromisoformat(lot.get('last_sample',lot['filled_at']))).total_seconds()
    if sample>=rules['lock_interval_ms']/1000:
        lot['last_sample']=now.isoformat()
        if sample>rules['lock_interval_ms']/1000+3:lot['breaches']=0
        lot['peak']=max(lot.get('peak',0),ret)
        if ret>=rules['lock_arm']-1e-9:lot['armed']=True
        lot['breaches']=lot.get('breaches',0)+1 if lot.get('armed') and ret<=rules['lock_retention']*lot['peak']+1e-9 else 0
        if lot['breaches']>=rules['lock_confirmations']:return remaining,'bird2_profit_lock'
    first=min(lot['qty']-1,math.floor(lot['qty']*rules['target_fractions'][0]))
    second=min(lot['qty']-first-1,math.floor((lot['qty']-first)*rules['target_fractions'][1]))
    first_left=first-lot.get('sold_target_1',0)
    if first_left>0 and ret>=rules['targets'][0]-1e-9:return min(first_left,remaining),'target_1'
    second_left=second-lot.get('sold_target_2',0)
    if first_left<=0 and second_left>0 and ret>=rules['targets'][1]-1e-9:return min(second_left,remaining),'target_2'
    return None
