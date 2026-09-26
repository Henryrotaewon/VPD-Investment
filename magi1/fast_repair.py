"""Asynchronous per-market native-candle repair; single SQLite writer.

Only HTTP runs in a thread. Planning, page commits, and live tick processing
share the event-loop owner. No independent writer or whole-universe pause.
"""
import asyncio
from collections import Counter
import json
from pathlib import Path
import shutil
import time
from magi1.fast_bootstrap import Public, api_rows, apply_page, bounds, missing_plan
from magi1.fast_observe import DAY,FIVE,now_ms


class Repair:
    def __init__(self,observer,symbols,path,client=None,clock=now_ms,emit=None):
        self.observer=observer;self.symbols=list(symbols);self.path=Path(path)
        self.client=client or Public();self.clock=clock
        self.emit=emit or (lambda x:print(json.dumps(x),flush=True))
        self.states={};self.retries={};self.pages=0;self.done=0;self.cycle=0
        self.last_emit=0;self.last_page_ms=None;self.started_ms=None
        observer.repair=self

    def ready(self,symbol,cutoff):
        full=cutoff//FIVE*FIVE-FIVE
        keys=[full-k*DAY for k in range(11)]
        rows=dict(self.observer.db.execute('SELECT bucket,value FROM baseline WHERE symbol=? AND bucket IN ('+','.join('?'*11)+')',(symbol,*keys)))
        return len(rows)==11 and sum(rows[t] for t in keys[1:])>0

    def gate(self,symbol):
        # Missing OHLCV at unrelated times must not block an otherwise valid
        # live signal. Only its current same-time reference is required here.
        ready=self.ready(symbol,self.clock())
        if not ready:
            self.observer.repair_blocked.add(symbol)
            self.observer.previous.pop(symbol,None)
        elif symbol in self.observer.repair_blocked:
            self.observer.repair_blocked.discard(symbol)
            self.observer.previous.pop(symbol,None)  # fresh two-window confirmation
        return ready

    def report(self):
        return {'cycle':self.cycle,'checked':self.done,'total':len(self.symbols),
                'states':dict(Counter(self.states.values())),'blocked':len(self.observer.repair_blocked),
                'pages':self.pages,'last_page_ms':self.last_page_ms,'started_ms':self.started_ms}

    def progress(self,force=False):
        now=self.clock()
        if force or now-self.last_emit>=10000:
            self.emit({'mode':'FAST_REPAIR_PROGRESS',**self.report()});self.last_emit=now

    async def repair_symbol(self,symbol,cutoff,max_pages=32):
        if self.clock()<self.retries.get(symbol,0):
            self.gate(symbol);return
        self.gate(symbol)
        self.states[symbol]='CHECKING'
        try:
            missing=missing_plan(self.observer.db,symbol,cutoff)
            pages=0
            self.states[symbol]='REPAIRING' if missing else 'COMPLETE'
            while missing and pages<max_pages:
                cursor=max(missing)+FIVE
                payload=await asyncio.to_thread(self.client.candles,symbol,cursor)
                rows=api_rows(payload,cursor);pages+=1;self.pages+=1
                if not rows:break
                # Recheck cutoff retention in case an unusually long request
                # crossed the day boundary; old pages never drive live ticks.
                self.observer.advance(self.clock())
                missing.difference_update(apply_page(self.observer.db,symbol,cutoff,rows,cursor,missing))
                self.last_page_ms=self.clock();self.progress()
                await asyncio.sleep(0)
            self.states[symbol]='INCOMPLETE_HISTORY' if missing else 'COMPLETE'
            if missing:self.retries[symbol]=self.clock()+30*60*1000
            else:self.retries.pop(symbol,None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.states[symbol]='RETRY_WAIT';self.retries[symbol]=self.clock()+60000
            self.observer.event(self.clock(),'REPAIR_ERROR',symbol,{'type':type(exc).__name__})
            self.observer.db.commit()
            # An IP block/rate limit pauses REST repair only, never the feeds.
            status=getattr(getattr(exc,'response',None),'status_code',None)
            if str(exc)=='API_BLOCKED_STOP' or status in (418,429):
                self.progress(force=True)
                await asyncio.sleep(300 if status==418 or str(exc)=='API_BLOCKED_STOP' else 60)
        finally:
            self.gate(symbol)

    async def run(self):
        self.started_ms=self.clock()
        # Gate unknown current references before issuing the first request.
        for symbol in self.symbols:self.gate(symbol)
        self.progress(force=True)
        while True:
            if shutil.disk_usage(self.path.parent).free<64*1024*1024:
                raise RuntimeError('DISK_RESERVE_STOP')
            self.cycle+=1;self.done=0;_,cutoff=bounds(self.clock())
            started=time.monotonic()
            for symbol in self.symbols:
                await self.repair_symbol(symbol,cutoff)
                self.done+=1;self.progress()
                await asyncio.sleep(0)
            self.progress(force=True)
            await asyncio.sleep(max(1,300-(time.monotonic()-started)))
