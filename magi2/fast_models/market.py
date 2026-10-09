"""Bounded, asynchronous public spot/perpetual feeds and explicit FX quotes."""
import asyncio
from datetime import datetime
import math
import time

ASSETS=('BTC','ETH','XRP','SOL','NEAR')
SPOT_FEE={'upbit':.0005,'bithumb':.0025,'binance':.001,'kraken':.004}
SLIP=.0005


def now_ms():return time.time_ns()//1_000_000

def number(x):
    n=float(x)
    if not math.isfinite(n):raise ValueError('NONFINITE')
    return n

def iso_ms(s):return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()*1000)

def key(venue,kind,asset):return f'{venue}:{kind}:{asset}'

def fresh(q,ts,limit=15_000):
    return bool(q and 0<=ts-q['ts']<=limit and q['rtt']<=3000 and
        (q.get('exchange_ts') is None or -1000<=ts-q['exchange_ts']<=limit))

def quote(venue,kind,asset,currency,symbol,bids,asks,ts,rtt,exchange_ts=None,**extra):
    bids=[(number(x[0]),number(x[1])) for x in bids if number(x[1])>0]
    asks=[(number(x[0]),number(x[1])) for x in asks if number(x[1])>0]
    if not bids or not asks or min(x[0] for x in bids+asks)<=0 or bids[0][0]>=asks[0][0]:raise ValueError('BAD_BOOK')
    if any(a[0]<=b[0] for a,b in zip(bids,bids[1:])) or any(a[0]>=b[0] for a,b in zip(asks,asks[1:])):raise ValueError('BAD_SORT')
    return dict(id=key(venue,kind,asset),venue=venue,kind=kind,asset=asset,quote=currency,symbol=symbol,
        bids=bids,asks=asks,ts=ts,rtt=rtt,exchange_ts=exchange_ts,
        fee=SPOT_FEE[venue] if kind=='spot' else .0005,**extra)


