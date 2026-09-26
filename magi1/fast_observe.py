"""Independent Upbit PUBLIC observation service; no order or Telegram APIs.

python -m magi1.fast_observe --db /data/fast-observe/observe.sqlite3 --duration 3600
"""
import argparse
import asyncio
from collections import defaultdict, deque
import json
import math
from pathlib import Path
import sqlite3
import shutil
import time
import uuid

import aiohttp

TEN = 10_000
FIVE = 300_000
DAY = 86_400_000  # UTC midnight = Upbit 09:00 KST
POLICY = {'version': 'fast-observe-v1', 'relative_value': 2., 'burst': 3.,
          'buy_share': .65, 'min_trades': 20, 'baseline_days': 10, 'top_n': 5,
          'horizons_seconds': [60, 180, 300, 900], 'max_feed_age_ms': 2000,
          'outcome_grace_ms': 10_000, 'daily_repeat': False}


def now_ms():
    return time.time_ns() // 1_000_000


def valid(value, positive=True):
    value = float(value)
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError('INVALID_NUMBER')
    return value


class Observer:
    def __init__(self, path, start):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS baseline(symbol TEXT,bucket INTEGER,value REAL,
            PRIMARY KEY(symbol,bucket));
          CREATE TABLE IF NOT EXISTS recent5m(symbol TEXT,bucket INTEGER,o REAL,h REAL,l REAL,
            c REAL,volume REAL,value REAL,source TEXT,PRIMARY KEY(symbol,bucket));
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts INTEGER,kind TEXT,
            symbol TEXT,payload TEXT);
          CREATE INDEX IF NOT EXISTS events_lookup ON events(kind,symbol,ts);
          CREATE TABLE IF NOT EXISTS captures(id INTEGER PRIMARY KEY,ts INTEGER,symbol TEXT,
            day INTEGER,price REAL,payload TEXT,UNIQUE(symbol,day));
          CREATE TABLE IF NOT EXISTS outcomes(capture_id INTEGER,horizon INTEGER,status TEXT,
            ts INTEGER,price REAL,return_pct REAL,PRIMARY KEY(capture_id,horizon));
        ''')
        policy = json.dumps(POLICY, sort_keys=True)
        old = self.db.execute("SELECT value FROM meta WHERE key='policy'").fetchone()
        if old and old[0] != policy:
            raise ValueError('POLICY_MISMATCH_USE_SEPARATE_DB')
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('policy',?)", (policy,))
        self.bars = defaultdict(lambda: deque(maxlen=12))
        self.books = {}; self.ids = defaultdict(set); self.id_queue = defaultdict(deque)
        self.previous = {}; self.last_price = {}; self.groups = {}; self.symbol_group = {}
        self.current = int(start) // TEN * TEN
        self.active = {}; self.started = int(start); self.stale_logged = {}
        for ident, ts, symbol, price in self.db.execute('SELECT id,ts,symbol,price FROM captures WHERE ts+900000+10000>=?', (start,)):
            self.active[ident] = (ts, symbol, price)
        self.event(start, 'START', '', {'mode': 'PUBLIC_OBSERVE', 'policy': POLICY})
        self.db.commit()

    def event(self, ts, kind, symbol, payload):
        self.db.execute('INSERT INTO events(ts,kind,symbol,payload) VALUES(?,?,?,?)',
                        (ts, kind, symbol, json.dumps(payload, allow_nan=False)))

    def connected(self, group, symbols, ts):
        self.groups[group] = ts
        for s in symbols:
            self.symbol_group[s] = group
            self.bars.pop(s, None); self.books.pop(s, None); self.previous.pop(s, None)
        self.event(ts, 'CONNECTED', group, {'symbols': len(symbols)})

    def gap(self, group, ts, reason):
        self.groups.pop(group, None)
        for s, g in list(self.symbol_group.items()):
            if g == group:
                self.bars.pop(s, None); self.books.pop(s, None); self.previous.pop(s, None)
                self.last_price.pop(s, None)
        self.event(ts, 'GAP', group, {'reason': reason})
        self.db.commit()

    def bar(self, symbol):
        q = self.bars[symbol]
        if not q or q[-1]['t'] != self.current:
            q.append(dict(t=self.current, value=0., buy=0., count=0, ofi=0., o=None,h=None,l=None,c=None,volume=0.))
        return q[-1]

    def ingest(self, message, received):
        self.advance(received)
        s = message.get('code')
        if self.symbol_group.get(s) not in self.groups:
            return
        stamp = int(message.get('trade_timestamp', message.get('timestamp', 0)))
        if not 0 <= received - stamp <= POLICY['max_feed_age_ms']:
            if received-self.stale_logged.get(s,0)>=60000:
                self.event(received, 'STALE_MESSAGE', s, {})
                self.stale_logged[s]=received
            self.previous.pop(s, None)
            return
        if message['type'] == 'orderbook':
            u = message['orderbook_units'][0]
            bp, ap = valid(u['bid_price']), valid(u['ask_price'])
            bq, aq = valid(u['bid_size'], False), valid(u['ask_size'], False)
            if bp >= ap or bq + aq <= 0:
                raise ValueError('INVALID_BOOK')
            old = self.books.get(s)
            if old and stamp < old['stamp']:
                return
            if old and received - old['ts'] <= 2000:
                flow = (bq if bp >= old['bp'] else 0) - (old['bq'] if bp <= old['bp'] else 0)
                flow -= (aq if ap <= old['ap'] else 0) - (old['aq'] if ap >= old['ap'] else 0)
                self.bar(s)['ofi'] += flow / ((bq + aq) / 2)
            self.books[s] = dict(bp=bp, ap=ap, bq=bq, aq=aq, ts=received, stamp=stamp)
        elif message['type'] == 'trade' and message.get('stream_type') != 'SNAPSHOT':
            ident = str(message['sequential_id'])
            if ident in self.ids[s]:
                return
            self.ids[s].add(ident); self.id_queue[s].append(ident)
            if len(self.id_queue[s]) > 10000:
                self.ids[s].remove(self.id_queue[s].popleft())
            price, qty = valid(message['trade_price']), valid(message['trade_volume'])
            if message['ask_bid'] not in ('ASK', 'BID'):
                raise ValueError('INVALID_SIDE')
            b = self.bar(s); b['value'] += price * qty; b['count'] += 1
            b['o'] = price if b['o'] is None else b['o']; b['h'] = max(b['h'] or price,price)
            b['l'] = min(b['l'] or price,price); b['c'] = price; b['volume'] += qty
            b['buy'] += price * qty if message['ask_bid'] == 'BID' else 0
            old = self.last_price.get(s)
            if not old or stamp >= old[2]:
                self.last_price[s] = (received, price, stamp)
                self.mark_outcomes(s, received, price)

    def mark_outcomes(self, symbol, ts, price):
        for ident, (captured, s, reference) in list(self.active.items()):
            if s != symbol:
                continue
            for h in POLICY['horizons_seconds']:
                target = captured + h * 1000
                if target <= ts <= target + POLICY['outcome_grace_ms']:
                    self.db.execute('INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?,?,?)',
                                    (ident, h, 'OBSERVED', ts, price, (price / reference - 1) * 100))

    def advance(self, ts):
        if ts < self.current:
            raise ValueError('CLOCK_REVERSED')
        # A stalled process must not manufacture timely decisions retrospectively.
        if ts - self.current > 2 * TEN:
            for g in list(self.groups):
                self.groups[g] = ts
            self.bars.clear(); self.books.clear(); self.previous.clear()
            self.event(ts, 'PROCESS_GAP', '', {})
            self.current = ts // TEN * TEN
        advanced = False
        while self.current + TEN <= ts:
            advanced = True
            end = self.current + TEN
            self.evaluate(end)
            self.current = end
        for ident, (captured, s, reference) in list(self.active.items()):
            for h in POLICY['horizons_seconds']:
                if ts > captured + h * 1000 + POLICY['outcome_grace_ms']:
                    self.db.execute('INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?,?,?)',
                                    (ident, h, 'MISSING', ts, None, None))
            if ts > captured + 910000:
                del self.active[ident]
        if advanced:
            self.db.commit()

    def evaluate(self, end):
        candidates = []
        for s, g in self.symbol_group.items():
            connected = self.groups.get(g)
            if connected is None:
                continue
            b = self.bar(s)
            history = [x for x in self.bars[s] if end - 70000 <= x['t'] < end - TEN]
            enough = connected <= end - 70000 and len(history) == 6
            baseline10 = sum(x['value'] for x in history) / 6 if enough else 0
            bucket = (end - 1) // FIVE * FIVE
            # Sum all ten-second buckets of this five-minute interval in SQLite.
            self.event(end, 'WINDOW', s, b)
            rvalue = None; same_n = 0
            if end % FIVE == 0:
                values = self.db.execute("SELECT payload FROM events WHERE kind='WINDOW' AND symbol=? AND ts>? AND ts<=?", (s, bucket, end)).fetchall()
                windows = [json.loads(x[0]) for x in values]
                total = sum(x['value'] for x in windows)
                if connected <= bucket and len(values) == 30:
                    self.db.execute('INSERT OR IGNORE INTO baseline VALUES(?,?,?)', (s, bucket, total))
                    traded = [x for x in windows if x.get('o') is not None]
                    ohlcv = ([traded[0]['o'],max(x['h'] for x in traded),min(x['l'] for x in traded),traded[-1]['c'],sum(x.get('volume',0) for x in traded)] if traded else [None,None,None,None,0])
                    self.db.execute('INSERT OR IGNORE INTO recent5m VALUES(?,?,?,?,?,?,?,?,?)',(s,bucket,*ohlcv,total,'LIVE_RECEIVE_TIME'))
            # Use latest completed five-minute window only (no future partial baseline).
            full = end // FIVE * FIVE - FIVE
            present = self.db.execute('SELECT value FROM baseline WHERE symbol=? AND bucket=?', (s, full)).fetchone()
            past = self.db.execute('SELECT value FROM baseline WHERE symbol=? AND bucket>=? AND bucket<? AND bucket % ?=? ORDER BY bucket DESC LIMIT 10',
                                   (s, full - 10 * DAY, full, DAY, full % DAY)).fetchall()
            same_n = len(past)
            if present and same_n == 10 and sum(x[0] for x in past) > 0:
                rvalue = present[0] / (sum(x[0] for x in past) / 10)
            book = self.books.get(s); quote = self.last_price.get(s)
            fresh = bool(book and quote and 0 <= end-book['ts'] <= 2000 and 0 <= end-quote[0] <= 2000)
            f = dict(relative_value=rvalue, baseline_days=same_n, burst=b['value']/baseline10 if baseline10 else None,
                     buy_share=b['buy']/b['value'] if b['value'] else 0, trades=b['count'], ofi=b['ofi'],
                     net_buy=b['buy']*2-b['value'], five_minute_start=full)
            qualifies = bool(enough and fresh and rvalue is not None and rvalue >= 2 and
                             f['burst'] is not None and f['burst'] >= 3 and f['buy_share'] >= .65 and
                             f['trades'] >= 20 and f['ofi'] > 0)
            old = self.previous.get(s)
            self.previous[s] = (end, qualifies)
            if qualifies and old == (end-TEN, True):
                candidates.append((s, f, quote[1], book))
            if end % FIVE == 0:
                self.event(end, 'ASSESSMENT', s, dict(f, qualifies=qualifies,
                           status='BASELINE_WARMUP' if rvalue is None else 'READY'))
        candidates.sort(key=lambda x: (-x[1]['relative_value'], -x[1]['net_buy'], x[0]))
        selected = 0
        for s, f, price, book in candidates:
            if self.db.execute('SELECT 1 FROM captures WHERE symbol=? AND day=?', (s, end//DAY)).fetchone():
                continue
            if len(self.active) >= POLICY['top_n']:
                self.event(end, 'NOT_SELECTED', s, {'reason': 'MAX5_ACTIVE_TRACKS'})
                continue
            cur = self.db.execute('INSERT INTO captures(ts,symbol,day,price,payload) VALUES(?,?,?,?,?)',
                                  (end, s, end//DAY, price, json.dumps(dict(f, best_bid=book['bp'], best_ask=book['ap'], mode='NO_ORDER'))))
            self.active[cur.lastrowid] = (end, s, price); selected += 1
            self.event(end, 'CAPTURE', s, dict(f, reference_price=price))
        if end % FIVE == 0:
            self.db.execute('DELETE FROM baseline WHERE bucket<?', (end//DAY*DAY-10*DAY,))
            self.db.execute('DELETE FROM recent5m WHERE bucket<?', (end-DAY,))
            self.db.execute("DELETE FROM events WHERE kind='WINDOW' AND ts<?", (end-FIVE,))
            self.db.execute("DELETE FROM events WHERE kind!='CAPTURE' AND ts<?", (end-DAY,))

    def report(self):
        return {'mode': 'PUBLIC_OBSERVE_NO_ORDERS', 'asof_ms': self.current,
                'connected_groups': len(self.groups), 'symbols': len(self.symbol_group),
                'captures': self.db.execute('SELECT count(*) FROM captures').fetchone()[0],
                'outcomes': dict(self.db.execute('SELECT status,count(*) FROM outcomes GROUP BY status')),
                'baseline_rows': self.db.execute('SELECT count(*) FROM baseline').fetchone()[0]}


async def run(args):
    observer = Observer(args.db, now_ms())
    async with aiohttp.ClientSession() as session:
        async with session.get('https://api.upbit.com/v1/market/all', params={'is_details': 'true'}, timeout=aiohttp.ClientTimeout(total=15)) as response:
            response.raise_for_status(); rows = await response.json()
        symbols = sorted(x['market'] for x in rows if x['market'].startswith('KRW-'))
        if not symbols:
            raise ValueError('EMPTY_UNIVERSE')
        observer.event(now_ms(), 'UNIVERSE', '', {'symbols': symbols, 'scope': 'ALL_KRW_OBSERVATION'})
        async def stream(group, names):
            backoff = 1
            while True:
                try:
                    async with session.ws_connect('wss://api.upbit.com/websocket/v1', heartbeat=20, receive_timeout=45) as ws:
                        await ws.send_json([{'ticket': str(uuid.uuid4())}, {'type': 'trade', 'codes': names, 'is_only_realtime': True},
                                            {'type': 'orderbook', 'codes': names, 'level': 0}, {'format': 'DEFAULT'}])
                        observer.connected(group, names, now_ms()); backoff = 1
                        async for msg in ws:
                            if msg.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                                continue
                            body = json.loads(msg.data)
                            if 'error' in body:
                                raise ValueError('SUBSCRIPTION_ERROR')
                            observer.ingest(body, now_ms())
                        raise ConnectionError('CLOSED')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    observer.gap(group, now_ms(), type(exc).__name__)
                    await asyncio.sleep(backoff); backoff = min(30, backoff*2)
        tasks = []
        try:
            for i in range(0, len(symbols), 100):
                tasks.append(asyncio.create_task(stream(str(i), symbols[i:i+100])))
                await asyncio.sleep(.3)
            stop = time.monotonic() + args.duration
            last_report = 0
            while time.monotonic() < stop:
                if time.monotonic()-last_report>=60 and shutil.disk_usage(Path(args.db).parent).free < 64*1024*1024:
                    raise RuntimeError('DISK_RESERVE_STOP')
                observer.advance(now_ms())
                if time.monotonic() - last_report >= 60:
                    print(json.dumps(observer.report()), flush=True); last_report = time.monotonic()
                await asyncio.sleep(.2)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            observer.event(now_ms(), 'STOP', '', observer.report())
            observer.db.commit(); observer.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--duration', type=int, default=3600)
    args = parser.parse_args()
    if not 1 <= args.duration <= 86400:
        parser.error('duration must be 1..86400 seconds')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
