"""Real spot balances only; no sample fallback, no invented cost/strategy basis."""
import time
from .adapters.base import CredentialsNotReady
from .market_data import number
VENUES=('upbit','binance','kraken','bithumb')


def normalise(venue,raw,asset_names=None):
    out=[]
    if venue in ('upbit','bithumb'):
        source=[(x['currency'],x) for x in raw]
    elif venue=='binance':source=[(x['asset'],x) for x in raw['balances']]
    else:source=list(raw.items())
    for symbol,x in source:
        cost=cost_currency=None
        if venue in ('upbit','bithumb'):
            free,locked=number(x['balance']),number(x['locked'])
            avg=number(x.get('avg_buy_price') or 0)
            cost_currency=x.get('unit_currency')
            if avg>0 and cost_currency=='KRW':cost=(free+locked)*avg
        elif venue=='binance':free,locked=number(x['free']),number(x['locked'])
        else:
            if number(x.get('credit_used',0))!=0:raise ValueError('MARGIN_BALANCE_UNSUPPORTED')
            total=number(x['balance']);locked=number(x.get('hold_trade',0));free=total-locked
        if free < -1e-12 or locked<0:raise ValueError('INVALID_ACCOUNT_BALANCE')
        if free+locked<=0:continue
        asset=(asset_names or {}).get(symbol,symbol)
        asset={'XBT':'BTC','XXBT':'BTC','XETH':'ETH','ZUSD':'USD','ZEUR':'EUR'}.get(asset,asset)
        out.append({'asset':asset,'exchange_asset':symbol,'free':free,'locked':locked,
                    'qty':free+locked,'cost_krw':cost,'cost_basis':'exchange_average' if cost is not None else 'unknown',
                    'pick_basis':['UNATTRIBUTED'],'signal_id':None})
    return out


def collect_accounts(adapters,market,now_ms=None):
    start=now_ms or time.time_ns()//1000000
    venues=[]
    for venue in VENUES:
        row={'venue':venue,'status':'UNAVAILABLE','positions':[], 'valued_total_krw':None,
             'unrealized_pnl_krw':None,'observed_ts_ms':None,'error':None}
        try:
            adapter=adapters[venue]
            raw=(adapter.accounts() if venue in ('upbit','bithumb') else
                 adapter.account() if venue=='binance' else adapter.balance())
            names=None
            if venue=='kraken':
                names={k:v.get('altname',k) for k,v in adapter.assets().items()}
            row['observed_ts_ms']=time.time_ns()//1000000
            row['positions']=normalise(venue,raw,names)
            for p in row['positions']:
                p.update(value_krw=None,unrealized_pnl_krw=None,price_krw=None,valuation_status='UNPRICED')
                try:
                    quote=market.mark(venue,p['asset'])
                    price=number(quote['price_krw'])
                    if price<=0 or not 0<=time.time_ns()//1000000-quote['received_ts_ms']<=60000:raise ValueError('STALE_MARK')
                    p.update(price_krw=price,value_krw=p['qty']*price,valuation_status='VALUED',valuation_source=quote['source'],price_ts_ms=quote['received_ts_ms'])
                    if p['cost_krw'] is not None:p['unrealized_pnl_krw']=p['value_krw']-p['cost_krw']
                except Exception:p['valuation_status']='UNPRICED'
            priced=[p for p in row['positions'] if p['value_krw'] is not None]
            row['valued_total_krw']=sum(p['value_krw'] for p in priced)
            row['status']='OK' if len(priced)==len(row['positions']) else 'PARTIAL_VALUATION'
            noncash=[p for p in row['positions'] if p['asset']!='KRW']
            if all(p['unrealized_pnl_krw'] is not None for p in noncash):
                row['unrealized_pnl_krw']=sum(p['unrealized_pnl_krw'] for p in noncash)
        except CredentialsNotReady:row['error']='CREDENTIALS_NOT_READY'
        except Exception as exc:
            status=getattr(getattr(exc,'response',None),'status_code',None)
            row['error']='AUTH_OR_PERMISSION' if status in (401,403) else 'ACCOUNT_QUERY_FAILED'
        venues.append(row)
    finished=time.time_ns()//1000000
    for v in venues:
        if v['status']=='OK' and (finished-v['observed_ts_ms']>180000 or any(finished-p.get('price_ts_ms',0)>180000 for p in v['positions'])):
            v['status']='STALE_VALUATION'
    complete=all(v['status']=='OK' for v in venues)
    available=[v for v in venues if v['valued_total_krw'] is not None]
    total=sum(v['valued_total_krw'] for v in available) if available else None
    return {'schema':'magi3-accounts-v1','mode':'REAL_READ_ONLY','currency':'KRW',
        'generated_ts_ms':time.time_ns()//1000000,'started_ts_ms':start,'complete':complete,
        'total_asset_krw':total if complete else None,'known_subtotal_krw':total,
        'venues':venues,'realized_pnl_krw':None,'strategy_summary':None,
        'valuation_note':'Spot API balances only; Earn/futures/subaccounts are not aggregated. Overseas FX is market-implied via USDT, not a bank FX fixing. Missing values and unknown costs are not zero. Existing assets are UNATTRIBUTED.'}


def money(x):return '미확인' if x is None else f'{x:,.0f}원'


def render_accounts(report,now_ms=None):
    now=now_ms or time.time_ns()//1000000
    age=max(0,(now-report['generated_ts_ms'])//1000)
    stale=age>180
    lines=['💼 실계좌 통합자산 [조회 전용]',f'갱신 {age}초 전'+(' · 오래된 자료' if stale else ''),
        ('전체 합계: ' if report['complete'] and not stale else '확인된 부분합: ')+money(report['known_subtotal_krw'])]
    for v in report['venues']:
        lines+=['',f"[{v['venue'].upper()}] {v['status']} · {money(v['valued_total_krw'])}"]
        if v['observed_ts_ms']:
            venue_age=max(0,(now-v['observed_ts_ms'])//1000)
            lines.append(f'잔고 관측 {venue_age}초 전'+(' · 오래된 자료' if venue_age>180 else ''))
        if v['error']:lines.append('조회 상태: '+v['error'])
        for p in v['positions']:
            lines.append(f"{p['asset']} {p['qty']:.8g} | 평가 {money(p['value_krw'])} | 평가손익 {money(p['unrealized_pnl_krw'])} | PICK=UNATTRIBUTED")
    lines+=['','부분합에는 조회 실패·미평가 자산이 빠집니다. 해외 환산은 시장 USDT 경유 기준입니다.',
            '기존 보유자산의 전략·실현손익은 확인되지 않았습니다. Shadow 성과는 /shadow로 분리 조회하세요.']
    return '\n'.join(lines)