class Collector:
    def __init__(self,session):
        self.session=session;self.books={};self.metadata={};self.funding={};self.health={};self.until={}
        self.meta_ts=0;self.fund_ts=0;self.regime={'state':'UNAVAILABLE','ts':0}

    async def get(self,url,params=None):
        host=url.split('/')[2]
        if now_ms()<self.until.get(host,0):raise ValueError('API_COOLDOWN')
        start=now_ms()
        async with self.session.get(url,params=params,timeout=8) as r:
            if r.status in (418,429,451):
                self.until[host]=now_ms()+300_000
            r.raise_for_status();data=await r.json(content_type=None)
        return data,now_ms(),now_ms()-start

    async def guarded(self,name,fn):
        try:
            await fn();self.health[name]={'state':'OK','ts':now_ms()}
        except Exception as e:
            self.health[name]={'state':'UNAVAILABLE','ts':now_ms(),'reason':type(e).__name__+':'+str(e)[:130]}

    async def meta(self):
        async def bn(kind):
            path='https://api.binance.com/api/v3/exchangeInfo' if kind=='spot' else 'https://fapi.binance.com/fapi/v1/exchangeInfo'
            d,t,_=await self.get(path)
            for s in d['symbols']:
                if s['baseAsset'] not in ASSETS or s['quoteAsset']!='USDT' or s['status']!='TRADING':continue
                if kind=='perp' and (s['contractType']!='PERPETUAL' or s['marginAsset']!='USDT'):continue
                f={x['filterType']:x for x in s['filters']};lot=f['LOT_SIZE']
                nt=f.get('NOTIONAL',f.get('MIN_NOTIONAL',{}))
                self.metadata[key('binance',kind,s['baseAsset'])]=dict(step=float(lot['stepSize']),min_qty=float(lot['minQty']),
                    min_notional=float(nt.get('minNotional',nt.get('notional',0))),ts=t,symbol=s['symbol'],type='linear' if kind=='perp' else 'spot')
        async def ks():
            d,t,_=await self.get('https://api.kraken.com/0/public/AssetPairs')
            for s in d['result'].values():
                a=s.get('wsname','').split('/')
                if len(a)!=2 or a[1]!='USD':continue
                asset='BTC' if a[0]=='XBT' else a[0]
                if asset not in ASSETS or s.get('status')!='online':continue
                self.metadata[key('kraken','spot',asset)]=dict(step=10**-s['lot_decimals'],min_qty=float(s['ordermin']),
                    min_notional=float(s.get('costmin',0)),ts=t,symbol=s['altname'],type='spot')
        async def kf():
            d,t,_=await self.get('https://futures.kraken.com/derivatives/api/v3/instruments')
            for s in d['instruments']:
                asset={'XBT':'BTC'}.get(s.get('base'),s.get('base'))
                if asset not in ASSETS or s.get('type')!='flexible_futures' or not s['symbol'].startswith('PF_'):continue
                if not s.get('tradeable') or s.get('postOnly') or s.get('isExpired') or s.get('contractSize')!=1:continue
                self.metadata[key('kraken','perp',asset)]=dict(step=10**-s['contractValueTradePrecision'],min_qty=10**-s['contractValueTradePrecision'],
                    min_notional=0,ts=t,symbol=s['symbol'],type='linear',margin_levels=s['marginLevels'])
        await asyncio.gather(*(self.guarded(n,f) for n,f in [('binance_spot_meta',lambda:bn('spot')),('binance_perp_meta',lambda:bn('perp')),('kraken_spot_meta',ks),('kraken_perp_meta',kf)]))
        self.meta_ts=now_ms()

    async def domestic(self,venue):
        host='api.upbit.com' if venue=='upbit' else 'api.bithumb.com'
        assets=ASSETS+('USDT',) if venue=='upbit' else ASSETS
        d,t,rtt=await self.get(f'https://{host}/v1/orderbook',{'markets':','.join('KRW-'+a for a in assets)})
        for x in d:
            a=x['market'][4:];units=x['orderbook_units'];stamp=int(x['timestamp'])
            if stamp>10**14:stamp//=1000
            self.books[key(venue,'spot',a)]=quote(venue,'spot',a,'KRW',x['market'],
                [(u['bid_price'],u['bid_size']) for u in units],[(u['ask_price'],u['ask_size']) for u in units],t,rtt,stamp,
                step=1e-8,min_qty=1e-8,min_notional=5000,meta_ts=t)

    async def foreign_book(self,venue,kind,asset):
        k=key(venue,kind,asset);m=self.metadata.get(k)
        if not m:raise ValueError('METADATA_UNAVAILABLE')
        if venue=='binance':
            root='https://api.binance.com/api/v3' if kind=='spot' else 'https://fapi.binance.com/fapi/v1'
            d,t,r=await self.get(root+'/depth',{'symbol':m['symbol'],'limit':20})
            b,a=d['bids'],d['asks'];stamp=d.get('E');currency='USDT'
        elif kind=='spot':
            d,t,r=await self.get('https://api.kraken.com/0/public/Depth',{'pair':m['symbol'],'count':20})
            x=next(iter(d['result'].values()));b,a=x['bids'],x['asks'];stamp=None;currency='USD'
        else:
            d,t,r=await self.get('https://futures.kraken.com/derivatives/api/v3/orderbook',{'symbol':m['symbol']})
            b=sorted(d['orderBook']['bids'],key=lambda x:float(x[0]),reverse=True)
            a=sorted(d['orderBook']['asks'],key=lambda x:float(x[0]));stamp=iso_ms(d['serverTime']);currency='USD'
        self.books[k]=quote(venue,kind,asset,currency,m['symbol'],b,a,t,r,stamp,
            step=m['step'],min_qty=m['min_qty'],min_notional=m['min_notional'],meta_ts=m['ts'])

    async def fxbook(self):
        d,t,r=await self.get('https://api.kraken.com/0/public/Depth',{'pair':'USDTUSD','count':20})
        x=next(iter(d['result'].values()))
        self.books['USDTUSD']=quote('kraken','spot','USDT','USD','USDTUSD',x['bids'],x['asks'],t,r)

    def fx(self,ts):
        result={'KRW':{'bid':1.,'ask':1.,'ts':ts}}
        k=self.books.get('upbit:spot:USDT');u=self.books.get('USDTUSD')
        if fresh(k,ts):result['USDT']=dict(bid=k['bids'][0][0],ask=k['asks'][0][0],ts=k['ts'])
        if fresh(k,ts) and fresh(u,ts):
            result['USD']=dict(bid=k['bids'][0][0]/u['asks'][0][0],ask=k['asks'][0][0]/u['bids'][0][0],ts=min(k['ts'],u['ts']))
        return result

    async def funding_data(self):
        async def bn(a):
            symbol=a+'USDT';k=key('binance','perp',a)
            d,t,_=await self.get('https://fapi.binance.com/fapi/v1/premiumIndex',{'symbol':symbol})
            h,_,_=await self.get('https://fapi.binance.com/fapi/v1/fundingRate',{'symbol':symbol,'limit':100})
            self.funding[k]=dict(ts=t,mark=float(d['markPrice']),index=float(d['indexPrice']),rate=float(d['lastFundingRate']),
                next=int(d['nextFundingTime']),events=[dict(ts=int(x['fundingTime']),rate=float(x['fundingRate']),mark=float(x['markPrice'])) for x in h])
        async def kraken():
            d,t,_=await self.get('https://futures.kraken.com/derivatives/api/v3/tickers')
            for x in d['tickers']:
                for a in ASSETS:
                    m=self.metadata.get(key('kraken','perp',a),{})
                    if x['symbol']!=m.get('symbol') or x.get('suspended') or x.get('postOnly'):continue
                    k=key('kraken','perp',a)
                    rates=self.funding.get(k,{}).get('rates',{})
                    if not rates:
                        h,_,_=await self.get('https://futures.kraken.com/derivatives/api/v3/historical-funding-rates',{'symbol':x['symbol']})
                        rates={str(iso_ms(r['timestamp'])):float(r['fundingRate']) for r in h['rates'] if iso_ms(r['timestamp'])>=t-7*86_400_000}
                    # Absolute USD per base-unit per hour, valid for this UTC hour.
                    server_ts=iso_ms(d['serverTime'])
                    rates[str(server_ts//3_600_000*3_600_000)]=float(x['fundingRate'])
                    rates={k:v for k,v in rates.items() if int(k)>=t-7*86_400_000}
                    self.funding[k]=dict(ts=t,mark=float(x['markPrice']),index=float(x['indexPrice']),
                        rate=float(x['relativeFundingRate']),rates=rates,
                        next=(t//3_600_000+1)*3_600_000)
        await asyncio.gather(self.guarded('kraken_funding',kraken),*(self.guarded('binance_funding_'+a,lambda a=a:bn(a)) for a in ASSETS))
        self.fund_ts=now_ms()

    async def btc_regime(self):
        d,t,_=await self.get('https://api.upbit.com/v1/candles/minutes/240',{'market':'KRW-BTC','count':60})
        rows=sorted((iso_ms(x['candle_date_time_utc']+'Z'),float(x['trade_price'])) for x in d)
        rows=[x for x in rows if x[0]+14_400_000<=t-3000]
        if len(rows)<30 or any(b[0]-a[0]!=14_400_000 for a,b in zip(rows,rows[1:])):raise ValueError('BTC_HISTORY_GAP')
        if not 3000<=t-(rows[-1][0]+14_400_000)<14_403_000:raise ValueError('BTC_HISTORY_STALE')
        ema=rows[0][1];previous=ema
        for _,p in rows[1:]:previous=ema;ema=ema+2/21*(p-ema)
        self.regime=dict(state='DOWN' if rows[-1][1]<ema and ema<previous else 'OTHER',ts=t,
            close=rows[-1][1],ema20=ema,previous_ema20=previous,bar_end=rows[-1][0]+14_400_000,
            source='UPBIT_COMPLETED_4H_EMA20')

    async def poll(self):
        if now_ms()-self.meta_ts>3_600_000:await self.meta()
        tasks=[self.guarded(v+'_spot',lambda v=v:self.domestic(v)) for v in ('upbit','bithumb')]
        tasks.append(self.guarded('fx_USDTUSD',self.fxbook))
        # At most six concurrent public requests; bounded timeouts and per-host cooldown.
        semaphore=asyncio.Semaphore(6)
        async def book(v,k,a):
            async with semaphore:await self.guarded(f'{v}_{k}_{a}',lambda:self.foreign_book(v,k,a))
        tasks.extend(book(v,k,a) for v in ('binance','kraken') for k in ('spot','perp') for a in ASSETS)
        if now_ms()-self.fund_ts>60_000:tasks.append(self.funding_data())
        if now_ms()-self.regime['ts']>300_000:tasks.append(self.guarded('btc_regime',self.btc_regime))
        await asyncio.gather(*tasks)
        return self.books,self.fx(now_ms())
