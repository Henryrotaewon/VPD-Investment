"""Four independent public-data scanners; liquidity universe then indicator ranking."""
from collections import Counter
from pathlib import Path
from threading import Event, Lock, Thread
import json
import math
import time
from magi2.fast_monitor import VENUES, now
from magi2.fast_wave_market import IndicatorMarket
from magi2.fast_wave_indicator import evaluate, VERSION
from magi2.fast_wave_store import EvidenceStore
from magi2.indicator_paper import COHORT, LABEL


class LiquidIndicatorMarket(IndicatorMarket):
    def liquid_symbols(self, count=20):
        # Turnover is comparable only within each venue's single quote currency.
        values = {}
        if self.venue in ('upbit','bithumb'):
            symbols = list(self.symbols)
            for i in range(0,len(symbols),80):
                for row in self.get('/v1/ticker', {'markets':','.join(symbols[i:i+80])}):
                    values[row['market']] = float(row['acc_trade_price_24h'])
                time.sleep(.15)
        elif self.venue == 'binance':
            for row in self.get('/api/v3/ticker/24hr'):
                if row['symbol'] in self.symbols: values[row['symbol']] = float(row['quoteVolume'])
        else:
            for symbol,row in self.get('/0/public/Ticker')['result'].items():
                if symbol in self.symbols: values[symbol] = float(row['v'][1])*float(row['p'][1])
        eligible = [(s,v) for s,v in values.items() if s in self.symbols and math.isfinite(v) and v > 0]
        if not eligible: raise ValueError('EMPTY_LIQUID_UNIVERSE')
        return [s for s,_ in sorted(eligible,key=lambda x:(-x[1],x[0]))[:count]]

    def capture(self, symbol):
        started = self.clock()
        if self.venue in ('upbit','bithumb'):
            rows = self.get('/v1/ticker', {'markets':symbol})
            price = float(next(x for x in rows if x['market']==symbol)['trade_price'])
        elif self.venue == 'binance':
            price = float(self.get('/api/v3/ticker/price', {'symbol':symbol})['price'])
        else:
            rows = self.get('/0/public/Ticker', {'pair':symbol})['result']
            if len(rows)!=1: raise ValueError('AMBIGUOUS_CAPTURE')
            price = float(next(iter(rows.values()))['c'][0])
        if not math.isfinite(price) or price<=0 or not 0<=self.clock()-started<=1500:
            raise ValueError('INVALID_OR_SLOW_CAPTURE')
        return price,started


