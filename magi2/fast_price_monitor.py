"""FAST v4: current public price versus a five-minute-old observation."""
from collections import deque
from threading import Thread
import math
import time
from magi2.fast_volume_monitor import FastVolumeMonitor
from magi2.fast_monitor import PublicMarket, now, VENUES

VERSION='fast-price-rise-v4'
POLICY=dict(min_return_5m_bps=500,scan_interval_seconds=60,
            baseline_tolerance_ms=15000,signal_cooldown_ms=600000,
            max_quote_checks=20,spread_filter=False,volume_filter=False,acceleration_filter=False)


def price_signal(rows,ts):
    if not rows or rows[-1][0]!=ts:return None
    target=ts-300000
    old=[r for r in rows if r[0]<ts and abs(r[0]-target)<=POLICY['baseline_tolerance_ms']]
    if not old:return None
    base=min(old,key=lambda r:abs(r[0]-target))
    price=rows[-1][1]
    if not all(math.isfinite(p) and p>0 for p in (price,base[1])):return None
    rise=(price/base[1]-1)*10000
    return dict(qualified=rise+1e-8>=500,return_5m_bps=rise,
                window_end_ms=ts,baseline_ms=base[0],baseline_price=base[1],
                last_price=price,actual_window_ms=ts-base[0],
                previous_return_5m_bps=None,turnover_ratio=None)


class FastPriceMonitor(FastVolumeMonitor):
    rule_version=VERSION
    rule_policy=POLICY

    def start(self):
        for venue in VENUES:
            for fn,prefix in ((self.worker,'fast-price-'),(self.observe,'fast-observe-')):
                t=Thread(target=fn,args=(venue,),daemon=True,name=prefix+venue)
                t.start();self.threads.append(t)
        self.log('fast_monitor_started rule='+VERSION+' scan=60s price_5m_bps=500 volume_filter=false acceleration_filter=false paper_only=true')

    def summary(self):
        with self.lock:state={k:dict(v) for k,v in self.state.items()}
        lines=['⚡ FAST · 현재가가 5분 전 대비 +5% 이상',
               '1분 간격 확인 · 거래대금/가속/호가 차이 필터 없음 · 실주문 OFF']
        for v in VENUES:
            s=state.get(v,{})
            lines.append(f'{v.upper()}: {s.get("status","STARTING")} · 포착 {s.get("qualified",0)} · 검사한도 제외 {s.get("budget_excluded",0)}')
        return '\n'.join(lines)

    def worker(self,venue):
        market=PublicMarket(venue);history={}
        while not self.stop.is_set():
            started=time.monotonic()
            try:
                prices=market.prices();ts=now();eligible=[];ready=0
                for symbol,price in prices.items():
                    rows=history.setdefault(symbol,deque(maxlen=8));rows.append((ts,price))
                    e=price_signal(rows,ts)
                    if e:
                        ready+=1
                        if e['qualified']:eligible.append((symbol,e))
                history={k:v for k,v in history.items() if k in prices}
                eligible.sort(key=lambda x:-x[1]['return_5m_bps'])
                selected=eligible[:POLICY['max_quote_checks']]
                stats=dict(symbols=len(prices),probed=0,qualified=0,
                           budget_excluded=max(0,len(eligible)-len(selected)),errors=0)
                for rank,(symbol,evidence) in enumerate(selected,1):
                    try:
                        begin=now();bid,ask=market.quotes([symbol])[symbol];decision=now()
                        if decision-begin>1500 or decision-ts>15000 or not all(math.isfinite(x) and x>0 for x in (bid,ask)) or ask<bid:
                            raise ValueError('INVALID_OR_STALE_QUOTE')
                        stats['probed']+=1
                        stats['qualified']+=int(self.emit(venue,symbol,market.symbols[symbol],evidence,bid,ask,decision,ts,rank))
                    except Exception as exc:
                        stats['errors']+=1
                        self.audit.record('CANDIDATE_REJECTED',f'price-check:{venue}:{symbol}:{ts}',venue,symbol,'ALERT_ONLY',rule_version=VERSION,reason=str(exc)[:80] if isinstance(exc,ValueError) else type(exc).__name__)
                    self.stop.wait(.15)
                status='RANKED' if ready else 'WARMING_UP'
                self.audit.record('SCAN_COMPLETED',f'scan:{venue}:{ts}',venue,'*','ALERT_ONLY',ts_ms=ts,
                                  rule_version=VERSION,criteria=POLICY,observed=dict(status=status,**stats))
                self.update(venue,status=status,**stats)
                self.log(f'fast_price_scan venue={venue} status={status} signals={stats["qualified"]} errors={stats["errors"]}')
            except Exception as exc:
                self.update(venue,status='UNAVAILABLE',error=type(exc).__name__)
                self.log(f'fast_price_error venue={venue} type={type(exc).__name__}')
            self.stop.wait(max(1,60-(time.monotonic()-started)))
