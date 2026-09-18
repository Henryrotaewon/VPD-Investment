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


VENUE_LABELS={'upbit':'업비트','bithumb':'빗썸','binance':'바이낸스','kraken':'크라켄'}
CASH_ASSETS={'KRW','USD','EUR','USDT','USDC','JPY','GBP'}


def holding_return(positions):
    """Current holdings return, cost weighted. Unknown basis is never zero."""
    invested=[p for p in positions if p['asset'] not in CASH_ASSETS]
    if not invested:return None
    if any(p.get('cost_krw') is None or p['cost_krw']<=0 or p.get('value_krw') is None for p in invested):return None
    cost=sum(p['cost_krw'] for p in invested)
    return (sum(p['value_krw'] for p in invested)/cost-1)*100


def percentage(value):return '미확인' if value is None else f'{value:+.2f}%'


def pick_strategy(position):
    tags=position.get('pick_basis') or []
    if isinstance(tags,str):tags=[tags]
    known=sorted(set(tags)-{'UNATTRIBUTED','UNKNOWN','unknown',''})
    # A joint strategy is one bucket: never double count the same holding.
    return '+'.join(known) if known and 'UNATTRIBUTED' not in tags and 'UNKNOWN' not in tags else '미분류'


def quantity(value):
    """Display only; preserve tiny positive holdings without showing zero."""
    return '<0.1' if 0<value<0.1 else f'{value:,.1f}'


def render_accounts(report,now_ms=None):
    now=time.time_ns()//1000000 if now_ms is None else now_ms
    venues=report.get('venues',[])
    connected=[v for v in venues if v.get('valued_total_krw') is not None and v.get('status')!='UNAVAILABLE' and not v.get('error')]
    positions=[p for v in connected for p in v.get('positions',[])]
    incomplete=any(v.get('status')=='PARTIAL_VALUATION' or any(p.get('value_krw') is None for p in v.get('positions',[])) for v in connected)
    unavailable=any(v.get('error')!='CREDENTIALS_NOT_READY' and v not in connected for v in venues)
    partial=incomplete or unavailable
    total=sum(v['valued_total_krw'] for v in connected) if connected else None
    rate=holding_return(positions) if not unavailable else None
    header='💼 실계좌 통합자산'
    if now-report.get('generated_ts_ms',now)>180000 or any(v.get('status')=='STALE_VALUATION' for v in connected):header+=' · 마지막 조회값'
    lines=[header,f"총 평가금액: {money(total)}"+(' (확인분)' if partial and total is not None else ''),
           f"보유 수익률: {percentage(rate)}"]
    groups={}
    for v in sorted(venues,key=lambda v:('upbit','bithumb','binance','kraken').index(v['venue']) if v['venue'] in VENUE_LABELS else 99):
        label=VENUE_LABELS.get(v['venue'],v['venue'])
        if v not in connected:
            lines.extend(['',f"{label}: "+('미연결' if v.get('error')=='CREDENTIALS_NOT_READY' else '금액 미확인')]);continue
        ps=v.get('positions',[])
        vpartial=any(p.get('value_krw') is None for p in ps)
        lines.extend(['',f"{label}: {money(v['valued_total_krw'])}"+(' (확인분)' if vpartial else '')])
        for p in sorted(ps,key=lambda p:(p['asset'] not in CASH_ASSETS,-(p.get('value_krw') or 0),p['asset'])):
            if p['asset'] in CASH_ASSETS:
                name='예수금' if p['asset']=='KRW' else f"예수금 {p['asset']}"
                lines.append(f"• {name}: {money(p.get('value_krw'))}");continue
            strategy=pick_strategy(p)
            lines.append(f"• {p['asset']} {quantity(p['qty'])}개 · {money(p.get('value_krw'))} · {percentage(holding_return([p]))} ({strategy})")
            groups.setdefault(strategy,[]).append(p)
        if not ps:lines.append('보유 잔고 없음')
    lines.extend(['','픽 전략별 · 현재 보유분'])
    if not connected:lines.append('투자금액·수익률 미확인')
    else:
        absent=[tag for tag in ('VPD','FAST','WAVE') if not any(tag in name.split('+') for name in groups)]
        if absent:lines.append(' · '.join(absent)+': 보유 없음')
        for strategy,ps in sorted(groups.items(),key=lambda item:(item[0]=='미분류',item[0])):
            cost=sum(p['cost_krw'] for p in ps) if all(p.get('cost_krw') is not None and p['cost_krw']>0 for p in ps) else None
            lines.append(f'{strategy}: 투자 {money(cost)} · {percentage(holding_return(ps))}')
    lines.extend(['','수익률: 보유종목 매입금액 기준 · 예수금 제외'])
    if partial or len(connected)<len(venues):lines.append('합계는 연결 계좌의 확인된 평가금액 기준입니다.')
    return '\n'.join(lines)
