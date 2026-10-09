"""Own both accounts inside the public-data process, isolated from live orders."""
import asyncio
import json
from pathlib import Path
from .bear import BearPaper, warm_history
from .derivatives import Derivatives
from .market import Collector, now_ms, fresh


class Models:
    def __init__(self,directory,stamp):
        self.root=Path(directory);self.root.mkdir(parents=True,exist_ok=True)
        self.bear=BearPaper(self.root/'bear.sqlite3',stamp)
        self.derivatives=Derivatives(self.root/'derivatives.sqlite3',stamp)
        self.collector=None;self.last_log=0

    async def run(self,session,symbols):
        self.collector=Collector(session)
        warmer=asyncio.create_task(warm_history(self.bear,symbols,now_ms))
        try:
            while True:
                if warmer.done():warmer.result();raise RuntimeError('BEAR_HISTORY_WORKER_STOPPED')
                start=now_ms()
                try:
                    books,fx=await self.collector.poll()
                    ts=now_ms();self.bear.regime=dict(self.collector.regime)
                    self.derivatives.tick(books,fx,self.collector.funding,self.collector.regime,self.bear,ts)
                    status=dict(ts=ts,bear=self.bear.report(ts),derivatives=self.derivatives.report(ts),
                        feeds=self.collector.health,fx=fx,quote_ready=sum(fresh(q,ts) for q in books.values()),
                        quote_total=len(books),regime=self.collector.regime,
                        pairs=self.derivatives.audit)
                    tmp=self.root/'status.tmp';tmp.write_text(json.dumps(status,allow_nan=False));tmp.replace(self.root/'status.json')
                    if ts-self.last_log>=60_000:
                        self.last_log=ts
                        print(json.dumps(dict(mode='FAST_MODELS_STATUS',**{k:status[k] for k in ('ts','bear','derivatives','quote_ready','quote_total','regime')},
                            feed_errors={k:v['reason'] for k,v in self.collector.health.items() if v['state']!='OK'}),ensure_ascii=False),flush=True)
                except Exception as exc:
                    print('FAST_MODELS_ERROR '+type(exc).__name__+':'+str(exc)[:180],flush=True)
                await asyncio.sleep(max(1,10-(now_ms()-start)/1000))
        finally:
            warmer.cancel();await asyncio.gather(warmer,return_exceptions=True)
            self.derivatives.save(now_ms());self.derivatives.db.close()