class IndicatorMonitor:
    def __init__(self, root, log, paper, market_factory=LiquidIndicatorMarket, clock=now):
        self.root=Path(root); self.log=log; self.paper=paper; self.market_factory=market_factory; self.clock=clock
        self.stop=Event(); self.lock=Lock(); self.state={}; self.threads=[]

    def update(self, venue, **fields):
        with self.lock: self.state[venue]=dict(fields,updated_ms=self.clock())

    def start(self):
        if getattr(self.paper.ledger,'policy_review_required',False):
            self.log('indicator_monitor_held reason=DAILY_POLICY_REVIEW minute_capture_enabled=false')
            return
        for venue in VENUES:
            t=Thread(target=self.worker,args=(venue,),daemon=True,name='indicator-'+venue)
            t.start(); self.threads.append(t)
        self.log('indicator_monitor_started version='+VERSION+' cohort='+COHORT+
                 ' venues=4 liquidity_top=20 interval_sec=60 entry_top=5 paper_only=true')

    def drain(self): return []

    def scan(self, market, store, symbols, generation):
        qualified=[]; reasons=Counter(); ready=0; errors=0
        for symbol in symbols:
            state=self.paper.ledger.control()
            if not state['enabled'] or state['generation']!=generation or self.stop.is_set(): return
            try:
                point=market.observe(symbol);store.append(point)
                points=store.recent(market.venue,symbol,point['observed_ms'])
                # A stop/start cannot reuse pre-resume observations as entry evidence.
                points=[p for p in points if p['observed_ms']>=state['resumed_ms']]
                result=evaluate(points,self.clock());store.record(point,result)
                ready+=bool(result['ready']);reasons[result['reason']]+=1
                self.paper.ledger.observe_holding(market.venue,symbol,result,self.clock())
                if result['technical_candidate'] and not any(t['symbol']==symbol for t in self.paper.ledger.active(market.venue)):
                    qualified.append(result)
            except Exception as exc:
                errors+=1;reasons[type(exc).__name__+':'+str(exc)[:65]]+=1
                store.record_error(market.venue,symbol,self.clock(),type(exc).__name__+':'+str(exc)[:100])
            self.stop.wait(1.05 if market.venue=='kraken' else .2)
        ranked=sorted(qualified,key=lambda r:(-r['score'],-r['latest']['current']['volume_pace'],r['latest']['symbol']))[:5]
        entries=0; confirmed=0
        for result in ranked:
            symbol=result['latest']['symbol']
            try:
                # Fresh capture first, flow last: do not trade on a minute-old last price.
                price,stamp=market.capture(symbol)
                flow=market.flow(symbol)
                checked=evaluate(result['observations'],self.clock(),flow=flow)
                store.record_confirmation(result['latest'],checked)
                confirmed+=bool(checked['paper_candidate'])
                if self.paper.ledger.offer_decision(checked,price,stamp,self.clock(),generation): entries+=1
            except Exception as exc:
                errors+=1;store.record_error(market.venue,symbol,self.clock(),type(exc).__name__+':'+str(exc)[:100])
            self.stop.wait(1.05 if market.venue=='kraken' else .2)
        self.paper.wake[market.venue].set()
        store.prune(self.clock())
        self.update(market.venue,status='MONITORING',symbols=len(symbols),ready=ready,
                    technical=len(qualified),confirmed=confirmed,entries=entries,errors=errors,
                    reasons=dict(reasons))
        self.log('indicator_scan '+json.dumps(dict(venue=market.venue,symbols=len(symbols),ready=ready,
                 technical=len(qualified),confirmed=confirmed,entries=entries,errors=errors,reasons=dict(reasons)),ensure_ascii=False))

    def worker(self, venue):
        market=self.market_factory(venue); store=EvidenceStore(self.root,COHORT+'-'+venue)
        selection=[];selected_ms=0;generation=None
        try:
            while not self.stop.is_set():
                started=time.monotonic(); state=self.paper.ledger.control()
                if not state['enabled']:
                    self.update(venue,status='PAUSED');self.stop.wait(1);continue
                try:
                    if not market.symbols or self.clock()-market.refreshed>3600000: market.discover()
                    if not selection or self.clock()-selected_ms>=900000 or generation!=state['generation']:
                        selection=market.liquid_symbols(); selected_ms=self.clock();generation=state['generation']
                        self.log(f'indicator_universe venue={venue} count={len(selection)} basis=24h_quote_turnover')
                    active=[t['symbol'] for t in self.paper.ledger.active(venue)]
                    symbols=list(dict.fromkeys(active+selection))
                    self.scan(market,store,symbols,generation)
                except Exception as exc:
                    self.update(venue,status='UNAVAILABLE',error=type(exc).__name__+':'+str(exc)[:80])
                    self.log(f'indicator_worker_error venue={venue} reason={type(exc).__name__}:{str(exc)[:80]}')
                self.stop.wait(max(1,60-(time.monotonic()-started)))
        finally: market.http.close();store.close()

    def summary(self):
        if getattr(self.paper.ledger,'policy_review_required',False):
            return ('지표가속 · 일봉 전략 설계 검토 중\n'
                    '분 단위 포착·신규 매수 중지. 이전 모의 이력은 별도 보존합니다.\n'
                    'D일 확정 일봉까지의 지표 변화·가속도 → D+1일 일봉 시작 매수 구상.\n'
                    '현재 일봉 자동 포착은 아직 시작하지 않았습니다. 가중치·임계값·매도 조건 미확정.')
        from magi2.fast_paper_report import NAMES
        lines=['⚡ 지표가속 · '+LABEL,'거래소별 거래대금 상위 20종목 + 보유종목 · 약 1분 관측',
               '전일 확정 지표와 현재 변화·가속도 비교 → 기술점수 상위 5개 수급 확인',
               '후보가 부족하면 현금 유지 · 점수는 승률이 아닙니다.','']
        with self.lock: states=dict(self.state)
        labels={'MONITORING':'관측 중','PAUSED':'정지','UNAVAILABLE':'자료 확인 대기'}
        for venue in VENUES:
            s=states.get(venue,{})
            lines.append(f'{NAMES[venue]} · {labels.get(s.get("status"),"준비 중")} · '
                         f'지표 준비 {s.get("ready",0)}/{s.get("symbols",0)} · 수급통과 {s.get("confirmed",0)} · 오류 {s.get("errors",0)}')
            if s.get('error'):lines.append(s['error'])
        return '\n'.join(lines)

    def captures(self, offset=0):
        from magi2.fast_paper_report import keyboard, clock, NAMES
        from magi2.fast_session import day,bounds
        ts=self.clock();start,_=bounds(day(ts))
        with self.paper.ledger.lock:
            rows=self.paper.ledger.db.execute('SELECT ts_ms,venue,symbol,payload FROM indicator_signals '
                'WHERE ts_ms>=? AND ts_ms<=? ORDER BY ts_ms DESC',(start,ts)).fetchall()
        size=8;offset=min(max(0,offset)//size*size,max(0,(len(rows)-1)//size*size))
        lines=['⚡ 지표가속 포착',f'{day(ts)} 07:30 이후 · 수급 확인 {len(rows)}건','']
        if getattr(self.paper.ledger,'policy_review_required',False):
            lines.insert(0,'이전 분 단위 실험 이력 · 신규 포착 중지 · 일봉 전략 결과 아님')
        for stamp,venue,symbol,payload in rows[offset:offset+size]:
            result=json.loads(payload)
            lines += [f'{NAMES[venue]} · {symbol} · {clock(stamp)}',
                      f'기술 {result["score"]}/100 · 매수주도 {result["flow"]["buyer_share_pct"]:.1f}% · '+
                      ('매수 접수' if result['accepted'] else '미매수: '+str(result.get('reason')))]
        if not rows:lines.append('조건을 통과한 포착이 없습니다.')
        markup=keyboard();nav=[]
        if offset:nav.append(dict(text='◀ 이전',callback_data=f'captures:{offset-size}'))
        nav.append(dict(text='새로고침',callback_data=f'captures:{offset}'))
        if offset+size<len(rows):nav.append(dict(text='다음 ▶',callback_data=f'captures:{offset+size}'))
        markup['inline_keyboard'].insert(0,nav)
        return '\n'.join(lines),markup
