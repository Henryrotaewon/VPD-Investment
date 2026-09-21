"""Public REST tape adapters and independent deadline/limit workers."""
from pathlib import Path
from threading import Event, Thread
import time

from magi2.fast_paper import VENUES
from magi2.fast_paper_service import PaperService, PublicPaperMarket, now
from magi2.fast_tick_paper import CurrentTickLedger, is_tick

VERSION=CurrentTickLedger.execution_version
from magi2.fast_tick_rules import UPBIT_GRID, BITHUMB_GRID


class TickMarket(PublicPaperMarket):
    def __init__(self,venue):
        super().__init__(venue)
        self.rule_cache={}

    def rules(self,symbol):
        cached=self.rule_cache.get(symbol)
        if cached and now()-cached[0]<3600000:return cached[1]
        if self.venue in ('upbit','bithumb'):
            r=dict(grid=UPBIT_GRID if self.venue=='upbit' else BITHUMB_GRID,
                   step='0.00000001' if self.venue=='upbit' else '0.0001',
                   min_qty=0,min_notional=5000,
                   source='UPBIT_KRW_20250731' if self.venue=='upbit' else 'BITHUMB_OFFICIAL_KIT_2b630426',
                   quantity_policy='KRW_PAPER_CONSERVATIVE_GRID')
        elif self.venue=='binance':
            x=self.market.get('/api/v3/exchangeInfo',{'symbol':symbol})['symbols'][0]
            if x['status']!='TRADING':raise ValueError('MARKET_NOT_TRADING')
            f={v['filterType']:v for v in x['filters']};p=f['PRICE_FILTER'];q=f['LOT_SIZE']
            n=f.get('NOTIONAL') or f.get('MIN_NOTIONAL')
            if not n:raise ValueError('MISSING_MIN_NOTIONAL')
            r=dict(tick=p['tickSize'],min_price=p['minPrice'],max_price=p['maxPrice'],
                   step=q['stepSize'],min_qty=q['minQty'],max_qty=q['maxQty'],
                   min_notional=n['minNotional'],source='BINANCE_EXCHANGE_INFO')
            m=f.get('MARKET_LOT_SIZE') or q
            r.update(market_step=m['stepSize'] if float(m['stepSize'])>0 else q['stepSize'],
                     market_min_qty=m['minQty'],market_max_qty=m['maxQty'])
        else:
            rows=self.market.get('/0/public/AssetPairs',{'pair':symbol})['result']
            x=rows.get(symbol) or next(iter(rows.values()))
            if x.get('status','online')!='online':raise ValueError('MARKET_NOT_TRADING')
            r=dict(tick=x['tick_size'],step=str(10**-int(x['lot_decimals'])),
                   min_qty=x['ordermin'],min_notional=x['costmin'],source='KRAKEN_ASSET_PAIRS')
        self.rule_cache[symbol]=(now(),r)
        return r

    def tape(self,symbol):
        started=now()
        if self.venue in ('upbit','bithumb'):
            data=self.market.get('/v1/trades/ticks',{'market':symbol,'count':500})
            rows=[dict(id=str(x['sequential_id']),ts=int(x['timestamp']),price=float(x['trade_price']),
                       qty=float(x['trade_volume']),buyer={'BID':True,'ASK':False}.get(x['ask_bid'])) for x in data]
        elif self.venue=='binance':
            data=self.market.get('/api/v3/aggTrades',{'symbol':symbol,'limit':1000})
            rows=[dict(id=str(x['a']),ts=int(x['T']),price=float(x['p']),qty=float(x['q']),
                       buyer=not x['m'] if isinstance(x['m'],bool) else None) for x in data]
        else:
            data=self.market.get('/0/public/Trades',{'pair':symbol,'count':1000})['result']
            rows=[dict(id=str(x[6]),ts=int(float(x[2])*1000),price=float(x[0]),qty=float(x[1]),
                       buyer={'b':True,'s':False}.get(x[3])) for k,v in data.items() if k!='last' for x in v]
        return dict(trades=rows,requested_ms=started,received_ms=now())


