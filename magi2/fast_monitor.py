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
import uuid
import requests
from magi2.fast_lab.rank_tracker import RankPolicy, RankTracker
from magi3.fast_audit import FastAudit
from magi2.fast_captures import flow_metrics, strength, AlertGate, captures
from magi2.fast_comparison import ForwardCheck, VERSION as EVALUATION_VERSION

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

    def buying_flow(self,symbol):
        if self.venue in ('upbit','bithumb'):
            data=self.get('/v1/trades/ticks',{'market':symbol,'count':200})
            rows=[(int(x['timestamp']),float(x['trade_price']),float(x['trade_volume']),{'BID':True,'ASK':False}.get(x['ask_bid'])) for x in data]
        elif self.venue=='binance':
            data=self.get('/api/v3/aggTrades',{'symbol':symbol,'limit':1000})
            rows=[(int(x['T']),float(x['p']),float(x['q']),not x['m'] if isinstance(x['m'],bool) else None) for x in data]
        else:
            data=self.get('/0/public/Trades',{'pair':symbol,'count':1000})['result']
            rows=[(int(float(x[2])*1000),float(x[0]),float(x[1]),{'b':True,'s':False}.get(x[3])) for k,v in data.items() if k!='last' for x in v]
        return flow_metrics(rows,now())

class Watch:
    """Fresh 30s high breakout, max spread 15bps, max chase 2% since selection."""
    def __init__(self,row,ts):
        self.evaluation=None;self.signal_id='fast-'+uuid.uuid4().hex;self.started=ts;self.metrics={};self.row=row;self.until=ts+900000;self.history=deque();self.alerted=False;self.last=0
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
        self.metrics={'bid':bid,'ask':ask,'spread_bps':(ask/bid-1)*10000,
                      'prior_high':high,'breakout_bps':(bid/high-1)*10000,
                      'chase_bps':(bid/self.row['price']-1)*10000,
                      'history_span_ms':ts-self.history[0][0] if self.history else 0}
        self.history.append((ts,bid))
        if hit:self.alerted=True
        return hit

