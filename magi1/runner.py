"""MAGI1 isolated COLLECT_ONLY runtime; no credentialed order clients."""
import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from .collectors import CollectorSupervisor
from .normalizer import now_ms
from .onchain import BitcoinPublicWebSocketProvider,OnChainShockAdapter
from .research import ResearchEngine
from .report import build_daily,report_cutoff,KST
from .storage import Storage
from .universe import discover,get
from .vpd_join import snapshot
import aiohttp

LOG=logging.getLogger('magi1')

class App:
    def __init__(self,root,assets):
        self.storage=Storage(root);self.engine=ResearchEngine(self.storage,assets,now_ms());self.assets=assets
        self.queue=asyncio.Queue(maxsize=20000);self.collector=CollectorSupervisor(assets,self.enqueue)
        self.tick_count=0;self.onchain_status={'received':0};self.derivative_status={}
    async def enqueue(self,event):await self.queue.put(('market',event))
    def process(self,kind,value):
        if kind=='market':self.engine.ingest(value)
        elif kind=='vpd':self.engine.add_vpd(value)
        elif kind=='onchain':
            row=self.storage.payload(value);self.storage.append_raw('onchain',row);self.storage.append('onchain_candidate',row);self.engine.onchain.append(row)
        elif kind=='derivatives':self.storage.append('derivatives_context',value);self.engine.derivatives.append(value)
        elif kind=='tick':
            self.engine.tick(value);self.tick_count+=1
            if self.tick_count%5==0:self.storage.flush()
            if self.tick_count%30==0:
                self.engine.checkpoint()
                diag={'event_ts_ms':value,'feeds':self.collector.diagnostics(),'queue_depth':self.queue.qsize(),'storage':self.storage.summary(),'onchain':self.onchain_status,'derivatives':self.derivative_status}
                self.storage.append('diagnostics',diag);LOG.info('feed_diagnostics=%s',json.dumps(diag))
                cutoff=report_cutoff(datetime.now(KST)).isoformat()
                if self.storage.restore('last_report')!=cutoff:
                    path=build_daily(self.storage);self.storage.checkpoint('last_report',cutoff);LOG.info('daily_report=%s',path)
            if self.tick_count%300==0:self.storage.maintain(int(os.getenv('MAGI1_RAW_RETENTION_DAYS','2')))
    async def consume(self):
        while True:
            kind,value=await self.queue.get()
            try:await asyncio.to_thread(self.process,kind,value)
            finally:self.queue.task_done()
    async def ticks(self):
        while True:await asyncio.sleep(1);await self.queue.put(('tick',now_ms()))
    async def vpd_loop(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25),headers={'User-Agent':'MAGI1-research'}) as session:
            last=None
            while True:
                try:
                    commits=await get(session,'https://api.github.com/repos/Henryrotaewon/VPD-Investment/commits',{'path':'data/vpd_latest.json','per_page':'1'})
                    sha=commits[0]['sha']
                    if sha!=last:
                        base=f'https://raw.githubusercontent.com/Henryrotaewon/VPD-Investment/{sha}/data/'
                        payload=await get(session,base+'vpd_latest.json')
                        async with session.get(base+'vpd_all_latest.csv') as r:
                            r.raise_for_status();csv_text=await r.text()
                        row=snapshot(payload,now_ms(),csv_text,sha)
                        await self.queue.put(('vpd',row));last=sha
                        LOG.info('vpd_snapshot asof=%s assets=%d source_commit=%s',payload['asof'],len(row['assets']),sha)
                except Exception as exc:LOG.warning('vpd_fetch error=%r',exc)
                await asyncio.sleep(300)
    async def onchain_loop(self):
        async for e in OnChainShockAdapter(BitcoinPublicWebSocketProvider(float(os.getenv('MAGI1_ONCHAIN_MIN_BTC','100')))).events():
            self.onchain_status={'received':self.onchain_status['received']+1,'last_ms':e.received_ts_ms}
            await self.queue.put(('onchain',e))
    async def derivatives_loop(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            while True:
                for asset in self.assets:
                    try:
                        x=await get(session,'https://api.bybit.com/v5/market/tickers',{'category':'linear','symbol':asset+'USDT'})
                        if x['result']['list']:
                            d=x['result']['list'][0];ts=now_ms()
                            row={'asset':asset,'provider':'bybit_public_linear_ticker','event_ts_ms':int(x.get('time',ts)),'received_ts_ms':ts,'open_interest':d.get('openInterest'),'funding_rate':d.get('fundingRate'),'mark_price':d.get('markPrice'),'last_price':d.get('lastPrice'),'standalone_signal':False}
                            await self.queue.put(('derivatives',row));self.derivative_status[asset]={'last_ms':ts,'status':'OK'}
                    except Exception as exc:
                        self.derivative_status[asset]={'status':'ERROR','error':str(exc)}
                    await asyncio.sleep(.2)
                await asyncio.sleep(60)
    async def universe_loop(self):
        while True:
            await asyncio.sleep(86400)
            try:
                selected=await discover(str(self.storage.root/'universe_next.json'))
                self.storage.append('universe',{'event_ts_ms':now_ms(),**selected})
                if selected['assets']!=self.assets:
                    self.collector_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):await self.collector_task
                    self.assets=selected['assets'];self.engine.assets=self.assets
                    self.collector=CollectorSupervisor(self.assets,self.enqueue)
                    self.collector_task=asyncio.create_task(self.collector.run())
            except Exception as exc:LOG.warning('universe_refresh error=%r',exc)
    async def run(self,duration=None):
        stop=asyncio.Event();loop=asyncio.get_running_loop()
        for sig in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(sig,stop.set)
        self.collector_task=asyncio.create_task(self.collector.run())
        consumer=asyncio.create_task(self.consume())
        tasks=[asyncio.create_task(f()) for f in (self.ticks,self.vpd_loop,self.onchain_loop,self.derivatives_loop,self.universe_loop)]
        stopper=asyncio.create_task(stop.wait())
        timer=asyncio.create_task(asyncio.sleep(duration)) if duration else None
        try:
            done,_=await asyncio.wait([consumer,stopper]+([timer] if timer else []),return_when=asyncio.FIRST_COMPLETED)
            if consumer in done:await consumer
        finally:
            for t in tasks+[self.collector_task]:t.cancel()
            await asyncio.gather(*tasks,self.collector_task,return_exceptions=True)
            if not consumer.done():
                await self.queue.join();consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError):await consumer
            stopper.cancel()
            if timer:timer.cancel()


