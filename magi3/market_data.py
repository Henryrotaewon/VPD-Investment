"""Public, bounded-depth quotes and explicit KRW valuation paths."""
import math
import threading
import time
from dataclasses import dataclass
from .adapters.upbit import UpbitAdapter
from .adapters.bithumb import BithumbAdapter
from .adapters.binance import BinanceAdapter
from .adapters.kraken import KrakenAdapter


def number(value):
    result=float(value)
    if not math.isfinite(result):raise ValueError('NON_FINITE_NUMBER')
    return result


@dataclass(frozen=True)
class Book:
    venue:str
    asset:str
    market:str
    bids:tuple
    asks:tuple
    received_ms:int
    valuation_path:str

    @property
    def mid(self):return (self.bids[0][0]+self.asks[0][0])/2


def clean_levels(rows,reverse=False):
    values=[(number(x[0]),number(x[1])) for x in rows]
    if not values or any(p<=0 or q<0 for p,q in values):raise ValueError('INVALID_BOOK')
    values=sorted((x for x in values if x[1]>0),reverse=reverse)
    if not values:raise ValueError('EMPTY_BOOK')
    return tuple(values)


def make_book(venue,asset,market,bids,asks,rate,path,now_ms=None):
    if not math.isfinite(rate) or rate<=0:raise ValueError('INVALID_FX')
    bids=clean_levels([(number(p)*rate,q) for p,q,*_ in bids],True)
    asks=clean_levels([(number(p)*rate,q) for p,q,*_ in asks])
    if bids[0][0]>asks[0][0]:raise ValueError('CROSSED_BOOK')
    return Book(venue,asset,market,bids,asks,now_ms or time.time_ns()//1000000,path)


class MarketData:
    def __init__(self,adapters=None):
        self.adapters=adapters or {'upbit':UpbitAdapter(),'bithumb':BithumbAdapter(),
                                  'binance':BinanceAdapter(),'kraken':KrakenAdapter()}
        self.cache={};self.pair_cache=None;self.lock=threading.RLock()

    def kraken_pair(self,asset,quote):
        if self.pair_cache is None:self.pair_cache=self.adapters['kraken'].pairs()
        base='XBT' if asset=='BTC' else asset
        for key,pair in self.pair_cache.items():
            if pair.get('wsname')==base+'/'+quote and pair.get('status','online')=='online':
                return pair.get('altname',key)
        raise ValueError('MARKET_UNAVAILABLE')

    def _raw(self,venue,asset):
        if venue in ('upbit','bithumb'):
            market='KRW-'+asset
            body=self.adapters[venue].orderbook(market)
            units=(body[0] if isinstance(body,list) else body)['orderbook_units']
            return market,[(x['bid_price'],x['bid_size']) for x in units],[(x['ask_price'],x['ask_size']) for x in units],1.,venue+':'+market
        if venue=='binance':
            market=asset+'USDT';body=self.adapters[venue].orderbook(market,limit=20)
            rate,path=self.currency_rate('USDT')
            return market,body['bids'],body['asks'],rate,'binance:'+market+' -> '+path
        if venue=='kraken':
            market=self.kraken_pair(asset,'USD');body=self.adapters[venue].orderbook(market,count=25)
            x=next(iter(body['result'].values()));rate,path=self.currency_rate('USD')
            return market,x['bids'],x['asks'],rate,'kraken:'+market+' -> '+path
        raise ValueError('UNKNOWN_VENUE')

    def currency_rate(self,currency):
        if currency=='KRW':return 1.,'KRW'
        usdt=self.book('upbit','USDT')
        if currency=='USDT':return usdt.mid,'upbit:KRW-USDT market conversion (not fiat USD/KRW)'
        if currency=='USD':
            pair=self.kraken_pair('USDT','USD')
            raw=next(iter(self.adapters['kraken'].orderbook(pair,count=1)['result'].values()))
            value=(number(raw['bids'][0][0])+number(raw['asks'][0][0]))/2
            if value<=0:raise ValueError('INVALID_USDT_USD')
            return usdt.mid/value,'kraken:USDT/USD -> upbit:KRW-USDT market-implied USD/KRW'
        raise ValueError('UNSUPPORTED_FX')

    def book(self,venue,asset,fresh=False):
        key=(venue,asset)
        with self.lock:
            now=time.time_ns()//1000000
            previous=self.cache.get(key)
            if not fresh and previous and now-previous.received_ms<30000:return previous
            market,bids,asks,rate,path=self._raw(venue,asset)
            result=make_book(venue,asset,market,bids,asks,rate,path)
            self.cache[key]=result
            return result

    def mark(self,venue,asset):
        if asset in ('KRW','USD','USDT'):
            value,path=self.currency_rate(asset)
            return {'price_krw':value,'source':path,'received_ts_ms':time.time_ns()//1000000}
        book=self.book(venue,asset)
        return {'price_krw':book.mid,'source':book.valuation_path,'received_ts_ms':book.received_ms}
