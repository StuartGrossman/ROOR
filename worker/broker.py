"""Alpaca adapter with fixed paper trading and OPRA data destinations."""

from decimal import Decimal
import requests

from execution import RejectedOrder, UncertainSubmission

PAPER = 'https://paper-api.alpaca.markets'
DATA = 'https://data.alpaca.markets'


class PaperBroker:
    def __init__(self, keys):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({'APCA-API-KEY-ID': keys['key_id'].strip(),
                                     'APCA-API-SECRET-KEY': keys['secret_key'].strip()})

    def request(self, method, path, *, data=False, absent_ok=False, **kwargs):
        try:
            r = self.session.request(method, (DATA if data else PAPER) + path,
                                     timeout=(3, 10), allow_redirects=False, **kwargs)
        except requests.RequestException:
            if method == 'POST':
                raise UncertainSubmission() from None
            raise RuntimeError('Alpaca connection unavailable.') from None
        if r.status_code == 404 and absent_ok:
            return None
        if not 200 <= r.status_code < 300:
            if method == 'POST':
                # A duplicate client ID can mean the earlier POST was accepted.
                if r.status_code in {400, 401, 403}:
                    raise RejectedOrder() from None
                if r.status_code == 422 and 'client_order_id' not in r.text:
                    raise RejectedOrder() from None
                raise UncertainSubmission() from None
            raise RuntimeError(f'Alpaca read/cancel failed with HTTP {r.status_code}.')
        if r.status_code == 204:
            return None
        try:
            return r.json()
        except ValueError:
            if method == 'POST':
                raise UncertainSubmission() from None
            raise RuntimeError('Alpaca returned invalid JSON.') from None

    def check_account(self):
        account = self.request('GET', '/v2/account')
        if account['status'] != 'ACTIVE' or account.get('trading_blocked') or int(account.get('options_trading_level', 0)) < 2:
            raise RuntimeError('Paper account is not enabled for buying options.')
        return {'environment': 'paper', 'status': account['status'],
                'options_trading_level': account['options_trading_level']}

    def is_open(self):
        return self.request('GET', '/v2/clock')['is_open'] is True

    def contract(self, signal):
        result = self.request('GET', '/v2/options/contracts', params={
            'underlying_symbols': signal['ticker'], 'root_symbol': signal['ticker'],
            'expiration_date': signal['expiration'], 'type': signal['right'],
            'strike_price_gte': signal['strike'], 'strike_price_lte': signal['strike'],
            'status': 'active', 'limit': 100})
        matches = [c for c in result.get('option_contracts', [])
            if c['underlying_symbol'] == signal['ticker'] and c['root_symbol'] == signal['ticker']
            and c['expiration_date'] == signal['expiration'] and c['type'] == signal['right']
            and Decimal(c['strike_price']) == Decimal(signal['strike'])
            and c['tradable'] is True and c['status'] == 'active' and int(c['size']) == 100]
        if len(matches) != 1 or result.get('next_page_token'):
            return None
        c = matches[0]
        return {'symbol': c['symbol'], 'size': int(c['size']), 'strike': c['strike_price'],
                'right': c['type'], 'expiration': c['expiration_date']}

    def quote(self, symbol):
        result = self.request('GET', '/v1beta1/options/quotes/latest', data=True,
                              params={'symbols': symbol, 'feed': 'opra'})
        quote = result.get('quotes', {}).get(symbol)
        if not quote:
            raise RuntimeError('No OPRA quote is available.')
        return {'ask': quote['ap'], 'timestamp': quote['t']}

    def position(self, symbol):
        # Refuse to race a manually placed order in this same contract.
        orders = self.request('GET', '/v2/orders', params={'status': 'open', 'symbols': symbol, 'limit': 500})
        if len(orders) >= 500 or any(not o['client_order_id'].startswith('geerawr-') for o in orders):
            raise RuntimeError('An unrelated open order exists in this contract.')
        result = self.request('GET', f'/v2/positions/{symbol}', absent_ok=True)
        return Decimal(result['qty']) if result else Decimal(0)

    def full_quote(self, symbol):
        result = self.request('GET', '/v1beta1/options/quotes/latest', data=True,
                              params={'symbols': symbol, 'feed': 'opra'})
        quote = result.get('quotes', {}).get(symbol)
        if not quote:
            raise RuntimeError('No OPRA quote is available.')
        return {'ask': quote['ap'], 'bid': quote['bp'], 'ask_size': quote['as'],
                'bid_size': quote['bs'], 'timestamp': quote['t']}

    def buying_power(self):
        account = self.request('GET', '/v2/account')
        return Decimal(account.get('options_buying_power', account.get('cash', '0')))

    def lookup(self, client_id):
        return self.request('GET', '/v2/orders:by_client_order_id', absent_ok=True,
                            params={'client_order_id': client_id})

    def submit(self, payload):
        return self.request('POST', '/v2/orders', json=payload)

    def cancel(self, order_id):
        return self.request('DELETE', f'/v2/orders/{order_id}', absent_ok=True)


    def activity_order(self, order_id):
        return self.request('GET', f'/v2/orders/{order_id}', absent_ok=True)

    def account_activities(self, after):
        rows=[];token=None;seen=set()
        for _ in range(100):
            params={'activity_types':'FILL,OPEXP,OPEXC,OPTRD,OPASN','after':after,
                    'direction':'asc','page_size':100}
            if token:params['page_token']=token
            page=self.request('GET','/v2/account/activities',params=params)
            if not isinstance(page,list):raise RuntimeError('Invalid account activity response.')
            rows.extend(page)
            if len(page)<100:return rows
            next_token=page[-1].get('id')
            if not next_token or next_token in seen:raise RuntimeError('Account activity pagination did not advance.')
            seen.add(next_token);token=next_token
        raise RuntimeError('Account activity history exceeded the reconciliation page bound.')
