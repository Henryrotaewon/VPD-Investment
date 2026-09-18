"""Opt-in bounded PUBLIC_CAPTURE. Never imports order clients or trading runners.

python -m magi1.fast_capture --venues upbit binance --duration 600 --output /tmp/fast.jsonl.gz
"""
import argparse
import asyncio
import gzip
import json
import time
import uuid
from pathlib import Path
import aiohttp
from .fast_universe import universe


def now():return time.time_ns()//1000000


def parse(venue,payload,markets,received):
    if venue=='upbit':
        x=payload;m=markets.get(x.get('code'))
        if not m or x.get('stream_type')=='SNAPSHOT':return []
        common={k:m[k] for k in ('venue','symbol','asset','quote')}
        common.update(received_ts_ms=received,event_ts_ms=x.get('trade_timestamp',x.get('timestamp')))
        if x.get('type')=='trade':
            out=[dict(common,kind='trade',trade_id=int(x['sequential_id']),price=float(x['trade_price']),qty=float(x['trade_volume']),side={'BID':'BUY','ASK':'SELL'}[x['ask_bid']])]
            # Quote-at-trade observations, not a continuous depth reconstruction.
            if all(x.get(k) is not None for k in ('best_bid_price','best_bid_size','best_ask_price','best_ask_size')):
                out.append(dict(common,kind='book',quote_source='trade_embedded_l1',
                    bids=[[float(x['best_bid_price']),float(x['best_bid_size'])]],
                    asks=[[float(x['best_ask_price']),float(x['best_ask_size'])]]))
            return out
    else:
        x=payload.get('data',payload);m=markets.get(x.get('s'))
        if not m:return []
        common={k:m[k] for k in ('venue','symbol','asset','quote')}
        common.update(received_ts_ms=received,event_ts_ms=x.get('T',x.get('E')))
        if x.get('e')=='trade':
            return [dict(common,kind='trade',trade_id=int(x['t']),price=float(x['p']),qty=float(x['q']),side='SELL' if x['m'] else 'BUY')]
        if all(k in x for k in ('b','B','a','A','u')):
            return [dict(common,kind='book',sequence=int(x['u']),quote_source='book_ticker_l1',
                         bids=[[float(x['b']),float(x['B'])]],asks=[[float(x['a']),float(x['A'])]])]
    return []


async def discover(session,venue,cap):
    if venue=='upbit':url='https://api.upbit.com/v1/market/all';params={'is_details':'true'}
    else:url='https://api.binance.com/api/v3/exchangeInfo';params={}
    async with session.get(url,params=params,timeout=aiohttp.ClientTimeout(total=20)) as r:
        r.raise_for_status();body=await r.json(content_type=None)
    return universe(venue,body,max_symbols=cap)


async def stream(session,venue,selected,write,connection_id):
    mapping={m['symbol']:m for m in selected};backoff=1
    while True:
        try:
            url='wss://api.upbit.com/websocket/v1' if venue=='upbit' else 'wss://stream.binance.com:443/ws'
            async with session.ws_connect(url,heartbeat=20,receive_timeout=45) as ws:
                if venue=='upbit':
                    await ws.send_json([{'ticket':uuid.uuid4().hex},{'type':'trade','codes':list(mapping),'is_only_realtime':True},{'format':'DEFAULT'}])
                else:
                    params=[m.lower()+suffix for m in mapping for suffix in ('@trade','@bookTicker')]
                    await ws.send_json({'method':'SUBSCRIBE','params':params,'id':1})
                write({'kind':'connection','venue':venue,'connection_id':connection_id,'received_ts_ms':now()})
                backoff=1
                async for msg in ws:
                    if msg.type not in (aiohttp.WSMsgType.TEXT,aiohttp.WSMsgType.BINARY):continue
                    body=json.loads(msg.data)
                    if 'error' in body or 'code' in body and 'msg' in body:raise ValueError('SUBSCRIPTION_REJECTED')
                    for row in parse(venue,body,mapping,now()):write(row)
                raise ConnectionError('STREAM_ENDED')
        except asyncio.CancelledError:raise
        except Exception as exc:
            write({'kind':'gap','venue':venue,'connection_id':connection_id,'received_ts_ms':now(),'reason':type(exc).__name__})
            await asyncio.sleep(backoff);backoff=min(backoff*2,30)


async def capture(args):
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():raise ValueError('OUTPUT_ALREADY_EXISTS')
    count=0;counts={};started=now()
    opener=gzip.open if str(path).endswith('.gz') else open
    with opener(path,'xt',encoding='utf-8') as out:
        def write(row):
            nonlocal count
            out.write(json.dumps(row,allow_nan=False,separators=(',',':'))+'\n');count+=1
            counts[row['kind']]=counts.get(row['kind'],0)+1
            if count%1000==0:out.flush()
        write({'kind':'capture_start','received_ts_ms':started,'mode':'PUBLIC_CAPTURE','schema':'fast-tape-v1',
               'scope_note':'Upbit quote-at-trade L1; Binance bookTicker L1. No historical order queue or market impact.'})
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
            tasks=[]
            try:
                for venue in args.venues:
                    u=await discover(session,venue,args.max_symbols);write(u)
                    print(json.dumps({'venue':venue,'selected':u['selected_count'],'eligible':u['eligible_count'],'mode':'PUBLIC_CAPTURE'}),flush=True)
                    # Each connection stays below 400 streams; startup control messages are spaced.
                    for i in range(0,len(u['markets']),150):
                        tasks.append(asyncio.create_task(stream(session,venue,u['markets'][i:i+150],write,f'{venue}:{i//150}')))
                        await asyncio.sleep(.25)
                await asyncio.sleep(args.duration)
            finally:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                write({'kind':'capture_end','received_ts_ms':now(),'counts':dict(counts),'started_ts_ms':started})
    print(json.dumps({'path':str(path),'counts':counts,'mode':'PUBLIC_CAPTURE'}))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--venues',nargs='+',choices=['upbit','binance'],default=['upbit','binance'])
    p.add_argument('--duration',type=int,default=600);p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--output',required=True);args=p.parse_args()
    if not 1<=args.duration<=86400 or args.max_symbols<0:p.error('duration 1..86400; max-symbols >=0')
    args.venues=list(dict.fromkeys(args.venues));asyncio.run(capture(args))
if __name__=='__main__':main()
