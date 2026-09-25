"""Upbit KRW daily recovery PAPER engine. Public candles only; no order client."""
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock, Event, Thread
from zoneinfo import ZoneInfo
import json
import math
import shutil
import sqlite3
import time
import requests
from magi2.fast_wave_indicator import Candle, indicators

DAY=86400000
HOUR=3600000
STEP=300000
EPS=1e-10
COHORT='indicator-hourly-20260926-v1'
KST=ZoneInfo('Asia/Seoul')
GUIDE=('지표가속 · 일봉 회복 2회 확인 [PAPER]\n'
       '업비트 원화 전 종목 · 완성 일봉 100개 이상\n'
       '직전 일봉 RSI·Williams 하락 후, 현재 일봉 RSI·Williams 회복 + MACD 히스토그램 증가가속\n'
       '같은 일봉에서 1시간 간격 두 번 확인 → 확인 이후 다음 5분봉 시가로 모의매수\n'
       '추세청산: 일봉 지표 3개 중 2개 하락 + 종가가 전일 저가 이탈\n'
       '초기 보호: 포착 당시 당일 저가 고정, 이후 시간구간 종가 이탈 시 청산\n'
       '300만원·최대 10종목, 가용현금/빈 슬롯 균등배분 · 미충족은 현금\n'
       '완전 청산 후 새 신호로 재진입 · 고정 익절/손절률·60분 제한 없음\n'
       '수수료·슬리피지 각 편도 0.05% · 실주문 없음')


def now(): return time.time_ns()//1000000

def clock(t): return datetime.fromtimestamp(t/1000,KST).strftime('%m/%d %H:%M:%S')

def date(t): return datetime.fromtimestamp(t/1000,KST).date().isoformat()

