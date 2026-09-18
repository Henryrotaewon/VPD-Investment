"""FAST venue-local spot universes; independent of WAVE's intersection."""
import hashlib
import json
import time

STABLE_BASES={'USDT','USDC','DAI','TUSD','FDUSD','USDP','USD1','EUR','KRW'}


def universe(venue,payload,received_ms=None,max_symbols=0):
    markets=[];excluded={}
    rows=payload if venue=='upbit' else payload['symbols']
    for x in rows:
        reason=None
        if venue=='upbit':
            symbol=x['market'];quote,asset=symbol.split('-',1)
            flags=x.get('market_event') or {}
            if quote!='KRW':reason='OTHER_QUOTE'
            elif x.get('market_warning','NONE')!='NONE' or flags.get('warning') or any((flags.get('caution') or {}).values()):reason='MARKET_WARNING'
            filters={}
        elif venue=='binance':
            symbol=x['symbol'];asset=x['baseAsset'];quote=x['quoteAsset'];filters=x.get('filters',[])
            if quote!='USDT':reason='OTHER_QUOTE'
            elif x.get('status')!='TRADING' or not x.get('isSpotTradingAllowed',False):reason='NOT_ACTIVE_SPOT'
        else:raise ValueError('UNSUPPORTED_FAST_VENUE')
        if asset in STABLE_BASES:reason='STABLE_BASE'
        if reason:excluded[reason]=excluded.get(reason,0)+1;continue
        markets.append({'venue':venue,'symbol':symbol,'asset':asset,'quote':quote,'filters':filters})
    markets.sort(key=lambda x:x['symbol'])
    eligible=len(markets)
    if max_symbols:
        if max_symbols<1:raise ValueError('INVALID_CAP')
        markets=markets[:max_symbols]
    if not markets:raise ValueError('EMPTY_UNIVERSE')
    digest=hashlib.sha256(json.dumps(markets,sort_keys=True).encode()).hexdigest()
    return {'kind':'universe','venue':venue,'received_ts_ms':received_ms if received_ms is not None else time.time_ns()//1000000,
            'version':'fast-universe-v1','universe_id':digest,'eligible_count':eligible,
            'selected_count':len(markets),'scope':'ALL_ELIGIBLE' if eligible==len(markets) else 'EXPLICIT_TEST_CAP',
            'excluded_counts':excluded,'markets':markets}
