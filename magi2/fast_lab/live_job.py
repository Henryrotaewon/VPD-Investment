"""One-shot public FAST capture + replay. No trading or Telegram credentials needed."""
import argparse
import asyncio
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import time

import aiohttp
from magi1.fast_capture import discover, stream, now
from .replay import Lab, Policy, read_tape


def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields), ensure_ascii=False, allow_nan=False), flush=True)


def save(path, value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    temporary.replace(path)


class Quality:
    def __init__(self):
        self.counts=Counter(); self.symbols=defaultdict(Counter); self.deltas=defaultdict(Counter)
        self.first=None; self.last=None; self.selected=[]; self.missing=Counter()
    def observe(self,row):
        kind=row['kind']; self.counts[kind]+=1
        if kind=='universe': self.selected=[m['symbol'] for m in row['markets']]
        if kind not in ('trade','book'): return
        ts=row['received_ts_ms']; self.first=ts if self.first is None else self.first; self.last=ts
        self.symbols[row['symbol']][kind]+=1
        event=row.get('event_ts_ms')
        if isinstance(event,(int,float)):
            # Fixed 10ms buckets, capped tails: bounded memory even during clock errors.
            delta=max(-60000,min(60000,int(ts-event)))
            self.deltas[kind][delta//10*10]+=1
        else: self.missing[kind]+=1
    def summary(self):
        timing={}
        for kind,hist in self.deltas.items():
            total=sum(hist.values()); out={'samples':total}
            for label,percent in [('p50',.5),('p95',.95),('p99',.99)]:
                cumulative=0
                for value,count in sorted(hist.items()):
                    cumulative+=count
                    if cumulative>=total*percent: out[label+'_bucket_ms']=value; break
            timing[kind]=out
        return {'counts':dict(self.counts),'selected_markets':len(self.selected),
                'markets_with_trades':sum(v['trade']>0 for v in self.symbols.values()),
                'markets_with_books':sum(v['book']>0 for v in self.symbols.values()),
                'no_trade_markets':[s for s in self.selected if not self.symbols[s]['trade']],
                'no_book_markets':[s for s in self.selected if not self.symbols[s]['book']],
                'first_data_ms':self.first,'last_data_ms':self.last,
                'data_span_seconds':(self.last-self.first)/1000 if self.first is not None else 0,
                'missing_event_timestamp':dict(self.missing),'receipt_minus_exchange_clock':timing,
                'timing_note':'Not pure network latency; includes clock skew. 10ms floor buckets capped at +/-60000ms.',
                'coverage_note':'No activity is not proof of data loss. Gaps are observed connection errors, not a completeness guarantee.'}


async def capture_venue(venue,path,duration,max_bytes):
    quality=Quality(); tasks=[]; total_bytes=0; stop=asyncio.Event(); reason='DURATION_LIMIT'
    deadline=time.monotonic()+duration
    with gzip.open(path,'xt',encoding='utf-8') as out:
        def write(row,force=False):
            nonlocal total_bytes,reason
            if stop.is_set() and not force:return
            line=json.dumps(row,allow_nan=False,separators=(',',':'))+'\n'
            size=len(line.encode())
            if total_bytes+size>max_bytes and not force:
                reason='BYTE_LIMIT'; stop.set(); return
            out.write(line);total_bytes+=size;quality.observe(row)
        write({'kind':'capture_start','received_ts_ms':now(),'schema':'fast-tape-v1','mode':'PUBLIC_CAPTURE'})
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
            try:
                u=await discover(session,venue,0);write(u)
                emit('fast_universe',venue=venue,selected=u['selected_count'],eligible=u['eligible_count'])
                for i in range(0,len(u['markets']),150):
                    if time.monotonic()>=deadline:break
                    tasks.append(asyncio.create_task(stream(session,venue,u['markets'][i:i+150],write,f'{venue}:{i//150}')))
                    await asyncio.sleep(.25)
                while not stop.is_set() and time.monotonic()<deadline:
                    try:await asyncio.wait_for(stop.wait(),timeout=min(30,max(.001,deadline-time.monotonic())))
                    except asyncio.TimeoutError:pass
                    out.flush()
                    emit('fast_capture_progress',venue=venue,counts=dict(quality.counts),uncompressed_bytes=total_bytes)
            except Exception as exc:
                reason='CAPTURE_ERROR_'+type(exc).__name__
                write({'kind':'gap','venue':venue,'received_ts_ms':now(),'reason':reason},force=True)
            finally:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                write({'kind':'capture_end','received_ts_ms':now(),'reason':reason},force=True)
    result=quality.summary();result.update(stop_reason=reason,uncompressed_bytes=total_bytes,compressed_bytes=path.stat().st_size)
    return result


def evaluate(tape):
    lab=Lab(Policy())
    for row in read_tape(tape):lab.feed(row)
    report=lab.finish()
    digest=hashlib.sha256()
    with tape.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):digest.update(chunk)
    report['tape_sha256']=digest.hexdigest()
    return report


async def run(args):
    root=Path(args.output_dir)
    if os.getenv('RAILWAY_PROJECT_ID'):
        mount=os.getenv('RAILWAY_VOLUME_MOUNT_PATH','')
        if not mount or not Path(mount).exists() or not root.resolve().is_relative_to(Path(mount).resolve()):
            raise RuntimeError('PERSISTENT_VOLUME_REQUIRED')
    root.mkdir(parents=True,exist_ok=True)
    folder=root/args.run_id
    try:folder.mkdir()
    except FileExistsError:
        emit('fast_run_already_claimed',run_id=args.run_id); return
    save(folder/'started.json',{'run_id':args.run_id,'duration_seconds':args.duration,'venues':args.venues,
         'max_uncompressed_bytes_per_venue':args.max_bytes,'started_ms':now(),'policy_id':Policy().identity})
    async def one(venue):
        tape=folder/(venue+'.jsonl.gz')
        quality=await capture_venue(venue,tape,args.duration,args.max_bytes)
        save(folder/(venue+'-quality.json'),quality)
        try:
            report=await asyncio.to_thread(evaluate,tape)
            save(folder/(venue+'-report.json'),report)
            summary={'venue':venue,'quality':quality,'trials':len(report['trials']),
                     'counts':report['counts'],'feature_quality':report['quality'],'groups':report['groups'],
                     'policy':report['policy'],'tape_sha256':report['tape_sha256'],'profitability_verdict':'NOT_VALIDATED'}
        except Exception as exc:
            summary={'venue':venue,'quality':quality,'replay_error':type(exc).__name__,'profitability_verdict':'NOT_VALIDATED'}
        # Per-market lists stay in persistent artifacts, not large log lines.
        logged={**summary,'quality':{k:v for k,v in quality.items() if k not in ('no_trade_markets','no_book_markets')}}
        emit('fast_venue_result',**logged)
        return summary
    results=await asyncio.gather(*(one(venue) for venue in args.venues))
    save(folder/'result.json',{'run_id':args.run_id,'results':results,'finished_ms':now()})
    emit('fast_run_finished',run_id=args.run_id,output=str(folder),venues=len(results),profitability_verdict='NOT_VALIDATED')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True);parser.add_argument('--run-id',required=True)
    parser.add_argument('--duration',type=int,default=600)
    parser.add_argument('--max-bytes',type=int,default=150_000_000)
    parser.add_argument('--venues',nargs='+',choices=['upbit','binance'],default=['upbit','binance'])
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',args.run_id):parser.error('Invalid run ID')
    if not 1<=args.duration<=3600 or not 10000<=args.max_bytes<=200_000_000:parser.error('Invalid duration or byte limit')
    args.venues=list(dict.fromkeys(args.venues))
    asyncio.run(run(args))


if __name__=='__main__':main()
