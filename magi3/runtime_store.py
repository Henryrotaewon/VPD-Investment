"""Transactional SHADOW-only SQLite store; real balances are separate snapshots."""
import json
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager


def encode(value):return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))


class RuntimeStore:
    def __init__(self,root,initial_cash=100000):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.db=sqlite3.connect(self.root/'shadow.sqlite3',check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,created_ms INTEGER);
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,signal_id TEXT,position_id TEXT,side TEXT,venue TEXT,asset TEXT,status TEXT,reason TEXT,ts_ms INTEGER);
            CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY,order_id TEXT UNIQUE,qty REAL,notional REAL,fee REAL,realized REAL,ts_ms INTEGER);
            CREATE TABLE IF NOT EXISTS positions(id TEXT PRIMARY KEY,signal_id TEXT,venue TEXT,asset TEXT,qty REAL,cost REAL,opened_ms INTEGER,deadline_ms INTEGER,tags TEXT,evidence TEXT);
            CREATE INDEX IF NOT EXISTS fills_time ON fills(ts_ms);
        ''')
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('initial_cash',encode(initial_cash)))
            db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('cash',encode(initial_cash)))

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback();raise

    def get(self,key,default=None):
        with self.lock:
            row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self,key,value):
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,encode(value)))

    def nonce(self):
        with self.transaction() as db:
            row=db.execute("SELECT value FROM meta WHERE key='kraken_nonce'").fetchone()
            value=max(int(row[0])+1 if row else 0,time.time_ns()//1000000)
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('kraken_nonce',str(value)))
            return value

    def rows(self,sql,args=()):
        with self.lock:return [dict(x) for x in self.db.execute(sql,args)]

    def positions(self):return self.rows('SELECT * FROM positions WHERE qty>1e-15')

    def recent_orders(self,limit=20):
        return self.rows('''SELECT o.*,f.qty,f.notional,f.fee,f.realized FROM orders o
            LEFT JOIN fills f ON f.order_id=o.id ORDER BY o.ts_ms DESC LIMIT ?''',(limit,))

    def orders_window(self,until=None,offset=0):
        now=time.time_ns()//1000000
        until=now if until is None else int(until)
        offset=int(offset)
        if until<0 or until>now+5000 or offset<0:raise ValueError('INVALID_WINDOW')
        since=until-72*60*60*1000
        with self.lock:
            total=self.rows('SELECT COUNT(*) AS n FROM orders WHERE ts_ms>=? AND ts_ms<=?',(since,until))[0]['n']
            orders=self.rows('''SELECT o.*,f.qty,f.notional,f.fee,f.realized FROM orders o
                LEFT JOIN fills f ON f.order_id=o.id WHERE o.ts_ms>=? AND o.ts_ms<=?
                ORDER BY o.ts_ms DESC,o.id DESC LIMIT 20 OFFSET ?''',(since,until,offset))
        return {'mode':'SHADOW','generated_ts_ms':now,'since_ts_ms':since,'until_ts_ms':until,
                'offset':offset,'total':total,'orders':orders,
                'next_offset':offset+len(orders) if offset+len(orders)<total else None}

    def close(self):
        with self.lock:self.db.close()
