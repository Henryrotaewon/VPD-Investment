"""MAGI2's isolated VPD shadow experiment; never mutates the PAPER portfolio."""
import asyncio
from datetime import datetime
import hashlib
import math
import os
import threading
import time
import requests
from aiohttp import web
from magi3.runtime_store import RuntimeStore
from magi3.service_http import make_app


def candidates(snapshot,now):
    if snapshot.get('universe')!='UPBIT_KRW':raise ValueError('NOT_UPBIT_KRW')
    dt=datetime.fromisoformat(snapshot['asof'])
    if dt.tzinfo is None:raise ValueError('MISSING_SOURCE_TIMEZONE')
    source_ms=int(dt.timestamp()*1000)
    if not 0<=now-source_ms<=12*3600000:raise ValueError('STALE_VPD_SNAPSHOT')
    out=[]
    for row in snapshot.get('top10',[])[:10]:
        score=float(row.get('VPD',0))
        if not math.isfinite(score) or not 75<=score<=100:continue
        asset=row['coin']
        if row.get('market')!='KRW-'+asset:continue
        ident='vpd:'+hashlib.sha256((snapshot['asof']+':'+asset).encode()).hexdigest()[:32]
        out.append({'id':ident,'source':'MAGI2','mode':'SHADOW','side':'BUY',
            'venue':'upbit','asset':asset,'strategy_tags':['VPD'],
            'notional_krw':10000,'max_holding_seconds':300,
            'created_ts_ms':now,'expires_ts_ms':now+120000,
            'source_ts_ms':source_ms,'source_snapshot':snapshot['asof'],
            'score':score,'score_semantics':'VPD heuristic; not win probability',
            'expected_move_bps':None,'pick_basis':['VPD>=75','SHADOW_EXPERIMENT'],
            'distribution_risk':row.get('DistributionRisk','UNKNOWN'),
            'policy':'VPD_SHADOW_V1; one attempt per asset and snapshot; not PAPER or validated live strategy'})
    return out


class Outbox:
    def __init__(self,root):self.store=RuntimeStore(root,0)
    def update(self,snapshot,now):
        rows=candidates(snapshot,now)
        with self.store.transaction() as db:
            from magi3.runtime_store import encode
            for s in rows:
                db.execute('INSERT OR IGNORE INTO signals VALUES(?,?,?,?,?)',(s['id'],encode(s),'OUTBOX',None,now))
        # Existing IDs keep their first-created expiry even after restarts.
        self.store.put('source_status',{'status':'OK','source_asof':snapshot['asof'],'last_success_ms':now})
    def report(self):
        import json
        now=time.time_ns()//1000000
        rows=self.store.rows('SELECT payload FROM signals WHERE created_ms>=? ORDER BY created_ms LIMIT 100',(now-120000,))
        return {'schema':'magi2-shadow-intents-v1','generated_ts_ms':now,
                'source_status':self.store.get('source_status'),
                'signals':[s for s in (json.loads(x['payload']) for x in rows) if s['expires_ts_ms']>now]}


def fetch_latest(repo):
    results=[]
    for period in ('morning','evening'):
        try:
            response=requests.get(f'https://raw.githubusercontent.com/{repo}/main/data/magi1_upbit_{period}_state.json',timeout=8)
            response.raise_for_status();body=response.json()
            dt=datetime.fromisoformat(body['asof'])
            if dt.tzinfo is None:continue
            results.append((dt.timestamp(),body))
        except (requests.RequestException,ValueError,KeyError,TypeError):continue
    if not results:raise ValueError('VPD_SOURCE_UNAVAILABLE')
    return max(results,key=lambda x:x[0])[1]


def source_error_reason(exc):
    # Never expose arbitrary exception text (HTTP errors can include credentials).
    allowed = {'NOT_UPBIT_KRW', 'MISSING_SOURCE_TIMEZONE', 'STALE_VPD_SNAPSHOT', 'VPD_SOURCE_UNAVAILABLE'}
    return str(exc) if isinstance(exc, ValueError) and str(exc) in allowed else 'SOURCE_VALIDATION_OR_FETCH_FAILED'


async def run(root,repo,log):
    outbox=Outbox(root)
    app=make_app(os.getenv('MAGI_SERVICE_TOKEN',''),{'/signals':outbox.report})
    runner=web.AppRunner(app,access_log=None);await runner.setup()
    await web.TCPSite(runner,os.getenv('MAGI2_SHADOW_BIND','::'),int(os.getenv('MAGI2_SHADOW_PORT','8082'))).start()
    log('shadow_bridge_started mode=SHADOW private_read_only=true')
    try:
        while True:
            try:
                snapshot=await asyncio.to_thread(fetch_latest,repo)
                outbox.update(snapshot,time.time_ns()//1000000)
            except Exception as exc:
                old=outbox.store.get('source_status',{})
                outbox.store.put('source_status',{**old,'status':'SOURCE_UNAVAILABLE_OR_STALE','error_type':type(exc).__name__,'reason':source_error_reason(exc)})
                log('shadow_bridge_source_unavailable reason='+source_error_reason(exc))
            await asyncio.sleep(60)
    finally:await runner.cleanup();outbox.store.close()


def start(root,repo,log):
    if os.getenv('MAGI2_SHADOW_BRIDGE_ENABLED','0')!='1':return None
    def worker():
        try:asyncio.run(run(root/'shadow_outbox',repo,log))
        except Exception as exc:log('shadow_bridge_failed type='+type(exc).__name__)
    thread=threading.Thread(target=worker,daemon=True,name='shadow-bridge');thread.start();return thread