def data_root():
    if os.getenv('MAGI1_MODE', 'COLLECT_ONLY') != 'COLLECT_ONLY':
        raise ValueError('MAGI1 supports COLLECT_ONLY only')
    root = Path(os.getenv('MAGI1_DATA_DIR', '/data/magi1')).resolve()
    if os.getenv('RAILWAY_ENVIRONMENT_ID'):
        if not os.path.ismount('/data') or not root.is_relative_to(Path('/data')):
            raise RuntimeError('Railway requires a mounted /data volume and data directory under /data')
    root.mkdir(parents=True, exist_ok=True)
    return root


async def start(duration=None):
    root = data_root()
    LOG.info('magi1_starting mode=COLLECT_ONLY data_dir=%s', root)
    selected = await discover(str(root / 'universe.json'))
    app = App(root, selected['assets'])
    try:
        app.storage.maintain(int(os.getenv('MAGI1_RAW_RETENTION_DAYS', '2')))
        app.storage.append('universe', {'event_ts_ms': now_ms(), **selected})
        app.storage.flush()
        LOG.info('storage_startup=%s', json.dumps(app.storage.summary()))
        await app.run(duration)
    finally:
        app.engine.checkpoint()
        app.storage.close()
        LOG.info('magi1_stopped')


def main():
    parser = argparse.ArgumentParser(description='MAGI1 public market-data collector')
    parser.add_argument('--duration', type=float, default=None,
                        help='Optional bounded observation in seconds; default runs continuously')
    args = parser.parse_args()
    if args.duration is not None and args.duration <= 0:
        parser.error('--duration must be positive')
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s', stream=sys.stdout, force=True)
    asyncio.run(start(args.duration))


if __name__ == '__main__':
    main()
