"""Public-only spot adapters. Each connection owns its incremental book state."""
from __future__ import annotations
import asyncio
import contextlib
import json
import logging
import random
import uuid
from collections import Counter, deque
from datetime import datetime
from decimal import Decimal
import zlib
import aiohttp
from ..config import BOOK_DEPTH, STALE_FEED_SEC
from ..normalizer import book, now_ms, trade

LOG = logging.getLogger(__name__)
URLS = {
    'binance': 'wss://stream.binance.com:9443/stream',
    'bybit': 'wss://stream.bybit.com/v5/public/spot',
    'kraken': 'wss://ws.kraken.com/v2',
    'upbit': 'wss://api.upbit.com/websocket/v1',
    'bithumb': 'wss://ws-api.bithumb.com/websocket/v1',
    'coinone': 'wss://stream.coinone.co.kr',
}

def timestamp(value):
    if value is None: return None
    if isinstance(value, str) and 'T' in value:
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()*1000)
    return int(value)

class Adapter:
    def __init__(self, venue):
        self.venue = venue
        self.books = {}
        self.sequences = {}

    def url(self, assets):
        if self.venue == 'binance':
            streams = '/'.join(f'{a.lower()}usdt@trade/{a.lower()}usdt@depth10@100ms' for a in assets)
            return URLS[self.venue] + '?streams=' + streams
        return URLS[self.venue]

    def subscriptions(self, assets):
        if self.venue == 'binance': return []
        if self.venue == 'bybit':
            return [{'op':'subscribe','args':[s for a in assets for s in (f'publicTrade.{a}USDT',f'orderbook.50.{a}USDT')]}]
        if self.venue == 'kraken':
            return [{'method':'subscribe','params':dict(channel=ch,symbol=[f'{a}/USD' for a in assets],**extra)} for ch,extra in [('trade',{'snapshot':False}),('book',{'depth':10,'snapshot':True})]]
        if self.venue in ('upbit','bithumb'):
            return [[{'ticket':str(uuid.uuid4())},{'type':'trade','codes':[f'KRW-{a}' for a in assets],'is_only_realtime':True},{'type':'orderbook','codes':[f'KRW-{a}' for a in assets]},{'format':'DEFAULT'}]]
        return [{'request_type':'SUBSCRIBE','channel':ch,'topic':{'quote_currency':'KRW','target_currency':a}} for a in assets for ch in ('TRADE','ORDERBOOK')]

    def subscribe(self, assets):
        return self.subscriptions(assets)

    def incremental(self, symbol, bids, asks, snapshot, sequence=None, depth=50, checksum=None):
        if snapshot:
            self.books[symbol] = ({}, {})
            self.sequences.pop(symbol, None)
        if symbol not in self.books:
            raise ValueError(f'{self.venue} delta before snapshot: {symbol}')
        old = self.sequences.get(symbol)
        if sequence is not None and old is not None and int(sequence) <= old:
            return None
        sides = self.books[symbol]
        for side, rows in zip(sides,(bids,asks)):
            for p,q in rows:
                p,q = Decimal(str(p)),Decimal(str(q))
                if q == 0: side.pop(p,None)
                else: side[p] = q
        for i,side in enumerate(sides):
            for p in sorted(side,reverse=i==0)[depth:]: del side[p]
        if checksum is not None:
            def digits(n): return format(n,'f').replace('.','').lstrip('0')
            s=''.join(digits(p)+digits(q) for i in (1,0) for p,q in sorted(sides[i].items(),reverse=i==0)[:10])
            if zlib.crc32(s.encode()) != int(checksum):
                raise ValueError(f'Kraken checksum mismatch: {symbol}')
        if sequence is not None: self.sequences[symbol]=int(sequence)
        return [sorted(sides[0].items(),reverse=True),sorted(sides[1].items())]

    def parse(self, m, received):
        v=self.venue
        if m.get('error') or m.get('success') is False or m.get('response_type')=='ERROR':
            raise ValueError(f'{v} subscription error: {m}')
        if v=='binance':
            d=m.get('data',m); symbol=d.get('s',m.get('stream','').split('@')[0])
            if d.get('e')=='trade':
                return [trade(v,symbol,d['p'],d['q'],d.get('T'),'SELL' if d['m'] else 'BUY',d.get('t'),received)]
            if 'lastUpdateId' in d:
                return [book(v,symbol,d['bids'],d['asks'],None,d['lastUpdateId'],received)]
        elif v=='bybit':
            d=m.get('data'); topic=m.get('topic','')
            if topic.startswith('publicTrade'):
                return [trade(v,x['s'],x['p'],x['v'],x['T'],x['S'],x.get('i'),received) for x in d]
            if topic.startswith('orderbook'):
                levels=self.incremental(d['s'],d['b'],d['a'],m.get('type')=='snapshot' or d.get('u')==1,d['u'])
                if levels: return [book(v,d['s'],*levels,m.get('cts',m.get('ts')),d['u'],received)]
        elif v=='kraken':
            ch=m.get('channel'); data=m.get('data',[])
            if ch=='trade' and m.get('type')!='snapshot':
                return [trade(v,x['symbol'],x['price'],x['qty'],timestamp(x['timestamp']),x['side'],x.get('trade_id'),received) for x in data]
            if ch=='book':
                out=[]
                for d in data:
                    levels=self.incremental(d['symbol'],[(x['price'],x['qty']) for x in d.get('bids',[])],[(x['price'],x['qty']) for x in d.get('asks',[])],m.get('type')=='snapshot',depth=10,checksum=d.get('checksum'))
                    if levels: out.append(book(v,d['symbol'],*levels,timestamp(d.get('timestamp')),d.get('checksum'),received))
                return out
        elif v in ('upbit','bithumb'):
            if m.get('type')=='trade':
                return [trade(v,m['code'],m['trade_price'],m['trade_volume'],m.get('trade_timestamp'),'BUY' if m['ask_bid']=='BID' else 'SELL',m.get('sequential_id'),received)]
            if m.get('type')=='orderbook':
                rows=m['orderbook_units']
                return [book(v,m['code'],[(x['bid_price'],x['bid_size']) for x in rows],[(x['ask_price'],x['ask_size']) for x in rows],m.get('timestamp'),None,received)]
        elif v=='coinone' and m.get('response_type')=='DATA':
            d=m['data']; symbol=f"{d['target_currency']}-{d['quote_currency']}"
            if m.get('channel')=='TRADE':
                side='BUY' if d.get('is_seller_maker') is True else 'SELL' if d.get('is_seller_maker') is False else None
                return [trade(v,symbol,d['price'],d['qty'],d.get('timestamp'),side,d.get('id'),received)]
            if m.get('channel')=='ORDERBOOK':
                return [book(v,symbol,[(x['price'],x['qty']) for x in d['bids']],[(x['price'],x['qty']) for x in d['asks']],d.get('timestamp'),d.get('id'),received)]
        return []

