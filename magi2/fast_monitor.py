"""Public-only FAST scanner. Bounded in-memory data, candidate-only L1 polling.

No account keys, trading adapters or Shadow engine are imported here.
"""
from collections import deque
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty, Full
from threading import Thread, Lock, Event
from zoneinfo import ZoneInfo
import json
import time
import requests
from magi2.fast_lab.rank_tracker import RankPolicy, RankTracker

VENUES=('upbit','bithumb','binance','kraken')
STABLE={'USDT','USDC','DAI','TUSD','FDUSD','USDP','USD1','EUR','KRW','USD'}

def now():return time.time_ns()//1000000

class PublicMarket:
    def __init__(self,venue):
        self.venue=venue;self.http=requests.Session();self.symbols={};self.refreshed=0
        self.base={'upbit':'https://api.upbit.com','bithumb':'https://api.bithumb.com',
                   'binance':'https://api.binance.com','kraken':'https://api.kraken.com'}[venue]
    def get(self,path,params=None):
        r=self.http.get(self.base+path,params=params,timeout=(3,5));r.raise_for_status()
        data=r.json()
        if isinstance(data,dict) and data.get('error'):raise ValueError('PUBLIC_API_REJECTED')
        return data
    def discover(self):
        v=self.venue;out={}
        if v in ('upbit','bithumb'):
            for x in self.get('/v1/market/all',{'isDetails':'true'}):
                quote,asset=x['market'].split('-',1)
                if quote=='KRW' and asset not in STABLE and x.get('market_warning','NONE')=='NONE' and not (x.get('market_event') or {}).get('warning'):
                    out[x['market']]=asset
        elif v=='binance':
            for x in self.get('/api/v3/exchangeInfo')['symbols']:
                if x['quoteAsset']=='USDT' and x['baseAsset'] not in STABLE and x['status']=='TRADING' and x.get('isSpotTradingAllowed',False):out[x['symbol']]=x['baseAsset']
        else:
            for symbol,x in self.get('/0/public/AssetPairs')['result'].items():
                pair=x.get('wsname','').split('/')
                if len(pair)==2 and pair[1]=='USD' and pair[0] not in STABLE and x.get('status','online')=='online' and '.d' not in symbol:
                    out[symbol]=pair[0]
        if not out:raise ValueError('EMPTY_UNIVERSE')
        self.symbols=out;self.refreshed=now()
    def prices(self):
        if now()-self.refreshed>3600000:self.discover()
        if self.venue in ('upbit','bithumb'):
            rows=[];symbols=list(self.symbols)
            for i in range(0,len(symbols),80):
                rows+=self.get('/v1/ticker',{'markets':','.join(symbols[i:i+80])})
                time.sleep(.12)
            return {x['market']:float(x['trade_price']) for x in rows if x['market'] in self.symbols}
        if self.venue=='binance':
            return {x['symbol']:float(x['price']) for x in self.get('/api/v3/ticker/price') if x['symbol'] in self.symbols}
        return {k:float(x['c'][0]) for k,x in self.get('/0/public/Ticker')['result'].items() if k in self.symbols}
    def quotes(self,symbols):
        if not symbols:return {}
        if self.venue in ('upbit','bithumb'):
            rows=self.get('/v1/orderbook',{'markets':','.join(symbols)})
            return {x['market']:(float(x['orderbook_units'][0]['bid_price']),float(x['orderbook_units'][0]['ask_price'])) for x in rows}
        if self.venue=='binance':
            return {x['symbol']:(float(x['bidPrice']),float(x['askPrice'])) for x in self.get('/api/v3/ticker/bookTicker',{'symbols':json.dumps(symbols,separators=(',',':'))})}
        return {k:(float(x['b'][0]),float(x['a'][0])) for k,x in self.get('/0/public/Ticker',{'pair':','.join(symbols)})['result'].items()}

class Watch:
    """Fresh 30s high breakout, max spread 15bps, max chase 2% since selection."""
    def __init__(self,row,ts):
        self.row=row;self.until=ts+900000;self.history=deque();self.alerted=False;self.last=0
    def quote(self,ts,bid,ask):
        import math
        if not all(math.isfinite(x) and x>0 for x in (bid,ask)) or ask<bid:return False
        if self.last and ts-self.last>3000:self.history.clear()
        self.last=ts
        while self.history and self.history[0][0]<ts-31000:self.history.popleft()
        ready=self.history and ts-self.history[0][0]>=30000
        high=max((p for _,p in self.history),default=bid)
        hit=bool(not self.alerted and ts<self.until and ready and bid>=high*1.0005 and
                 (ask/bid-1)*10000<=15 and bid<=self.row['price']*1.02)
        self.history.append((ts,bid))
        if hit:self.alerted=True
        return hit

