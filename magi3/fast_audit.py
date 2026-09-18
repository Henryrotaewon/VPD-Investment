"""Sparse, append-only FAST evidence events. No credentials or raw API responses."""
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import threading
import time
import uuid

SCHEMA='fast-evidence-v1'

class FastAudit:
    def __init__(self,path=None,connection=None,lock=None):
        self.lock=lock or threading.RLock()
        if connection is None:
            Path(path).parent.mkdir(parents=True,exist_ok=True)
            connection=sqlite3.connect(path,check_same_thread=False,timeout=10)
        self.db=connection
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS fast_events(
            event_id TEXT PRIMARY KEY,ts_ms INTEGER NOT NULL,signal_id TEXT NOT NULL,
            event_type TEXT NOT NULL,venue TEXT NOT NULL,symbol TEXT NOT NULL,
            mode TEXT NOT NULL,order_id TEXT,payload TEXT NOT NULL)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS fast_events_signal ON fast_events(signal_id,ts_ms)')
        self.db.execute('CREATE INDEX IF NOT EXISTS fast_events_time ON fast_events(ts_ms)')
        self.db.commit()
    def record(self,event_type,signal_id,venue,symbol,mode,*,ts_ms=None,order_id=None,**data):
        # Callers supply numerical evidence or curated fields, never arbitrary HTTP bodies.
        permitted={'rule_version','criteria','observed','reason','request','result','capital','duration_ms','exchange_ts_ms'}
        if set(data)-permitted:raise ValueError('UNSUPPORTED_AUDIT_FIELDS')
        ts=time.time_ns()//1000000 if ts_ms is None else int(ts_ms)
        payload={'schema':SCHEMA,'ts_utc':datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),**data}
        encoded=json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        with self.lock,self.db:
            self.db.execute('INSERT INTO fast_events VALUES(?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,ts,signal_id,event_type,venue,symbol,mode,order_id,encoded))
    def export(self,since_ms=0,until_ms=None):
        until_ms=time.time_ns()//1000000 if until_ms is None else until_ms
        # Fetch batches so export does not require all history in memory.
        with self.lock:
            cursor=self.db.execute('SELECT * FROM fast_events WHERE ts_ms>=? AND ts_ms<=? ORDER BY ts_ms,rowid',(since_ms,until_ms))
            keys=[x[0] for x in cursor.description]
            while rows:=cursor.fetchmany(1000):
                for row in rows:
                    event=dict(zip(keys,row));event.update(json.loads(event.pop('payload')));yield event


def order_result(data):
    """Allow-list fields required to distinguish acceptance from actual fills."""
    keys=('uuid','identifier','state','side','ord_type','price','volume','remaining_volume',
          'executed_volume','paid_fee','reserved_fee','locked','trades_count','created_at','market')
    out={key:data[key] for key in keys if key in data}
    trades=data.get('trades')
    if isinstance(trades,list):
        out['fills']=[{k:t[k] for k in ('uuid','price','volume','funds','side','created_at') if k in t} for t in trades]
    return out

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('database');p.add_argument('--since-ms',type=int,default=0);p.add_argument('--until-ms',type=int)
    args=p.parse_args()
    if not Path(args.database).is_file():p.error('database does not exist')
    audit=FastAudit(args.database)
    for event in audit.export(args.since_ms,args.until_ms):print(json.dumps(event,ensure_ascii=False))
