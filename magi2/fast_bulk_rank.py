"""Fast 1D gainers from bulk public tickers; no per-symbol candle warmup."""
import json
import math
import time
from magi2.fast_monitor import PublicMarket
from magi2.fast_rank_monitor import FastRankMonitor
from magi2.fast_rank_paper import INTERVAL, rank_day, rank_slot

BASIS = 'EXCHANGE_DAY_OPEN'
LABEL = '1D 당일시가 대비 TOP 5'


class BulkRankMarket(PublicMarket):
    def day_tickers(self):
        """Each price and its opening reference come from the same response."""
        out={};calls=0
        def add(symbol,price,opening):
            if symbol not in self.symbols:return
            if symbol in out:raise ValueError('DUPLICATE_RANK_TICKER')
            price=float(price);opening=float(opening)
            if not math.isfinite(price) or price<0 or not math.isfinite(opening) or opening<0 or (price==0 and opening>0):
                raise ValueError('INVALID_DAY_TICKER')
            out[symbol]=(price,opening)
        if self.venue in ('upbit','bithumb'):
            symbols=list(self.symbols)
            for i in range(0,len(symbols),80):
                rows=self.get('/v1/ticker',{'markets':','.join(symbols[i:i+80])});calls+=1
                for x in rows:add(x['market'],x['trade_price'],x['opening_price'])
                if i+80<len(symbols):time.sleep(.12)
        elif self.venue=='binance':
            symbols=list(self.symbols)
            for i in range(0,len(symbols),100):
                rows=self.get('/api/v3/ticker/tradingDay',dict(
                    symbols=json.dumps(symbols[i:i+100],separators=(',',':')),timeZone='0',type='FULL'))
                calls+=1
                for x in rows:add(x['symbol'],x['lastPrice'],x['openPrice'])
                if i+100<len(symbols):time.sleep(.12)
        else:
            data=self.get('/0/public/Ticker')['result'];calls=1
            for symbol,x in data.items():
                if symbol in self.symbols:add(symbol,x['c'][0],x['o'])
        if set(out)!=set(self.symbols):
            missing=len(set(self.symbols)-set(out))
            raise ValueError(f'INCOMPLETE_RANK_UNIVERSE missing={missing}')
        return out,calls


def gainers(tickers,symbols):
    if set(tickers)!=set(symbols):raise ValueError('INCOMPLETE_RANK_UNIVERSE')
    rows=[]
    for symbol,(price,opening) in tickers.items():
        if not all(math.isfinite(x) for x in (price,opening)) or price<0 or opening<0 or (price==0 and opening>0):
            raise ValueError('INVALID_DAY_TICKER')
        if opening==0:continue  # No published day opening reference; no fabricated return.
        rows.append(dict(symbol=symbol,asset=symbols[symbol],price=price,baseline_price=opening,
                         basis=BASIS,rise_pct=(price/opening-1)*100))
    if not rows:raise ValueError('NO_VALID_DAY_OPEN')
    rows.sort(key=lambda r:(-r['rise_pct'],r['symbol']))
    return [dict(row,rank=i) for i,row in enumerate(rows[:5],1)]


class FastBulkRankMonitor(FastRankMonitor):
    rank_label=LABEL

    def __init__(self,root,log,paper,market_factory=BulkRankMarket,clock=None):
        kwargs=dict(market_factory=market_factory)
        if clock is not None:kwargs['clock']=clock
        super().__init__(root,log,paper,**kwargs)

    def sample(self,market):
        began=self.clock();monotonic=time.monotonic()
        tickers,calls=market.day_tickers();finished=self.clock()
        if finished<began or finished-began>15000 or rank_day(began)!=rank_day(finished):
            raise ValueError('STALE_OR_CROSS_DAY_RANK')
        rows=gainers(tickers,market.symbols)
        return rows,dict(fetch_ms=round((time.monotonic()-monotonic)*1000),requests=calls,
                         symbols=len(tickers),excluded_no_day_open=sum(o==0 for _,o in tickers.values()))

    def scan(self,venue,market,slot,generation):
        rows,stats=self.sample(market);ts=self.clock()
        if not self.paper.ledger.save_rank(venue,slot,rows,ts,generation):return False
        accepted=self.paper.ledger.offer_rank(venue,self.clock())
        self.paper.wake[venue].set()
        self.update(venue,status='RANKED',slot=slot,qualified=len(rows),entries=len(accepted),**stats)
        self.log(f'fast_rank_saved venue={venue} slot={slot} basis={BASIS} top5='+
                 ','.join(r['symbol'] for r in rows)+f' entries={len(accepted)} '+
                 ' '.join(f'{k}={v}' for k,v in stats.items()))
        return True

    def worker(self,venue):
        market=self.market_factory(venue);generation=None;next_slot=None;ready=False
        try:
            while not self.stop.is_set():
                state=self.paper.ledger.control();ts=self.clock()
                if not state['enabled'] and not self.paper.ledger.prewarming():
                    self.update(venue,status='PAUSED');next_slot=None;ready=False
                    self.stop.wait(1);continue
                if generation!=state['generation']:
                    generation=state['generation'];next_slot=None
                try:
                    if not market.symbols or self.clock()-market.refreshed>3600000:market.discover()
                    if not ready:
                        # Read-only startup measurement. Never place an off-schedule order.
                        _,stats=self.sample(market);ready=True
                        self.update(venue,status='READY',**stats)
                        self.log(f'fast_bulk_ready venue={venue} basis={BASIS} '+
                                 ' '.join(f'{k}={v}' for k,v in stats.items())+' candle_requests=0')
                    ts=self.clock()
                    if not state['enabled']:
                        self.stop.wait(.5);continue
                    if next_slot is None:
                        candidate=rank_slot(ts)
                        next_slot=candidate if ts-candidate<=15000 and candidate>=state['resumed_ms'] else candidate+INTERVAL
                    if ts>=next_slot:
                        slot=rank_slot(ts);next_slot=slot+INTERVAL
                        if ts-slot<=15000 and slot>=rank_day(ts)+60000:
                            self.scan(venue,market,slot,generation)
                    self.stop.wait(.5)
                except Exception as exc:
                    ready=False
                    if str(exc).startswith('INCOMPLETE_RANK_UNIVERSE'):market.refreshed=0
                    self.update(venue,status='UNAVAILABLE',error=str(exc)[:80])
                    self.log(f'fast_rank_error venue={venue} reason={str(exc)[:100]}')
                    self.stop.wait(5)
        finally:market.http.close()
