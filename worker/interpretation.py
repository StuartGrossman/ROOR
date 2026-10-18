"""Versioned text interpretation shared by API, paper execution and replay.

Interpretations describe source intent; ownership, freshness and fills are separate.
"""
import re
from signals import parse_signal, directed_add_legs

PARSER_VERSION = '2.0.1'

def normalize(text):
    return ' '.join(text.lower().replace('’', "'").split())

def direct_sell_choice(text):
    return bool(re.fullmatch(r'(?:you can )?(?:sell(?: now)? or hold|hold or sell(?: now)?)(?:,? (?:your choice|up to you))?[.!]?',text)
                or re.fullmatch(r'you can sell for meh gains or risk it all for itm[.!]?',text))

def protection_kind(text):
    text=normalize(text)
    if '?' in text or re.search(r"\b(?:do not|don't|dont|never) (?:set|have)\b",text):return None
    if re.search(r'\b(?:set|setting|have)(?: a)?(?: tight)? profit stops?\b',text):return 'profit_protection'
    if re.fullmatch(r'(?:must )?have a tight stop[.!]*',text):return 'tight_profit_protection'
    return None

def holding_pair(text):
    text=normalize(text).rstrip('.!')
    text=re.sub(r'(\d+(?:\.\d+)?) (calls?|puts?) and (\d+(?:\.\d+)?)$',r'\1 and \3 \2',text)
    return re.fullmatch(r"(?:i'?m |im |still )?holding (?:the )?(\d+(?:\.\d+)?) and (\d+(?:\.\d+)?) (calls?|puts?)",text)

def classify_guidance(text):
    text=normalize(text).rstrip('.!')
    if '?' in text:return None
    if re.fullmatch(r"(?:do not|don't|dont) sell(?: yet)?|cancel (?:the )?(?:sell|exit)(?: guidance)?|keep holding",text):return 'cancel_exit'
    protection=protection_kind(text)
    if protection:return protection
    if re.match(r'^(?:you can )?(?:sell when you see green|when you see green sell)\b',text):return 'conditional_profit'
    if re.search(r'\b(?:sell|cut|exit)\b',text) and re.search(r'\b(?:if|when|once|on any sign)\b',text):return 'conditional_exit_undefined'
    if re.search(r'\b(?:get ready to sell|prepare to sell|be ready to sell|fingers on the sell|know the (?:fucking )?risk)\b',text):return 'exit_warning'
    if re.fullmatch(r'(?:sell|exit|cut)(?: it| them| all| everything)? now(?: guys)?|start selling(?: we green af)?',text):return 'actionable_exit'
    if text=='now':return 'contextual_now'
    return None

def interpret_signal(text,sent_at):
    signal=parse_signal(text,sent_at)
    if len(text)>4000:return signal
    n=normalize(text)
    if signal['kind'] in {'entry','exit','add'}:return signal
    if holding_pair(n):return {'kind':'guidance','guidance_class':'position_focus','right':None,'needs_position_context':True}
    guidance=classify_guidance(n)
    if guidance:
        result={'kind':'exit' if guidance=='actionable_exit' else 'guidance','guidance_class':guidance,'right':None,'needs_position_context':True}
        m=re.search(r'\b(\d+(?:\.\d+)?)\s*(?:spy\s*|spx\s*)?(calls?|puts?)\b',n)
        if m:result.update(strike=m[1],right=m[2].rstrip('s'))
        return result
    if n.rstrip('.!') in {'your choice','or','risk it all and hold','or sell'}:
        return {'kind':'guidance','guidance_class':'sell_choice_context','right':None,'needs_position_context':True}
    if direct_sell_choice(n):return {'kind':'exit','right':None,'choice_exit':True,'needs_position_context':True}
    legs=directed_add_legs(text)
    if legs:return legs[0] if len(legs)==1 else {'kind':'add_batch','legs':legs,'needs_position_context':True}
    if '?' not in n:
        trim=re.fullmatch(r'(?:trim|sell) (half|a quarter|one quarter|\d+(?:\.\d+)?%|\d+ contracts?)(?: of (?:the |my )?(?:position|calls?|puts?))?[.!]?',n)
        if trim:
            amount=trim[1];result={'kind':'exit','action':'trim','right':None,'needs_position_context':True}
            if amount in {'half','a quarter','one quarter'}:result['exit_fraction']='0.5' if amount=='half' else '0.25'
            elif amount.endswith('%'):
                from decimal import Decimal
                fraction=Decimal(amount[:-1])/100
                if not 0<fraction<=1:return {'kind':'review','guidance_class':'possible_trim','reason':'Invalid trim fraction.'}
                result['exit_fraction']=str(fraction)
            else:result['exit_qty']=int(amount.split()[0])
            side=re.search(r'\b(calls?|puts?)\b',n)
            if side:result['right']=side[1].rstrip('s')
            return result
        add=re.fullmatch(r'added (?P<qty>\d+) (?:more )?(?:at |for )?(?P<cents>\d+(?:\.\d+)?)(?: cents?)?[.!]?',n)
        if add and int(add['qty'])>0 and float(add['cents'])>0:return {'kind':'add','right':None,'needs_position_context':True}
        if re.fullmatch(r"(?:stop loss triggered|sl triggered|outz|i'?m (?:outz|outs)(?: guys)?|(?:sl|stop loss) triggered(?:\.? (?:out )?at \d+(?:\.\d+)? cents?)?|got sl at \d+(?:\.\d+)? cents?|stopped out(?: 😫)?|profit stopped|sold the (?:top|peak)|sold\d+(?:\.\d+)?|sell the move guys|what'?s with the eyes, i been sold)[.!]?",n):
            return {'kind':'exit','right':None,'needs_position_context':True}
        for kind,pattern in [('exit',r'(?:sell|sold|cut|out)(?: the)? (?:(\d+(?:\.\d+)?) )?(calls?|puts?)(?: at \d+(?:\.\d+)? cents)?[.!]?'),('add',r'(?:double|triple|tripe|quadruple|quadriple)(?: the| to the)? (?:(\d+(?:\.\d+)?) )?(calls?|puts?)[.!]?')]:
            m=re.fullmatch(pattern,n)
            if m:return {'kind':kind,'strike':m[1],'right':m[2].rstrip('s'),'needs_position_context':True}
        if re.fullmatch(r'cut at \d+(?:\.\d+)? cents[.!]?',n):return {'kind':'exit','right':None,'needs_position_context':True}
        if re.fullmatch(r"(?:(?:i'?m|im|still) )?(?:holding|holdin|holdint|hodling)(?: the)? (?:\d+(?:\.\d+)?(?: and \d+(?:\.\d+)?)? (?:spy )?(?:calls?|puts?)|both(?: (?:calls|puts|sides))?(?: still)?)[.!]?",n):
            return {'kind':'guidance','guidance_class':'position_focus','right':None,'needs_position_context':True}
    if re.search(r'\btrim(?:ming|med)?\b',n):return {'kind':'review','guidance_class':'possible_trim','reason':'Review quantity, intent, and position before acting.'}
    return signal
