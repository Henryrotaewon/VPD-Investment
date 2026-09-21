"""Public-data FAST v2 scanner; paper entries and observation are independent."""
from datetime import datetime
from queue import Full
from threading import Thread
from zoneinfo import ZoneInfo
import json
import math
import time

from magi2.fast_monitor import FastMonitor, PublicMarket, now, VENUES
from magi2.fast_lab.rank_tracker import RankTracker, RankPolicy
from magi2.fast_comparison import ForwardCheck, VERSION as EVALUATION_VERSION
from magi2.fast_volume import VERSION, POLICY, parse_candles, evaluate, preselect


class VolumeMarket(PublicMarket):
    def candles(self,symbol):
        if self.venue in ('upbit','bithumb'):
            data=self.get('/v1/candles/minutes/1',{'market':symbol,'count':20})
        elif self.venue=='binance':
            data=self.get('/api/v3/klines',{'symbol':symbol,'interval':'1m','limit':20})
        else:
            data=self.get('/0/public/OHLC',{'pair':symbol,'interval':1,'since':now()//1000-1200})
        return parse_candles(self.venue,data)


class FastVolumeMonitor(FastMonitor):
    def __init__(self,root,log,paper=None):
        super().__init__(root,log,paper)
        self.observations={};self.recent_signals={}
        with self.audit.lock:
            rows=self.audit.db.execute("SELECT venue,symbol,MAX(ts_ms) FROM fast_events WHERE event_type='SIGNAL_DETECTED' AND ts_ms>=? GROUP BY venue,symbol",(now()-POLICY['signal_cooldown_ms'],)).fetchall()
        self.recent_signals={(v,s):t for v,s,t in rows}

    def start(self):
        for venue in VENUES:
            for fn,prefix in ((self.worker,'fast-volume-'),(self.observe,'fast-observe-')):
                t=Thread(target=fn,args=(venue,),daemon=True,name=prefix+venue)
                t.start();self.threads.append(t)
        self.log('fast_monitor_started rule='+VERSION+' scan=60s price_5m_bps=100 turnover_ratio=2 acceleration=positive spread_filter=false paper_only=true')

    def summary(self):
        with self.lock:state={k:dict(v) for k,v in self.state.items()}
        lines=['⚡ FAST 거래량·가격 가속 감시 · 실주문 OFF',
            '1분 재탐색 · 최근 완결 5분 +1% · 직전 5분보다 상승폭 확대 · 체결 거래대금 2배',
            '호가 차이로 제외하지 않으며 모의 체결 손익에 반영합니다.',
            '분당 가격 상승 상위 최대 20개 상세검사 · 모의 보유는 거래소별 최대 5개']
        for venue in VENUES:
            s=state.get(venue,{})
            lines.append(f'{venue.upper()}: {s.get("status","STARTING")} · 가격 {s.get("symbols",0)} · 상세 {s.get("probed",0)} · 포착 {s.get("qualified",0)} · 검사한도 제외 {s.get("budget_excluded",0)}')
        return '\n'.join(lines)

    def emit(self,venue,symbol,asset,evidence,bid,ask,ts,scan_ts,rank):
        ident=f'fast-v2:{venue}:{symbol}:{evidence["window_end_ms"]}'
        if ts-self.recent_signals.get((venue,symbol),-10**15)<POLICY['signal_cooldown_ms']:
            return False
        spread=(ask/bid-1)*10000
        observed=dict(asset=asset,bid=bid,ask=ask,spread_bps=spread,
            returns_bps={'5':evidence['return_5m_bps']},rank=rank,
            selection_scan_ts_ms=scan_ts,volume_acceleration=evidence,
            legacy_spread_10bps_pass=spread<=10,
            strength={'version':VERSION,'strong':True,'label':'거래대금·가격 가속','score':None})
        self.audit.record('SIGNAL_DETECTED',ident,venue,symbol,'ALERT_ONLY',ts_ms=ts,
                          rule_version=VERSION,criteria=POLICY,observed=observed)
        self.recent_signals[(venue,symbol)]=ts
        self.recent_signals={k:v for k,v in self.recent_signals.items() if ts-v<POLICY['signal_cooldown_ms']}
        self.audit.record('ORDER_SKIPPED',ident,venue,symbol,'ALERT_ONLY',ts_ms=ts,reason='LIVE_ORDERS_DISABLED')
        if self.paper:
            try:self.paper.offer(ident,venue,symbol,ts,strategy_version=VERSION)
            except Exception as exc:self.log(f'fast_paper_offer_error venue={venue} type={type(exc).__name__}')
        with self.lock:
            same_venue=[(k,w) for k,w in self.observations.items() if w['venue']==venue]
            if len(same_venue)>=50:
                old_key,old=min(same_venue,key=lambda item:item[1]['until'])
                self.observations.pop(old_key,None)
                self.audit.record('SIGNAL_EVALUATED',old_key,venue,old['symbol'],'ALERT_ONLY',ts_ms=ts,
                    rule_version=EVALUATION_VERSION,result={'status':'UNAVAILABLE','reason':'OBSERVATION_CAP'})
            self.observations[ident]=dict(venue=venue,symbol=symbol,until=ts+600000,
                evaluation=ForwardCheck(ts,ask),last=ts,bid=bid)
        reason=self.alert_gate.claim(ident,venue,symbol,asset,ts)
        if reason:self.audit.record('ALERT_SUPPRESSED',ident,venue,symbol,'ALERT_ONLY',ts_ms=ts,reason=reason)
        else:
            stamp=datetime.fromtimestamp(ts/1000,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M:%S')
            message=(f'⚡ FAST 포착 · 거래대금·가격 가속\n{stamp} KST · {venue.upper()} {symbol}\n'
                f'최근 5분 {evidence["return_5m_bps"]/100:+.2f}% · 직전 5분 {evidence["previous_return_5m_bps"]/100:+.2f}%\n'
                f'체결 거래대금 {evidence["turnover_ratio"]:.2f}배 · 호가 차이 {spread/100:.3f}%\n'
                '모의 진입·청산 /fast_report · 실주문 OFF')
            try:self.events.put_nowait({'id':ident,'text':message})
            except Full:self.audit.record('NOTIFICATION_DROPPED',ident,venue,symbol,'ALERT_ONLY',reason='QUEUE_FULL')
        self.log(f'fast_volume_signal venue={venue} symbol={symbol} rise_bps={evidence["return_5m_bps"]:.1f} turnover_ratio={evidence["turnover_ratio"]:.2f}')
        return True

    def worker(self,venue):
        market=VolumeMarket(venue);tracker=RankTracker(RankPolicy(scan_minutes=5))
        while not self.stop.is_set():
            started=time.monotonic()
            try:
                prices=market.prices();ts=now();tracker.snapshot(venue,ts,prices)
                ranked=tracker.returns(venue,ts);selected,excluded=preselect(ranked)
                stats=dict(symbols=len(prices),probed=0,qualified=0,budget_excluded=len(excluded),errors=0)
                for rank,row in enumerate(selected,1):
                    if self.stop.is_set():break
                    symbol=row['symbol'];ident=f'volume-check:{venue}:{symbol}:{ts}'
                    try:
                        evidence=evaluate(market.candles(symbol),now());stats['probed']+=1
                        if not evidence['qualified']:
                            self.audit.record('CANDIDATE_REJECTED',ident,venue,symbol,'ALERT_ONLY',
                                rule_version=VERSION,reason=evidence['reason'],observed=evidence)
                            continue
                        quote_start=now();quotes=market.quotes([symbol]);decision=now()
                        bid,ask=quotes[symbol]
                        if decision-quote_start>1500 or not all(math.isfinite(x) and x>0 for x in (bid,ask)) or ask<bid:
                            raise ValueError('INVALID_OR_SLOW_QUOTE')
                        stats['qualified']+=int(self.emit(venue,symbol,market.symbols[symbol],evidence,bid,ask,decision,ts,rank))
                    except Exception as exc:
                        stats['errors']+=1
                        self.audit.record('CANDIDATE_REJECTED',ident,venue,symbol,'ALERT_ONLY',
                            rule_version=VERSION,reason=str(exc)[:80] if isinstance(exc,ValueError) else type(exc).__name__)
                    finally:self.stop.wait(1.05 if venue=='kraken' else .15)
                status='RANKED' if ranked else 'WARMING_UP'
                self.audit.record('SCAN_COMPLETED',f'scan:{venue}:{ts}',venue,'*','ALERT_ONLY',ts_ms=ts,
                    rule_version=VERSION,criteria=POLICY,observed=dict(status=status,ranked=len(ranked),
                        **stats,probe_symbols=[r['symbol'] for r in selected],budget_excluded_symbols=[r['symbol'] for r in excluded]))
                self.update(venue,status=status,**stats)
                self.log(f'fast_volume_scan venue={venue} status={status} symbols={len(prices)} probed={stats["probed"]} signals={stats["qualified"]} errors={stats["errors"]} budget_excluded={len(excluded)}')
            except Exception as exc:
                self.update(venue,status='UNAVAILABLE',error=type(exc).__name__)
                self.log(f'fast_volume_error venue={venue} type={type(exc).__name__}')
            self.stop.wait(max(1,60-(time.monotonic()-started)))

    def observe(self,venue):
        market=PublicMarket(venue)
        while not self.stop.is_set():
            started=now()
            with self.lock:active={k:v for k,v in self.observations.items() if v['venue']==venue}
            for ident,w in active.items():
                if started>=w['until']:
                    self.audit.record('WATCH_ENDED',ident,venue,w['symbol'],'ALERT_ONLY',ts_ms=started,
                        observed={'alerted':True,'last_quote_ts_ms':w['last'],'last_quote':{'bid':w['bid']}},reason='10_MINUTE_OBSERVATION_ENDED')
                    with self.lock:self.observations.pop(ident,None)
            active={k:w for k,w in active.items() if started<w['until']}
            try:
                symbols=list(dict.fromkeys(w['symbol'] for w in active.values()))
                quotes={}
                for i in range(0,len(symbols),20):quotes.update(market.quotes(symbols[i:i+20]))
                ts=now()
                if ts-started<=1500:
                    for ident,w in active.items():
                        bid,ask=quotes.get(w['symbol'],(0,0))
                        if not math.isfinite(bid) or bid<=0 or ask<bid:continue
                        w.update(last=ts,bid=bid)
                        with self.lock:
                            self.tracking[ident]={'bid':bid,'ts_ms':ts,'until':w['until']}
                            self.tracking={k:v for k,v in self.tracking.items() if v['ts_ms']>=ts-86400000}
                        result=w['evaluation'].quote(ts,bid)
                        if result:self.audit.record('SIGNAL_EVALUATED',ident,venue,w['symbol'],'ALERT_ONLY',ts_ms=ts,rule_version=EVALUATION_VERSION,result=result)
            except Exception as exc:self.log(f'fast_observe_error venue={venue} type={type(exc).__name__}')
            self.stop.wait(max(.1,1-(now()-started)/1000))
