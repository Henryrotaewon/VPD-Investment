"""Bounded public 5m cache: recent 24h OHLCV plus ten prior UTC days of turnover.

Missing archive rows stay unknown. Only validated API pagination proves zero-trade
intervals between returned candles and the exclusive request boundary.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import shutil
from pathlib import Path
import time

import requests
from magi1.fast_observe import Observer, FIVE, DAY, now_ms


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS recent5m(symbol TEXT,bucket INTEGER,o REAL,h REAL,l REAL,
        c REAL,volume REAL,value REAL,source TEXT,PRIMARY KEY(symbol,bucket));
      CREATE TABLE IF NOT EXISTS imports(path TEXT,sha256 TEXT,rows INTEGER,cutoff INTEGER,
        PRIMARY KEY(path,sha256));
    ''')


def bounds(cutoff):
    cutoff = int(cutoff)//FIVE*FIVE
    return cutoff//DAY*DAY-10*DAY, cutoff


def validate(row):
    if len(row) != 7:
        raise ValueError('EXPECTED_SEVEN_FIELDS')
    t = int(row[0])
    values = [float(x) for x in row[1:]]
    if row[0] != t or t % FIVE or not all(math.isfinite(x) for x in values):
        raise ValueError('INVALID_CANDLE')
    o,h,l,c,v,value = values
    if min(o,h,l,c) <= 0 or min(v,value) < 0 or not l<=min(o,c)<=max(o,c)<=h:
        raise ValueError('INVALID_OHLCV')
    return [t,*values]


def put(db, symbol, row, cutoff, source):
    start, end = bounds(cutoff)
    t,*values = row
    if not start <= t < end:
        return 0
    old = db.execute('SELECT value FROM baseline WHERE symbol=? AND bucket=?',(symbol,t)).fetchone()
    if old and not math.isclose(old[0], values[-1], rel_tol=1e-9, abs_tol=1e-6):
        # Native exchange candles supersede reception-time live sums.
        # Archive/API conflicts stay explicit in diagnostics.
        db.execute('INSERT INTO events(ts,kind,symbol,payload) VALUES(?,?,?,?)',
                   (cutoff,'BASELINE_RECONCILED',symbol,json.dumps({'bucket':t,'old':old[0],'new':values[-1],'source':source})))
    db.execute('INSERT OR REPLACE INTO baseline VALUES(?,?,?)',(symbol,t,values[-1]))
    if t >= end-DAY:
        db.execute('INSERT OR REPLACE INTO recent5m VALUES(?,?,?,?,?,?,?,?,?)',(symbol,t,*values,source))
    return 1


def import_archive(db, path, cutoff):
    path = Path(path)
    symbol = path.name.removesuffix('_5m.json.gz')
    if not symbol.startswith('KRW-') or path.name != symbol+'_5m.json.gz':
        raise ValueError('ARCHIVE_FILENAME')
    raw = path.read_bytes(); rows = json.loads(gzip.decompress(raw))
    checked = [validate(r) for r in rows]
    if len({r[0] for r in checked}) != len(checked):
        raise ValueError('DUPLICATE_ARCHIVE_CANDLE')
    count = 0
    with db:
        for row in checked:
            count += put(db,symbol,row,cutoff,'ARCHIVE')
        db.execute('INSERT OR REPLACE INTO imports VALUES(?,?,?,?)',
                   (str(path),hashlib.sha256(raw).hexdigest(),count,cutoff))
    return {'symbol':symbol,'usable_candles':count,'archive_rows':len(rows)}


class Public:
    def __init__(self):
        self.session=requests.Session(); self.next=0; self.calls=0

    def get(self,path,params=None):
        for attempt in range(3):
            time.sleep(max(0,self.next-time.monotonic()))
            self.next=time.monotonic()+.4; self.calls+=1
            r=self.session.get('https://api.upbit.com/v1/'+path,params=params,timeout=10)
            if r.status_code==418:
                raise RuntimeError('API_BLOCKED_STOP')
            if r.status_code==429 or r.status_code>=500:
                if attempt<2:
                    self.next=time.monotonic()+max(2,attempt*3);continue
            r.raise_for_status();x=r.json()
            if not isinstance(x,list):raise ValueError('INVALID_API_RESPONSE')
            return x
        raise RuntimeError('RETRY_EXHAUSTED')

    def candles(self,symbol,cursor):
        iso=datetime.fromtimestamp(cursor/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return self.get('candles/minutes/5',{'market':symbol,'to':iso,'count':200})


def api_rows(payload,cursor):
    rows=[]
    for x in payload:
        t=int(datetime.fromisoformat(x['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()*1000)
        row=validate([t,x['opening_price'],x['high_price'],x['low_price'],x['trade_price'],
                      x['candle_acc_trade_volume'],x['candle_acc_trade_price']])
        if t>=cursor:raise ValueError('NON_EXCLUSIVE_API_BOUNDARY')
        rows.append(row)
    if len(rows)>200 or len({r[0] for r in rows})!=len(rows):
        raise ValueError('INVALID_API_PAGE')
    return sorted(rows)


def fill(db,symbol,cutoff,client,max_pages=32):
    start,end=bounds(cutoff)
    expected=set(range(start,end,FIVE))
    # Recent detailed data is also required; turnover alone is not sufficient.
    known={r[0] for r in db.execute('SELECT bucket FROM baseline WHERE symbol=? AND bucket>=? AND bucket<?',(symbol,start,end))}
    details={r[0] for r in db.execute('SELECT bucket FROM recent5m WHERE symbol=? AND bucket>=? AND bucket<?',(symbol,end-DAY,end))}
    missing=(expected-known)| (set(range(end-DAY,end,FIVE))-details)
    missing |= {r[0] for r in db.execute("SELECT bucket FROM recent5m WHERE symbol=? AND source='LIVE_RECEIVE_TIME' AND bucket>=? AND bucket<?",(symbol,start,end))}
    pages=0
    while missing and pages<max_pages:
        cursor=max(missing)+FIVE
        rows=api_rows(client.candles(symbol,cursor),cursor);pages+=1
        if not rows:break  # Do not invent pre-listing zero history.
        lower=max(start,rows[0][0]);native={r[0]:r for r in rows}
        with db:
            for t in range(lower,cursor,FIVE):
                if t not in missing:continue
                if t in native:put(db,symbol,native[t],cutoff,'API')
                else:
                    db.execute('INSERT OR REPLACE INTO baseline VALUES(?,?,0)',(symbol,t))
                    if t>=end-DAY:
                        db.execute('INSERT OR REPLACE INTO recent5m VALUES(?,?,NULL,NULL,NULL,NULL,0,0,?)',(symbol,t,'API_NO_TRADE'))
                missing.discard(t)
        # Next loop jumps over already imported archive ranges.
    return {'symbol':symbol,'pages':pages,'missing_buckets':len(missing),
            'status':'COMPLETE' if not missing else 'INCOMPLETE_HISTORY_OR_PAGE_LIMIT'}


def prune(db,cutoff):
    start,end=bounds(cutoff)
    with db:
        db.execute('DELETE FROM baseline WHERE bucket<?',(start,))
        db.execute('DELETE FROM recent5m WHERE bucket<?',(end-DAY,))
        db.execute('DELETE FROM imports WHERE cutoff<?',(end-11*DAY,))
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True)
    p.add_argument('--archive-dir',action='append',default=[])
    p.add_argument('--cutoff',help='ISO timestamp with timezone; default current completed 5m')
    p.add_argument('--offline',action='store_true')
    p.add_argument('--max-pages',type=int,default=32)
    args=p.parse_args()
    cutoff=now_ms()
    if args.cutoff:
        dt=datetime.fromisoformat(args.cutoff)
        if dt.tzinfo is None:p.error('cutoff timezone required')
        cutoff=int(dt.timestamp()*1000)
    if not 1<=args.max_pages<=100:p.error('max-pages 1..100')
    _,cutoff=bounds(cutoff)
    obs=Observer(args.db,cutoff);db=obs.db;initialize(db)
    if db.execute('SELECT 1 FROM baseline WHERE bucket>=? LIMIT 1',(cutoff,)).fetchone():
        db.close();raise ValueError('CUTOFF_PRECEDES_EXISTING_DATA_USE_NEW_DB')
    print(json.dumps({'mode':'BOOTSTRAP_NO_ORDERS','cutoff_ms':cutoff}),flush=True)
    try:
        for directory in args.archive_dir:
            for path in sorted(Path(directory).glob('*_5m.json.gz')):
                print(json.dumps(import_archive(db,path,cutoff)),flush=True)
        if not args.offline:
            client=Public()
            markets=client.get('market/all',{'is_details':'false'})
            symbols=sorted({x['market'] for x in markets if x['market'].startswith('KRW-')})
            if not symbols:raise ValueError('EMPTY_UNIVERSE')
            print(json.dumps({'market_count':len(symbols)}),flush=True)
            for symbol in symbols:
                if shutil.disk_usage(Path(args.db).parent).free < 64*1024*1024:
                    raise RuntimeError('DISK_RESERVE_STOP')
                print(json.dumps(fill(db,symbol,cutoff,client,args.max_pages)),flush=True)
        prune(db,cutoff)
        print(json.dumps({'baseline_rows':db.execute('SELECT count(*) FROM baseline').fetchone()[0],
                          'recent_rows':db.execute('SELECT count(*) FROM recent5m').fetchone()[0],
                          'network_fetch':not args.offline}),flush=True)
    finally:db.close()

if __name__=='__main__':main()