class FastMonitor:
    def __init__(self,root,log):
        self.root=Path(root);self.log=log;self.events=Queue(maxsize=100);self.lock=Lock();self.stop=Event()
        self.state={};self.threads=[]
    def start(self):
        for venue in VENUES:
            t=Thread(target=self.worker,args=(venue,),daemon=True,name='fast-'+venue);t.start();self.threads.append(t)
        self.log('fast_monitor_started scan=300s watch=900s quote_target=1s mode=ALERT_ONLY')
    def update(self,venue,**fields):
        with self.lock:
            self.state[venue]={**self.state.get(venue,{}),**fields,'updated_ts_ms':now()}
    def summary(self):
        with self.lock:state={k:dict(v) for k,v in self.state.items()}
        rows=['⚡ FAST 자동 감시 · 실주문 OFF','5분 탐색 · 후보별 15분 집중 감시 · 목표 1초 판단',
              '초기 상승 조건: 5분 +1% 이상 상위/순위 급상승 → 30초 고점 +0.05% 돌파, 스프레드 ≤0.15%. 연구 기준입니다.']
        for venue in VENUES:
            x=state.get(venue,{})
            rows.append(f"{venue.upper()}: {x.get('status','STARTING')} · 후보 {x.get('watching',0)} · 가격 {x.get('symbols',0)}종목")
        return '\n'.join(rows)
    def drain(self):
        rows=[]
        for _ in range(4):
            try:rows.append(self.events.get_nowait())
            except Empty:break
        return rows
    def worker(self,venue):
        market=PublicMarket(venue);tracker=RankTracker(RankPolicy(scan_minutes=5,watch_minutes=15))
        watches={};cooldown={};snapshot_due=0;backoff=1
        while not self.stop.is_set():
            started=time.monotonic();ts=now()
            try:
                for symbol in list(watches):
                    if ts>=watches[symbol].until:del watches[symbol];cooldown[symbol]=ts+900000
                cooldown={s:t for s,t in cooldown.items() if t>ts}
                if ts>=snapshot_due:
                    prices=market.prices();ts=now();tracker.snapshot(venue,ts,prices);snapshot_due=ts+60000
                    scan=tracker.scan(venue,ts)
                    if scan:
                        for row in scan['watchlist']:
                            symbol=row['symbol']
                            if symbol not in watches and symbol not in cooldown and len(watches)<5:watches[symbol]=Watch(row,ts)
                        self.update(venue,status=scan['status'],watching=len(watches),symbols=len(prices))
                        self.log(f'fast_scan venue={venue} status={scan["status"]} symbols={len(prices)} watching={len(watches)}')
                quote_started=now()
                quotes=market.quotes(list(watches));ts=now()
                if ts-quote_started<=1500:
                    for symbol,(bid,ask) in quotes.items():
                        watch=watches.get(symbol)
                        if watch and watch.quote(ts,bid,ask):
                            stamp=datetime.fromtimestamp(ts/1000,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M:%S')
                            event={'id':f'fast:{venue}:{symbol}:{watch.until}', 'venue':venue,'symbol':symbol,
                                   'created_ts_ms':ts,'watch_until_ms':watch.until,'bid':bid,'ask':ask,'mode':'ALERT_ONLY',
                                   'text':f'⚡ FAST WARNING · 초기 상승 조건 포착\n{stamp} KST · {venue.upper()} {symbol}\n5분 상승 {watch.row["returns_bps"]["5"]/100:+.2f}% · 순위 {watch.row["rank"]}\n30초 고점 돌파 · 후보별 15분 집중 감시\n실주문 OFF · 예산 설계: FAST 전체 5% 미만. 수익 보장 신호가 아닙니다.'}
                            try:self.events.put_nowait(event)
                            except Full:self.log('fast_alert_queue_full')
                            self.log(f'fast_breakout venue={venue} symbol={symbol} mode=ALERT_ONLY')
                self.update(venue,watching=len(watches));backoff=1
            except Exception as exc:
                self.update(venue,status='UNAVAILABLE',error=type(exc).__name__)
                self.log(f'fast_monitor_error venue={venue} type={type(exc).__name__}')
                self.stop.wait(backoff);backoff=min(60,backoff*2)
            self.stop.wait(max(.05,1-(time.monotonic()-started)))
