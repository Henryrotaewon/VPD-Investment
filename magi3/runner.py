"""Single-process durable SHADOW worker and read-only real account collector."""
import asyncio
import fcntl
import os
from pathlib import Path
import requests
from aiohttp import web
from .config import Config
from .runtime_store import RuntimeStore,encode
from .market_data import MarketData
from .accounts import collect_accounts
from .shadow_runtime import ShadowEngine,stamp
from .service_http import make_app


def log(event,**values):print(encode({'event':event,**values}),flush=True)


def pull_signals(url,token):
    with requests.Session() as http:
        http.trust_env=False
        response=http.get(url,headers={'Authorization':'Bearer '+token},timeout=5)
        response.raise_for_status();body=response.json()
    if body.get('schema')!='magi2-shadow-intents-v1' or not -5000<=stamp()-int(body['generated_ts_ms'])<=30000:
        raise ValueError('INVALID_OR_STALE_SIGNAL_FEED')
    if not isinstance(body['signals'],list) or len(body['signals'])>100:raise ValueError('INVALID_FEED_SIZE')
    return body


async def run():
    config=Config.from_env()
    root=Path(os.getenv('MAGI3_DATA_DIR','/data/magi3'))
    if os.getenv('RAILWAY_PROJECT_ID'):
        mount=os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
        if not mount or not root.is_relative_to(Path(mount)) or not Path(mount).exists():
            raise RuntimeError('PERSISTENT_VOLUME_REQUIRED')
    root.mkdir(parents=True,exist_ok=True)
    lock=(root/'runtime.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    store=RuntimeStore(root)
    # Different sessions/caches keep slow account valuations out of the execution path.
    market=MarketData();account_market=MarketData()
    account_market.adapters['kraken'].nonce_provider=store.nonce
    engine=ShadowEngine(store,market,config,float(os.getenv('MAGI3_SHADOW_FEE_BPS','10')))
    token=os.getenv('MAGI_SERVICE_TOKEN','')
    url=os.getenv('MAGI2_SHADOW_SIGNALS_URL','')
    if not url:raise ValueError('MAGI2_SHADOW_SIGNALS_URL_REQUIRED')
    store.put('status',{'mode':config.mode,'live_enabled':False,'started_ts_ms':stamp(),'heartbeat_ms':None,
                        'feed_status':'STARTING','loop_status':'STARTING','persistent':True})
    app=make_app(token,{'/status':lambda:store.get('status'),'/accounts':lambda:store.get('accounts'),
                        '/shadow':lambda:store.get('shadow'),
                        '/orders/recent':lambda request:store.orders_window(request.query.get('until'),request.query.get('offset',0)),
                        '/orders':lambda:{'mode':'SHADOW','generated_ts_ms':stamp(),'orders':store.recent_orders()}})
    runner=web.AppRunner(app,access_log=None);await runner.setup()
    await web.TCPSite(runner,os.getenv('MAGI3_HTTP_BIND','::'),int(os.getenv('MAGI3_HTTP_PORT','8083'))).start()

    def cycle():
        state=store.get('status');state['heartbeat_ms']=stamp()
        try:
            exits=engine.exit_due();engine.recover_pending()
            state['pending_exit_count']=sum(p['deadline_ms']<=stamp() for p in store.positions())
            if exits:log('shadow_exits',results=exits)
            try:
                body=pull_signals(url,token)
                source=body.get('source_status') or {}
                state.update(feed_status='OK' if source.get('status')=='OK' else 'SOURCE_UNAVAILABLE_OR_STALE',
                             feed_last_success_ms=stamp(),source_status=source)
                for s in body['signals']:
                    result=engine.ingest(s)
                    if result not in ('FILLED','REJECTED') or not store.get('last_signal_id')==s.get('id'):
                        log('shadow_signal',signal_id=s.get('id'),result=result)
                    store.put('last_signal_id',s.get('id'))
            except Exception as exc:
                state.update(feed_status='UNAVAILABLE',feed_error_type=type(exc).__name__)
            store.put('shadow',engine.report())
            state.update(loop_status='OK',last_cycle_success_ms=stamp())
        except Exception as exc:state.update(loop_status='ERROR',loop_error_type=type(exc).__name__)
        state['heartbeat_ms']=stamp();store.put('status',state)
        log('shadow_cycle',mode=config.mode,feed_status=state['feed_status'],loop_status=state['loop_status'],
            feed_error_type=state.get('feed_error_type') if state['feed_status']=='UNAVAILABLE' else None,positions=len(store.positions()),orders=len(store.recent_orders(100)),pending_exits=state.get('pending_exit_count'))

    async def execution():
        while True:await asyncio.to_thread(cycle);await asyncio.sleep(10)
    async def accounts():
        while True:
            try:
                report=await asyncio.to_thread(collect_accounts,account_market.adapters,account_market)
                store.put('accounts',report)
                log('accounts_collected',complete=report['complete'],venues={v['venue']:v['error'] or v['status'] for v in report['venues']})
            except Exception as exc:log('accounts_collection_failed',error_type=type(exc).__name__)
            await asyncio.sleep(60)
    log('magi3_started',mode=config.mode,live_enabled=False,persistent=True)
    try:await asyncio.gather(execution(),accounts())
    finally:
        await runner.cleanup();store.close();lock.close()


def main():asyncio.run(run())
if __name__=='__main__':main()