def iso(t): return datetime.fromtimestamp(t/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def normalized(payload):
    result=[]
    for x in payload:
        t=int(datetime.fromisoformat(x['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()*1000)
        r=[t,*[float(x[k]) for k in ('opening_price','high_price','low_price','trade_price','candle_acc_trade_volume')]]
        if not all(math.isfinite(v) for v in r) or not 0<r[3]<=min(r[1],r[4])<=max(r[1],r[4])<=r[2] or r[5]<0:
            raise ValueError('INVALID_OHLCV')
        result.append(r)
    result.sort()
    if len({r[0] for r in result})!=len(result): raise ValueError('DUPLICATE_CANDLE')
    return result


def recovery_point(daily,hours,boundary):
    day=(boundary-1)//DAY*DAY
    complete=[r for r in daily if r[0]<day]
    live=[r for r in hours if day<=r[0] and r[0]+HOUR<=boundary]
    if len(complete)<100: raise ValueError('WARMUP_UNDER_100')
    if complete[-1][0]!=day-DAY or any(b[0]-a[0]!=DAY for a,b in zip(complete,complete[1:])):
        raise ValueError('DAILY_HISTORY_GAP')
    if not live or live[-1][0]!=boundary-HOUR: raise ValueError('NO_TRADE_IN_LAST_HOUR')
    bars=[Candle(r[0],r[2],r[3],r[4],r[5]) for r in complete]
    previous=indicators(bars);before=indicators(bars[:-1])
    current=Candle(day,max(r[2] for r in live),min(r[3] for r in live),live[-1][4],sum(r[5] for r in live))
    values=indicators([*bars,current])
    velocity={k:values[k]-previous[k] for k in values}
    accel={k:values[k]-2*previous[k]+before[k] for k in values}
    qualifies=(all(previous[k]-before[k]<-EPS and velocity[k]>EPS for k in ('rsi','williams')) and accel['macd_hist']>EPS)
    return dict(boundary=boundary,day=day,qualifies=qualifies,close=current.close,low=current.low,
                values=values,velocity=velocity,acceleration=accel,history=len(complete),
                trend_exit=boundary%DAY==0 and sum(v<-EPS for v in velocity.values())>=2 and current.close<complete[-1][3])


class PublicPauseError(RuntimeError):
    pass


class PublicCandles:
    def __init__(self): self.http=requests.Session();self.cache={};self.last_request=0
    def get(self,path,params=None):
        for attempt in range(3):
            time.sleep(max(0,.2-(time.monotonic()-self.last_request)))
            self.last_request=time.monotonic()
            r=self.http.get('https://api.upbit.com/v1/'+path,params=params,timeout=(3,8))
            if r.status_code==418:raise PublicPauseError('PUBLIC_ACCESS_TEMPORARILY_BLOCKED')
            if r.status_code==429:
                time.sleep(min(5,1+attempt));continue
            r.raise_for_status();return r.json()
        raise PublicPauseError('PUBLIC_RATE_LIMIT')
    def discover(self):
        return {x['market']:x['korean_name'] for x in self.get('market/all',{'is_details':'false'}) if x['market'].startswith('KRW-')}
    def observe(self,symbol,boundary):
        day=(boundary-1)//DAY*DAY
        if self.cache.get(symbol,{}).get('day')!=day:
            rows=normalized(self.get('candles/days',{'market':symbol,'to':iso(day),'count':200}))
            self.cache[symbol]=dict(day=day,rows=rows)
        hours=normalized(self.get('candles/minutes/60',{'market':symbol,'to':iso(boundary),'count':24}))
        return recovery_point(self.cache[symbol]['rows'],hours,boundary)
    def fills(self,symbol,earliest,stamp):
        rows=normalized(self.get('candles/minutes/5',{'market':symbol,'to':iso(stamp),'count':200}))
        found=[r for r in rows if earliest<=r[0]<stamp]
        # Never invent a fill in an unobserved gap outside the returned window.
        if rows and rows[0][0]>earliest and len(rows)>=200: raise ValueError('FILL_WINDOW_MISSING')
        return found[0] if found else None
    def prices(self,symbols):
        out={}
        for i in range(0,len(symbols),80):
            for x in self.get('ticker',{'markets':','.join(symbols[i:i+80])}):
                px=float(x['trade_price']);ts=int(x['timestamp'])
                if math.isfinite(px) and px>0:out[x['market']]={'price':px,'ts':ts}
        return out


class HourlyLedger:
    hourly_strategy=True
    indicator_strategy=True
    policy_review_required=False
    def __init__(self,root,stamp,log=lambda x:None):
        self.lock=RLock();self.root=Path(root);parent=self.root/'indicator_paper';target=parent/COHORT
        target.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(target/'ledger.sqlite3',check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts INTEGER,kind TEXT,symbol TEXT,payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS marks(bucket INTEGER PRIMARY KEY,ts INTEGER,date TEXT,payload TEXT)')
        row=self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        if row:self.s=json.loads(row[0])
        else:
            cfg=json.loads(Path(__file__).with_name('config.json').read_text())
            if cfg['mode']!='PAPER':raise ValueError('PAPER_ONLY')
            capital=float(cfg['initial_cash_krw']);slots=int(cfg['session']['top_n'])
            self.s=dict(cohort=COHORT,initial=capital,slots=slots,cash=capital,realized=0.,positions={},pending={},
                        previous={},used_day={},last_exit={},prices={},enabled=True,generation=1,resumed_ms=stamp,
                        started_ms=stamp,fee=.0005,slip=.0005,scan={},reset_done=False)
            self.save()
        if not self.s['reset_done']:
            removed=[]
            for old in parent.iterdir():
                if old==target:continue
                if old.is_symlink():old.unlink()
                elif old.is_dir():shutil.rmtree(old)
                else:old.unlink()
                removed.append(old.name)
            evidence=self.root/'fast_wave'
            if evidence.exists():
                for old in evidence.glob('*/indicator-paper-*'):
                    if old.is_symlink():old.unlink()
                    elif old.is_dir():shutil.rmtree(old)
                    removed.append(old.name)
            self.s['reset_done']=True;self.save()
            log('indicator_reset '+json.dumps(dict(cohort=COHORT,removed=removed,initial=self.s['initial'],slots=self.s['slots'],vpd_untouched=True)))
    def save(self):
        with self.db:self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',(json.dumps(self.s,allow_nan=False),))
    def event(self,stamp,kind,symbol,payload):
        self.db.execute('INSERT INTO events(ts,kind,symbol,payload) VALUES(?,?,?,?)',(stamp,kind,symbol,json.dumps(payload,allow_nan=False)))
    def control(self):
        with self.lock:return {k:self.s[k] for k in ('enabled','generation','resumed_ms')}
    def schedule_exit(self,symbol,stamp,reason,boundary):
        if symbol in self.s['pending']:return
        self.s['pending'][symbol]=dict(side='SELL',reason=reason,decision_ms=stamp,signal_ms=boundary,
                                      earliest=(stamp//STEP+1)*STEP,generation=self.s['generation'])
        self.event(stamp,'EXIT_SIGNAL',symbol,self.s['pending'][symbol])
    def observe(self,symbol,point,stamp,generation):
        with self.lock,self.db:
            if generation!=self.s['generation'] or not self.s['enabled'] or point['boundary']<self.s['resumed_ms']:return
            old=self.s['previous'].get(symbol)
            if old and old['boundary']>=point['boundary']:return
            self.s['previous'][symbol]=point
            self.event(stamp,'OBSERVATION',symbol,point)
            pos=self.s['positions'].get(symbol)
            if pos and point['boundary']>pos['entry_ms']:
                reason=('DAILY_WEAKNESS_AND_PREVIOUS_LOW_BREAK' if point['trend_exit'] else
                        'HOURLY_CLOSE_BELOW_INITIAL_STRUCTURE' if point['close']<pos['initial_stop'] else None)
                if reason:self.schedule_exit(symbol,stamp,reason,point['boundary'])
            confirmed=bool(old and old['boundary']+HOUR==point['boundary'] and old['day']==point['day'] and old['qualifies'] and point['qualifies'])
            if confirmed and self.s['used_day'].get(symbol)!=point['day']:
                self.s['used_day'][symbol]=point['day'];reason='ACCEPTED'
                if pos or symbol in self.s['pending']:reason='HOLDING_OR_PENDING'
                elif point['boundary']<=self.s['last_exit'].get(symbol,0):reason='STALE_AFTER_EXIT'
                vacant=self.s['slots']-len(self.s['positions'])-sum(o['side']=='BUY' for o in self.s['pending'].values())
                reserved=sum(o.get('budget',0) for o in self.s['pending'].values())
                if reason=='ACCEPTED' and vacant<=0:reason='ALL_SLOTS_USED'
                budget=(self.s['cash']-reserved)/vacant if vacant>0 else 0
                if reason=='ACCEPTED' and budget<5000:reason='INSUFFICIENT_CASH'
                if reason=='ACCEPTED':
                    self.s['pending'][symbol]=dict(side='BUY',reason='FRESH_TWO_HOUR_DAILY_RECOVERY',decision_ms=stamp,
                        signal_ms=point['boundary'],earliest=(stamp//STEP+1)*STEP,budget=budget,
                        initial_stop=point['low'],generation=generation)
                self.event(stamp,'SIGNAL',symbol,dict(point,decision=reason,budget=budget))
            self.save()
    def pending(self):
        with self.lock:return json.loads(json.dumps(self.s['pending']))
    def execute(self,symbol,order,bar,stamp):
        with self.lock,self.db:
            if self.s['pending'].get(symbol)!=order:return False
            if bar[0]<order['earliest'] or bar[0]>=stamp:return False
            price=bar[1]
            if not math.isfinite(price) or price<=0:raise ValueError('INVALID_FILL')
            fee=self.s['fee'];slip=self.s['slip']
            if order['side']=='BUY':
                if not self.s['enabled'] or order['generation']!=self.s['generation']:return False
                cost=order['budget'];qty=cost/(price*(1+slip)*(1+fee))
                if cost>self.s['cash']+1e-7 or len(self.s['positions'])>=self.s['slots']:raise ValueError('CAPITAL_INVARIANT')
                self.s['cash']-=cost
                self.s['positions'][symbol]=dict(entry_ms=bar[0],cost=cost,qty=qty,reference=price,initial_stop=order['initial_stop'])
                payload=dict(order,reference=price,fill=price*(1+slip),qty=qty,cost=cost,fill_ms=bar[0],recorded_ms=stamp)
            else:
                pos=self.s['positions'].pop(symbol);proceeds=pos['qty']*price*(1-slip)*(1-fee);pnl=proceeds-pos['cost']
                self.s['cash']+=proceeds;self.s['realized']+=pnl;self.s['last_exit'][symbol]=bar[0]
                payload=dict(order,reference=price,fill=price*(1-slip),qty=pos['qty'],proceeds=proceeds,pnl=pnl,
                             entry=pos,fill_ms=bar[0],recorded_ms=stamp)
            del self.s['pending'][symbol]
            self.s['prices'][symbol]=dict(price=price,ts=bar[0])
            self.event(stamp,order['side'],symbol,payload);self.save();return True
    def mark(self,prices,stamp):
        with self.lock,self.db:
            self.s['prices'].update(prices)
            snap=self.balance(stamp)
            self.db.execute('INSERT OR IGNORE INTO marks VALUES(?,?,?,?)',(stamp//STEP,stamp,date(stamp),json.dumps(snap)))
            self.db.execute("DELETE FROM events WHERE kind='OBSERVATION' AND ts<?",(stamp-14*DAY,))
            self.save()
    def balance(self,stamp):
        with self.lock:
            value=0.;unknown=[];cost=0.
            for symbol,p in self.s['positions'].items():
                quote=self.s['prices'].get(symbol,{})
                if not quote or not 0<=stamp-quote['ts']<=5*60000:unknown.append(symbol)
                value+=p['qty']*quote.get('price',p['reference'])*(1-self.s['fee'])*(1-self.s['slip']);cost+=p['cost']
            eq=self.s['cash']+value
            return dict(ts=stamp,initial=self.s['initial'],cash=self.s['cash'],invested=cost,equity=None if unknown else eq,
                        last_known_equity=eq,unknown=unknown,realized=self.s['realized'],positions=len(self.s['positions']),pending=len(self.s['pending']))
    def clear(self,stamp):
        with self.lock,self.db:
            self.s['enabled']=False;self.s['generation']+=1
            buys=[s for s,o in self.s['pending'].items() if o['side']=='BUY']
            for s in buys:del self.s['pending'][s]
            for s in self.s['positions']:self.schedule_exit(s,stamp,'MANUAL_CLEAR',stamp)
            self.save();return dict(positions=len(self.s['positions']),canceled_entries=len(buys))
    def resume(self,stamp):
        with self.lock:
            if any(o['side']=='SELL' for o in self.s['pending'].values()):return False
            if self.s['enabled']:return True
            self.s.update(enabled=True,generation=self.s['generation']+1,resumed_ms=stamp,previous={})
            self.save();return True
    def performance(self):
        with self.lock:
            values=[json.loads(r[0])['equity'] for r in self.db.execute('SELECT payload FROM marks ORDER BY ts')]
            peak=self.s['initial'];dd=0
            for v in values:
                if v is not None:peak=max(peak,v);dd=min(dd,(v/peak-1)*100)
            return dict(valid_samples=sum(v is not None for v in values),missing=sum(v is None for v in values),mdd_pct=dd)


class HourlyPaperService:
    def __init__(self,root,log,clock=now):self.clock=clock;self.log=log;self.ledger=HourlyLedger(root,clock(),log)
    def start(self):self.log(f'indicator_paper_started cohort={COHORT} initial={self.ledger.s["initial"]} slots={self.ledger.s["slots"]} enabled={self.ledger.s["enabled"]} live_orders=false')
    def clear(self):return self.ledger.clear(self.clock())
    def resume(self):return self.ledger.resume(self.clock())
    def daily(self,send):pass  # Hourly monitor persists actual 5-minute marks; UI reads them.


class HourlyMonitor:
    def __init__(self,root,log,paper,market_factory=PublicCandles,clock=now):
        self.log=log;self.ledger=paper.ledger;self.clock=clock;self.market_factory=market_factory;self.stop=Event()
    def start(self):
        self.thread=Thread(target=self.worker,daemon=True,name='indicator-hourly');self.thread.start()
    def drain(self):return []
    def update(self,**fields):
        with self.ledger.lock:self.ledger.s['scan'].update(fields);self.ledger.save()
    def worker(self):
        market=self.market_factory();names={};last_boundary=0;last_mark=0;last_discover=0
        self.log('indicator_monitor_started universe=UPBIT_ALL_KRW checkpoint=1h confirmation=2 paper_only=true')
        while not self.stop.is_set():
            try:
                ts=self.clock()
                if ts-last_discover>=HOUR or not names:
                    names=market.discover();last_discover=ts;self.update(universe=len(names),names=names)
                    self.log(f'indicator_universe venue=upbit count={len(names)} basis=ALL_KRW')
                boundary=ts//HOUR*HOUR;control=self.ledger.control()
                if boundary>last_boundary and control['enabled']:
                    ready=0;errors={};last_boundary=boundary
                    # Bootstrap history only: no trades or confirmation from before activation.
                    bootstrap=boundary<control['resumed_ms']
                    self.update(status='WARMING' if bootstrap else 'SCANNING',boundary=boundary,checked=0,errors={})
                    for i,symbol in enumerate(sorted(names)):
                        if self.stop.is_set():return
                        try:
                            point=market.observe(symbol,boundary);ready+=1
                            if not bootstrap:self.ledger.observe(symbol,point,self.clock(),control['generation'])
                        except PublicPauseError:
                            raise
                        except Exception as exc:
                            key=type(exc).__name__+':'+str(exc)[:90];errors[key]=errors.get(key,0)+1
                        if i%20==0:self.update(checked=i+1,ready=ready,errors=errors)
                    self.update(status='MONITORING' if ready else 'DATA_UNAVAILABLE',checked=len(names),ready=ready,errors=errors,last_scan_ms=self.clock(),bootstrap=bootstrap)
                    self.log('indicator_hourly_scan '+json.dumps(dict(boundary=clock(boundary),universe=len(names),ready=ready,errors=errors,bootstrap=bootstrap)))
                # Pending orders use an actual candle that starts AFTER the observed decision.
                for symbol,order in sorted(self.ledger.pending().items(),key=lambda x:(x[1]['side']=='BUY',x[1]['signal_ms'],x[0])):
                    ts=self.clock()
                    if ts<=order['earliest']:continue
                    if order['side']=='BUY' and ts-order['decision_ms']>HOUR:
                        with self.ledger.lock,self.ledger.db:
                            if self.ledger.s['pending'].get(symbol)==order:
                                del self.ledger.s['pending'][symbol];self.ledger.event(ts,'CANCEL',symbol,dict(order,reason='STALE_UNFILLED'));self.ledger.save()
                        continue
                    try:
                        bar=market.fills(symbol,order['earliest'],ts)
                        if bar and self.ledger.execute(symbol,order,bar,ts):self.log(f'indicator_paper_fill side={order["side"]} market={symbol} price={bar[1]} signal={clock(order["signal_ms"])} fill={clock(bar[0])}')
                    except PublicPauseError:
                        raise
                    except Exception as exc:self.log(f'indicator_fill_error market={symbol} type={type(exc).__name__}')
                if self.clock()-last_mark>=60000:
                    with self.ledger.lock:symbols=list(self.ledger.s['positions'])
                    prices=market.prices(symbols) if symbols else {}
                    self.ledger.mark(prices,self.clock());last_mark=self.clock()
                    b=self.ledger.balance(self.clock())
                    self.log('indicator_paper_status '+json.dumps(dict(cohort=COHORT,enabled=self.ledger.control()['enabled'],**b)))
            except PublicPauseError as exc:
                self.update(status='RATE_LIMIT_WAIT',error=str(exc));self.log('indicator_rate_limit_wait '+str(exc));self.stop.wait(60)
            except Exception as exc:
                self.update(status='ERROR',error=type(exc).__name__+':'+str(exc)[:100]);self.log('indicator_monitor_error '+type(exc).__name__+':'+str(exc)[:100])
            self.stop.wait(10)
    def summary(self):
        with self.ledger.lock:s=dict(self.ledger.s['scan'])
        return (GUIDE+'\n\n관측 상태: '+str(s.get('status','준비 중'))+
                f' · 준비 {s.get("ready",0)}/{s.get("universe",0)}종목\n마지막 확인 '+(clock(s['last_scan_ms']) if s.get('last_scan_ms') else '준비 중')+
                '\n오류/자료부족: '+json.dumps(s.get('errors',{}),ensure_ascii=False))
    def captures(self,offset=0):
        from magi2.hourly_indicator_report import events_view
        return events_view(self.ledger,self.clock(),offset,'SIGNAL')
