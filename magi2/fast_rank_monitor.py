"""Public daily-close TOP5 rankings on KST xx:01/06/...; no live orders."""
from datetime import datetime, timezone
from threading import Event, Thread
import math
import time
from magi2.fast_monitor import PublicMarket, FastMonitor, VENUES, now
from magi2.fast_rank_paper import RankLedger, VERSION, DAY, INTERVAL, rank_day, rank_slot
from magi2.fast_target_service import TargetPaperService


class RankPaperService(TargetPaperService):
    ledger_class=RankLedger

    def start(self):
        super().start()
        self.log('fast_rank_policy top=5 baseline=previous_UTC_day_close schedule=KST_0901_every_5m '
                 'stop=outside_top5_AND_minus6 profit=plus12_limit entry_depth=100pct cumulative=preserved')
        return self


class RankMarket(PublicMarket):
    def previous_close(self, symbol, boundary):
        expected=boundary-DAY
        if self.venue in ('upbit','bithumb'):
            end=datetime.fromtimestamp(boundary/1000,timezone.utc).isoformat()
            data=self.get('/v1/candles/days',dict(market=symbol,to=end,count=2))
            rows=[(int(datetime.fromisoformat(x['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()*1000),
                   float(x['trade_price'])) for x in data]
        elif self.venue=='binance':
            data=self.get('/api/v3/klines',dict(symbol=symbol,interval='1d',startTime=expected,
                                               endTime=boundary-1,limit=2))
            rows=[(int(x[0]),float(x[4])) for x in data if int(x[6])<boundary]
        else:
            data=self.get('/0/public/OHLC',dict(pair=symbol,interval=1440,since=expected//1000))['result']
            rows=[(int(x[0])*1000,float(x[4])) for key,values in data.items() if key!='last' for x in values]
        matches=[price for stamp,price in rows if stamp==expected]
        if not matches:return None  # No prior-day candle: explicitly ineligible, never substitute today's open.
        if len(matches)!=1 or not math.isfinite(matches[0]) or matches[0]<=0:
            raise ValueError('INVALID_PREVIOUS_CLOSE')
        return matches[0]


def top_five(prices, baselines, symbols):
    if set(prices)!=set(symbols) or set(baselines)!=set(symbols):
        raise ValueError('INCOMPLETE_RANK_UNIVERSE')
    rows=[]
    for symbol,price in prices.items():
        if not math.isfinite(price) or price<=0:raise ValueError('INVALID_RANK_PRICE')
        base=baselines[symbol]
        if base is None:continue
        if not math.isfinite(base) or base<=0:raise ValueError('INVALID_PREVIOUS_CLOSE')
        rows.append(dict(symbol=symbol,asset=symbols[symbol],price=price,previous_close=base,
                         rise_pct=(price/base-1)*100))
    if not rows:raise ValueError('NO_ELIGIBLE_DAILY_CLOSE')
    rows.sort(key=lambda r:(-r['rise_pct'],r['symbol']))
    return [dict(row,rank=i) for i,row in enumerate(rows[:5],1)]


class FastRankMonitor(FastMonitor):
    def __init__(self, root, log, paper, market_factory=RankMarket, clock=now):
        super().__init__(root,log,paper)
        self.market_factory=market_factory;self.clock=clock

    def start(self):
        for venue in VENUES:
            thread=Thread(target=self.worker,args=(venue,),daemon=True,name='fast-rank-'+venue)
            thread.start();self.threads.append(thread)
        self.log('fast_monitor_started rule='+VERSION+' scan=300s anchor=0901_KST paused_state=preserved')

    def drain(self):return []

    def scan(self, venue, market, baselines, slot, generation):
        prices=market.prices();ts=self.clock()
        rows=top_five(prices,baselines,market.symbols)
        if not self.paper.ledger.save_rank(venue,slot,rows,ts,generation):return False
        # save_rank's SQLite transaction has committed before entries are reserved.
        accepted=self.paper.ledger.offer_rank(venue,self.clock())
        self.paper.wake[venue].set()
        self.update(venue,status='RANKED',slot=slot,qualified=len(rows),entries=len(accepted))
        self.log(f'fast_rank_saved venue={venue} slot={slot} top5='+','.join(r['symbol'] for r in rows)+
                 f' entries={len(accepted)} excluded_no_previous_close={sum(v is None for v in baselines.values())}')
        return True

    def worker(self, venue):
        market=self.market_factory(venue);baselines={};boundary=None;generation=None;next_slot=None
        try:
            while not self.stop.is_set():
                state=self.paper.ledger.control();ts=self.clock()
                if not state['enabled']:
                    self.update(venue,status='PAUSED');next_slot=None
                    self.stop.wait(1);continue
                if generation!=state['generation']:
                    generation=state['generation'];next_slot=None
                try:
                    day=rank_day(ts)
                    if boundary!=day or self.clock()-market.refreshed>3600000:
                        market.discover();boundary=day;baselines={}
                    baselines={s:p for s,p in baselines.items() if s in market.symbols}
                    # Cached once per day; rate-limited cold start, interrupted by pause/day rollover.
                    incomplete=False
                    for symbol in market.symbols:
                        if symbol in baselines:continue
                        current=self.paper.ledger.control()
                        if (not current['enabled'] or current['generation']!=generation
                                or rank_day(self.clock())!=boundary):
                            incomplete=True;break
                        found,value=self.paper.ledger.baseline(venue,boundary,symbol)
                        if not found:
                            self.update(venue,status='BASELINE_LOADING',ready=len(baselines),total=len(market.symbols))
                            value=market.previous_close(symbol,boundary)
                            self.paper.ledger.save_baseline(venue,boundary,symbol,value)
                            self.stop.wait(1.05 if venue=='kraken' else .15)
                        baselines[symbol]=value
                    if incomplete:continue
                    ts=self.clock()
                    if next_slot is None:
                        candidate=rank_slot(ts)
                        next_slot=candidate if ts-candidate<=15000 and candidate>=state['resumed_ms'] else candidate+INTERVAL
                        self.update(venue,status='READY',next_slot=next_slot)
                    if ts>=next_slot:
                        slot=rank_slot(ts)
                        next_slot=slot+INTERVAL
                        if ts-slot<=15000 and slot>=boundary+60000:
                            self.scan(venue,market,baselines,slot,generation)
                    self.stop.wait(.5)
                except Exception as exc:
                    self.update(venue,status='UNAVAILABLE',error=str(exc)[:80])
                    self.log(f'fast_rank_error venue={venue} reason={str(exc)[:100]}')
                    self.stop.wait(5)
        finally:market.http.close()

    def captures(self, offset=0):
        from magi2.fast_paper_report import keyboard, NAMES, clock
        from magi2.fast_session import day, bounds
        import json
        ts=self.clock();date=day(ts);start,_=bounds(date)
        with self.paper.ledger.lock:
            rows=self.paper.ledger.db.execute('SELECT payload FROM fast_rank_snapshots WHERE slot>=? '
                'AND slot<=? ORDER BY slot DESC,venue',(start,ts)).fetchall()
        latest={}
        for payload, in rows:
            snapshot=json.loads(payload)
            for row in snapshot['rows']:
                if row['entry_slot']>=start:
                    latest.setdefault((snapshot['venue'],row['symbol']),
                                      (row['entry_slot'],snapshot['venue'],row['asset']))
        selected=sorted(latest.values(),reverse=True);size=15
        offset=min(max(0,int(offset))//size*size,max(0,(len(selected)-1)//size*size))
        lines=['⚡ FAST 포착 리스트',f'{date} 07:30 이후 · {len(selected)}종목 · 최신순',
               '전일종가 대비 TOP 5 · 09:01 기준 5분 갱신',
               f'{offset//size+1}/{max(1,(len(selected)+size-1)//size)}페이지','']
        lines += [f'{NAMES[v]} · {asset} · {clock(stamp)}' for stamp,v,asset in selected[offset:offset+size]]
        if not selected:lines.append('당일 포착 종목이 없습니다.')
        with self.lock:states={v:dict(s) for v,s in self.state.items()}
        for v,s in states.items():
            if s.get('status')=='BASELINE_LOADING':
                lines.append(f'{NAMES[v]} 전일종가 준비 중 {s.get("ready",0)}/{s.get("total",0)}')
            elif s.get('status')=='UNAVAILABLE':lines.append(f'{NAMES[v]} 순위 확인 대기 (시세 조회 실패)')
        markup=keyboard();nav=[]
        if offset:nav.append(dict(text='◀ 이전',callback_data=f'captures:{offset-size}'))
        nav.append(dict(text='새로고침',callback_data=f'captures:{offset}'))
        if offset+size<len(selected):nav.append(dict(text='다음 ▶',callback_data=f'captures:{offset+size}'))
        markup['inline_keyboard'].insert(0,nav)
        return '\n'.join(lines),markup