VENUE_ADAPTERS={v:Adapter(v) for v in URLS}

class CollectorSupervisor:
    def __init__(self,symbols,sink):
        self.symbols=symbols; self.sink=sink
        self.last_message={}; self.last_event={}; self.counts=Counter(); self.reconnects=Counter()
        self.offsets={v:deque(maxlen=1000) for v in URLS}
        self.missing_ts=Counter(); self.timestamp_regressions=Counter(); self.last_exchange={}
        self.connected={}; self.parse_errors=Counter()

    async def heartbeat(self,ws,v):
        while True:
            await asyncio.sleep(15)
            if v=='bybit': await ws.send_json({'op':'ping'})
            elif v=='coinone': await ws.send_json({'request_type':'PING'})
            else: await ws.ping()
            ages=[now_ms()-t for (venue,asset,kind),t in self.last_event.items() if venue==v]
            if ages and min(ages)>STALE_FEED_SEC*1000:
                await ws.close(); return

    async def run_venue(self,session,venue):
        attempt=0
        while True:
            started=now_ms()
            adapter=Adapter(venue)
            try:
                async with session.ws_connect(adapter.url(self.symbols),heartbeat=20,receive_timeout=45) as ws:
                    self.connected[venue]=True
                    for payload in adapter.subscriptions(self.symbols): await ws.send_json(payload)
                    heartbeat=asyncio.create_task(self.heartbeat(ws,venue))
                    try:
                        async for msg in ws:
                            if msg.type not in (aiohttp.WSMsgType.TEXT,aiohttp.WSMsgType.BINARY):
                                if msg.type in (aiohttp.WSMsgType.ERROR,aiohttp.WSMsgType.CLOSED): break
                                continue
                            received=now_ms(); self.last_message[venue]=received
                            try:
                                m=json.loads(msg.data,parse_float=Decimal) if venue=='kraken' else json.loads(msg.data)
                                events=adapter.parse(m,received)
                            except (ValueError,KeyError,TypeError):
                                self.parse_errors[venue]+=1
                                raise
                            for event in events:
                                kind=event.__class__.__name__; key=(venue,event.base,kind)
                                if not self.counts[key]: LOG.info('first_event venue=%s asset=%s type=%s',*key)
                                self.counts[key]+=1; self.last_event[key]=received
                                if event.exchange_ts_ms is None: self.missing_ts[venue]+=1
                                else:
                                    self.offsets[venue].append(received-event.exchange_ts_ms)
                                    if event.exchange_ts_ms<self.last_exchange.get(key,0): self.timestamp_regressions[venue]+=1
                                    self.last_exchange[key]=event.exchange_ts_ms
                                await self.sink(event)
                            if now_ms()-started>60_000 and not any(k[0]==venue and t>=started for k,t in self.last_event.items()):
                                raise TimeoutError('connected but no normalized market events')
                    finally:
                        heartbeat.cancel()
                        with contextlib.suppress(asyncio.CancelledError): await heartbeat
            except asyncio.CancelledError: raise
            except Exception as exc: LOG.warning('venue=%s error=%r',venue,exc)
            self.connected[venue]=False; self.reconnects[venue]+=1
            attempt=0 if now_ms()-started>60_000 else min(attempt+1,5)
            await asyncio.sleep(min(30,2**attempt)+random.random())

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,sock_connect=20)) as session:
            await asyncio.gather(*(self.run_venue(session,v) for v in URLS))

    def diagnostics(self):
        result={}; current=now_ms()
        for v in URLS:
            feeds={f'{a}/{k}':{'count':self.counts[(v,a,k)],'age_ms':current-self.last_event[(v,a,k)] if (v,a,k) in self.last_event else None} for a in self.symbols for k in ('TradeEvent','BookEvent')}
            offsets=sorted(self.offsets[v])
            result[v]={'connected':self.connected.get(v,False),'feeds':feeds,'stale':any(x['age_ms'] is None or x['age_ms']>STALE_FEED_SEC*1000 for x in feeds.values()),'reconnects':self.reconnects[v],'parse_errors':self.parse_errors[v],'timestamp_missing':self.missing_ts[v],'timestamp_regressions':self.timestamp_regressions[v],'clock_offset_median_ms':offsets[len(offsets)//2] if offsets else None,'clock_offset_note':'includes transport delay; not synchronized latency'}
        return result
