"""Reproducible six-venue active spot intersection using actual pair metadata."""
from __future__ import annotations
import asyncio
import json
import logging
import math
import time
from pathlib import Path
import aiohttp

LOG=logging.getLogger(__name__)
VENUES=('binance','bybit','kraken','upbit','bithumb','coinone')

async def get(session,url,params=None):
    async with session.get(url,params=params) as r:
        r.raise_for_status(); data=await r.json()
        if isinstance(data,dict) and (data.get('error') or data.get('retCode',0)!=0 or data.get('result')=='error'):
            raise ValueError(f'API error {url}: {data}')
        return data

async def venue_markets(session,v):
    if v=='binance':
        info=await get(session,'https://api.binance.com/api/v3/exchangeInfo')
        ticks=await get(session,'https://api.binance.com/api/v3/ticker/24hr')
        pairs={x['symbol']:x['baseAsset'] for x in info['symbols'] if x['quoteAsset']=='USDT' and x['status']=='TRADING' and x.get('isSpotTradingAllowed',True)}
        return {pairs[x['symbol']]:{'pair':x['symbol'],'quote':'USDT','turnover':float(x['quoteVolume'])} for x in ticks if x['symbol'] in pairs}
    if v=='bybit':
        info=await get(session,'https://api.bybit.com/v5/market/instruments-info',{'category':'spot'})
        ticks=await get(session,'https://api.bybit.com/v5/market/tickers',{'category':'spot'})
        pairs={x['symbol']:x['baseCoin'] for x in info['result']['list'] if x['quoteCoin']=='USDT' and x['status']=='Trading'}
        return {pairs[x['symbol']]:{'pair':x['symbol'],'quote':'USDT','turnover':float(x['turnover24h'])} for x in ticks['result']['list'] if x['symbol'] in pairs}
    if v=='kraken':
        info=(await get(session,'https://api.kraken.com/0/public/AssetPairs'))['result']
        ticks=(await get(session,'https://api.kraken.com/0/public/Ticker'))['result']
        out={}
        for key,x in info.items():
            pair=x.get('wsname','')
            if not pair.endswith('/USD') or x.get('status')!='online' or key not in ticks: continue
            base=pair.split('/')[0]; base={'XBT':'BTC','XDG':'DOGE'}.get(base,base)
            t=ticks[key]
            out[base]={'pair':f'{base}/USD','quote':'USD','turnover':float(t['v'][1])*float(t['p'][1])}
        return out
    if v in ('upbit','bithumb'):
        host=f'https://api.{v}.com'
        info=await get(session,host+'/v1/market/all')
        pairs=[x['market'] for x in info if x['market'].startswith('KRW-')]
        ticks=[]
        for i in range(0,len(pairs),50):
            ticks.extend(await get(session,host+'/v1/ticker',{'markets':','.join(pairs[i:i+50])})); await asyncio.sleep(.15)
        return {x['market'][4:]:{'pair':x['market'],'quote':'KRW','turnover':float(x['acc_trade_price_24h'])} for x in ticks}
    info=await get(session,'https://api.coinone.co.kr/public/v2/markets/KRW')
    ticks=await get(session,'https://api.coinone.co.kr/public/v2/ticker_new/KRW')
    active={x['target_currency'].upper() for x in info['markets'] if int(x['trade_status'])==1 and int(x['maintenance_status'])==0}
    return {x['target_currency'].upper():{'pair':x['target_currency'].upper()+'-KRW','quote':'KRW','turnover':float(x['quote_volume'])} for x in ticks['tickers'] if x['target_currency'].upper() in active}

def select(markets,size=5):
    if set(markets)!=set(VENUES): raise ValueError('six complete venues required')
    markets={v:{a:x for a,x in m.items() if math.isfinite(x['turnover']) and x['turnover']>0} for v,m in markets.items()}
    common=set.intersection(*(set(m) for m in markets.values()))
    # Stablecoins/fiat are excluded from directional shock research.
    common-= {'USDT','USDC','DAI','TUSD','FDUSD','USD','EUR','KRW'}
    if len(common)<size: raise ValueError(f'only {len(common)} common active liquid assets; need {size}')
    ranks={v:{a:i/max(1,len(m)-1) for i,(a,x) in enumerate(sorted(m.items(),key=lambda item:(-item[1]['turnover'],item[0])))} for v,m in markets.items()}
    scores={a:sum(1-ranks[v][a] for v in VENUES)/6 for a in common}
    assets=sorted(common,key=lambda a:(-scores[a],a))[:size]
    return {'selected_at_ms':time.time_ns()//1_000_000,'assets':assets,'eligible_count':len(common),'method':'mean_venue_turnover_percentile','size_proxy':'24h traded notional, NOT circulating market capitalization','scores':{a:scores[a] for a in assets},'inputs':markets}

async def discover(path=None,size=5):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        results=await asyncio.gather(*(venue_markets(session,v) for v in VENUES),return_exceptions=True)
    errors={v:str(r) for v,r in zip(VENUES,results) if isinstance(r,Exception)}
    if errors: raise RuntimeError(f'universe discovery failed: {errors}')
    result=select(dict(zip(VENUES,results)),size)
    if path:
        p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
        temp=p.with_suffix('.tmp'); temp.write_text(json.dumps(result),encoding='utf-8'); temp.replace(p)
    LOG.info('universe_selected assets=%s eligible=%s method=%s',result['assets'],result['eligible_count'],result['method'])
    return result
