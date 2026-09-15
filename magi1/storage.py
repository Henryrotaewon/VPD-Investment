"""Batched compressed raw files and transactional research/checkpoint persistence."""
import dataclasses
import gzip
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from datetime import datetime,timezone

class Storage:
    def __init__(self,root):
        self.root=Path(root); self.raw=self.root/'raw'; self.raw.mkdir(parents=True,exist_ok=True)
        self._lock=threading.RLock(); self.buffer={}; self.raw_rows=0
        self.db=sqlite3.connect(self.root/'research.db',check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT, ts_ms INTEGER, asset TEXT, payload TEXT)')
        self.db.execute('CREATE INDEX IF NOT EXISTS idx_records ON records(kind,ts_ms,asset)')
        self.db.execute('CREATE TABLE IF NOT EXISTS checkpoints(key TEXT PRIMARY KEY, payload TEXT)'); self.db.commit()
    @staticmethod
    def payload(value): return dataclasses.asdict(value) if dataclasses.is_dataclass(value) else dict(value)
    def append_raw(self,kind,value):
        row=self.payload(value); ts=row.get('received_ts_ms',row.get('event_ts_ms',0))
        hour=datetime.fromtimestamp(ts/1000,timezone.utc).strftime('%Y-%m-%d-%H')
        with self._lock:
            self.buffer.setdefault(f'{kind}-{hour}.jsonl.gz',[]).append(json.dumps(row,separators=(',',':'),ensure_ascii=False)+'\n'); self.raw_rows+=1
            if self.raw_rows>=2000:self.flush()
    def append(self,kind,value):
        row=self.payload(value); ts=row.get('event_ts_ms',row.get('received_ts_ms',row.get('origin_ts_ms',0)))
        with self._lock: self.db.execute('INSERT INTO records VALUES(?,?,?,?)',(kind,ts,row.get('asset',row.get('base')),json.dumps(row,ensure_ascii=False)))
    def query(self,kind,start_ms,end_ms):
        with self._lock: return [json.loads(x[0]) for x in self.db.execute('SELECT payload FROM records WHERE kind=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms',(kind,start_ms,end_ms))]
    def checkpoint(self,key,value):
        with self._lock:self.db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?)',(key,json.dumps(value))); self.db.commit()
    def restore(self,key,default=None):
        with self._lock:
            row=self.db.execute('SELECT payload FROM checkpoints WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else default
    def flush(self):
        with self._lock:
            for name,lines in self.buffer.items():
                with gzip.open(self.raw/name,'at',encoding='utf-8') as f:f.writelines(lines)
            self.buffer.clear(); self.raw_rows=0; self.db.commit()
    def maintain(self,raw_days=2):
        self.flush()
        for p in self.raw.glob('*.jsonl.gz'):
            if p.stat().st_mtime<time.time()-raw_days*86400:p.unlink()
        if self.health()['free_bytes']<100*1024*1024: raise RuntimeError('volume below 100 MiB free; stop collection to protect research DB')
    def health(self):
        x=os.statvfs(self.root)
        return {'root':str(self.root),'free_bytes':x.f_bavail*x.f_frsize,'raw_buffer_rows':self.raw_rows}
    def close(self):self.flush(); self.db.close()