class FastMonitor:
    def __init__(self,root,log):
        self.root=Path(root);self.log=log;self.events=Queue(maxsize=100);self.lock=Lock();self.stop=Event()
        self.state={};self.threads=[];self.tracking={}
        self.audit=FastAudit(self.root/'fast_evidence.sqlite3')
        self.alert_gate=AlertGate(self.audit)
        self.log('fast_audit_ready schema=fast-evidence-v1 storage=persistent_sqlite')
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
    def captures(self,offset=0):
        with self.lock:tracking={k:dict(v) for k,v in self.tracking.items()}
        return captures(self.audit,now(),tracking,offset)

    def drain(self):
        rows=[]
        for _ in range(4):
            try:rows.append(self.events.get_nowait())
            except Empty:break
        return rows
    def record_signal(self,venue,symbol,watch,ts,duration_ms):
        watch.evaluation=ForwardCheck(ts,watch.metrics['ask'])
        self.audit.record('SIGNAL_DETECTED',watch.signal_id,venue,symbol,'ALERT_ONLY',ts_ms=ts,
            rule_version='fast-auto-v1',exchange_ts_ms=None,duration_ms=duration_ms,
            criteria={'min_rise_bps':100,'scan_minutes':5,'top_n':5,'min_rank_jump':5,
                      'lookback_ms':30000,'min_breakout_bps':5,'max_spread_bps':15,
                      'max_chase_bps':200,'max_response_ms':1500,'max_quote_gap_ms':3000,'watch_ms':900000},
            observed={**watch.metrics,'asset':watch.row.get('asset'),'quote_currency':watch.row.get('quote_currency'),'returns_bps':watch.row['returns_bps'],'rank':watch.row['rank'],
                      'previous_rank':watch.row.get('previous_rank'),'rank_jump':watch.row.get('rank_jump'),
                      'strength':getattr(watch,'strength',{}),'selection_scan_ts_ms':watch.row.get('selection_scan_ts_ms'),'selection_reason':watch.row.get('reason'),'watch_started_ts_ms':watch.started,'watch_until_ts_ms':watch.until})
        self.audit.record('ORDER_SKIPPED',watch.signal_id,venue,symbol,'ALERT_ONLY',ts_ms=ts,
                          reason='LIVE_EXECUTION_NOT_CONNECTED',result={'submitted':False})

    def worker(self,venue):
        market=PublicMarket(venue);tracker=RankTracker(RankPolicy(scan_minutes=5,watch_minutes=15))
        watches={};cooldown={};snapshot_due=0;backoff=1
        while not self.stop.is_set():
            started=time.monotonic();ts=now()
            try:
                for symbol in list(watches):
                    if ts>=watches[symbol].until:
                        w=watches[symbol]
                        self.audit.record('WATCH_ENDED',w.signal_id,venue,symbol,'ALERT_ONLY',ts_ms=ts,
                                          observed={'alerted':w.alerted,'last_quote_ts_ms':w.last,'last_quote':w.metrics},reason='15_MINUTE_WINDOW_ENDED')
                        del watches[symbol];cooldown[symbol]=ts+900000
                cooldown={s:t for s,t in cooldown.items() if t>ts}
                if ts>=snapshot_due:
                    prices=market.prices();ts=now();tracker.snapshot(venue,ts,prices);snapshot_due=ts+60000
                    scan=tracker.scan(venue,ts)
                    if scan:
                        for row in scan['watchlist']:
                            symbol=row['symbol']
                            if symbol not in watches and symbol not in cooldown and len(watches)<5:
                                row=dict(row,selection_scan_ts_ms=ts,asset=market.symbols[symbol],quote_currency='KRW' if venue in ('upbit','bithumb') else 'USDT' if venue=='binance' else 'USD')
                                w=Watch(row,ts)
                                self.audit.record('CANDIDATE_SELECTED',w.signal_id,venue,symbol,'ALERT_ONLY',ts_ms=ts,
                                    rule_version='fast-auto-v1',criteria=scan['policy'],observed=dict(row,watch_until_ms=w.until),exchange_ts_ms=None)
                                watches[symbol]=w
                        self.audit.record('SCAN_COMPLETED',f'scan:{venue}:{ts}',venue,'*','ALERT_ONLY',ts_ms=ts,
                                          rule_version='fast-auto-v1',observed={'status':scan['status'],'ranked':scan['ranked']})
                        self.update(venue,status=scan['status'],watching=len(watches),symbols=len(prices))
                        self.log(f'fast_scan venue={venue} status={scan["status"]} symbols={len(prices)} watching={len(watches)}')
                quote_started=now()
                quotes=market.quotes(list(watches));ts=now()
                if ts-quote_started<=1500:
                    for symbol,(bid,ask) in quotes.items():
                        watch=watches.get(symbol)
                        hit=watch.quote(ts,bid,ask) if watch else False
                        if watch and watch.alerted and watch.last==ts:
                            with self.lock:
                                self.tracking[watch.signal_id]={'bid':bid,'ts_ms':ts,'until':watch.until}
                                self.tracking={k:v for k,v in self.tracking.items() if v['ts_ms']>=ts-86400000}
                        if watch and watch.evaluation and watch.last==ts:
                            outcome=watch.evaluation.quote(ts,bid)
                            if outcome:
                                self.audit.record('SIGNAL_EVALUATED',watch.signal_id,venue,symbol,'ALERT_ONLY',ts_ms=ts,
                                                  rule_version=EVALUATION_VERSION,result=outcome)
                        if hit:
                            try:flow=market.buying_flow(symbol)
                            except Exception as exc:flow={'sufficient':False,'error':type(exc).__name__}
                            decision_ts=now()
                            watch.strength=strength(flow,watch.metrics)
                            watch.strength['quote_age_ms']=decision_ts-ts
                            if decision_ts-ts>1500:
                                watch.strength.update(strong=False,label='호가 확인 지연')
                            stamp=datetime.fromtimestamp(decision_ts/1000,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M:%S')
                            event={'id':watch.signal_id, 'venue':venue,'symbol':symbol,
                                   'created_ts_ms':decision_ts,'watch_until_ms':watch.until,'bid':bid,'ask':ask,'mode':'ALERT_ONLY',
                                   'text':f'⚡ FAST 포착 · 강한 매수세\n{stamp} KST · {venue.upper()} {symbol}\n5분 상승 {watch.row["returns_bps"]["5"]/100:+.2f}% · 순위 {watch.row["rank"]}\n매수 주도 비중 {(flow.get("buyer_share_pct") or 0):.1f}% · 강도는 확률이 아닙니다.\n30초 고점 돌파 · 후보별 15분 집중 감시\n실주문 OFF · 예산 설계: FAST 전체 5% 미만. 수익 보장 신호가 아닙니다.'}
                            self.record_signal(venue,symbol,watch,decision_ts,decision_ts-quote_started)
                            reason='BUY_FLOW_FILTER' if not watch.strength['strong'] else self.alert_gate.claim(watch.signal_id,venue,symbol,watch.row['asset'],now())
                            if reason:
                                self.audit.record('ALERT_SUPPRESSED',watch.signal_id,venue,symbol,'ALERT_ONLY',reason=reason,observed={'strength':watch.strength})
                            else:
                                try:self.events.put_nowait(event)
                                except Full:
                                    self.audit.record('NOTIFICATION_DROPPED',watch.signal_id,venue,symbol,'ALERT_ONLY',reason='QUEUE_FULL')
                                    self.log('fast_alert_queue_full')
                            self.log(f'fast_breakout venue={venue} symbol={symbol} mode=ALERT_ONLY')
                self.update(venue,watching=len(watches));backoff=1
            except Exception as exc:
                self.update(venue,status='UNAVAILABLE',error=type(exc).__name__)
                self.log(f'fast_monitor_error venue={venue} type={type(exc).__name__}')
                self.stop.wait(backoff);backoff=min(60,backoff*2)
            self.stop.wait(max(.05,1-(time.monotonic()-started)))