class TickPaperService(PaperService):
    def __init__(self,root,log,market_factory=TickMarket,clock=now):
        self.clock=clock;self.log=log;self.market_factory=market_factory
        self.ledger=CurrentTickLedger(Path(root)/'fast_paper.sqlite3',clock())
        self.stop=Event();self.threads=[];self.wake={v:Event() for v in VENUES}
        self.last_mark={v:0 for v in VENUES}

    def start(self):
        for venue in VENUES:
            for fn,prefix in ((self.worker,'tick-deadline-'),(self.limit_worker,'tick-limit-')):
                t=Thread(target=fn,args=(venue,),daemon=True,name=prefix+venue)
                t.start();self.threads.append(t)
        self.log(f'fast_paper_started policy={VERSION} accounts=4 seed_preserved=true '
                 'slot_krw=200000 deadline_from_signal_sec=600 limit_cycle=true live_orders=false')
        return self

    def step(self,venue,market):
        self.ledger.advance(venue,self.clock())
        if self.ledger.account(venue)['funded_ms'] is None:
            fx,source=market.fx();self.ledger.fund(venue,fx,source,self.clock())
        positions=self.ledger.active(venue);errors=[]
        exits=sorted((t for t in positions if t['status']=='EXIT_PENDING'),key=lambda t:t['deadline_ms'])
        legacy_entries=[t for t in positions if t['status']=='ENTRY_PENDING' and not is_tick(t)]
        for t in exits+legacy_entries:
            ready=t.get('exit_ready_ms') if t['status']=='EXIT_PENDING' else t['ready_ms']
            if self.clock()<ready:continue
            try:
                book=market.book(t['symbol']);self.ledger.advance(venue,self.clock())
                changed=(self.ledger.exit if t['status']=='EXIT_PENDING' else self.ledger.enter)(t['id'],book,self.clock())
                if changed:
                    latest=self.ledger.get(t['id'])
                    self.log(f'fast_paper_fill venue={venue} symbol={t["symbol"]} status={latest["status"]} reason={latest.get("reason")}')
            except Exception as exc:
                reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                self.ledger.fail(t['id'],reason);errors.append(reason)
        if self.clock()-self.last_mark[venue]>=5000:
            self.last_mark[venue]=self.clock()
            symbols=[t['symbol'] for t in self.ledger.active(venue) if t['remaining_qty']>0]
            if symbols:
                try:
                    quotes,received=market.quotes(symbols);self.ledger.mark(venue,quotes,received)
                except Exception as exc:errors.append(type(exc).__name__)
        self.ledger.heartbeat(venue,self.clock(),','.join(sorted(set(errors))) or None)

    def worker(self,venue):
        market=self.market_factory(venue)
        while not self.stop.is_set():
            self.wake[venue].clear()
            try:self.step(venue,market)
            except Exception as exc:
                self.ledger.heartbeat(venue,self.clock(),type(exc).__name__)
                self.log(f'fast_tick_deadline_error venue={venue} type={type(exc).__name__}')
            self.wake[venue].wait(.25)

    def limit_step(self,t,market):
        if self.clock()>=t['deadline_ms']:return
        rules=market.rules(t['symbol']);tape=market.tape(t['symbol'])
        order=t.get('order')
        book=market.book(t['symbol']) if order and (order['active_ms'] is None or order.get('resync')) else None
        changed=self.ledger.limit_step(t['id'],tape,book,rules,self.clock())
        latest=self.ledger.get(t['id'])
        if changed and latest['order'] is None and latest['status'] in ('OPEN','ENTRY_PENDING'):
            # Place the next side from the current observed last trade; no reused fills.
            self.ledger.limit_step(t['id'],tape,None,rules,self.clock())

    def limit_worker(self,venue):
        market=self.market_factory(venue)
        while not self.stop.is_set():
            started=time.monotonic()
            for t in self.ledger.active(venue):
                if not is_tick(t) or t['status']=='EXIT_PENDING':continue
                try:self.limit_step(t,market)
                except Exception as exc:
                    reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                    self.ledger.fail(t['id'],reason)
                    self.log(f'fast_tick_limit_error venue={venue} symbol={t["symbol"]} reason={reason[:80]}')
            self.stop.wait(max(.1,1-(time.monotonic()-started)))
